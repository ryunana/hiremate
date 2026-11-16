"""本地 SQLite 四态精细求职账本、三维查重与防注入 CSV 导出。"""

from __future__ import annotations

import csv
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

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
    "discovered",        # 待审阅候选（免费预览或半自动待确认）
    "screened_skip",     # 已深筛排雷跳过
    "opening_sent",      # 单向系统开场已送达
    "connected",         # 双方已建联有实质回复
    "resume_requested",  # 已发起附件简历请求待对方点同意（绝不等同于已送达！）
    "resume_delivered",  # 确认 PDF 附件简历已送达
    "handed_off",        # 触发约面/谈薪/超纲红线，已交回本人跟进
    "closed_rejected",   # 不合适已礼貌收尾归档
)

STATUS_LABELS: dict[str, str] = {
    "discovered": "待确认候选",
    "screened_skip": "排雷跳过",
    "opening_sent": "开场已送达",
    "connected": "双方已建联",
    "resume_requested": "待同意简历请求",
    "resume_delivered": "PDF 简历已送达",
    "handed_off": "待本人约面/决策",
    "closed_rejected": "不合适归档",
}

ACTIVE_CHAT_STATUSES: frozenset[str] = frozenset({
    "opening_sent",
    "connected",
    "resume_requested",
    "resume_delivered",
    "handed_off",
})

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
        'screened_skip',
        'opening_sent',
        'connected',
        'resume_requested',
        'resume_delivered',
        'handed_off',
        'closed_rejected'
    )),
    resume_used TEXT NOT NULL DEFAULT '',
    job_url TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    opening_sent_at TEXT,
    connected_at TEXT,
    resume_requested_at TEXT,
    resume_delivered_at TEXT,
    handed_off_at TEXT,
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
"""


def _validate_status(status: str) -> str:
    st = (status or "").strip()
    if st not in VALID_STATUSES:
        raise ValueError(f"非法账本状态 {status!r}，允许枚举：{VALID_STATUSES}")
    return st


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
    """按严格四态规则维护里程碑时间戳，绝不将 resume_requested 混同为 resume_delivered。"""
    prev = existing or {}
    milestones = {
        "opening_sent_at": prev.get("opening_sent_at"),
        "connected_at": prev.get("connected_at"),
        "resume_requested_at": prev.get("resume_requested_at"),
        "resume_delivered_at": prev.get("resume_delivered_at"),
        "handed_off_at": prev.get("handed_off_at"),
        "closed_at": prev.get("closed_at"),
    }
    if status == "opening_sent":
        milestones["opening_sent_at"] = milestones["opening_sent_at"] or ts
    elif status == "connected":
        milestones["opening_sent_at"] = milestones["opening_sent_at"] or ts
        milestones["connected_at"] = milestones["connected_at"] or ts
    elif status == "resume_requested":
        milestones["opening_sent_at"] = milestones["opening_sent_at"] or ts
        milestones["connected_at"] = milestones["connected_at"] or ts
        milestones["resume_requested_at"] = milestones["resume_requested_at"] or ts
        # 严格规则：仅发起请求绝不自动写入 resume_delivered_at！
    elif status == "resume_delivered":
        milestones["opening_sent_at"] = milestones["opening_sent_at"] or ts
        milestones["connected_at"] = milestones["connected_at"] or ts
        milestones["resume_delivered_at"] = milestones["resume_delivered_at"] or ts
    elif status == "handed_off":
        milestones["handed_off_at"] = milestones["handed_off_at"] or ts
    elif status == "closed_rejected":
        milestones["closed_at"] = milestones["closed_at"] or ts
    return milestones


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["risk_tags"] = _parse_risk_tags(data.get("risk_tags"))
    data["status_label"] = STATUS_LABELS.get(data["status"], data["status"])
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
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return _row_to_dict(row) if row else None

    def upsert_job(self, payload: dict[str, Any]) -> dict[str, Any]:
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
            "channel": pick_str("channel", "outbound"),
            "risk_tags": risk_tags_json,
            "match_reasons": pick_str("match_reasons"),
            "gaps": pick_str("gaps"),
            "icebreaker": pick_str("icebreaker"),
            "status": status,
            "resume_used": pick_str("resume_used"),
            "job_url": pick_str("job_url"),
            "note": pick_str("note"),
            "opening_sent_at": milestones["opening_sent_at"],
            "connected_at": milestones["connected_at"],
            "resume_requested_at": milestones["resume_requested_at"],
            "resume_delivered_at": milestones["resume_delivered_at"],
            "handed_off_at": milestones["handed_off_at"],
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
                    job_url, note, opening_sent_at, connected_at,
                    resume_requested_at, resume_delivered_at, handed_off_at, closed_at,
                    created_at, updated_at
                ) VALUES (
                    :job_id, :platform, :company, :company_clean, :company_core,
                    :company_scale, :business_nature, :title, :title_norm, :salary, :city,
                    :recruiter_name, :recruiter_title, :channel, :risk_tags,
                    :match_reasons, :gaps, :icebreaker, :status, :resume_used,
                    :job_url, :note, :opening_sent_at, :connected_at,
                    :resume_requested_at, :resume_delivered_at, :handed_off_at, :closed_at,
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
                    opening_sent_at = excluded.opening_sent_at,
                    connected_at = excluded.connected_at,
                    resume_requested_at = excluded.resume_requested_at,
                    resume_delivered_at = excluded.resume_delivered_at,
                    handed_off_at = excluded.handed_off_at,
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
        return {"ok": True, "created": existing is None, "job": updated}

    def transition(
        self,
        *,
        job_id: str,
        to_status: str,
        note: str | None = None,
        resume_used: str | None = None,
        timestamp: str | None = None,
        notify_desktop: bool = True,
    ) -> dict[str, Any]:
        target_status = _validate_status(to_status)
        existing = self.get_job(job_id)
        if not existing:
            raise KeyError(f"未找到 job_id={job_id!r} 的岗位记录")

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
                    opening_sent_at = ?,
                    connected_at = ?,
                    resume_requested_at = ?,
                    resume_delivered_at = ?,
                    handed_off_at = ?,
                    closed_at = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    target_status,
                    final_resume,
                    final_note,
                    milestones["opening_sent_at"],
                    milestones["connected_at"],
                    milestones["resume_requested_at"],
                    milestones["resume_delivered_at"],
                    milestones["handed_off_at"],
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

    def check_duplicate(
        self,
        *,
        company: str,
        title: str,
        recruiter: str = "",
        blocked_companies: list[str] | None = None,
        past_employers: list[str] | None = None,
        chat_already_open: bool = False,
        filter_outsourcing: bool = True,
        filter_onsite: bool = True,
    ) -> dict[str, Any]:
        """三维查重：1. 归一化公司比对（含前东家黑名单） 2. 岗位名相似度 3. 网页已有会话与同公司在聊/按当前偏好生效的公司级排雷记录。"""
        clean_c, core_c = normalize_company_parts(company)
        norm_t = normalize_title(title)

        all_blocked = list(blocked_companies or []) + list(past_employers or [])
        for blk in all_blocked:
            if employer_shield_match(company, blk):
                is_former = any(employer_shield_match(company, emp) for emp in (past_employers or []))
                return {
                    "ok": True,
                    "duplicate": True,
                    "blocked_employer": is_former,
                    "blocked_company_hit": blk,
                    "normalized_company": core_c,
                    "normalized_title": norm_t,
                    "same_company_active": False,
                    "same_company_skipped": False,
                    "chat_already_open": chat_already_open,
                    "max_title_similarity": 0.0,
                    "reason": f"命中{'前东家/现东家' if is_former else '屏蔽企业'}黑名单：{blk}",
                    "warnings": [],
                    "matches": [],
                }

        if chat_already_open:
            return {
                "ok": True,
                "duplicate": True,
                "blocked_employer": False,
                "blocked_company_hit": None,
                "normalized_company": core_c,
                "normalized_title": norm_t,
                "same_company_active": True,
                "same_company_skipped": False,
                "chat_already_open": True,
                "max_title_similarity": 1.0,
                "reason": "BOSS 网页已存在与该招聘者/岗位的历史会话（按钮显示“继续沟通”或聊天列表已存在）",
                "warnings": [],
                "matches": [],
            }

        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC").fetchall()

        same_company_matches: list[dict[str, Any]] = []
        warnings: list[str] = []
        max_sim = 0.0
        same_company_active = False
        same_company_skipped = False
        company_level_skipped = False
        exact_or_similar_role = False
        recruiter_changed_repost = False

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
            if st in ACTIVE_CHAT_STATUSES:
                same_company_active = True
            if st in ("screened_skip", "closed_rejected"):
                same_company_skipped = True
                raw_meta = f"{row['business_nature'] or ''} {row['risk_tags'] or ''} {row['note'] or ''}".lower()
                sanitized_meta = _NEGATED_OUTSOURCING_RE.sub(" ", raw_meta)
                if any(hint in sanitized_meta for hint in company_block_hints):
                    company_level_skipped = True
            if sim >= 0.55:
                exact_or_similar_role = True
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
                "updated_at": row["updated_at"],
                "note": row["note"],
            })

        is_dup = bool(exact_or_similar_role or same_company_active or company_level_skipped)
        reasons: list[str] = []
        if recruiter_changed_repost:
            reasons.append("检测到同公司换招聘者重发相似岗位（防连投社死拦截）")
        elif exact_or_similar_role:
            reasons.append(f"同公司存在相同或高度相似岗位记录（相似度 {max_sim:.2f}）")
        if same_company_active:
            reasons.append("同公司已有在聊/推进中的岗位会话")
        if company_level_skipped:
            reasons.append("同公司此前已确认为外包/派遣或公司级排雷跳过")
        elif same_company_skipped and not is_dup:
            warnings.append("同公司曾跳过其他不同方向的岗位，当前新岗位可独立评估")

        return {
            "ok": True,
            "duplicate": is_dup,
            "blocked_employer": False,
            "blocked_company_hit": None,
            "normalized_company": core_c,
            "company_clean": clean_name_or_core(clean_c, core_c),
            "normalized_title": norm_t,
            "same_company_active": same_company_active,
            "same_company_skipped": same_company_skipped,
            "company_level_skipped": company_level_skipped,
            "chat_already_open": False,
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
        return [_row_to_dict(r) for r in rows]

    def summary(self, local_date_prefix: str | None = None) -> dict[str, Any]:
        today_prefix = local_date_prefix or datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs").fetchall()

        by_status = {st: 0 for st in VALID_STATUSES}
        opening_delivered_total = 0
        connected_total = 0
        pdf_delivered_total = 0
        today_opening_sent = 0

        for row in rows:
            st = row["status"]
            by_status[st] = by_status.get(st, 0) + 1

            has_opening = bool(row["opening_sent_at"]) or st in (
                "opening_sent", "connected", "resume_requested", "resume_delivered", "handed_off"
            )
            if has_opening:
                opening_delivered_total += 1

            has_connected = bool(row["connected_at"]) or st in (
                "connected", "resume_requested", "resume_delivered", "handed_off"
            )
            if has_connected:
                connected_total += 1

            # 严格拆账：resume_requested 绝不计入 pdf_delivered_total！
            has_pdf_delivered = (st == "resume_delivered") or bool(row["resume_delivered_at"])
            if has_pdf_delivered:
                pdf_delivered_total += 1

            op_ts = row["opening_sent_at"] or ""
            if op_ts.startswith(today_prefix):
                today_opening_sent += 1

        return {
            "ok": True,
            "total_evaluated": len(rows),
            "by_status": by_status,
            "funnel": {
                "screened_total": len(rows),
                "screened_skip": by_status["screened_skip"],
                "discovered_pending": by_status["discovered"],
                "opening_delivered_total": opening_delivered_total,
                "opening_waiting_reply": by_status["opening_sent"],
                "connected_total": connected_total,
                "connected_active": by_status["connected"],
                "resume_requested_pending": by_status["resume_requested"],
                "pdf_delivered_total": pdf_delivered_total,
                "handed_off_pending": by_status["handed_off"],
                "closed_rejected": by_status["closed_rejected"],
            },
            "today_date": today_prefix,
            "today_opening_sent": today_opening_sent,
        }

    def _export_rows_and_headers(self) -> tuple[list[str], list[list[object]]]:
        jobs = self.list_jobs(limit=10000)
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
            "最新备注",
            "岗位链接",
            "更新时间",
        ]
        rows: list[list[object]] = []
        for item in jobs:
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
                item["channel"],
                " / ".join(item["risk_tags"]),
                item["match_reasons"],
                item["gaps"],
                item["resume_used"],
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
