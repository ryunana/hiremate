"""本地确定性调度门控与「收藏批次统一投递」状态机。

## 为什么需要这个模块

在此之前，"现在该不该动"这个判断分散在两处：

1. 云端 `pro patrol-gate`：需要联网、需要 Pro 权益，返回 `allow_unread_check` / `discover_due`；
2. 每个宿主 Agent 在提示词里自己写一遍「几点到几点算工作时间、每天最多几个、冷却多久」。

第二处没有任何程序性保证——它只是一段自然语言。用户报告的真实事故是：模型某轮没读到
「批次到点前不准投」这句约束，于是把本该留到次日 09:30 统一投递的收藏岗位提前投了出去。

本模块把这些判断收敛成一份可测试的纯函数，并把「延迟批量投递」做成带硬约束的状态机：
`apply_at` 之前，投递请求在**代码层**就被拒绝，而不是靠提示词提醒。

## 设计要点

- **纯本地、零网络**：免费版也能用；云端 Pro 守卫仍是另一层，两者取更严格结果。
- **channel 分桶**：只有 `counts_toward_quota()` 为真的外发才消耗每日额度，
  收藏与批次补投走各自的语义（见 `hiremate_lib/channel.py`）。
- **配额只有一个计算入口**：`quota_state()` 是唯一的口径来源，避免多处各算一套导致漂移。
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from . import channel as hm_channel
from .ledger import JobLedger
from .workspace import atomic_write_text, load_config, load_runtime_state

# ---------------------------------------------------------------------------
# 收藏批次常量
# ---------------------------------------------------------------------------

BATCH_FILE_NAME = "favorite_batches.json"

BATCH_PENDING = "pending"
BATCH_APPLYING = "applying"
BATCH_COMPLETED = "completed"
BATCH_ABORTED = "aborted"

BATCH_TERMINAL_STATES = frozenset({BATCH_COMPLETED, BATCH_ABORTED})

ITEM_PENDING = "pending"
ITEM_APPLIED = "applied"
ITEM_SKIPPED = "skipped"


class GateError(Exception):
    """门控拒绝执行。携带机器可读 code，便于宿主 Agent 判断而非解析自然语言。"""

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
# 时间与时间窗
# ---------------------------------------------------------------------------


def local_now() -> datetime:
    """带本地时区偏移的当前时间（与 `workspace.now_iso()` 同一时区口径）。"""
    return datetime.now(timezone.utc).astimezone()


def parse_iso(value: str) -> datetime:
    """解析 ISO-8601；要求带时区，避免 naive 时间静默按本机时区误算。"""
    raw = str(value or "").strip()
    if not raw:
        raise GateError("INVALID_TIMESTAMP", "时间戳为空")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise GateError("INVALID_TIMESTAMP", f"无法解析时间戳 {value!r}：{exc}") from exc
    if dt.tzinfo is None:
        raise GateError(
            "NAIVE_TIMESTAMP",
            f"时间戳必须带时区偏移（如 +08:00），收到 {value!r}",
        )
    return dt


def _parse_hhmm(value: str) -> time:
    raw = str(value or "").strip()
    try:
        hh, mm = raw.split(":", 1)
        return time(hour=int(hh), minute=int(mm))
    except (ValueError, TypeError) as exc:
        raise GateError("INVALID_WINDOW", f"无法解析时间窗端点 {value!r}（应为 HH:MM）") from exc


def parse_windows(active_windows: Any) -> list[tuple[time, time]]:
    """把 `["09:00-12:00", "14:00-18:00"]` 解析成半开区间列表 `[start, end)`。"""
    items = active_windows or ["09:00-12:00", "14:00-18:00"]
    if isinstance(items, str):
        items = [items]
    out: list[tuple[time, time]] = []
    for raw in items:
        text = str(raw or "").strip()
        if not text or "-" not in text:
            continue
        left, right = text.split("-", 1)
        start, end = _parse_hhmm(left), _parse_hhmm(right)
        if start >= end:
            raise GateError("INVALID_WINDOW", f"时间窗 {text!r} 的起点必须早于终点")
        out.append((start, end))
    return out or [(time(9, 0), time(12, 0)), (time(14, 0), time(18, 0))]


def in_windows(now: datetime, windows: list[tuple[time, time]]) -> bool:
    """半开区间判定：`[start, end)`，即 12:00 整点算窗内结束、不算窗内。"""
    current = now.time().replace(tzinfo=None)
    return any(start <= current < end for start, end in windows)


def _workdays_only(config: dict[str, Any]) -> bool:
    sched = config.get("schedule") or {}
    return bool(sched.get("weekdays_only", True))


# ---------------------------------------------------------------------------
# 配额：唯一口径
# ---------------------------------------------------------------------------


def quota_state(workspace: Path, *, ledger: JobLedger | None = None) -> dict[str, Any]:
    """返回当日配额使用情况（channel 分桶）。

    这是整个项目里唯一允许计算「今天还能投几个」的地方。宿主 Agent、本地门控、
    看板都应当读这里，而不是各自按自己的 SQL 再算一遍。
    """
    led = ledger or JobLedger(workspace)
    summary = led.summary()
    by_channel = summary.get("today_by_channel") or {}

    counted = 0
    detail: dict[str, dict[str, int]] = {}
    for ch, bucket in by_channel.items():
        if not isinstance(bucket, dict):
            continue
        counted_openings = int(bucket.get("counted_opening_sent", 0))
        detail[ch] = {
            "favorited": int(bucket.get("favorited", 0)),
            "opening_sent": int(bucket.get("opening_sent", 0)),
            "counted_opening_sent": counted_openings,
        }
        if hm_channel.counts_toward_quota(ch):
            counted += counted_openings

    return {
        "today_date": summary.get("today_date"),
        "counted_openings_today": counted,
        "by_channel": detail,
        "legacy_outbound_actions": int(summary.get("today_outbound_actions", 0)),
        "today_favorited": int(summary.get("today_favorited", 0)),
        "today_opening_sent": int(summary.get("today_opening_sent", 0)),
    }


# ---------------------------------------------------------------------------
# 收藏批次状态机
# ---------------------------------------------------------------------------


def _batch_path(workspace: Path) -> Path:
    return workspace / BATCH_FILE_NAME


def _empty_store() -> dict[str, Any]:
    return {"version": 1, "batches": {}}


def load_batches(workspace: Path) -> dict[str, Any]:
    path = _batch_path(workspace)
    if not path.is_file():
        return _empty_store()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 损坏时降级为空而非抛错：批次文件绝不能成为打不开账本的理由。
        return _empty_store()
    if not isinstance(raw, dict):
        return _empty_store()
    store = _empty_store()
    if isinstance(raw.get("batches"), dict):
        store["batches"] = raw["batches"]
    return store


def save_batches(workspace: Path, store: dict[str, Any]) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        _batch_path(workspace),
        json.dumps(store, ensure_ascii=False, indent=2) + "\n",
    )
    return store


def create_batch(
    workspace: Path,
    *,
    apply_at: str,
    batch_id: str = "",
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """新建一个收藏批次。`apply_at` 必须是未来时间，否则拒绝。"""
    moment = now or local_now()
    deadline = parse_iso(apply_at)
    store = load_batches(workspace)
    bid = (batch_id or f"batch_{moment.strftime('%Y%m%d_%H%M%S')}").strip()

    if bid in store["batches"]:
        raise GateError("BATCH_EXISTS", f"批次 {bid} 已存在", batch_id=bid)

    batch = {
        "batch_id": bid,
        "status": BATCH_PENDING,
        "channel": hm_channel.CHANNEL_FAVORITE_QUEUE,
        "apply_at": deadline.isoformat(),
        "note": str(note or ""),
        "created_at": moment.isoformat(),
        "updated_at": moment.isoformat(),
        "items": [],
    }
    store["batches"][bid] = batch
    save_batches(workspace, store)
    return batch


def add_item(
    workspace: Path,
    batch_id: str,
    item: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """向批次追加一个待投递岗位（仅允许在 `pending`/`applying` 阶段追加）。"""
    moment = now or local_now()
    store = load_batches(workspace)
    batch = store["batches"].get(batch_id)
    if not batch:
        raise GateError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", batch_id=batch_id)
    if batch.get("status") in BATCH_TERMINAL_STATES:
        raise GateError(
            "BATCH_TERMINAL",
            f"批次 {batch_id} 已结束（{batch.get('status')}），不可追加",
            batch_id=batch_id,
            status=batch.get("status"),
        )

    company = str(item.get("company") or "").strip()
    title = str(item.get("title") or "").strip()
    if not company or not title:
        raise GateError("INVALID_ITEM", "批次岗位必须同时包含 company 与 title")

    entry = {
        "job_id": str(item.get("job_id") or "").strip(),
        "company": company,
        "title": title,
        "salary": str(item.get("salary") or "").strip(),
        "job_url": str(item.get("job_url") or "").strip(),
        "reason": str(item.get("reason") or "").strip(),
        "status": ITEM_PENDING,
        "added_at": moment.isoformat(),
        "applied_at": None,
        "skipped_reason": None,
    }

    for existing in batch["items"]:
        if (
            existing.get("company") == company
            and existing.get("title") == title
            and existing.get("status") == ITEM_PENDING
        ):
            raise GateError(
                "ITEM_DUPLICATE",
                f"批次中已存在同公司同岗位的待投递项：{company} / {title}",
                batch_id=batch_id,
            )

    batch["items"].append(entry)
    batch["updated_at"] = moment.isoformat()
    save_batches(workspace, store)
    return batch


def _require_apply_window(batch: dict[str, Any], moment: datetime) -> None:
    """硬约束：`apply_at` 之前拒绝一切投递动作。"""
    if batch.get("status") in BATCH_TERMINAL_STATES:
        raise GateError(
            "BATCH_TERMINAL",
            f"批次已结束（{batch.get('status')}）",
            batch_id=batch.get("batch_id"),
            status=batch.get("status"),
        )
    deadline = parse_iso(batch.get("apply_at", ""))
    if moment < deadline:
        raise GateError(
            "BATCH_NOT_DUE",
            f"批次尚未到投递时间（apply_at={batch.get('apply_at')}），当前禁止提前投递",
            batch_id=batch.get("batch_id"),
            apply_at=batch.get("apply_at"),
            now=moment.isoformat(),
        )


def claim_next(batch: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
    """取出下一个待投递项，并把批次推进到 `applying`。

    返回 `None` 表示队列已空。`apply_at` 之前调用会抛 `BATCH_NOT_DUE`。
    """
    moment = now or local_now()
    _require_apply_window(batch, moment)
    for item in batch.get("items") or []:
        if item.get("status") == ITEM_PENDING:
            batch["status"] = BATCH_APPLYING
            batch["updated_at"] = moment.isoformat()
            return dict(item)
    return None


def claim_next_persisted(
    workspace: Path,
    batch_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """`claim_next` 的落盘版：取出下一项并立即写回状态。"""
    moment = now or local_now()
    store = load_batches(workspace)
    batch = store["batches"].get(batch_id)
    if not batch:
        raise GateError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", batch_id=batch_id)
    item = claim_next(batch, now=moment)
    save_batches(workspace, store)
    return item


def mark_item(
    workspace: Path,
    batch_id: str,
    *,
    company: str,
    title: str,
    outcome: str = ITEM_APPLIED,
    reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """把某一项标记为已投递 / 已跳过，并返回批次当前进度。"""
    moment = now or local_now()
    if outcome not in (ITEM_APPLIED, ITEM_SKIPPED):
        raise GateError("INVALID_OUTCOME", f"未支持的结果 {outcome!r}")

    store = load_batches(workspace)
    batch = store["batches"].get(batch_id)
    if not batch:
        raise GateError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", batch_id=batch_id)

    target = None
    for item in batch.get("items") or []:
        if item.get("company") == company and item.get("title") == title:
            target = item
            break
    if target is None:
        raise GateError(
            "ITEM_NOT_FOUND",
            f"批次中找不到 {company} / {title}",
            batch_id=batch_id,
        )

    target["status"] = outcome
    target["applied_at"] = moment.isoformat() if outcome == ITEM_APPLIED else None
    target["skipped_reason"] = str(reason or "") if outcome == ITEM_SKIPPED else None
    batch["updated_at"] = moment.isoformat()

    # 已经有实际投递动作发生，批次理应处于进行中（即使宿主跳过了 claim-next）
    if batch.get("status") == BATCH_PENDING and outcome == ITEM_APPLIED:
        _require_apply_window(batch, moment)
        batch["status"] = BATCH_APPLYING

    if not any(i.get("status") == ITEM_PENDING for i in batch.get("items") or []):
        batch["status"] = BATCH_COMPLETED
        batch["completed_at"] = moment.isoformat()

    save_batches(workspace, store)
    return batch_progress(batch)


def abort_batch(
    workspace: Path,
    batch_id: str,
    *,
    reason: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """因登录失效/验证码等人为中断而中止批次，保留断点供下次续投。"""
    moment = now or local_now()
    store = load_batches(workspace)
    batch = store["batches"].get(batch_id)
    if not batch:
        raise GateError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", batch_id=batch_id)
    batch["status"] = BATCH_ABORTED
    batch["aborted_at"] = moment.isoformat()
    batch["abort_reason"] = str(reason or "")
    batch["updated_at"] = moment.isoformat()
    save_batches(workspace, store)
    return batch_progress(batch)


def batch_progress(batch: dict[str, Any]) -> dict[str, Any]:
    items = batch.get("items") or []
    applied = sum(1 for i in items if i.get("status") == ITEM_APPLIED)
    skipped = sum(1 for i in items if i.get("status") == ITEM_SKIPPED)
    pending = sum(1 for i in items if i.get("status") == ITEM_PENDING)
    return {
        "batch_id": batch.get("batch_id"),
        "status": batch.get("status"),
        "channel": batch.get("channel"),
        "apply_at": batch.get("apply_at"),
        "total": len(items),
        "applied": applied,
        "skipped": skipped,
        "pending": pending,
        "updated_at": batch.get("updated_at"),
    }


def due_batches(workspace: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """列出已到点、且还有待投递项的批次（按 apply_at 升序）。"""
    moment = now or local_now()
    store = load_batches(workspace)
    out: list[dict[str, Any]] = []
    for batch in store["batches"].values():
        if batch.get("status") in BATCH_TERMINAL_STATES:
            continue
        if not any(i.get("status") == ITEM_PENDING for i in batch.get("items") or []):
            continue
        try:
            deadline = parse_iso(batch.get("apply_at", ""))
        except GateError:
            continue
        if moment >= deadline:
            out.append(batch_progress(batch))
    out.sort(key=lambda b: b.get("apply_at") or "")
    return out


# ---------------------------------------------------------------------------
# 总门控
# ---------------------------------------------------------------------------


def evaluate(
    workspace: Path,
    *,
    now: datetime | None = None,
    ledger: JobLedger | None = None,
) -> dict[str, Any]:
    """计算当前这一轮该做什么。纯函数，不写任何状态、不发任何网络请求。"""
    moment = now or local_now()
    config = load_config(workspace)
    runtime = load_runtime_state(workspace)
    sched = config.get("schedule") or {}

    windows = parse_windows(sched.get("active_windows"))
    workday = (not _workdays_only(config)) or moment.weekday() < 5
    inside_window = workday and in_windows(moment, windows)

    quota = quota_state(workspace, ledger=ledger)
    max_per_day = max(1, int(sched.get("max_per_day", 20)))
    max_per_round = max(1, int(sched.get("max_per_round", 5)))
    remaining = max(0, max_per_day - quota["counted_openings_today"])

    busy = bool(runtime.get("busy"))
    last_discover = runtime.get("last_discover_at")
    cooldown_minutes = max(1, int(sched.get("min_interval_minutes", 90)))
    cooldown_ready = True
    next_discover_at = None
    if last_discover:
        try:
            ready_at = parse_iso(last_discover) + timedelta(minutes=cooldown_minutes)
            next_discover_at = ready_at.isoformat()
            cooldown_ready = moment >= ready_at
        except GateError:
            cooldown_ready = True

    batches_due = due_batches(workspace, now=moment)

    # 批次投递优先于常规找岗，且不受常规配额与冷却限制（channel 已豁免）。
    return {
        "ok": True,
        "now": moment.isoformat(),
        "workday": workday,
        "in_active_window": inside_window,
        "active_windows": [f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in windows],
        "busy": busy,
        "quota": {
            "counted_openings_today": quota["counted_openings_today"],
            "max_per_day": max_per_day,
            "remaining_today": remaining,
            "max_per_round": min(max_per_round, remaining),
            "by_channel": quota["by_channel"],
            "exempt_channels": sorted(hm_channel.NON_QUOTA_CHANNELS),
        },
        "cooldown": {
            "minutes": cooldown_minutes,
            "last_discover_at": last_discover,
            "next_discover_at": next_discover_at,
            "ready": cooldown_ready,
        },
        "favorite_batches_due": batches_due,
        "favorite_batch_due": bool(batches_due),
        # 动作许可
        "may_check_messages": inside_window and not busy,
        "may_favorite_batch": bool(batches_due) and not busy,
        "may_search_discover": bool(
            inside_window and not busy and remaining > 0 and cooldown_ready
        ),
        "has_due_work": (not busy) and (inside_window or bool(batches_due)),
    }


def assert_batch_dispatch_allowed(
    workspace: Path,
    batch_id: str,
    *,
    now: datetime | None = None,
) -> None:
    """宿主在真正点「立即沟通」之前调用；未到点即抛 `BATCH_NOT_DUE`。"""
    moment = now or local_now()
    store = load_batches(workspace)
    batch = store["batches"].get(batch_id)
    if not batch:
        raise GateError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在", batch_id=batch_id)
    _require_apply_window(batch, moment)


def snapshot_for_diagnostics(workspace: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """给看板/排查用的只读快照（不改变任何状态）。"""
    moment = now or local_now()
    store = load_batches(workspace)
    return {
        "now": moment.isoformat(),
        "batches": [batch_progress(b) for b in store["batches"].values()],
        "quota": quota_state(workspace),
    }


__all__ = [
    "BATCH_APPLYING",
    "BATCH_ABORTED",
    "BATCH_COMPLETED",
    "BATCH_FILE_NAME",
    "BATCH_PENDING",
    "BATCH_TERMINAL_STATES",
    "GateError",
    "ITEM_APPLIED",
    "ITEM_PENDING",
    "ITEM_SKIPPED",
    "abort_batch",
    "add_item",
    "assert_batch_dispatch_allowed",
    "batch_progress",
    "claim_next",
    "claim_next_persisted",
    "create_batch",
    "due_batches",
    "evaluate",
    "in_windows",
    "load_batches",
    "local_now",
    "mark_item",
    "parse_iso",
    "parse_windows",
    "quota_state",
    "save_batches",
    "snapshot_for_diagnostics",
]
