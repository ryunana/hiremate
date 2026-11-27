"""本地 SQLite 四态精细求职账本、三维查重与防注入 CSV 导出。"""

from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .channel import (
    CHANNEL_FAVORITE_QUEUE,
    CHANNEL_OUTBOUND,
    channel_label,
    counts_toward_quota,
    normalize_channel,
)
from .normalize import (
    _NEGATED_OUTSOURCING_RE,
    companies_match,
    employer_shield_match,
    normalize_company_parts,
    normalize_title,
    sanitize_csv_cell,
    title_similarity,
)
from .notifier import send_handoff_notification
from .workspace import ledger_db_path, now_iso
from .xlsx_export import write_xlsx_workbook

VALID_STATUSES: tuple[str, ...] = (
    "discovered",           # 待审阅候选（免费预览或半自动待确认）
    "favorited",            # 已自动收藏待本人投递（网页已点击感兴趣）
    "screened_skip",        # 已深筛排雷跳过
    "opening_sent",         # 单向系统开场已送达
    "connected",            # 双方已建联有实质回复
    "resume_requested",     # 已发起附件简历请求待对方点同意（绝不等同于已送达！）
    "resume_delivered",     # 确认 PDF 附件简历已送达
    "handed_off",           # 触发约面/谈薪/超纲红线，已交回本人待处理
    "interview_scheduled",  # 已敲定约面（可由助手从 BOSS 约面消息同步或本人告知）
    "interviewing",         # 面试推进中（二面/终面等后续轮次，由本人告知记录）
    "offer",                # 已获 Offer / 谈薪决策中（由本人告知记录）
    "offer_accepted",       # 已接 Offer / 待入职归档
    "closed_rejected",      # 不合适或面试未通过已归档
)

STATUS_LABELS: dict[str, str] = {
    "discovered": "待确认候选",
    "favorited": "已收藏·待本人投递",
    "screened_skip": "不匹配跳过",
    "opening_sent": "开场已送达",
    "connected": "双方已建联",
    "resume_requested": "待同意简历请求",
    "resume_delivered": "PDF 简历已送达",
    "handed_off": "待本人约面/决策",
    "interview_scheduled": "已约面",
    "interviewing": "面试推进中",
    "offer": "已获 Offer",
    "offer_accepted": "已接 Offer",
    "closed_rejected": "不合适归档",
}

INTERVIEW_STAGE_STATUSES: frozenset[str] = frozenset({
    "interview_scheduled",
    "interviewing",
    "offer",
    "offer_accepted",
})

ACTIVE_CHAT_STATUSES: frozenset[str] = frozenset({
    "opening_sent",
    "connected",
    "resume_requested",
    "resume_delivered",
    "handed_off",
    "interview_scheduled",
    "interviewing",
    "offer",
    "offer_accepted",
})

VALID_INTERVIEW_OUTCOMES: tuple[str, ...] = (
    "pending",
    "passed",
    "failed",
    "cancelled",
)

INTERVIEW_OUTCOME_LABELS: dict[str, str] = {
    "pending": "待面试",
    "passed": "已通过",
    "failed": "未通过",
    "cancelled": "已取消",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT 'boss',
    company TEXT NOT NULL,
    company_clean TEXT NOT NULL,
    company_core TEXT NOT NULL,
    company_scale TEXT NOT NULL DEFAULT '',
    business_nature TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    salary TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    recruiter_name TEXT NOT NULL DEFAULT '',
    recruiter_title TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'outbound',
    risk_tags TEXT NOT NULL DEFAULT '[]',
    match_reasons TEXT NOT NULL DEFAULT '',
    gaps TEXT NOT NULL DEFAULT '',
    icebreaker TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN (
        'discovered',
        'favorited',
        'screened_skip',
        'opening_sent',
        'connected',
        'resume_requested',
        'resume_delivered',
        'handed_off',
        'interview_scheduled',
        'interviewing',
        'offer',
        'offer_accepted',
        'closed_rejected'
    )),
    resume_used TEXT NOT NULL DEFAULT '',
    job_url TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    favorited_at TEXT,
    opening_sent_at TEXT,
    connected_at TEXT,
    resume_requested_at TEXT,
    resume_delivered_at TEXT,
    handed_off_at TEXT,
    interview_scheduled_at TEXT,
    offer_at TEXT,
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_company_core ON jobs(company_core);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at DESC);

CREATE TABLE IF NOT EXISTS ledger_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    resume_used TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_job_id ON ledger_events(job_id);

CREATE TABLE IF NOT EXISTS interview_rounds (
    round_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    round_label TEXT NOT NULL DEFAULT '一面',
    scheduled_at TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',
    interviewer TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'pending' CHECK(outcome IN ('pending', 'passed', 'failed', 'cancelled')),
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interview_rounds_job_id ON interview_rounds(job_id);
CREATE INDEX IF NOT EXISTS idx_interview_rounds_outcome ON interview_rounds(outcome);

CREATE TABLE IF NOT EXISTS favorite_batches (
    batch_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'applying', 'paused', 'completed', 'cancelled')),
    channel TEXT NOT NULL DEFAULT 'favorite_queue',
    apply_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    pause_reason TEXT NOT NULL DEFAULT '',
    last_dispatch_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    cancelled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_favorite_batches_status ON favorite_batches(status);

CREATE TABLE IF NOT EXISTS favorite_batch_items (
    item_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    salary TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    job_url TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'in_flight', 'applied', 'skipped')),
    claimed_at TEXT,
    applied_at TEXT,
    skipped_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_favorite_batch_items_batch_id ON favorite_batch_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_favorite_batch_items_job_id ON favorite_batch_items(job_id);
CREATE INDEX IF NOT EXISTS idx_favorite_batch_items_status ON favorite_batch_items(status);

CREATE TABLE IF NOT EXISTS company_intel (
    company_core TEXT PRIMARY KEY,
    legal_name TEXT NOT NULL DEFAULT '',
    company_clean TEXT NOT NULL DEFAULT '',
    scale TEXT,
    scale_max_num INTEGER,
    financing_stage TEXT,
    insured_count INTEGER,
    established TEXT,
    former_name TEXT,
    business_scope TEXT,
    reg_city TEXT,
    office_city TEXT,
    intro TEXT,
    jobs_json TEXT,
    profile_probed_at TEXT,
    jobs_probed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_company_intel_updated_at ON company_intel(updated_at DESC);
"""


def _validate_status(status: str) -> str:
    st = (status or "").strip()
    if st not in VALID_STATUSES:
        raise ValueError(f"非法账本状态 {status!r}，允许枚举：{VALID_STATUSES}")
    return st


def _validate_interview_outcome(outcome: str) -> str:
    oc = (outcome or "pending").strip() or "pending"
    if oc not in VALID_INTERVIEW_OUTCOMES:
        raise ValueError(f"非法面试结果状态 {outcome!r}，允许枚举：{VALID_INTERVIEW_OUTCOMES}")
    return oc


def _parse_risk_tags(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    return [str(x).strip() for x in arr if str(x).strip()]
            except json.JSONDecodeError:
                pass
        return [part.strip() for part in s.replace("，", ",").split(",") if part.strip()]
    return []


def _milestone_updates_for_status(
    status: str,
    ts: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, str | None]:
    """按严格四态规则维护里程碑时间戳，并在误标状态往回修正时清理超前时间戳（防止误扣当日配额或虚增简历送达数）。"""
    prev = existing or {}
    milestones = {
        "favorited_at": prev.get("favorited_at"),
        "opening_sent_at": prev.get("opening_sent_at"),
        "connected_at": prev.get("connected_at"),
        "resume_requested_at": prev.get("resume_requested_at"),
        "resume_delivered_at": prev.get("resume_delivered_at"),
        "handed_off_at": prev.get("handed_off_at"),
        "interview_scheduled_at": prev.get("interview_scheduled_at"),
        "offer_at": prev.get("offer_at"),
        "closed_at": prev.get("closed_at"),
    }
    if status == "discovered":
        return {
            "favorited_at": None,
            "opening_sent_at": None,
            "connected_at": None,
            "resume_requested_at": None,
            "resume_delivered_at": None,
            "handed_off_at": None,
            "interview_scheduled_at": None,
            "offer_at": None,
            "closed_at": None,
        }
    if status == "favorited":
        return {
            "favorited_at": milestones["favorited_at"] or ts,
            "opening_sent_at": None,
            "connected_at": None,
            "resume_requested_at": None,
            "resume_delivered_at": None,
            "handed_off_at": None,
            "interview_scheduled_at": None,
            "offer_at": None,
            "closed_at": None,
        }
    if status == "screened_skip":
        # 若此前已在招聘平台真实发出过开场打招呼或自动收藏，保留时间戳以防重置当日平台配额计数
        return {
            "favorited_at": milestones["favorited_at"],
            "opening_sent_at": milestones["opening_sent_at"],
            "connected_at": None,
            "resume_requested_at": None,
            "resume_delivered_at": None,
            "handed_off_at": None,
            "interview_scheduled_at": None,
            "offer_at": None,
            "closed_at": ts,
        }
    if status == "opening_sent":
        milestones["opening_sent_at"] = milestones["opening_sent_at"] or ts
        milestones["connected_at"] = None
        milestones["resume_requested_at"] = None
        milestones["resume_delivered_at"] = None
        milestones["handed_off_at"] = None
        milestones["interview_scheduled_at"] = None
        milestones["offer_at"] = None
        milestones["closed_at"] = None
    elif status == "connected":
        milestones["opening_sent_at"] = milestones["opening_sent_at"] or ts
        milestones["connected_at"] = milestones["connected_at"] or ts
        milestones["resume_requested_at"] = None
        milestones["resume_delivered_at"] = None
        milestones["handed_off_at"] = None
        milestones["interview_scheduled_at"] = None
        milestones["offer_at"] = None
        milestones["closed_at"] = None
    elif status == "resume_requested":
        milestones["opening_sent_at"] = milestones["opening_sent_at"] or ts
        milestones["connected_at"] = milestones["connected_at"] or ts
        milestones["resume_requested_at"] = milestones["resume_requested_at"] or ts
        # 严格规则：仅发起请求或从误标状态回退时，绝不保留 resume_delivered_at！
        milestones["resume_delivered_at"] = None
        milestones["handed_off_at"] = None
        milestones["interview_scheduled_at"] = None
        milestones["offer_at"] = None
        milestones["closed_at"] = None
    elif status == "resume_delivered":
        milestones["opening_sent_at"] = milestones["opening_sent_at"] or ts
        milestones["connected_at"] = milestones["connected_at"] or ts
        milestones["resume_delivered_at"] = milestones["resume_delivered_at"] or ts
        milestones["handed_off_at"] = None
        milestones["interview_scheduled_at"] = None
        milestones["offer_at"] = None
        milestones["closed_at"] = None
    elif status == "handed_off":
        milestones["handed_off_at"] = milestones["handed_off_at"] or ts
        milestones["interview_scheduled_at"] = None
        milestones["offer_at"] = None
        milestones["closed_at"] = None
    elif status in ("interview_scheduled", "interviewing"):
        milestones["opening_sent_at"] = milestones["opening_sent_at"] or ts
        milestones["connected_at"] = milestones["connected_at"] or ts
        milestones["handed_off_at"] = milestones["handed_off_at"] or ts
        milestones["interview_scheduled_at"] = milestones["interview_scheduled_at"] or ts
        milestones["offer_at"] = None
        milestones["closed_at"] = None
    elif status in ("offer", "offer_accepted"):
        milestones["opening_sent_at"] = milestones["opening_sent_at"] or ts
        milestones["connected_at"] = milestones["connected_at"] or ts
        milestones["handed_off_at"] = milestones["handed_off_at"] or ts
        milestones["interview_scheduled_at"] = milestones["interview_scheduled_at"] or ts
        milestones["offer_at"] = milestones["offer_at"] or ts
        milestones["closed_at"] = None
    elif status == "closed_rejected":
        milestones["closed_at"] = milestones["closed_at"] or ts
    return milestones


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["risk_tags"] = _parse_risk_tags(data.get("risk_tags"))
    data["status_label"] = STATUS_LABELS.get(data["status"], data["status"])
    return data


def _round_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["outcome_label"] = INTERVIEW_OUTCOME_LABELS.get(data.get("outcome", ""), data.get("outcome", ""))
    return data


class JobLedger:
    """求职助手本地 SQLite 账本封装。"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.db_path = ledger_db_path(workspace)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.workspace.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _ensure_schema(self) -> None:
        import shutil as _shutil

        self.workspace.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=15000;")
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
            except sqlite3.OperationalError:
                pass

            conn.execute("BEGIN IMMEDIATE;")
            try:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
                ).fetchone()
                table_sql = str(row["sql"]) if (row and row["sql"]) else ""
                if table_sql and (
                    any(f"'{st}'" not in table_sql for st in VALID_STATUSES)
                    or "favorited_at" not in table_sql
                    or "interview_scheduled_at" not in table_sql
                    or "offer_at" not in table_sql
                ):
                    try:
                        _shutil.copy2(self.db_path, self.db_path.with_suffix(".sqlite3.pre_mig_bak"))
                    except OSError:
                        pass
                    conn.execute("DROP TABLE IF EXISTS jobs_v2")
                    create_v2_sql = (
                        SCHEMA_SQL.replace("CREATE TABLE IF NOT EXISTS jobs", "CREATE TABLE jobs_v2")
                        .split("CREATE INDEX")[0]
                        .strip()
                    )
                    conn.execute(create_v2_sql)
                    old_cols = {
                        str(r["name"]) for r in conn.execute("PRAGMA table_info(jobs)").fetchall()
                    }
                    v2_cols = [
                        str(r["name"]) for r in conn.execute("PRAGMA table_info(jobs_v2)").fetchall()
                    ]
                    common_cols = [c for c in v2_cols if c in old_cols]
                    select_exprs: list[str] = list(common_cols)
                    insert_cols: list[str] = list(common_cols)
                    if "company_clean" not in old_cols and "company" in old_cols:
                        insert_cols.append("company_clean")
                        select_exprs.append("company")
                    if "company_core" not in old_cols:
                        insert_cols.append("company_core")
                        select_exprs.append("company_norm" if "company_norm" in old_cols else "company")
                    col_csv = ", ".join(insert_cols)
                    sel_csv = ", ".join(select_exprs)
                    conn.execute(f"INSERT INTO jobs_v2 ({col_csv}) SELECT {sel_csv} FROM jobs")
                    conn.execute("DROP TABLE jobs")
                    conn.execute("ALTER TABLE jobs_v2 RENAME TO jobs")

                for stmt in SCHEMA_SQL.split(";"):
                    s = stmt.strip()
                    if s:
                        conn.execute(s)

                ci_cols = {
                    str(r["name"])
                    for r in conn.execute("PRAGMA table_info(company_intel)").fetchall()
                }
                if "financing_stage" not in ci_cols:
                    conn.execute("ALTER TABLE company_intel ADD COLUMN financing_stage TEXT")
                if "insured_count" not in ci_cols:
                    conn.execute("ALTER TABLE company_intel ADD COLUMN insured_count INTEGER")

                conn.execute("COMMIT;")
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                raise
        finally:
            conn.close()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return _row_to_dict(row) if row else None

    def upsert_job(self, payload: dict[str, Any], *, _allow_favorite_queue: bool = False) -> dict[str, Any]:
        company = str(payload.get("company") or "").strip()
        title = str(payload.get("title") or "").strip()
        if not company or not title:
            raise ValueError("upsert-job 必须提供非空的 company 与 title")

        job_id = str(payload.get("job_id") or "").strip() or f"job_{uuid.uuid4().hex[:10]}"
        existing = self.get_job(job_id)
        status = _validate_status(str(payload.get("status") or (existing["status"] if existing else "discovered")))
        ts = str(payload.get("timestamp") or now_iso())

        clean_name, core_name = normalize_company_parts(company)
        title_norm = normalize_title(title)
        milestones = _milestone_updates_for_status(status, ts, existing)

        def pick_str(key: str, default: str = "") -> str:
            if key in payload and payload[key] is not None:
                return str(payload[key]).strip()
            if existing and existing.get(key) is not None:
                return str(existing[key])
            return default

        raw_channel = normalize_channel(pick_str("channel", CHANNEL_OUTBOUND))
        if raw_channel == CHANNEL_FAVORITE_QUEUE and not _allow_favorite_queue:
            # 防止绕过 favorite-batch 批次状态机直接伪造 favorite_queue 逃避常规投递额度；
            # 若已有记录本来就是 favorite_queue 则保留原值。
            if existing and existing.get("channel") == CHANNEL_FAVORITE_QUEUE:
                resolved_channel = CHANNEL_FAVORITE_QUEUE
            else:
                resolved_channel = normalize_channel(
                    existing.get("channel") if existing else CHANNEL_OUTBOUND,
                    default=CHANNEL_OUTBOUND,
                )
        else:
            resolved_channel = raw_channel

        risk_tags_list = (
            _parse_risk_tags(payload["risk_tags"])
            if "risk_tags" in payload and payload["risk_tags"] is not None
            else (existing["risk_tags"] if existing else [])
        )
        risk_tags_json = json.dumps(risk_tags_list, ensure_ascii=False)

        record = {
            "job_id": job_id,
            "platform": pick_str("platform", "boss"),
            "company": company,
            "company_clean": clean_name,
            "company_core": core_name,
            "company_scale": pick_str("company_scale"),
            "business_nature": pick_str("business_nature"),
            "title": title,
            "title_norm": title_norm,
            "salary": pick_str("salary"),
            "city": pick_str("city"),
            "recruiter_name": pick_str("recruiter_name"),
            "recruiter_title": pick_str("recruiter_title"),
            "channel": resolved_channel,
            "risk_tags": risk_tags_json,
            "match_reasons": pick_str("match_reasons"),
            "gaps": pick_str("gaps"),
            "icebreaker": pick_str("icebreaker"),
            "status": status,
            "resume_used": pick_str("resume_used"),
            "job_url": pick_str("job_url"),
            "note": pick_str("note"),
            "favorited_at": milestones["favorited_at"],
            "opening_sent_at": milestones["opening_sent_at"],
            "connected_at": milestones["connected_at"],
            "resume_requested_at": milestones["resume_requested_at"],
            "resume_delivered_at": milestones["resume_delivered_at"],
            "handed_off_at": milestones["handed_off_at"],
            "interview_scheduled_at": milestones["interview_scheduled_at"],
            "offer_at": milestones["offer_at"],
            "closed_at": milestones["closed_at"],
            "created_at": existing["created_at"] if existing else ts,
            "updated_at": ts,
        }

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, platform, company, company_clean, company_core,
                    company_scale, business_nature, title, title_norm, salary, city,
                    recruiter_name, recruiter_title, channel, risk_tags,
                    match_reasons, gaps, icebreaker, status, resume_used,
                    job_url, note, favorited_at, opening_sent_at, connected_at,
                    resume_requested_at, resume_delivered_at, handed_off_at,
                    interview_scheduled_at, offer_at, closed_at,
                    created_at, updated_at
                ) VALUES (
                    :job_id, :platform, :company, :company_clean, :company_core,
                    :company_scale, :business_nature, :title, :title_norm, :salary, :city,
                    :recruiter_name, :recruiter_title, :channel, :risk_tags,
                    :match_reasons, :gaps, :icebreaker, :status, :resume_used,
                    :job_url, :note, :favorited_at, :opening_sent_at, :connected_at,
                    :resume_requested_at, :resume_delivered_at, :handed_off_at,
                    :interview_scheduled_at, :offer_at, :closed_at,
                    :created_at, :updated_at
                )
                ON CONFLICT(job_id) DO UPDATE SET
                    platform = excluded.platform,
                    company = excluded.company,
                    company_clean = excluded.company_clean,
                    company_core = excluded.company_core,
                    company_scale = excluded.company_scale,
                    business_nature = excluded.business_nature,
                    title = excluded.title,
                    title_norm = excluded.title_norm,
                    salary = excluded.salary,
                    city = excluded.city,
                    recruiter_name = excluded.recruiter_name,
                    recruiter_title = excluded.recruiter_title,
                    channel = excluded.channel,
                    risk_tags = excluded.risk_tags,
                    match_reasons = excluded.match_reasons,
                    gaps = excluded.gaps,
                    icebreaker = excluded.icebreaker,
                    status = excluded.status,
                    resume_used = excluded.resume_used,
                    job_url = excluded.job_url,
                    note = excluded.note,
                    favorited_at = excluded.favorited_at,
                    opening_sent_at = excluded.opening_sent_at,
                    connected_at = excluded.connected_at,
                    resume_requested_at = excluded.resume_requested_at,
                    resume_delivered_at = excluded.resume_delivered_at,
                    handed_off_at = excluded.handed_off_at,
                    interview_scheduled_at = excluded.interview_scheduled_at,
                    offer_at = excluded.offer_at,
                    closed_at = excluded.closed_at,
                    updated_at = excluded.updated_at
                """,
                record,
            )
            if not existing or existing["status"] != status:
                conn.execute(
                    """
                    INSERT INTO ledger_events (job_id, from_status, to_status, resume_used, note, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        existing["status"] if existing else None,
                        status,
                        record["resume_used"],
                        record["note"],
                        ts,
                    ),
                )
        updated = self.get_job(job_id)
        assert updated is not None
        out_res: dict[str, Any] = {"ok": True, "created": existing is None, "job": updated}
        if (
            status == "handed_off"
            and (not existing or existing["status"] != "handed_off")
            and bool(payload.get("notify_desktop", True))
        ):
            out_res["desktop_notification"] = send_handoff_notification(
                company=str(updated.get("company") or ""),
                title=str(updated.get("title") or ""),
                reason=str(updated.get("note") or ""),
            )
        return out_res

    def transition(
        self,
        *,
        job_id: str = "",
        to_status: str,
        company: str | None = None,
        title: str | None = None,
        channel: str = "inbound",
        note: str | None = None,
        resume_used: str | None = None,
        timestamp: str | None = None,
        notify_desktop: bool = True,
    ) -> dict[str, Any]:
        target_status = _validate_status(to_status)
        clean_id = (job_id or "").strip()
        existing = self.get_job(clean_id) if clean_id else None

        if not existing and company and company.strip():
            # 被动来信或跨轮次跟进兜底：按公司（及可选岗位名）匹配最近活跃记录；若完全未建档则自动创建并触发通知
            comp_q = company.strip()
            title_q = (title or "").strip()
            candidates = [
                r for r in self.list_jobs(limit=500) if companies_match(str(r.get("company") or ""), comp_q)
            ]
            if candidates:
                if title_q:
                    best = max(candidates, key=lambda r: title_similarity(str(r.get("title") or ""), title_q))
                    if title_similarity(str(best.get("title") or ""), title_q) >= 0.50:
                        existing = best
                elif len(candidates) == 1:
                    existing = candidates[0]
                else:
                    active_cands = [r for r in candidates if r.get("status") not in ("screened_skip", "closed_rejected")]
                    if len(active_cands) == 1:
                        existing = active_cands[0]
            if existing:
                clean_id = str(existing["job_id"])
            else:
                up_payload: dict[str, Any] = {
                    "company": comp_q,
                    "title": title_q or "招聘方沟通岗位",
                    "channel": channel or "inbound",
                    "status": target_status,
                    "note": note or "",
                    "resume_used": resume_used or "",
                    "notify_desktop": notify_desktop,
                }
                if clean_id:
                    up_payload["job_id"] = clean_id
                if timestamp:
                    up_payload["timestamp"] = timestamp
                up_res = self.upsert_job(up_payload)
                return {
                    "ok": True,
                    "auto_created": True,
                    "from_status": None,
                    "to_status": target_status,
                    "job": up_res["job"],
                    **(
                        {"desktop_notification": up_res["desktop_notification"]}
                        if "desktop_notification" in up_res
                        else {}
                    ),
                }

        if not existing:
            raise KeyError(
                f"未找到 job_id={job_id!r} 的岗位记录（处理新会话可传入 --company 与 --title 自动匹配或建档）"
            )
        job_id = clean_id

        ts = timestamp or now_iso()
        milestones = _milestone_updates_for_status(target_status, ts, existing)
        final_resume = (
            resume_used.strip()
            if resume_used is not None and resume_used.strip()
            else existing["resume_used"]
        )
        if note is not None and note.strip():
            prev_note = existing["note"].strip()
            final_note = f"{prev_note} | {note.strip()}" if prev_note else note.strip()
        else:
            final_note = existing["note"]

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    status = ?,
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
                    target_status,
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
                    job_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO ledger_events (job_id, from_status, to_status, resume_used, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, existing["status"], target_status, final_resume, note or "", ts),
            )

        updated = self.get_job(job_id)
        assert updated is not None
        res: dict[str, Any] = {
            "ok": True,
            "from_status": existing["status"],
            "to_status": target_status,
            "job": updated,
        }
        if target_status == "handed_off" and notify_desktop and existing["status"] != "handed_off":
            res["desktop_notification"] = send_handoff_notification(
                company=str(updated.get("company") or ""),
                title=str(updated.get("title") or ""),
                reason=str(note or updated.get("note") or ""),
            )
        return res

    def _resolve_existing_job(
        self,
        *,
        job_id: str = "",
        company: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any] | None:
        clean_id = (job_id or "").strip()
        if clean_id:
            found = self.get_job(clean_id)
            if found:
                return found
        if company and company.strip():
            comp_q = company.strip()
            title_q = (title or "").strip()
            candidates = [
                r for r in self.list_jobs(limit=500) if companies_match(str(r.get("company") or ""), comp_q)
            ]
            if candidates:
                if title_q:
                    best = max(candidates, key=lambda r: title_similarity(str(r.get("title") or ""), title_q))
                    if title_similarity(str(best.get("title") or ""), title_q) >= 0.50:
                        return best
                elif len(candidates) == 1:
                    return candidates[0]
                else:
                    active_cands = [r for r in candidates if r.get("status") not in ("screened_skip", "closed_rejected")]
                    if len(active_cands) == 1:
                        return active_cands[0]
                    return candidates[0]
        return None

    def get_interview_round(self, round_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM interview_rounds WHERE round_id = ?", (round_id,)).fetchone()
            return _round_row_to_dict(row) if row else None

    def add_interview_round(
        self,
        *,
        job_id: str = "",
        company: str | None = None,
        title: str | None = None,
        round_label: str = "一面",
        scheduled_at: str = "",
        mode: str = "",
        interviewer: str = "",
        outcome: str = "pending",
        summary: str = "",
        auto_advance_status: bool = True,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """新增一条面试轮次记录；支持按 job_id 或公司名定位（未建档时自动建档），并按需自动推进岗位面试状态。"""
        oc = _validate_interview_outcome(outcome)
        ts = timestamp or now_iso()
        label = (round_label or "").strip() or "一面"

        existing = self._resolve_existing_job(job_id=job_id, company=company, title=title)
        auto_created_job = False
        if not existing:
            if company and company.strip():
                init_status = "interviewing" if any(k in label for k in ("二面", "三面", "四面", "终面", "HR", "hr")) else "interview_scheduled"
                up_res = self.upsert_job({
                    "job_id": (job_id or "").strip() or None,
                    "company": company.strip(),
                    "title": (title or "").strip() or "面试跟进岗位",
                    "channel": "inbound",
                    "status": init_status,
                    "note": f"已记录{label}" + (f"（{scheduled_at.strip()}）" if scheduled_at.strip() else ""),
                    "timestamp": ts,
                    "notify_desktop": False,
                })
                existing = up_res["job"]
                auto_created_job = True
            else:
                raise KeyError(f"未找到 job_id={job_id!r} 的岗位记录（也可传入 --company 自动定位或建档）")

        resolved_job_id = str(existing["job_id"])
        existing_rounds = self.list_interview_rounds(job_id=resolved_job_id)
        round_id = f"iv_{uuid.uuid4().hex[:10]}"

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO interview_rounds (
                    round_id, job_id, round_label, scheduled_at, mode,
                    interviewer, outcome, summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    round_id,
                    resolved_job_id,
                    label,
                    (scheduled_at or "").strip(),
                    (mode or "").strip(),
                    (interviewer or "").strip(),
                    oc,
                    (summary or "").strip(),
                    ts,
                    ts,
                ),
            )
            conn.execute("UPDATE jobs SET updated_at = ? WHERE job_id = ?", (ts, resolved_job_id))

        status_transition: dict[str, Any] | None = None
        if auto_advance_status and not auto_created_job:
            cur_st = str(existing.get("status") or "")
            is_later_round = len(existing_rounds) >= 1 or any(
                k in label for k in ("二面", "三面", "四面", "终面", "HR", "hr", "总监", "VP")
            )
            target_st: str | None = None
            if cur_st not in INTERVIEW_STAGE_STATUSES and cur_st != "closed_rejected":
                target_st = "interviewing" if is_later_round else "interview_scheduled"
            elif cur_st == "interview_scheduled" and (is_later_round or oc == "passed"):
                target_st = "interviewing"
            if target_st and target_st != cur_st:
                note_parts = [f"新增面试轮次：{label}"]
                if scheduled_at.strip():
                    note_parts.append(scheduled_at.strip())
                if mode.strip():
                    note_parts.append(mode.strip())
                status_transition = self.transition(
                    job_id=resolved_job_id,
                    to_status=target_st,
                    note=" · ".join(note_parts),
                    timestamp=ts,
                    notify_desktop=False,
                )

        round_rec = self.get_interview_round(round_id)
        updated_job = self.get_job(resolved_job_id)
        return {
            "ok": True,
            "auto_created_job": auto_created_job,
            "interview_round": round_rec,
            "job": updated_job,
            **(
                {"status_transition": {"from_status": status_transition["from_status"], "to_status": status_transition["to_status"]}}
                if status_transition
                else {}
            ),
        }

    def update_interview_round(
        self,
        round_id: str,
        *,
        round_label: str | None = None,
        scheduled_at: str | None = None,
        mode: str | None = None,
        interviewer: str | None = None,
        outcome: str | None = None,
        summary: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """更新指定面试轮次的时间、方式、面试官、结果或面经复盘。"""
        existing = self.get_interview_round((round_id or "").strip())
        if not existing:
            raise KeyError(f"未找到 round_id={round_id!r} 的面试轮次记录")
        ts = timestamp or now_iso()
        new_label = round_label.strip() if (round_label is not None and round_label.strip()) else existing["round_label"]
        new_sched = scheduled_at.strip() if scheduled_at is not None else existing["scheduled_at"]
        new_mode = mode.strip() if mode is not None else existing["mode"]
        new_iv = interviewer.strip() if interviewer is not None else existing["interviewer"]
        new_outcome = _validate_interview_outcome(outcome) if outcome is not None else existing["outcome"]
        if summary is not None:
            new_summary = summary.strip()
        else:
            new_summary = existing["summary"]

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE interview_rounds SET
                    round_label = ?,
                    scheduled_at = ?,
                    mode = ?,
                    interviewer = ?,
                    outcome = ?,
                    summary = ?,
                    updated_at = ?
                WHERE round_id = ?
                """,
                (new_label, new_sched, new_mode, new_iv, new_outcome, new_summary, ts, existing["round_id"]),
            )
            conn.execute("UPDATE jobs SET updated_at = ? WHERE job_id = ?", (ts, existing["job_id"]))

        updated_round = self.get_interview_round(existing["round_id"])
        updated_job = self.get_job(existing["job_id"])
        return {
            "ok": True,
            "interview_round": updated_round,
            "job": updated_job,
        }

    def list_interview_rounds(
        self,
        *,
        job_id: str | None = None,
        outcome: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if job_id and job_id.strip():
            clauses.append("job_id = ?")
            params.append(job_id.strip())
        if outcome and outcome.strip():
            oc = _validate_interview_outcome(outcome)
            clauses.append("outcome = ?")
            params.append(oc)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM interview_rounds {where_sql} ORDER BY created_at ASC, rowid ASC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_round_row_to_dict(r) for r in rows]

    def get_rounds_by_job_ids(self, job_ids: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as conn:
            if job_ids is None:
                rows = conn.execute(
                    "SELECT * FROM interview_rounds ORDER BY created_at ASC, rowid ASC"
                ).fetchall()
            elif not job_ids:
                return {}
            else:
                placeholders = ",".join("?" for _ in job_ids)
                rows = conn.execute(
                    f"SELECT * FROM interview_rounds WHERE job_id IN ({placeholders}) ORDER BY created_at ASC, rowid ASC",
                    job_ids,
                ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            d = _round_row_to_dict(row)
            grouped.setdefault(str(d["job_id"]), []).append(d)
        return grouped

    def check_duplicate(
        self,
        *,
        company: str,
        title: str,
        recruiter: str = "",
        blocked_companies: list[str] | None = None,
        past_employers: list[str] | None = None,
        chat_already_open: bool = False,
        already_favorited: bool = False,
        filter_outsourcing: bool = True,
        filter_onsite: bool = True,
        no_reply_followup_days: int = 7,
        now_iso_override: str | None = None,
    ) -> dict[str, Any]:
        """三维查重 + 重复投递分层判定：
        1. 归一化公司比对（含前东家黑名单）
        2. 岗位名相似度（明确拒绝/排雷永久跳过；投过超 N 天未回默认允许礼貌二次跟进）
        3. 网页已有会话/已收藏与同公司在聊/公司级排雷记录。
        """
        clean_c, core_c = normalize_company_parts(company)
        norm_t = normalize_title(title)

        def _parse_dt_safe(raw: str | None) -> datetime | None:
            text = str(raw or "").strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                return None
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        now_dt = _parse_dt_safe(now_iso_override) or datetime.now(timezone.utc)
        followup_threshold = max(1, int(no_reply_followup_days))

        all_blocked = list(blocked_companies or []) + list(past_employers or [])
        for blk in all_blocked:
            if employer_shield_match(company, blk):
                is_former = any(employer_shield_match(company, emp) for emp in (past_employers or []))
                return {
                    "ok": True,
                    "duplicate": True,
                    "allow_followup": False,
                    "reapply_policy": "blocked_employer",
                    "blocked_employer": is_former,
                    "blocked_company_hit": blk,
                    "normalized_company": core_c,
                    "company_clean": clean_name_or_core(clean_c, core_c),
                    "normalized_title": norm_t,
                    "same_company_active": False,
                    "same_company_favorited": False,
                    "same_company_skipped": False,
                    "chat_already_open": chat_already_open,
                    "already_favorited": already_favorited,
                    "max_title_similarity": 0.0,
                    "reason": f"命中{'前东家/现东家' if is_former else '屏蔽企业'}黑名单：{blk}",
                    "warnings": [],
                    "matches": [],
                }

        if chat_already_open:
            return {
                "ok": True,
                "duplicate": True,
                "allow_followup": False,
                "reapply_policy": "chat_already_open",
                "blocked_employer": False,
                "blocked_company_hit": None,
                "normalized_company": core_c,
                "company_clean": clean_name_or_core(clean_c, core_c),
                "normalized_title": norm_t,
                "same_company_active": True,
                "same_company_favorited": False,
                "same_company_skipped": False,
                "chat_already_open": True,
                "already_favorited": already_favorited,
                "max_title_similarity": 1.0,
                "reason": "BOSS 网页已存在与该招聘者/岗位的历史会话（按钮显示“继续沟通”或聊天列表已存在）",
                "warnings": [],
                "matches": [],
            }

        if already_favorited:
            return {
                "ok": True,
                "duplicate": True,
                "allow_followup": False,
                "reapply_policy": "already_favorited",
                "blocked_employer": False,
                "blocked_company_hit": None,
                "normalized_company": core_c,
                "company_clean": clean_name_or_core(clean_c, core_c),
                "normalized_title": norm_t,
                "same_company_active": False,
                "same_company_favorited": True,
                "same_company_skipped": False,
                "chat_already_open": False,
                "already_favorited": True,
                "max_title_similarity": 1.0,
                "reason": "BOSS 网页显示该职位已处于「已感兴趣/收藏」状态，跳过以防误点取消收藏",
                "warnings": [],
                "matches": [],
            }

        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC").fetchall()

        same_company_matches: list[dict[str, Any]] = []
        warnings: list[str] = []
        max_sim = 0.0
        same_company_active = False
        same_company_favorited = False
        favorited_similar_role = False
        same_company_skipped = False
        company_level_skipped = False
        exact_or_similar_role = False
        rejected_similar_role = False
        recruiter_changed_repost = False
        stale_unreplied_days: int | None = None

        # 仅将企业主体层面的黑名单/外包派遣属性作为公司级连坐跳过依据；驻场属单岗位属性（如自研公司或外包公司同时招驻场岗与本部岗），不连坐封杀整家公司其他岗位
        company_block_hints: list[str] = ["黑名单"]
        if filter_outsourcing:
            company_block_hints.extend(["外包", "派遣", "劳务"])

        for row in rows:
            if not companies_match(company, row["company"]):
                continue
            sim = title_similarity(title, row["title"])
            max_sim = max(max_sim, sim)
            st = row["status"]

            # 判定是否为「投过但多天未回」的单向 opening_sent：默认允许礼貌二次跟进，不再一刀切拦截
            is_stale_unreplied = False
            days_elapsed: int | None = None
            if st == "opening_sent":
                op_dt = _parse_dt_safe(row["opening_sent_at"]) or _parse_dt_safe(row["updated_at"])
                if op_dt is not None:
                    days_elapsed = int((now_dt - op_dt).total_seconds() // 86400)
                    if days_elapsed >= followup_threshold:
                        is_stale_unreplied = True

            if st in ACTIVE_CHAT_STATUSES and not is_stale_unreplied:
                same_company_active = True
            if st == "favorited":
                same_company_favorited = True
            if st in ("screened_skip", "closed_rejected"):
                same_company_skipped = True
                raw_meta = f"{row['business_nature'] or ''} {row['risk_tags'] or ''} {row['note'] or ''}".lower()
                sanitized_meta = _NEGATED_OUTSOURCING_RE.sub(" ", raw_meta)
                if any(hint in sanitized_meta for hint in company_block_hints):
                    company_level_skipped = True

            sim_threshold = 0.68 if st == "screened_skip" else 0.55
            if sim >= sim_threshold:
                if is_stale_unreplied:
                    if days_elapsed is not None:
                        stale_unreplied_days = (
                            days_elapsed
                            if stale_unreplied_days is None
                            else max(stale_unreplied_days, days_elapsed)
                        )
                else:
                    exact_or_similar_role = True
                    if st == "closed_rejected":
                        rejected_similar_role = True
                    if st == "favorited":
                        favorited_similar_role = True
                    if recruiter and row["recruiter_name"] and recruiter.strip() != row["recruiter_name"].strip():
                        recruiter_changed_repost = True

            same_company_matches.append({
                "job_id": row["job_id"],
                "company": row["company"],
                "title": row["title"],
                "status": st,
                "status_label": STATUS_LABELS.get(st, st),
                "recruiter_name": row["recruiter_name"],
                "recruiter_title": row["recruiter_title"],
                "title_similarity": sim,
                "days_since_opening": days_elapsed,
                "stale_unreplied_followup_eligible": is_stale_unreplied,
                "updated_at": row["updated_at"],
                "note": row["note"],
            })

        is_dup = bool(exact_or_similar_role or same_company_active or company_level_skipped)
        allow_followup = bool((not is_dup) and (stale_unreplied_days is not None))

        reasons: list[str] = []
        if recruiter_changed_repost:
            reasons.append("检测到同公司换招聘者重发相似岗位（防连投社死拦截）")
        elif rejected_similar_role:
            reasons.append(f"同公司相似岗位此前已被明确拒绝/未通过（相似度 {max_sim:.2f}），永久跳过不再复投")
        elif favorited_similar_role:
            reasons.append(f"同公司相同或高度相似岗位已在收藏待投递列表中（相似度 {max_sim:.2f}）")
        elif exact_or_similar_role:
            reasons.append(f"同公司存在相同或高度相似岗位记录（相似度 {max_sim:.2f}）")
        if same_company_active:
            reasons.append("同公司已有在聊/推进中的岗位会话")
        if company_level_skipped:
            reasons.append("同公司此前已确认为外包/派遣或公司级排雷跳过")
        elif same_company_skipped and not is_dup:
            warnings.append("同公司曾跳过其他不同方向的岗位，当前新岗位可独立评估")

        if allow_followup:
            reapply_policy = "allow_followup_after_no_reply"
            followup_msg = (
                f"该岗位已投递 {stale_unreplied_days} 天且对方未回复（≥{followup_threshold}天），"
                "默认允许礼貌二次跟进（同岗位每天最多跟进一次）"
            )
            reasons.append(followup_msg)
            warnings.append(followup_msg)
        elif not is_dup:
            reapply_policy = "new_candidate"
        elif rejected_similar_role or company_level_skipped:
            reapply_policy = "permanent_skip_rejected"
        elif same_company_active:
            reapply_policy = "active_or_recent_opening_skip"
        else:
            reapply_policy = "duplicate_skip"

        return {
            "ok": True,
            "duplicate": is_dup,
            "allow_followup": allow_followup,
            "reapply_policy": reapply_policy,
            "days_since_opening": stale_unreplied_days,
            "blocked_employer": False,
            "blocked_company_hit": None,
            "normalized_company": core_c,
            "company_clean": clean_name_or_core(clean_c, core_c),
            "normalized_title": norm_t,
            "same_company_active": same_company_active,
            "same_company_favorited": same_company_favorited,
            "same_company_skipped": same_company_skipped,
            "company_level_skipped": company_level_skipped,
            "chat_already_open": False,
            "already_favorited": False,
            "max_title_similarity": max_sim,
            "reason": "；".join(reasons) if reasons else "未命中重复记录",
            "warnings": warnings,
            "matches": same_company_matches,
        }

    def list_jobs(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        limit: int = 200,
        include_interviews: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            statuses = [_validate_status(s) for s in status.split(",") if s.strip()]
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                clauses.append(f"status IN ({placeholders})")
                params.extend(statuses)
        if search and search.strip():
            kw = f"%{search.strip()}%"
            clauses.append(
                "(company LIKE ? OR title LIKE ? OR recruiter_name LIKE ? OR match_reasons LIKE ? OR note LIKE ?)"
            )
            params.extend([kw, kw, kw, kw, kw])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM jobs {where_sql} ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, int(limit)))

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = [_row_to_dict(r) for r in rows]
        if include_interviews and items:
            rounds_map = self.get_rounds_by_job_ids([str(it["job_id"]) for it in items])
            for it in items:
                it["interviews"] = rounds_map.get(str(it["job_id"]), [])
        return items

    def summary(self, local_date_prefix: str | None = None) -> dict[str, Any]:
        today_prefix = local_date_prefix or datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs").fetchall()
            round_rows = conn.execute("SELECT job_id, outcome FROM interview_rounds").fetchall()

        by_status = {st: 0 for st in VALID_STATUSES}
        favorited_total = 0
        opening_delivered_total = 0
        connected_total = 0
        pdf_delivered_total = 0
        interview_total = 0
        today_favorited = 0
        today_opening_sent = 0
        today_outbound_actions = 0

        # 按来源渠道分桶统计当日动作，供额度分桶与云端/本地安全护栏使用（见 hiremate_lib/channel.py）。
        today_by_channel: dict[str, dict[str, int]] = {}

        def _bucket(ch: str) -> dict[str, int]:
            key = normalize_channel(ch)
            if key not in today_by_channel:
                today_by_channel[key] = {
                    "favorited": 0,
                    "opening_sent": 0,
                    "counted_opening_sent": 0,
                }
            return today_by_channel[key]

        jobs_with_rounds = {str(r["job_id"]) for r in round_rows}
        upcoming_interviews_count = sum(1 for r in round_rows if r["outcome"] == "pending")

        for row in rows:
            st = row["status"]
            by_status[st] = by_status.get(st, 0) + 1

            row_channel = normalize_channel(row["channel"])
            fav_ts = row["favorited_at"] or ""
            op_ts = row["opening_sent_at"] or ""
            if st == "favorited" or bool(fav_ts):
                favorited_total += 1
            if fav_ts.startswith(today_prefix):
                today_favorited += 1
                _bucket(row_channel)["favorited"] += 1
            if op_ts.startswith(today_prefix):
                today_opening_sent += 1
                bucket = _bucket(row_channel)
                bucket["opening_sent"] += 1
                if counts_toward_quota(row_channel):
                    bucket["counted_opening_sent"] += 1
            if fav_ts.startswith(today_prefix) or op_ts.startswith(today_prefix):
                today_outbound_actions += 1

            has_opening = bool(op_ts) or st in ACTIVE_CHAT_STATUSES
            if has_opening:
                opening_delivered_total += 1

            has_connected = bool(row["connected_at"]) or st in (
                "connected",
                "resume_requested",
                "resume_delivered",
                "handed_off",
                "interview_scheduled",
                "interviewing",
                "offer",
                "offer_accepted",
            )
            if has_connected:
                connected_total += 1

            # 严格拆账：resume_requested 绝不计入 pdf_delivered_total！
            has_pdf_delivered = (st == "resume_delivered") or bool(row["resume_delivered_at"])
            if has_pdf_delivered:
                pdf_delivered_total += 1

            has_interview = (
                st in INTERVIEW_STAGE_STATUSES
                or bool(row["interview_scheduled_at"])
                or str(row["job_id"]) in jobs_with_rounds
            )
            if has_interview:
                interview_total += 1

        interview_active_total = by_status["interview_scheduled"] + by_status["interviewing"]
        offer_count = by_status["offer"] + by_status["offer_accepted"]

        return {
            "ok": True,
            "total_evaluated": len(rows),
            "by_status": by_status,
            "funnel": {
                "screened_total": len(rows),
                "screened_skip": by_status["screened_skip"],
                "discovered_pending": by_status["discovered"],
                "favorited_pending": by_status["favorited"],
                "favorited_total": favorited_total,
                "opening_delivered_total": opening_delivered_total,
                "opening_waiting_reply": by_status["opening_sent"],
                "connected_total": connected_total,
                "connected_active": by_status["connected"],
                "resume_requested_pending": by_status["resume_requested"],
                "pdf_delivered_total": pdf_delivered_total,
                "handed_off_pending": by_status["handed_off"],
                "interview_scheduled": by_status["interview_scheduled"],
                "interviewing": by_status["interviewing"],
                "interview_active_total": interview_active_total,
                "interview_total": interview_total,
                "upcoming_interviews_count": upcoming_interviews_count,
                "offer_pending": by_status["offer"],
                "offer_accepted": by_status["offer_accepted"],
                "offer_count": offer_count,
                "closed_rejected": by_status["closed_rejected"],
            },
            "today_date": today_prefix,
            "today_favorited": today_favorited,
            "today_opening_sent": today_opening_sent,
            "today_outbound_actions": today_outbound_actions,
            # 额度分桶：只有 counts_toward_quota() 为真（outbound）的渠道才计入每日主动投递额度。
            # 保留 today_outbound_actions 原值以兼容既有看板与外部脚本。
            "today_by_channel": today_by_channel,
            "today_counted_opening_sent": sum(
                b["counted_opening_sent"] for b in today_by_channel.values()
            ),
            "quota_exempt_channels": sorted(
                ch for ch in today_by_channel if not counts_toward_quota(ch)
            ),
        }

    def _export_rows_and_headers(self) -> tuple[list[str], list[list[object]]]:
        jobs = self.list_jobs(limit=10000, include_interviews=True)
        headers = [
            "岗位ID",
            "当前状态",
            "公司名称",
            "规模与融资",
            "业务性质",
            "岗位名称",
            "面板薪资",
            "城市",
            "招聘者姓名",
            "招聘者头衔",
            "来源渠道",
            "风险/渠道标签",
            "匹配依据",
            "待确认缺口",
            "已投简历版本",
            "面试轮次记录",
            "最新备注",
            "岗位链接",
            "更新时间",
        ]
        rows: list[list[object]] = []
        for item in jobs:
            rounds = item.get("interviews") or []
            iv_summary_parts: list[str] = []
            for rd in rounds:
                meta_bits = [str(rd.get("outcome_label") or rd.get("outcome") or "")]
                if rd.get("scheduled_at"):
                    meta_bits.append(str(rd["scheduled_at"]))
                if rd.get("mode"):
                    meta_bits.append(str(rd["mode"]))
                piece = f"{rd.get('round_label', '面试')}({' '.join(x for x in meta_bits if x)})"
                if rd.get("summary"):
                    piece += f"：{rd['summary']}"
                iv_summary_parts.append(piece)
            rows.append([
                item["job_id"],
                item["status_label"],
                item["company"],
                item["company_scale"],
                item["business_nature"],
                item["title"],
                item["salary"],
                item["city"],
                item["recruiter_name"],
                item["recruiter_title"],
                channel_label(item["channel"]),
                " / ".join(item["risk_tags"]),
                item["match_reasons"],
                item["gaps"],
                item["resume_used"],
                " → ".join(iv_summary_parts),
                item["note"],
                item["job_url"],
                item["updated_at"],
            ])
        return headers, rows

    def export_xlsx(self, out_path: Path) -> dict[str, Any]:
        """导出原生 Office Open XML (.xlsx) Excel 工作簿（带冻结表头与防公式注入保护）。"""
        headers, rows = self._export_rows_and_headers()
        return write_xlsx_workbook(
            out_path,
            sheet_name="求职投递转化台账",
            headers=headers,
            rows=rows,
        )

    def export_csv(self, out_path: Path) -> dict[str, Any]:
        out_path = out_path.expanduser().resolve()
        if out_path.suffix.lower() == ".xlsx":
            return self.export_xlsx(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        headers, rows = self._export_rows_and_headers()

        # 使用 utf-8-sig 写入 UTF-8 BOM，确保 Excel 直接双击打开中文不乱码
        with out_path.open("w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(headers)
            for row_values in rows:
                writer.writerow([sanitize_csv_cell(v) for v in row_values])

        return {
            "ok": True,
            "format": "csv",
            "out_path": str(out_path),
            "exported_count": len(rows),
            "encoding": "utf-8-sig",
        }


def clean_name_or_core(clean_c: str, core_c: str) -> str:
    return clean_c or core_c
