"""收藏批次延迟统一投递状态机与 L0 本地安全底线（SQLite 同库同事务版）。

核心不变量：
1. **代码层强拦截**：`apply_at` 之前 `claim-next` 与 `mark --outcome applied` 均拒绝执行；
2. **与账本同事务联动**：`mark --outcome applied` 在同一个 SQLite 事务内更新批次条目并把
   账本对应 `job_id` 推进为 `status='opening_sent'` + `channel='favorite_queue'`，杜绝
   账本与批次脱钩或伪造 `favorite_queue` 逃配额；
3. **并发安全与租约**：基于 `ledger.db` 的 `BEGIN IMMEDIATE` 事务抢占条目，条目进入
   `in_flight` 并绑定 10 分钟租约，防止多宿主定时任务并发重复发消息；
4. **L0 本地安全底线（Free/Pro 共用，无逐次绕过开关）**：
   - 固定宵禁护栏 `08:30 ~ 21:30`（复用云端 `SAFE_DAY`）；
   - 代码层串行抖动间隔（30~90 秒，`BATCH_THROTTLED` + `retry_after_seconds`）；
   - 每日总外发硬顶（`outbound + favorite_queue` 默认 ≤ 30，`auto_risk_acknowledged=true` 时 ≤ 40），
     并与云端下发的 `favorite_batch_budget` 取较小值，超额部分保持 `pending` 自动顺延次日（`carry_over`）；
   - 独立的每日收藏上限（`favorite_daily_cap`）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from . import channel as hm_channel
from .ledger import JobLedger, _milestone_updates_for_status
from .workspace import load_config, load_runtime_state

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BATCH_PENDING = "pending"
BATCH_APPLYING = "applying"
BATCH_PAUSED = "paused"
BATCH_COMPLETED = "completed"
BATCH_CANCELLED = "cancelled"

BATCH_TERMINAL_STATES = frozenset({BATCH_COMPLETED, BATCH_CANCELLED})

ITEM_PENDING = "pending"
ITEM_IN_FLIGHT = "in_flight"
ITEM_APPLIED = "applied"
ITEM_SKIPPED = "skipped"

#: 条目认领租约（秒）：超过此时间未 mark 的 in_flight 条目会自动回收为 pending
CLAIM_LEASE_SECONDS = 600
#: 主锁超时自愈阈值（分钟），对齐 pro-server/policy_engine.py
STALE_BUSY_MINUTES = 20.0
#: 固定安全宵禁护栏（与云端 SAFE_DAY 一致：08:30 ~ 21:30）
SAFE_DAY_START = time(8, 30)
SAFE_DAY_END = time(21, 30)
#: 默认每日总外发安全硬顶（outbound + favorite_queue，不含 inbound）
DEFAULT_HARD_DAILY_CAP = 30
#: 开启 auto_risk_acknowledged 后的最高每日总外发硬顶
ACK_HARD_DAILY_CAP = 40
#: 批次单轮最多连续投递条数（45 分钟滚动窗口）
MAX_BATCH_PER_ROUND = 10
BATCH_ROUND_WINDOW_MINUTES = 45
#: 串行投递最小/最大抖动间隔（秒）
MIN_DISPATCH_INTERVAL_SECONDS = 30
MAX_DISPATCH_INTERVAL_SECONDS = 90
#: 默认每日收藏动作上限（防 favorite 模式无上限刷接口）
DEFAULT_FAVORITE_DAILY_CAP = 40
#: 云端 favorite_batch_budget 缓存有效期（分钟）
CLOUD_BUDGET_TTL_MINUTES = 30


class BatchGateError(Exception):
    """批次门控拒绝执行，携带机器可读 code 供宿主 Agent 判断。"""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "code": self.code, "error": self.message}
        payload.update(self.extra)
        return payload


# ---------------------------------------------------------------------------
# 时间与宵禁辅助
# ---------------------------------------------------------------------------


def local_now() -> datetime:
    """带本地时区偏移的当前时间。"""
    return datetime.now(timezone.utc).astimezone()


def parse_iso(value: str) -> datetime:
    """解析 ISO-8601；强制要求带时区偏移，拒绝 naive 时间戳。"""
    raw = str(value or "").strip()
    if not raw:
        raise BatchGateError("INVALID_TIMESTAMP", "时间戳为空")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BatchGateError("INVALID_TIMESTAMP", f"无法解析时间戳 {value!r}：{exc}") from exc
    if dt.tzinfo is None:
        raise BatchGateError(
            "NAIVE_TIMESTAMP",
            f"时间戳必须带时区偏移（如 +08:00），收到 {value!r}",
        )
    return dt


def in_safe_day(moment: datetime) -> bool:
    """是否处于 08:30 ~ 21:30 安全护栏区间内（半开区间）。"""
    cur = moment.time().replace(tzinfo=None)
    return SAFE_DAY_START <= cur < SAFE_DAY_END


def next_safe_day_start(moment: datetime) -> datetime:
    """计算下一个允许执行的安全时段起点（08:30）。"""
    cur = moment.time().replace(tzinfo=None)
    base_date = moment.date() if cur < SAFE_DAY_START else (moment.date() + timedelta(days=1))
    return datetime(
        base_date.year,
        base_date.month,
        base_date.day,
        SAFE_DAY_START.hour,
        SAFE_DAY_START.minute,
        0,
        tzinfo=moment.tzinfo,
    )


def _deterministic_jitter_seconds(batch_id: str, applied_count: int) -> int:
    """基于 batch_id 与已投条数生成 30~90 秒稳定抖动间隔（兼顾防风控与可测试性）。"""
    digest = hashlib.sha256(f"{batch_id}:{applied_count}".encode("utf-8")).digest()
    span = MAX_DISPATCH_INTERVAL_SECONDS - MIN_DISPATCH_INTERVAL_SECONDS + 1
    return MIN_DISPATCH_INTERVAL_SECONDS + (int.from_bytes(digest[:2], "big") % span)


# ---------------------------------------------------------------------------
# 唯一配额与预算口径
# ---------------------------------------------------------------------------


def _resolve_hard_daily_cap(config: dict[str, Any]) -> int:
    if bool(config.get("auto_risk_acknowledged", False)):
        return ACK_HARD_DAILY_CAP
    return DEFAULT_HARD_DAILY_CAP


def _resolve_favorite_daily_cap(config: dict[str, Any]) -> int:
    sched = config.get("schedule") or {}
    raw = sched.get("favorite_daily_cap")
    if raw is None:
        return DEFAULT_FAVORITE_DAILY_CAP
    try:
        return max(5, min(80, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_FAVORITE_DAILY_CAP


def _resolve_cloud_batch_budget(runtime: dict[str, Any], moment: datetime) -> int | None:
    """读取 pro patrol-gate 下发并缓存的 favorite_batch_budget（过期则返回 None）。"""
    budget = runtime.get("favorite_batch_budget")
    updated_raw = runtime.get("favorite_batch_budget_updated_at")
    if budget is None or not updated_raw:
        return None
    try:
        updated_at = parse_iso(str(updated_raw))
        val = int(budget)
    except (BatchGateError, TypeError, ValueError):
        return None
    elapsed_min = (moment - updated_at).total_seconds() / 60.0
    if elapsed_min > CLOUD_BUDGET_TTL_MINUTES or elapsed_min < -5.0:
        return None
    return max(0, val)


def _busy_state(runtime: dict[str, Any], moment: datetime) -> tuple[bool, bool]:
    """返回 (effective_busy, stale_busy_recovery)，对齐云端 20 分钟死锁自愈。"""
    raw_busy = bool(runtime.get("busy", False))
    if not raw_busy:
        return False, False
    since_raw = runtime.get("busy_since")
    if not since_raw:
        return True, False
    try:
        since_dt = parse_iso(str(since_raw))
    except BatchGateError:
        return True, False
    elapsed_min = (moment - since_dt).total_seconds() / 60.0
    if elapsed_min >= STALE_BUSY_MINUTES:
        return False, True
    return True, False


def quota_state(
    workspace: Path,
    *,
    now: datetime | None = None,
    ledger: JobLedger | None = None,
) -> dict[str, Any]:
    """返回当日配额分桶与收藏批次可用预算（唯一本地口径）。"""
    moment = now or local_now()
    config = load_config(workspace)
    runtime = load_runtime_state(workspace)
    led = ledger or JobLedger(workspace)
    summary = led.summary(local_date_prefix=moment.strftime("%Y-%m-%d"))
    by_channel = summary.get("today_by_channel") or {}

    outbound_openings = int((by_channel.get(hm_channel.CHANNEL_OUTBOUND) or {}).get("opening_sent", 0))
    batch_openings = int((by_channel.get(hm_channel.CHANNEL_FAVORITE_QUEUE) or {}).get("opening_sent", 0))
    inbound_openings = int((by_channel.get(hm_channel.CHANNEL_INBOUND) or {}).get("opening_sent", 0))
    counted = int(summary.get("today_counted_opening_sent", outbound_openings))

    sched = config.get("schedule") or {}
    max_per_day = max(1, min(30, int(sched.get("max_per_day", 20))))
    hard_daily_cap = _resolve_hard_daily_cap(config)
    total_outbound_and_batch = outbound_openings + batch_openings
    local_batch_budget = max(0, hard_daily_cap - total_outbound_and_batch)
    cloud_batch_budget = _resolve_cloud_batch_budget(runtime, moment)
    effective_batch_budget = (
        min(local_batch_budget, cloud_batch_budget)
        if cloud_batch_budget is not None
        else local_batch_budget
    )

    fav_cap = _resolve_favorite_daily_cap(config)
    today_favorited = int(summary.get("today_favorited", 0))

    return {
        "today_date": summary.get("today_date"),
        "counted_openings_today": counted,
        "max_per_day": max_per_day,
        "remaining_regular_today": max(0, max_per_day - counted),
        "outbound_openings_today": outbound_openings,
        "favorite_queue_openings_today": batch_openings,
        "inbound_openings_today": inbound_openings,
        "total_dispatched_today": total_outbound_and_batch,
        "hard_daily_cap": hard_daily_cap,
        "local_batch_budget": local_batch_budget,
        "cloud_batch_budget": cloud_batch_budget,
        "effective_batch_budget": effective_batch_budget,
        "today_favorited": today_favorited,
        "favorite_daily_cap": fav_cap,
        "remaining_favorites_today": max(0, fav_cap - today_favorited),
        "by_channel": by_channel,
        "exempt_channels": sorted(hm_channel.NON_QUOTA_CHANNELS),
        "legacy_outbound_actions": int(summary.get("today_outbound_actions", 0)),
    }


# ---------------------------------------------------------------------------
# SQLite 连接与内部辅助
# ---------------------------------------------------------------------------


def _open_db(workspace: Path) -> sqlite3.Connection:
    # 确保账本及批次表已通过 SCHEMA_SQL 初始化
    led = JobLedger(workspace)
    conn = sqlite3.connect(str(led.db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _reclaim_expired_leases(conn: sqlite3.Connection, batch_id: str, moment: datetime) -> int:
    """回收超过 CLAIM_LEASE_SECONDS 未确认的 in_flight 条目回 pending。"""
    rows = conn.execute(
        "SELECT item_id, claimed_at FROM favorite_batch_items WHERE batch_id = ? AND status = ?",
        (batch_id, ITEM_IN_FLIGHT),
    ).fetchall()
    reclaimed = 0
    for row in rows:
        claimed_raw = row["claimed_at"]
        expired = True
        if claimed_raw:
            try:
                claimed_dt = parse_iso(str(claimed_raw))
                expired = (moment - claimed_dt).total_seconds() >= CLAIM_LEASE_SECONDS
            except BatchGateError:
                expired = True
        if expired:
            conn.execute(
                """
                UPDATE favorite_batch_items
                SET status = ?, claimed_at = NULL, updated_at = ?
                WHERE item_id = ?
                """,
                (ITEM_PENDING, moment.isoformat(), row["item_id"]),
            )
            reclaimed += 1
    return reclaimed


def _batch_progress_from_conn(conn: sqlite3.Connection, batch_row: sqlite3.Row) -> dict[str, Any]:
    bid = str(batch_row["batch_id"])
    item_rows = conn.execute(
        "SELECT status FROM favorite_batch_items WHERE batch_id = ?",
        (bid,),
    ).fetchall()
    counts = {ITEM_PENDING: 0, ITEM_IN_FLIGHT: 0, ITEM_APPLIED: 0, ITEM_SKIPPED: 0}
    for r in item_rows:
        st = str(r["status"])
        if st in counts:
            counts[st] += 1
    return {
        "batch_id": bid,
        "status": str(batch_row["status"]),
        "channel": str(batch_row["channel"]),
        "apply_at": batch_row["apply_at"],
        "mode": "scheduled" if batch_row["apply_at"] else "manual",
        "note": str(batch_row["note"] or ""),
        "pause_reason": str(batch_row["pause_reason"] or ""),
        "total": len(item_rows),
        "pending": counts[ITEM_PENDING],
        "in_flight": counts[ITEM_IN_FLIGHT],
        "applied": counts[ITEM_APPLIED],
        "skipped": counts[ITEM_SKIPPED],
        "last_dispatch_at": batch_row["last_dispatch_at"],
        "created_at": batch_row["created_at"],
        "updated_at": batch_row["updated_at"],
        "completed_at": batch_row["completed_at"],
        "cancelled_at": batch_row["cancelled_at"],
    }


def _require_dispatchable_batch(
    batch_row: sqlite3.Row,
    moment: datetime,
    *,
    config: dict[str, Any],
) -> None:
    """校验批次状态、apply_at 与安全宵禁护栏。"""
    bid = str(batch_row["batch_id"])
    st = str(batch_row["status"])
    if st in BATCH_TERMINAL_STATES:
        raise BatchGateError("BATCH_TERMINAL", f"批次 {bid} 已结束（{st}）", batch_id=bid, status=st)
    if st == BATCH_PAUSED:
        raise BatchGateError(
            "BATCH_PAUSED",
            f"批次 {bid} 已暂停（{batch_row['pause_reason'] or '待人工处理'}），请先执行 favorite-batch resume 恢复",
            batch_id=bid,
            status=st,
        )

    apply_at_raw = batch_row["apply_at"]
    deadline: datetime | None = None
    if apply_at_raw:
        deadline = parse_iso(str(apply_at_raw))
        if moment < deadline:
            raise BatchGateError(
                "BATCH_NOT_DUE",
                f"批次尚未到投递时间（apply_at={apply_at_raw}），当前禁止提前投递",
                batch_id=bid,
                apply_at=apply_at_raw,
                now=moment.isoformat(),
            )

    # 固定 08:30 ~ 21:30 宵禁护栏（不提供逐次绕过开关）
    if not in_safe_day(moment):
        next_at = next_safe_day_start(moment)
        raise BatchGateError(
            "BATCH_CURFEW",
            f"当前处于平台防风控宵禁时段（仅允许 08:30–21:30 投递），请于 {next_at.isoformat()} 后再试",
            batch_id=bid,
            now=moment.isoformat(),
            next_allowed_at=next_at.isoformat(),
        )

    # 周末规则：仅当 apply_at 显式指定在周末当天时视为用户明确意图放行；
    # 手动立即批次或未设周末 apply_at 时尊重 weekdays_only 配置。
    sched = config.get("schedule") or {}
    weekdays_only = bool(sched.get("weekdays_only", True))
    if weekdays_only and moment.weekday() >= 5:
        explicit_weekend_intent = (
            deadline is not None
            and deadline.astimezone(moment.tzinfo).weekday() >= 5
            and deadline.astimezone(moment.tzinfo).date() == moment.date()
        )
        if not explicit_weekend_intent:
            raise BatchGateError(
                "BATCH_WEEKEND_QUIET",
                "当前为周末且已开启仅工作日执行（如需周末定时投递，请在建批或 resume 时显式指定周末的 --apply-at）",
                batch_id=bid,
                now=moment.isoformat(),
            )


# ---------------------------------------------------------------------------
# 公开状态机 API
# ---------------------------------------------------------------------------


def create_batch(
    workspace: Path,
    *,
    apply_at: str | None = None,
    batch_id: str = "",
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """创建收藏批次。`apply_at` 省略时为手动立即批次；传入时必须带时区偏移。"""
    moment = now or local_now()
    apply_at_iso: str | None = None
    if apply_at is not None and str(apply_at).strip():
        apply_at_iso = parse_iso(str(apply_at)).isoformat()

    bid = (batch_id or f"batch_{moment.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}").strip()
    ts = moment.isoformat()

    conn = _open_db(workspace)
    try:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM favorite_batches WHERE batch_id = ?",
            (bid,),
        ).fetchone()
        if exists:
            conn.rollback()
            raise BatchGateError("BATCH_EXISTS", f"批次 {bid} 已存在", batch_id=bid)

        conn.execute(
            """
            INSERT INTO favorite_batches (
                batch_id, status, channel, apply_at, note, pause_reason,
                last_dispatch_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, '', NULL, ?, ?)
            """,
            (bid, BATCH_PENDING, hm_channel.CHANNEL_FAVORITE_QUEUE, apply_at_iso, str(note or ""), ts, ts),
        )
        row = conn.execute("SELECT * FROM favorite_batches WHERE batch_id = ?", (bid,)).fetchone()
        progress = _batch_progress_from_conn(conn, row)
        conn.commit()
        return progress
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def add_item(
    workspace: Path,
    batch_id: str,
    item: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """向批次追加一个待投递岗位（强制绑定账本 job_id，支持 skipped 后重新入队）。"""
    moment = now or local_now()
    ts = moment.isoformat()
    job_id = str(item.get("job_id") or "").strip()
    if not job_id:
        raise BatchGateError(
            "MISSING_JOB_ID",
            "加入收藏批次必须提供账本 job_id（请先用 ledger upsert-job 落库 favorited 再入队）",
            batch_id=batch_id,
        )

    conn = _open_db(workspace)
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not batch_row:
            conn.rollback()
            raise BatchGateError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", batch_id=batch_id)
        if str(batch_row["status"]) in BATCH_TERMINAL_STATES:
            conn.rollback()
            raise BatchGateError(
                "BATCH_TERMINAL",
                f"批次 {batch_id} 已结束（{batch_row['status']}），不可追加",
                batch_id=batch_id,
                status=str(batch_row["status"]),
            )

        job_row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if not job_row:
            conn.rollback()
            raise BatchGateError(
                "JOB_NOT_FOUND",
                f"账本中找不到 job_id={job_id}，请先将收藏岗位写入账本",
                batch_id=batch_id,
                job_id=job_id,
            )

        dup = conn.execute(
            """
            SELECT item_id FROM favorite_batch_items
            WHERE batch_id = ? AND job_id = ? AND status IN (?, ?)
            """,
            (batch_id, job_id, ITEM_PENDING, ITEM_IN_FLIGHT),
        ).fetchone()
        if dup:
            conn.rollback()
            raise BatchGateError(
                "ITEM_DUPLICATE",
                f"批次 {batch_id} 中已存在未完成的同岗位条目（job_id={job_id}）",
                batch_id=batch_id,
                job_id=job_id,
                item_id=str(dup["item_id"]),
            )

        item_id = f"fbi_{uuid.uuid4().hex[:10]}"
        company = str(item.get("company") or job_row["company"] or "").strip()
        title = str(item.get("title") or job_row["title"] or "").strip()
        salary = str(item.get("salary") or job_row["salary"] or "").strip()
        city = str(item.get("city") or job_row["city"] or "").strip()
        job_url = str(item.get("job_url") or job_row["job_url"] or "").strip()
        reason = str(item.get("reason") or job_row["match_reasons"] or "").strip()

        conn.execute(
            """
            INSERT INTO favorite_batch_items (
                item_id, batch_id, job_id, company, title, salary, city, job_url, reason,
                status, claimed_at, applied_at, skipped_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (item_id, batch_id, job_id, company, title, salary, city, job_url, reason, ITEM_PENDING, ts, ts),
        )
        conn.execute(
            "UPDATE favorite_batches SET updated_at = ? WHERE batch_id = ?",
            (ts, batch_id),
        )
        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        progress = _batch_progress_from_conn(conn, batch_row)
        conn.commit()
        return {"item_id": item_id, "job_id": job_id, "batch": progress}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def claim_next(
    workspace: Path,
    batch_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """原子认领下一个待投递项（带租约、宵禁、抖动间隔、单轮与每日安全上限校验）。"""
    moment = now or local_now()
    ts = moment.isoformat()
    config = load_config(workspace)
    quota = quota_state(workspace, now=moment)

    conn = _open_db(workspace)
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not batch_row:
            conn.rollback()
            raise BatchGateError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", batch_id=batch_id)

        _require_dispatchable_batch(batch_row, moment, config=config)
        _reclaim_expired_leases(conn, batch_id, moment)

        # 检查是否还有 pending 项
        pending_rows = conn.execute(
            """
            SELECT * FROM favorite_batch_items
            WHERE batch_id = ? AND status = ?
            ORDER BY rowid ASC
            """,
            (batch_id, ITEM_PENDING),
        ).fetchall()

        if not pending_rows:
            in_flight_count = conn.execute(
                "SELECT COUNT(*) FROM favorite_batch_items WHERE batch_id = ? AND status = ?",
                (batch_id, ITEM_IN_FLIGHT),
            ).fetchone()[0]
            progress = _batch_progress_from_conn(conn, batch_row)
            conn.commit()
            return {
                "ok": True,
                "next_item": None,
                "queue_empty": in_flight_count == 0,
                "in_flight_count": int(in_flight_count),
                "carry_over": 0,
                "batch": progress,
            }

        # 每日总外发安全硬顶 + 云端预算收敛（超额顺延次日，条目保持 pending）
        if quota["effective_batch_budget"] <= 0:
            progress = _batch_progress_from_conn(conn, batch_row)
            conn.commit()
            return {
                "ok": True,
                "next_item": None,
                "queue_empty": False,
                "carry_over": len(pending_rows),
                "reason": "DAILY_SAFETY_CAP_REACHED",
                "quota": quota,
                "batch": progress,
            }

        # 单轮滚动上限检查（45 分钟内同批次最多 MAX_BATCH_PER_ROUND 条）
        cutoff = moment - timedelta(minutes=BATCH_ROUND_WINDOW_MINUTES)
        applied_rows = conn.execute(
            """
            SELECT applied_at FROM favorite_batch_items
            WHERE batch_id = ? AND status = ? AND applied_at IS NOT NULL
            """,
            (batch_id, ITEM_APPLIED),
        ).fetchall()
        recent_applied = 0
        for r in applied_rows:
            try:
                if parse_iso(str(r["applied_at"])) >= cutoff:
                    recent_applied += 1
            except BatchGateError:
                continue
        if recent_applied >= MAX_BATCH_PER_ROUND:
            progress = _batch_progress_from_conn(conn, batch_row)
            conn.commit()
            return {
                "ok": True,
                "next_item": None,
                "queue_empty": False,
                "carry_over": len(pending_rows),
                "reason": "BATCH_ROUND_LIMIT_REACHED",
                "max_per_round": MAX_BATCH_PER_ROUND,
                "batch": progress,
            }

        # 串行抖动间隔强制检查（代码层防风控节流）
        last_dispatch_raw = batch_row["last_dispatch_at"]
        if last_dispatch_raw:
            try:
                last_dt = parse_iso(str(last_dispatch_raw))
                required_gap = _deterministic_jitter_seconds(batch_id, len(applied_rows))
                elapsed = (moment - last_dt).total_seconds()
                if elapsed < required_gap:
                    retry_after = max(1, int(round(required_gap - elapsed)))
                    conn.rollback()
                    raise BatchGateError(
                        "BATCH_THROTTLED",
                        f"为防范平台风控，两次批次投递之间需间隔至少 {required_gap} 秒，请 {retry_after} 秒后再试",
                        batch_id=batch_id,
                        retry_after_seconds=retry_after,
                        min_interval_seconds=required_gap,
                    )
            except BatchGateError as exc:
                if exc.code == "BATCH_THROTTLED":
                    raise

        target = pending_rows[0]
        conn.execute(
            """
            UPDATE favorite_batch_items
            SET status = ?, claimed_at = ?, updated_at = ?
            WHERE item_id = ?
            """,
            (ITEM_IN_FLIGHT, ts, ts, target["item_id"]),
        )
        conn.execute(
            """
            UPDATE favorite_batches
            SET status = ?, updated_at = ?
            WHERE batch_id = ?
            """,
            (BATCH_APPLYING, ts, batch_id),
        )
        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        progress = _batch_progress_from_conn(conn, batch_row)
        conn.commit()
        return {
            "ok": True,
            "next_item": {
                "item_id": str(target["item_id"]),
                "job_id": str(target["job_id"]),
                "company": str(target["company"]),
                "title": str(target["title"]),
                "salary": str(target["salary"]),
                "city": str(target["city"]),
                "job_url": str(target["job_url"]),
                "reason": str(target["reason"]),
                "claimed_at": ts,
                "lease_seconds": CLAIM_LEASE_SECONDS,
            },
            "queue_empty": False,
            "carry_over": 0,
            "batch": progress,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def mark_item(
    workspace: Path,
    batch_id: str,
    *,
    item_id: str = "",
    job_id: str = "",
    outcome: str = ITEM_APPLIED,
    reason: str = "",
    note: str = "",
    resume_used: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """标记批次条目结果。`outcome=applied` 在同一 SQLite 事务中联动更新账本为
    `status='opening_sent'` + `channel='favorite_queue'`。"""
    moment = now or local_now()
    ts = moment.isoformat()
    if outcome not in (ITEM_APPLIED, ITEM_SKIPPED):
        raise BatchGateError("INVALID_OUTCOME", f"未支持的结果 {outcome!r}")
    if not item_id and not job_id:
        raise BatchGateError("MISSING_ITEM_IDENTIFIER", "mark 必须提供 --item-id（推荐）或 --job-id")

    config = load_config(workspace)
    conn = _open_db(workspace)
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not batch_row:
            conn.rollback()
            raise BatchGateError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", batch_id=batch_id)
        if str(batch_row["status"]) in BATCH_TERMINAL_STATES:
            conn.rollback()
            raise BatchGateError(
                "BATCH_TERMINAL",
                f"批次 {batch_id} 已结束（{batch_row['status']}），不可修改条目",
                batch_id=batch_id,
                status=str(batch_row["status"]),
            )

        if outcome == ITEM_APPLIED:
            # 防止绕过 claim-next 在 apply_at 前或宵禁内偷跑 mark applied
            _require_dispatchable_batch(batch_row, moment, config=config)

        target = None
        if item_id:
            target = conn.execute(
                "SELECT * FROM favorite_batch_items WHERE batch_id = ? AND item_id = ?",
                (batch_id, item_id),
            ).fetchone()
        else:
            # 按 job_id 查找时优先匹配 in_flight，其次 pending，绝不命中旧 skipped 项
            target = conn.execute(
                """
                SELECT * FROM favorite_batch_items
                WHERE batch_id = ? AND job_id = ? AND status IN (?, ?)
                ORDER BY CASE status WHEN 'in_flight' THEN 0 ELSE 1 END, rowid ASC
                LIMIT 1
                """,
                (batch_id, job_id, ITEM_IN_FLIGHT, ITEM_PENDING),
            ).fetchone()

        if target is None:
            conn.rollback()
            raise BatchGateError(
                "ITEM_NOT_FOUND",
                f"批次 {batch_id} 中找不到可标记的条目（item_id={item_id!r}, job_id={job_id!r}）",
                batch_id=batch_id,
            )

        if str(target["status"]) in (ITEM_APPLIED, ITEM_SKIPPED):
            conn.rollback()
            raise BatchGateError(
                "ITEM_ALREADY_FINAL",
                f"条目 {target['item_id']} 已处于终态（{target['status']}），不可重复标记",
                batch_id=batch_id,
                item_id=str(target["item_id"]),
                status=str(target["status"]),
            )

        target_item_id = str(target["item_id"])
        target_job_id = str(target["job_id"])

        if outcome == ITEM_APPLIED:
            job_row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (target_job_id,)).fetchone()
            if not job_row:
                conn.rollback()
                raise BatchGateError(
                    "JOB_NOT_FOUND",
                    f"账本中找不到关联岗位 job_id={target_job_id}",
                    batch_id=batch_id,
                    job_id=target_job_id,
                )

            existing_job = dict(job_row)
            milestones = _milestone_updates_for_status("opening_sent", ts, existing_job)
            final_resume = resume_used.strip() if resume_used.strip() else str(existing_job.get("resume_used") or "")
            note_piece = note.strip() or f"收藏批次 {batch_id} 统一投递"
            prev_note = str(existing_job.get("note") or "").strip()
            final_note = f"{prev_note} | {note_piece}" if prev_note else note_piece

            conn.execute(
                """
                UPDATE jobs SET
                    status = 'opening_sent',
                    channel = ?,
                    resume_used = ?,
                    note = ?,
                    favorited_at = ?,
                    opening_sent_at = ?,
                    connected_at = ?,
                    resume_requested_at = ?,
                    resume_delivered_at = ?,
                    handed_off_at = ?,
                    interview_scheduled_at = ?,
                    offer_at = ?,
                    closed_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    hm_channel.CHANNEL_FAVORITE_QUEUE,
                    final_resume,
                    final_note,
                    milestones["favorited_at"],
                    milestones["opening_sent_at"],
                    milestones["connected_at"],
                    milestones["resume_requested_at"],
                    milestones["resume_delivered_at"],
                    milestones["handed_off_at"],
                    milestones["interview_scheduled_at"],
                    milestones["offer_at"],
                    milestones["closed_at"],
                    ts,
                    target_job_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO ledger_events (job_id, from_status, to_status, resume_used, note, created_at)
                VALUES (?, ?, 'opening_sent', ?, ?, ?)
                """,
                (target_job_id, str(existing_job["status"]), final_resume, note_piece, ts),
            )
            conn.execute(
                """
                UPDATE favorite_batch_items
                SET status = ?, claimed_at = NULL, applied_at = ?, skipped_reason = NULL, updated_at = ?
                WHERE item_id = ?
                """,
                (ITEM_APPLIED, ts, ts, target_item_id),
            )
            conn.execute(
                """
                UPDATE favorite_batches
                SET status = ?, last_dispatch_at = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (BATCH_APPLYING, ts, ts, batch_id),
            )
        else:
            conn.execute(
                """
                UPDATE favorite_batch_items
                SET status = ?, claimed_at = NULL, skipped_reason = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (ITEM_SKIPPED, str(reason or ""), ts, target_item_id),
            )
            conn.execute(
                "UPDATE favorite_batches SET updated_at = ? WHERE batch_id = ?",
                (ts, batch_id),
            )

        remaining = conn.execute(
            """
            SELECT COUNT(*) FROM favorite_batch_items
            WHERE batch_id = ? AND status IN (?, ?)
            """,
            (batch_id, ITEM_PENDING, ITEM_IN_FLIGHT),
        ).fetchone()[0]
        if int(remaining) == 0:
            conn.execute(
                """
                UPDATE favorite_batches
                SET status = ?, completed_at = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (BATCH_COMPLETED, ts, ts, batch_id),
            )

        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        progress = _batch_progress_from_conn(conn, batch_row)
        conn.commit()
        return {
            "ok": True,
            "item_id": target_item_id,
            "job_id": target_job_id,
            "outcome": outcome,
            "ledger_synced": outcome == ITEM_APPLIED,
            "batch": progress,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def pause_batch(
    workspace: Path,
    batch_id: str,
    *,
    reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """暂停批次（如遇滑块验证码/登录失效），释放 in_flight 租约保留断点，可用 resume 续投。"""
    moment = now or local_now()
    ts = moment.isoformat()
    conn = _open_db(workspace)
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not batch_row:
            conn.rollback()
            raise BatchGateError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", batch_id=batch_id)
        if str(batch_row["status"]) in BATCH_TERMINAL_STATES:
            conn.rollback()
            raise BatchGateError(
                "BATCH_TERMINAL",
                f"批次 {batch_id} 已结束（{batch_row['status']}），不可暂停",
                batch_id=batch_id,
                status=str(batch_row["status"]),
            )

        # 释放正在进行中的租约回 pending，确保恢复后可续投
        conn.execute(
            """
            UPDATE favorite_batch_items
            SET status = ?, claimed_at = NULL, updated_at = ?
            WHERE batch_id = ? AND status = ?
            """,
            (ITEM_PENDING, ts, batch_id, ITEM_IN_FLIGHT),
        )
        conn.execute(
            """
            UPDATE favorite_batches
            SET status = ?, pause_reason = ?, updated_at = ?
            WHERE batch_id = ?
            """,
            (BATCH_PAUSED, str(reason or ""), ts, batch_id),
        )
        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        progress = _batch_progress_from_conn(conn, batch_row)
        conn.commit()
        return progress
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def resume_batch(
    workspace: Path,
    batch_id: str,
    *,
    apply_at: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """恢复已暂停的批次，可选重新设定 apply_at。"""
    moment = now or local_now()
    ts = moment.isoformat()
    new_apply_at: str | None = None
    update_apply_at = False
    if apply_at is not None and str(apply_at).strip():
        new_apply_at = parse_iso(str(apply_at)).isoformat()
        update_apply_at = True

    conn = _open_db(workspace)
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not batch_row:
            conn.rollback()
            raise BatchGateError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", batch_id=batch_id)
        if str(batch_row["status"]) != BATCH_PAUSED:
            conn.rollback()
            raise BatchGateError(
                "BATCH_NOT_PAUSED",
                f"批次 {batch_id} 当前状态为 {batch_row['status']}，仅 paused 状态可恢复",
                batch_id=batch_id,
                status=str(batch_row["status"]),
            )

        applied_cnt = conn.execute(
            "SELECT COUNT(*) FROM favorite_batch_items WHERE batch_id = ? AND status = ?",
            (batch_id, ITEM_APPLIED),
        ).fetchone()[0]
        target_status = BATCH_APPLYING if int(applied_cnt) > 0 else BATCH_PENDING

        if update_apply_at:
            conn.execute(
                """
                UPDATE favorite_batches
                SET status = ?, apply_at = ?, pause_reason = '', updated_at = ?
                WHERE batch_id = ?
                """,
                (target_status, new_apply_at, ts, batch_id),
            )
        else:
            conn.execute(
                """
                UPDATE favorite_batches
                SET status = ?, pause_reason = '', updated_at = ?
                WHERE batch_id = ?
                """,
                (target_status, ts, batch_id),
            )
        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        progress = _batch_progress_from_conn(conn, batch_row)
        conn.commit()
        return progress
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def cancel_batch(
    workspace: Path,
    batch_id: str,
    *,
    reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """彻底取消批次（进入终态 cancelled，不可再投递或恢复）。"""
    moment = now or local_now()
    ts = moment.isoformat()
    conn = _open_db(workspace)
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not batch_row:
            conn.rollback()
            raise BatchGateError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", batch_id=batch_id)
        if str(batch_row["status"]) in BATCH_TERMINAL_STATES:
            conn.rollback()
            raise BatchGateError(
                "BATCH_TERMINAL",
                f"批次 {batch_id} 已结束（{batch_row['status']}），不可重复取消",
                batch_id=batch_id,
                status=str(batch_row["status"]),
            )

        conn.execute(
            """
            UPDATE favorite_batch_items
            SET status = ?, claimed_at = NULL, skipped_reason = ?, updated_at = ?
            WHERE batch_id = ? AND status IN (?, ?)
            """,
            (ITEM_SKIPPED, str(reason or "批次已取消"), ts, batch_id, ITEM_PENDING, ITEM_IN_FLIGHT),
        )
        conn.execute(
            """
            UPDATE favorite_batches
            SET status = ?, pause_reason = ?, cancelled_at = ?, updated_at = ?
            WHERE batch_id = ?
            """,
            (BATCH_CANCELLED, str(reason or ""), ts, ts, batch_id),
        )
        batch_row = conn.execute(
            "SELECT * FROM favorite_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        progress = _batch_progress_from_conn(conn, batch_row)
        conn.commit()
        return progress
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 只读诊断与到点检查（替代顶层 gate）
# ---------------------------------------------------------------------------


def due_batches(workspace: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """列出已到点、且仍有待投递项的非暂停非终态批次（按真实时间戳升序）。"""
    moment = now or local_now()
    conn = _open_db(workspace)
    try:
        rows = conn.execute(
            "SELECT * FROM favorite_batches WHERE status IN (?, ?)",
            (BATCH_PENDING, BATCH_APPLYING),
        ).fetchall()
        due: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows:
            progress = _batch_progress_from_conn(conn, row)
            if progress["pending"] <= 0 and progress["in_flight"] <= 0:
                continue
            apply_at_raw = row["apply_at"]
            if apply_at_raw:
                try:
                    deadline = parse_iso(str(apply_at_raw))
                except BatchGateError:
                    continue
                if moment < deadline:
                    continue
                sort_key = deadline.astimezone(timezone.utc)
            else:
                try:
                    sort_key = parse_iso(str(row["created_at"])).astimezone(timezone.utc)
                except BatchGateError:
                    sort_key = moment.astimezone(timezone.utc)
            due.append((sort_key, progress))
        due.sort(key=lambda pair: pair[0])
        return [item for _, item in due]
    finally:
        conn.close()


def check_due(workspace: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """纯只读批次到点与安全底线诊断（不替代 pro patrol-gate 的巡检调度）。"""
    moment = now or local_now()
    config = load_config(workspace)
    runtime = load_runtime_state(workspace)
    quota = quota_state(workspace, now=moment)
    effective_busy, stale_recovery = _busy_state(runtime, moment)
    safe_day = in_safe_day(moment)
    due = due_batches(workspace, now=moment)

    conn = _open_db(workspace)
    try:
        paused_rows = conn.execute(
            "SELECT * FROM favorite_batches WHERE status = ? ORDER BY updated_at DESC",
            (BATCH_PAUSED,),
        ).fetchall()
        paused = [_batch_progress_from_conn(conn, r) for r in paused_rows]
    finally:
        conn.close()

    can_dispatch_now = bool(due) and safe_day and (not effective_busy) and (quota["effective_batch_budget"] > 0)
    return {
        "ok": True,
        "now": moment.isoformat(),
        "in_safe_day": safe_day,
        "safe_day_window": "08:30-21:30",
        "next_allowed_at": None if safe_day else next_safe_day_start(moment).isoformat(),
        "busy": effective_busy,
        "stale_busy_recovery": stale_recovery,
        "favorite_batch_due": bool(due),
        "due_batches": due,
        "paused_batches": paused,
        "can_dispatch_now": can_dispatch_now,
        "quota": quota,
    }


def snapshot_for_diagnostics(workspace: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """查看全部批次进度与当日配额分桶。"""
    moment = now or local_now()
    conn = _open_db(workspace)
    try:
        rows = conn.execute(
            "SELECT * FROM favorite_batches ORDER BY created_at DESC",
        ).fetchall()
        batches = [_batch_progress_from_conn(conn, r) for r in rows]
    finally:
        conn.close()
    return {
        "now": moment.isoformat(),
        "batches": batches,
        "quota": quota_state(workspace, now=moment),
    }


__all__ = [
    "ACK_HARD_DAILY_CAP",
    "BATCH_APPLYING",
    "BATCH_CANCELLED",
    "BATCH_COMPLETED",
    "BATCH_PAUSED",
    "BATCH_PENDING",
    "BATCH_TERMINAL_STATES",
    "BatchGateError",
    "CLAIM_LEASE_SECONDS",
    "DEFAULT_FAVORITE_DAILY_CAP",
    "DEFAULT_HARD_DAILY_CAP",
    "ITEM_APPLIED",
    "ITEM_IN_FLIGHT",
    "ITEM_PENDING",
    "ITEM_SKIPPED",
    "MAX_BATCH_PER_ROUND",
    "add_item",
    "cancel_batch",
    "check_due",
    "claim_next",
    "create_batch",
    "due_batches",
    "in_safe_day",
    "local_now",
    "mark_item",
    "next_safe_day_start",
    "parse_iso",
    "pause_batch",
    "quota_state",
    "resume_batch",
    "snapshot_for_diagnostics",
]
