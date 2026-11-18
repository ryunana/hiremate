"""本地只读高信息密度静态投递看板构建器（零外部 CDN、支持桌面与移动端自适应）。"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .ledger import STATUS_LABELS, VALID_STATUSES, JobLedger
from .workspace import DEFAULT_API_BASE, atomic_write_text, get_effective_api_base, load_config, profile_facts_path

DECISION_MAKER_KEYWORDS = ("vp", "总监", "负责人", "合伙人", "总经理", "ceo", "cto", "创始人", "主管", "leader", "专家")


def _esc(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _safe_url(url: str) -> str:
    cleaned = (url or "").strip()
    if cleaned.startswith(("https://", "http://")):
        return _esc(cleaned)
    return ""


def _is_decision_maker(recruiter_title: str) -> bool:
    lower = (recruiter_title or "").lower()
    return any(kw in lower for kw in DECISION_MAKER_KEYWORDS)


def _mode_cn(mode: str) -> str:
    return "全自动" if mode == "auto" else "半自动"


def _headhunter_cn(policy: str) -> str:
    mapping = {
        "block_all": "全部屏蔽",
        "inbound_only": "主动不投 · 被动择优接洽",
        "allow_all": "全部开放",
    }
    return mapping.get(policy, policy)


def _outsourcing_cn(config: dict[str, Any], *, is_pro: bool) -> str:
    policy = str(
        config.get("outsourcing_policy")
        or ("block_all" if config.get("filter_outsourcing", True) else "allow_all")
    )
    if policy == "allow_all":
        return "开放接受（不排除外包 / 派遣 / OD 机会）"
    if policy == "inbound_only":
        return "主动不投 · 被动来信择优接洽"
    if is_pro:
        return "已开启过滤（100+ 深度名录 + 社区动态池）"
    return "已开启基础过滤（100+ 深度名录与社区动态池需 Pro）"


def _onsite_cn(config: dict[str, Any]) -> str:
    policy = str(
        config.get("onsite_policy")
        or ("block_all" if config.get("filter_onsite", True) else "allow_all")
    )
    if policy == "allow_all":
        return "开放接受（不排除驻场 / 客户现场外派机会）"
    if policy == "inbound_only":
        return "主动不投 · 被动来信择优接洽"
    return "已开启过滤（跳过驻场与客户现场外派岗）"


def _skip_kpi_sub(config: dict[str, Any]) -> str:
    out_pol = str(
        config.get("outsourcing_policy")
        or ("block_all" if config.get("filter_outsourcing", True) else "allow_all")
    )
    onsite_pol = str(
        config.get("onsite_policy")
        or ("block_all" if config.get("filter_onsite", True) else "allow_all")
    )
    parts: list[str] = []
    if out_pol != "allow_all":
        parts.append("外包")
    if onsite_pol != "allow_all":
        parts.append("驻场")
    parts.extend(["黑名单", "不符拦截"])
    return " / ".join(parts)


def _inbound_resume_cn(policy: str) -> str:
    return "除非明显不符尽量给付" if policy == "generous" else "仅高匹配岗位给付"


def _mask_email(email: str) -> str:
    cleaned = (email or "").strip()
    if "@" not in cleaned:
        return ""
    local, domain = cleaned.split("@", 1)
    if len(local) <= 2:
        masked_local = local[:1] + "***"
    else:
        masked_local = local[:3] + "***"
    return f"{masked_local}@{domain}"


def build_demo_dataset(demo_tier: str = "pro") -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], str]:
    """生成虚构人物“林远”的全链路典型求职数据供 --demo 预览（支持 free / pro 两种视角）。"""
    demo_config: dict[str, Any] = {
        "account_display_name": "林远",
        "mode_discover": "semi",
        "mode_reply": "auto" if demo_tier == "pro" else "semi",
        "target_roles": ["AI 应用产品经理", "Agent 产品经理", "B端 SaaS 产品经理"],
        "preferred_cities": ["深圳", "杭州"],
        "remote_bigtech_flex": True,
        "min_salary_k": 28,
        "salary_panel_tolerance": True,
        "filter_outsourcing": True,
        "outsourcing_policy": "block_all",
        "filter_onsite": True,
        "onsite_policy": "block_all",
        "headhunter_policy": "inbound_only",
        "past_employers": ["示例科技", "远山数字"],
        "blocked_companies": ["示例科技", "远山数字", "博软通达信息技术", "某竞业关联企业"],
        "exclude_keywords": ["驻场", "外派", "电销", "纯硬件量产"],
        "resume_routes": [
            {
                "name": "林远-AI产品经理.pdf",
                "keywords": ["AI", "大模型", "Agent", "RAG", "智能体", "知识库"],
                "is_default": True,
            },
            {
                "name": "林远-SaaS产品经理.pdf",
                "keywords": ["SaaS", "B端", "工作流", "ERP", "协同", "权限"],
                "is_default": False,
            },
        ],
        "inbound_resume_policy": "generous",
        "schedule": {
            "weekdays_only": True,
            "active_windows": ["09:00-12:00", "14:00-18:00"],
            "min_interval_minutes": 90,
            "max_per_round": 5,
            "max_per_day": 20,
        },
    }

    demo_profile_md = (
        "【虚构示例档案：林远】5 年企业级 SaaS 与 AI 应用产品经验。"
        "从 0 到 1 主导企业 AI 员工助手与千万级文档知识库问答落地（知识问答准确率达 89%，高频问题自助解决率超 70%）及多租户审批流引擎；"
        "明确无硬件嵌入式及 C 端游戏买量经验；前东家「示例科技」「远山数字」已自动同步至屏蔽黑名单。"
    )

    if demo_tier == "free":
        free_jobs: list[dict[str, Any]] = [
            {
                "job_id": "demo_f01",
                "company": "星海智联",
                "company_scale": "B轮 · 150-500人",
                "business_nature": "自研产品",
                "title": "AI Agent 应用产品经理",
                "salary": "30-45K·15薪",
                "city": "深圳·科技园",
                "recruiter_name": "陈明",
                "recruiter_title": "产品VP",
                "channel": "outbound",
                "risk_tags": ["基础规则通过", "业务负责人直招"],
                "match_reasons": "JD 要求独立搭建 RAG 评测集与工作流编排，与近两年核心产出直接对应",
                "gaps": "JD 提及熟悉多模态语音交互，属相邻但未深做方向",
                "icebreaker": "您好，看咱们在招自研 AI 应用产品，我近两年主要负责企业级知识库 RAG 与多步 Agent 工作流从 0 到 1 上线，感觉经历挺契合的，方便聊聊吗？",
                "status": "discovered",
                "status_label": STATUS_LABELS["discovered"],
                "resume_used": "",
                "job_url": "https://www.zhipin.com/job_detail/demo_02.html",
                "note": "单轮手动找岗初筛通过，回对话输入序号「1」即可点击立即沟通",
                "opening_sent_at": None,
                "connected_at": None,
                "resume_requested_at": None,
                "resume_delivered_at": None,
                "handed_off_at": None,
                "updated_at": "2026-11-15 10:42",
            },
            {
                "job_id": "demo_f02",
                "company": "澄观智能",
                "company_scale": "B轮 · 150-500人",
                "business_nature": "自研产品",
                "title": "大模型知识库产品经理",
                "salary": "24-32K·15薪",
                "city": "深圳·宝安区",
                "recruiter_name": "赵哲",
                "recruiter_title": "AI业务负责人",
                "channel": "outbound",
                "risk_tags": ["面板标薪偏低容忍"],
                "match_reasons": "企业内部搜索与企业文档切片召回调优经验完全对口",
                "gaps": "面板标薪上限略低，先正常沟通业务匹配度，进入约面/谈薪阶段再由本人确认总包空间",
                "icebreaker": "您好，看到咱们在招大模型知识库产品，我之前主导过千万级企业文档切片召回与重排调优（知识问答准确率稳定在 89%），方向挺对口的，方便沟通了解下吗？",
                "status": "discovered",
                "status_label": STATUS_LABELS["discovered"],
                "resume_used": "",
                "job_url": "https://www.zhipin.com/job_detail/demo_05.html",
                "note": "单轮手动找岗初筛通过，回对话输入序号「2」即可点击立即沟通",
                "opening_sent_at": None,
                "connected_at": None,
                "resume_requested_at": None,
                "resume_delivered_at": None,
                "handed_off_at": None,
                "updated_at": "2026-11-15 10:40",
            },
            {
                "job_id": "demo_f03",
                "company": "矩石科技",
                "company_scale": "A轮 · 50-150人",
                "business_nature": "自研产品",
                "title": "企业级 SaaS 协同产品经理",
                "salary": "25-35K·14薪",
                "city": "深圳·福田区",
                "recruiter_name": "王琳",
                "recruiter_title": "HRBP",
                "channel": "outbound",
                "risk_tags": ["手动确认已发"],
                "match_reasons": "B端多租户权限体系与审批流重构经验完全匹配",
                "gaps": "团队处于 A 轮扩张期，需关注商业化客单验证进展",
                "icebreaker": "",
                "status": "opening_sent",
                "status_label": STATUS_LABELS["opening_sent"],
                "resume_used": "",
                "job_url": "https://www.zhipin.com/job_detail/demo_04.html",
                "note": "本人确认序号后已发送破冰语；免费版不含后台自动盯消息，后续 HR 回复需手动查看或升级 Pro 自动跟进",
                "opening_sent_at": "2026-11-15T09:30:00+08:00",
                "connected_at": None,
                "resume_requested_at": None,
                "resume_delivered_at": None,
                "handed_off_at": None,
                "updated_at": "2026-11-15 09:30",
            },
            {
                "job_id": "demo_f04",
                "company": "博软通达信息技术",
                "company_scale": "不需要融资 · 500-1000人",
                "business_nature": "驻场外包",
                "title": "高级产品经理（驻场项目）",
                "salary": "22-30K",
                "city": "深圳·罗湖区",
                "recruiter_name": "张婷",
                "recruiter_title": "人事专员",
                "channel": "outbound",
                "risk_tags": ["排除词命中跳过"],
                "match_reasons": "",
                "gaps": "标题命中排除词「驻场」，按基础排雷规则直接跳过",
                "icebreaker": "",
                "status": "screened_skip",
                "status_label": STATUS_LABELS["screened_skip"],
                "resume_used": "",
                "job_url": "https://www.zhipin.com/job_detail/demo_06.html",
                "note": "基础规则识别「驻场」关键词，直接记账跳过不打招呼",
                "opening_sent_at": None,
                "connected_at": None,
                "resume_requested_at": None,
                "resume_delivered_at": None,
                "handed_off_at": None,
                "updated_at": "2026-11-15 09:20",
            },
        ]
        by_st_free = {st: 0 for st in VALID_STATUSES}
        for j in free_jobs:
            by_st_free[j["status"]] += 1
        free_summary = {
            "total_evaluated": 4,
            "by_status": by_st_free,
            "funnel": {
                "screened_total": 4,
                "screened_skip": 1,
                "discovered_pending": 2,
                "opening_delivered_total": 1,
                "opening_waiting_reply": 1,
                "connected_total": 0,
                "connected_active": 0,
                "resume_requested_pending": 0,
                "pdf_delivered_total": 0,
                "handed_off_pending": 0,
                "closed_rejected": 0,
            },
        }
        return demo_config, free_jobs, free_summary, demo_profile_md

    demo_jobs: list[dict[str, Any]] = [
        {
            "job_id": "demo_01",
            "company": "深蓝云图（猎头代招：头部出海效率独角兽）",
            "company_scale": "D轮及以上 · 1000-2000人",
            "business_nature": "自研核心岗",
            "title": "高级 AI 平台产品经理",
            "salary": "35-50K·16薪",
            "city": "深圳·南山区",
            "recruiter_name": "苏敏",
            "recruiter_title": "资深猎头顾问",
            "channel": "inbound",
            "risk_tags": ["猎头来信", "约面跟进"],
            "match_reasons": "命中 AI 平台与多智能体编排评测经验；猎头已披露终端客户为自研出海 SaaS 独角兽，薪资覆盖预期",
            "gaps": "需跟进确认海外跨时区协作频率",
            "icebreaker": "",
            "status": "handed_off",
            "status_label": STATUS_LABELS["handed_off"],
            "resume_used": "林远-AI产品经理.pdf",
            "job_url": "https://www.zhipin.com/job_detail/demo_01.html",
            "note": "对方已查阅 PDF 简历，询问周四 14:00 或周五 10:30 腾讯会议业务一面时间，已暂停自动回复交回本人敲定",
            "opening_sent_at": "2026-11-14T10:12:00+08:00",
            "connected_at": "2026-11-14T10:15:00+08:00",
            "resume_requested_at": "2026-11-14T10:25:00+08:00",
            "resume_delivered_at": "2026-11-14T10:28:00+08:00",
            "handed_off_at": "2026-11-15T11:05:00+08:00",
            "updated_at": "2026-11-15 11:05",
        },
        {
            "job_id": "demo_02",
            "company": "星海智联",
            "company_scale": "B轮 · 150-500人",
            "business_nature": "自研产品",
            "title": "AI Agent 应用产品经理",
            "salary": "30-45K·15薪",
            "city": "深圳·科技园",
            "recruiter_name": "陈明",
            "recruiter_title": "产品VP",
            "channel": "outbound",
            "risk_tags": ["强匹配候选", "业务负责人直招"],
            "match_reasons": "公司主页核验为企业服务自研团队；JD 要求独立搭建 RAG 评测集与工作流编排，与近两年核心产出直接对应",
            "gaps": "JD 提及熟悉多模态语音交互，属相邻但未深做方向",
            "icebreaker": "做过两年企业级知识库 RAG 与多步 Agent 工作流从 0 到 1 上线，搭建过完整的离线评测集与 Badcase 闭环，看咱们在招自研 AI 应用产品，方便聊聊吗？",
            "status": "discovered",
            "status_label": STATUS_LABELS["discovered"],
            "resume_used": "",
            "job_url": "https://www.zhipin.com/job_detail/demo_02.html",
            "note": "半自动深筛通过，等待确认发送破冰语（推荐路由：林远-AI产品经理.pdf）",
            "opening_sent_at": None,
            "connected_at": None,
            "resume_requested_at": None,
            "resume_delivered_at": None,
            "handed_off_at": None,
            "updated_at": "2026-11-15 10:42",
        },
        {
            "job_id": "demo_03",
            "company": "云脉数智",
            "company_scale": "C轮 · 500-1000人",
            "business_nature": "自研产品",
            "title": "智能工作流产品专家",
            "salary": "28-38K·14薪",
            "city": "杭州·余杭区",
            "recruiter_name": "许航",
            "recruiter_title": "产研总监",
            "channel": "outbound",
            "risk_tags": ["异地知名团队"],
            "match_reasons": "核心负责自动化流程编排与企业权限集成，与过往 SaaS + AI 双背景高度契合",
            "gaps": "工作地位于杭州，若进入终面需本人评估异地入职安排",
            "icebreaker": "",
            "status": "resume_delivered",
            "status_label": STATUS_LABELS["resume_delivered"],
            "resume_used": "林远-AI产品经理.pdf",
            "job_url": "https://www.zhipin.com/job_detail/demo_03.html",
            "note": "已简短答复离线准确率提升指标，对方点击同意并已接收 AI 方向 PDF 简历",
            "opening_sent_at": "2026-11-14T14:20:00+08:00",
            "connected_at": "2026-11-14T15:02:00+08:00",
            "resume_requested_at": "2026-11-14T15:10:00+08:00",
            "resume_delivered_at": "2026-11-14T15:18:00+08:00",
            "handed_off_at": None,
            "updated_at": "2026-11-14 15:18",
        },
        {
            "job_id": "demo_04",
            "company": "矩石科技",
            "company_scale": "A轮 · 50-150人",
            "business_nature": "自研产品",
            "title": "企业级 SaaS 协同产品经理",
            "salary": "25-35K·14薪",
            "city": "深圳·福田区",
            "recruiter_name": "王琳",
            "recruiter_title": "HRBP",
            "channel": "outbound",
            "risk_tags": ["待对方点同意"],
            "match_reasons": "B端多租户权限体系与审批流重构经验完全匹配",
            "gaps": "团队处于 A 轮扩张期，需关注商业化客单验证进展",
            "icebreaker": "",
            "status": "resume_requested",
            "status_label": STATUS_LABELS["resume_requested"],
            "resume_used": "林远-SaaS产品经理.pdf",
            "job_url": "https://www.zhipin.com/job_detail/demo_04.html",
            "note": "双方已建联，按 B端方向路由发起附件简历发送请求，等待 HRBP 点击同意（未计入已送达）",
            "opening_sent_at": "2026-11-15T09:30:00+08:00",
            "connected_at": "2026-11-15T09:48:00+08:00",
            "resume_requested_at": "2026-11-15T09:55:00+08:00",
            "resume_delivered_at": None,
            "handed_off_at": None,
            "updated_at": "2026-11-15 09:55",
        },
        {
            "job_id": "demo_05",
            "company": "澄观智能",
            "company_scale": "B轮 · 150-500人",
            "business_nature": "自研产品",
            "title": "大模型知识库产品经理",
            "salary": "24-32K·15薪",
            "city": "深圳·宝安区",
            "recruiter_name": "赵哲",
            "recruiter_title": "AI业务负责人",
            "channel": "outbound",
            "risk_tags": ["面板标薪偏低容忍"],
            "match_reasons": "企业内部搜索与企业文档切片召回调优经验完全对口",
            "gaps": "面板标薪上限略低，先正常沟通业务匹配度，进入约面/谈薪阶段再由本人确认总包空间",
            "icebreaker": "",
            "status": "opening_sent",
            "status_label": STATUS_LABELS["opening_sent"],
            "resume_used": "",
            "job_url": "https://www.zhipin.com/job_detail/demo_05.html",
            "note": "首条个性化能力破冰已送达，等待招聘者回复",
            "opening_sent_at": "2026-11-15T10:15:00+08:00",
            "connected_at": None,
            "resume_requested_at": None,
            "resume_delivered_at": None,
            "handed_off_at": None,
            "updated_at": "2026-11-15 10:15",
        },
        {
            "job_id": "demo_06",
            "company": "博软通达信息技术",
            "company_scale": "不需要融资 · 500-1000人",
            "business_nature": "驻场外包",
            "title": "高级产品经理（金融核心项目）",
            "salary": "22-30K",
            "city": "深圳·罗湖区",
            "recruiter_name": "张婷",
            "recruiter_title": "人事专员",
            "channel": "outbound",
            "risk_tags": ["外包已排雷"],
            "match_reasons": "",
            "gaps": "公司主页核验为软件人力外包供应商，岗位实为银行驻场外派",
            "icebreaker": "",
            "status": "screened_skip",
            "status_label": STATUS_LABELS["screened_skip"],
            "resume_used": "",
            "job_url": "https://www.zhipin.com/job_detail/demo_06.html",
            "note": "深筛识别驻场外包属性，按策略直接跳过不打招呼",
            "opening_sent_at": None,
            "connected_at": None,
            "resume_requested_at": None,
            "resume_delivered_at": None,
            "handed_off_at": None,
            "updated_at": "2026-11-15 09:20",
        },
    ]

    by_status = {st: 0 for st in VALID_STATUSES}
    for j in demo_jobs:
        by_status[j["status"]] += 1

    demo_summary = {
        "total_evaluated": 6,
        "by_status": by_status,
        "funnel": {
            "screened_total": 6,
            "screened_skip": 1,
            "discovered_pending": 1,
            "opening_delivered_total": 4,
            "opening_waiting_reply": 1,
            "connected_total": 3,
            "connected_active": 0,
            "resume_requested_pending": 1,
            "pdf_delivered_total": 2,
            "handed_off_pending": 1,
            "closed_rejected": 0,
        },
    }

    demo_profile_md = (
        "【虚构示例档案：林远】5 年企业级 SaaS 与 AI 应用产品经验。"
        "主导过千万级文档企业知识库 RAG 检索重构（首屏答案采纳率由 41% 提至 73%）与多租户审批流引擎；"
        "明确无硬件嵌入式及 C 端游戏买量经验；前东家「示例科技」「远山数字」已自动同步至屏蔽黑名单。"
    )
    return demo_config, demo_jobs, demo_summary, demo_profile_md


def _render_job_card(job: dict[str, Any], *, highlight_icebreaker: bool = False) -> str:
    status = str(job.get("status") or "discovered")
    status_label = _esc(job.get("status_label") or STATUS_LABELS.get(status, status))
    title = _esc(job.get("title") or "未命名岗位")
    salary = _esc(job.get("salary") or "薪资面议")
    company = _esc(job.get("company") or "未知企业")
    scale = _esc(job.get("company_scale") or "")
    nature = _esc(job.get("business_nature") or "")
    city = _esc(job.get("city") or "")
    rec_name = _esc(job.get("recruiter_name") or "招聘方")
    rec_title = _esc(job.get("recruiter_title") or "职位未标注")
    is_dm = _is_decision_maker(str(job.get("recruiter_title") or ""))
    rec_badge_cls = "recruiter-pill decision-maker" if is_dm else "recruiter-pill"

    risk_tags = job.get("risk_tags") or []
    channel = str(job.get("channel") or "outbound")
    channel_text = "被动来信" if "inbound" in channel else "主动检索"

    is_skipped = status == "screened_skip"
    tags_html_parts = [f'<span class="tag tag-channel">{_esc(channel_text)}</span>']
    if nature:
        raw_nature = str(job.get("business_nature") or "")
        for neg_tok in ("非外包", "非驻场", "非派遣", "非外派"):
            raw_nature = raw_nature.replace(neg_tok, "")
        has_work_mode_flag = any(w in raw_nature for w in ("外包", "驻场", "派遣", "外派", "OD"))
        nature_cls = ("tag-danger" if is_skipped else "tag-warn") if has_work_mode_flag else "tag-selfdev"
        tags_html_parts.append(f'<span class="tag {nature_cls}">{nature}</span>')
    for tg in risk_tags:
        tg_str = str(tg)
        is_hard_block_tag = any(w in tg_str for w in ("跳过", "排雷", "拦截", "黑名单")) or (
            is_skipped and any(w in tg_str for w in ("外包", "驻场", "派遣"))
        )
        t_cls = "tag-danger" if is_hard_block_tag else (
            "tag-warn" if any(w in tg_str for w in ("猎头", "外包", "驻场", "派遣", "偏低", "待", "预警")) else "tag-info"
        )
        tags_html_parts.append(f'<span class="tag {t_cls}">{_esc(tg_str)}</span>')

    match_reasons = _esc(job.get("match_reasons") or "—")
    gaps = _esc(job.get("gaps") or "无明显硬伤")
    resume_used = _esc(job.get("resume_used") or "尚未投递附件简历")
    note = _esc(job.get("note") or "")
    icebreaker = _esc(job.get("icebreaker") or "")
    updated_at = _esc(str(job.get("updated_at") or "")[:16].replace("T", " "))
    safe_href = _safe_url(str(job.get("job_url") or ""))
    link_html = (
        f'<a class="job-link" href="{safe_href}" target="_blank" rel="noopener noreferrer">查看原帖 ↗</a>'
        if safe_href
        else '<span class="job-link muted">无原帖链接</span>'
    )

    search_blob = _esc(
        " ".join([
            str(job.get("company") or ""),
            str(job.get("title") or ""),
            str(job.get("recruiter_name") or ""),
            str(job.get("recruiter_title") or ""),
            " ".join(str(x) for x in risk_tags),
            str(job.get("match_reasons") or ""),
            str(job.get("gaps") or ""),
            str(job.get("resume_used") or ""),
            str(job.get("note") or ""),
        ]).lower()
    )

    meta_company = " · ".join(x for x in [company, scale, city] if x)
    icebreaker_block = (
        f'<div class="detail-row icebreaker-box"><span class="detail-label">拟发破冰</span>'
        f'<span class="detail-val">“{icebreaker}”</span></div>'
        if highlight_icebreaker and icebreaker
        else ""
    )
    note_block = (
        f'<div class="detail-row note-row"><span class="detail-label">最新进展</span>'
        f'<span class="detail-val">{note}</span></div>'
        if note
        else ""
    )

    return f"""
        <article class="job-card status-border-{_esc(status)}" data-status="{_esc(status)}" data-search="{search_blob}">
          <div class="job-card-top">
            <div class="job-main-title">
              <div class="title-salary-line">
                <h3 class="job-title">{title}</h3>
                <span class="job-salary">{salary}</span>
              </div>
              <div class="company-line">{meta_company}</div>
            </div>
            <div class="job-right-meta">
              <span class="status-badge status-{_esc(status)}">{status_label}</span>
              <span class="{rec_badge_cls}" title="招聘者姓名与职务头衔">{rec_name} · {rec_title}</span>
            </div>
          </div>
          <div class="tag-row">
            {"".join(tags_html_parts)}
          </div>
          <div class="job-details-grid">
            <div class="detail-row">
              <span class="detail-label">匹配依据</span>
              <span class="detail-val">{match_reasons}</span>
            </div>
            <div class="detail-row">
              <span class="detail-label">待确认缺口</span>
              <span class="detail-val">{gaps}</span>
            </div>
            {icebreaker_block}
            {note_block}
          </div>
          <div class="job-card-footer">
            <div class="footer-left">
              <span class="resume-tag">简历版本：{resume_used}</span>
              <span class="time-tag">更新：{updated_at}</span>
            </div>
            <div class="footer-right">
              {link_html}
            </div>
          </div>
        </article>"""


def render_dashboard_html(
    *,
    config: dict[str, Any],
    jobs: list[dict[str, Any]],
    summary: dict[str, Any],
    profile_excerpt: str,
    tier: str = "free",
    pro_meta: dict[str, Any] | None = None,
    is_demo: bool = False,
    snapshot_time: str | None = None,
    update_notice: dict[str, Any] | None = None,
) -> str:
    """渲染完整内联、零外部依赖的静态投递看板 HTML。"""
    snap = snapshot_time or datetime.now().strftime("%Y-%m-%d %H:%M")
    funnel = summary.get("funnel") or {}
    by_status = summary.get("by_status") or {}
    is_pro = tier == "pro"
    meta = pro_meta or {}

    roles_str = " / ".join(config.get("target_roles") or []) or "未配置目标岗位（请先对话建档）"
    cities_str = " · ".join(config.get("preferred_cities") or []) or "不限城市"
    if config.get("remote_bigtech_flex", True):
        cities_str += "（异地大厂可谈）"

    if is_pro:
        mode_badge = (
            f"找岗：{_mode_cn(str(config.get('mode_discover', 'semi')))} / "
            f"答疑：{_mode_cn(str(config.get('mode_reply', 'semi')))}"
        )
        masked_mail = _mask_email(str(meta.get("email") or ""))
        days_left = meta.get("days_left")
        is_lifetime = bool(meta.get("lifetime", days_left is None))
        pro_suffix = ["买断长期有效"] if is_lifetime else ([f"剩余 {int(days_left)} 天"] if days_left is not None else [])
        if masked_mail:
            pro_suffix.append(masked_mail)
        tier_badge = f"Pro 已激活（{' · '.join(pro_suffix)}）" if pro_suffix else "Pro 已激活（买断版）"
        tier_cls = "badge-pro"
    else:
        mode_badge = "当前可用：手动单轮找岗（半自动确认）"
        tier_badge = "免费基础版"
        tier_cls = "badge-free"

    demo_banner = (
        '<span class="header-pill badge-demo">示例预览数据（虚构人物：林远）</span>'
        if is_demo
        else ""
    )

    has_update = bool((not is_demo) and isinstance(update_notice, dict) and update_notice.get("update_available"))
    latest_ver_str = str((update_notice or {}).get("latest_version") or __version__)
    cur_ver_str = str((update_notice or {}).get("current_version") or __version__)
    if has_update:
        version_pill_html = (
            f'<a class="header-pill badge-update-ready" href="#release-update-banner">'
            f'🚀 新版 v{_esc(latest_ver_str)} 可升级</a>'
        )
    else:
        version_pill_html = f'<span class="header-pill badge-version">v{_esc(__version__)}</span>'

    release_update_banner_html = ""
    if has_update and isinstance(update_notice, dict):
        is_req = bool(update_notice.get("required"))
        pub_date = str(update_notice.get("published_at") or "").strip()
        raw_hl = [str(x).strip() for x in (update_notice.get("highlights") or []) if str(x).strip()][:3]
        hl_items_html = "".join(f"<span>• {_esc(item)}</span>" for item in raw_hl)
        if not hl_items_html:
            hl_items_html = "<span>• 包含最新功能升级、体验优化与排雷规则迭代</span>"
        tag_label = "必升更新" if is_req else "发现新版本"
        banner_mod_cls = " release-update-required" if is_req else ""
        pub_suffix = f" · 发布于 {_esc(pub_date)}" if pub_date else ""
        release_update_banner_html = f"""
    <section id="release-update-banner" class="release-update-banner{banner_mod_cls}" aria-label="插件新版本更新提醒">
      <div class="release-update-left">
        <div class="release-update-head">
          <span class="release-update-tag">{tag_label}</span>
          <strong class="release-update-title">求职助手 v{_esc(latest_ver_str)} 已发布</strong>
          <span class="release-update-meta">当前版本 v{_esc(cur_ver_str)}{pub_suffix} · 升级完整保留本地简历与账本数据</span>
        </div>
        <div class="release-update-highlights">{hl_items_html}</div>
      </div>
      <div class="release-update-right">
        <button type="button" class="release-copy-btn" data-copy="请帮我升级求职助手到最新版本" data-done-label="✓ 已复制口令，粘贴发给助手即可自动更新">一键复制对话升级口令</button>
        <span class="release-cmd-hint">DSH 亦可直接输入 <code>/hiremate-update</code></span>
      </div>
    </section>
"""

    portal_base = _safe_url(get_effective_api_base(config)) or DEFAULT_API_BASE
    pro_checkout_url = f"{portal_base}/activate?plan=pro_buyout#subscribe-box"
    pro_pricing_url = f"{portal_base}/#pricing"
    top_upgrade_pill = (
        ""
        if is_pro
        else (
            f'<a class="header-pill badge-upgrade-cta" href="{pro_checkout_url}" '
            f'target="_blank" rel="noopener noreferrer">⚡ ¥69 购买解锁 Pro →</a>'
        )
    )

    upgrade_banner_html = ""
    if not is_pro:
        upgrade_banner_html = f"""
    <section class="upgrade-banner" aria-label="免费版与 Pro 专业版能力说明">
      <div class="upgrade-left">
        <div class="upgrade-title-row">
          <span class="upgrade-tag">当前为免费基础版</span>
          <span class="upgrade-headline">已包含建档防撞车、单轮找岗初筛、半自动破冰待确认队列与本地四态账本</span>
        </div>
        <div class="upgrade-features">
          <strong>特惠 ¥69（原价 ¥99）一次买断解锁全链路托管：</strong>
          <span>① 工作时间主动匹配破冰 + 自动巡检</span>
          <span>② HR 未读消息真人感答疑（约面 / 谈薪 / 微信自动停手交回本人）</span>
          <span>③ 100+ 深度外包词库 + 多版 PDF 简历路由</span>
          <span>④ <strong>一次买断长期有效，支持 2 台个人电脑同时绑定</strong></span>
        </div>
      </div>
      <div class="upgrade-right">
        <div class="upgrade-btn-row">
          <a class="upgrade-cta-btn" href="{pro_checkout_url}" target="_blank" rel="noopener noreferrer">¥69 立即购买解锁 Pro →</a>
          <a class="upgrade-secondary-btn" href="{pro_pricing_url}" target="_blank" rel="noopener noreferrer">查看功能对比</a>
        </div>
        <div class="upgrade-cmd-label">
          付完无需输卡密 · 对 AI 助手说
          <button type="button" class="inline-copy-btn" data-copy="登录求职助手并同步 Pro 状态" data-done-label="✓ 已复制口令">登录求职助手并激活 Pro</button>
          即可连上本机
        </div>
      </div>
    </section>"""

    handed_off_jobs = [j for j in jobs if j.get("status") == "handed_off"]
    discovered_jobs = [j for j in jobs if j.get("status") == "discovered"]
    queue_count = len(handed_off_jobs) + len(discovered_jobs)

    past_emps_for_guide = config.get("past_employers") or []
    is_copilot_mode = str(config.get("execution_mode", "browser")) == "copilot"
    pro_auto_discover = (not is_copilot_mode) and is_pro and str(config.get("mode_discover", "semi")) == "auto"
    if is_copilot_mode:
        first_search_cmd = "帮我评估下面这个 BOSS 岗位靠不靠谱（排外包+核对前东家+看匹配缺口），并拟一句真人感打招呼短句："
        step1_title = "粘贴首个岗位试跑排雷"
        step1_desc = "直接把看中的岗位 JD 发给助手，立即体验前东家防撞、外包识别与破冰话术草稿。"
    elif pro_auto_discover:
        first_search_cmd = "帮我在 BOSS 直聘按当前 Pro 全自动策略跑第一轮定额搜岗破冰并检查未读消息"
        step1_title = "跑首轮 Pro 全自动定额搜岗 + 未读检查"
        step1_desc = "自动在已登录浏览器筛除伪装外包与前东家，对强匹配自研岗直接定额发送破冰短句（每轮≤5岗）并处理未读消息。"
    else:
        first_search_cmd = "帮我在 BOSS 直聘按当前求职档案跑第一轮搜岗深筛预览（先出破冰审阅单给我确认，未经确认别点发送）"
        step1_title = "去 BOSS 跑首轮搜岗深筛预览"
        step1_desc = "自动在已登录浏览器按你的条件筛出 3～5 个靠谱自研岗，先出审阅单给你过目，绝不乱发。"

    if pro_auto_discover:
        second_reply_cmd = "帮我挂载求职助手工作日工作时间自动巡检定时任务"
        step2_title = "方式 2 · 一键挂载工作日工作时间自动巡检"
        step2_desc = "周一至周五 09:00–12:00、14:00–18:00 定时自动盯未读消息与定额找岗，遇到约面/谈薪立即桌面弹窗提醒本人接管。"
        step2_btn = "复制巡检口令"
    else:
        second_reply_cmd = "帮我对照个人履历档案拟定下面这条 HR 追问的真人感回复，并记入求职台账："
        step2_title = "方式 2 · 让助手参谋 HR 追问消息并记账"
        step2_desc = "把 HR 发来的问题贴给助手，严格按你真实履历拟 1～2 句自然回复，拒绝 AI 官腔与胡编。"
        step2_btn = "复制参谋口令"

    onboarding_launchpad_html = ""
    if not jobs:
        onboarding_launchpad_html = f"""
        <div class="launchpad-card">
          <div class="launchpad-head">
            <div>
              <span class="launchpad-badge">🎉 档案已就绪 · 下一步开始实战</span>
              <h2 class="launchpad-title">本地求职建档已完成，选一种方式跑通第一轮！</h2>
              <p class="launchpad-sub">
                已锁定方向 <strong>{_esc(roles_str)}</strong>（期望 <strong>{int(config.get("min_salary_k", 20))}K+</strong> · 已自动拉黑保护 <strong>{len(past_emps_for_guide)}</strong> 家前东家）。复制下方任一句发回你的 AI 助手即可开跑：
              </p>
            </div>
          </div>
          <div class="launchpad-grid">
            <div class="launchpad-step">
              <div class="launchpad-step-title">方式 1（推荐）· {step1_title}</div>
              <p class="launchpad-step-desc">{step1_desc}</p>
              <div class="launchpad-cmd-bar">
                <code>{_esc(first_search_cmd)}</code>
                <button type="button" class="copy-action-btn" data-copy="{_esc(first_search_cmd)}" data-done-label="✓ 已复制，切回 AI 粘贴发送">复制口令开跑</button>
              </div>
            </div>
            <div class="launchpad-step">
              <div class="launchpad-step-title">{step2_title}</div>
              <p class="launchpad-step-desc">{step2_desc}</p>
              <div class="launchpad-cmd-bar">
                <code>{_esc(second_reply_cmd)}</code>
                <button type="button" class="copy-action-btn secondary" data-copy="{_esc(second_reply_cmd)}" data-done-label="✓ 已复制">{step2_btn}</button>
              </div>
            </div>
          </div>
        </div>"""

    if handed_off_jobs:
        handed_off_html = "\n".join(_render_job_card(j) for j in handed_off_jobs)
    elif is_pro:
        handed_off_html = '<div class="empty-box">当前暂无待本人约面、谈薪或超纲决策会话。</div>'
    else:
        handed_off_html = (
            '<div class="empty-box empty-locked">'
            '<strong>Pro 专属托管队列（当前未开启自动消息巡检与答疑）</strong><br/>'
            '激活 Pro 后，求职助手会在工作时间自动答复 HR 常规背景追问并投递对应 PDF 简历；'
            '一旦遇到「约面试时间 / 要微信电话 / 压薪谈底线」，将自动暂停回复并置顶在此处提醒你本人接管。<br/>'
            f'<a class="inline-pro-link" href="{pro_checkout_url}" target="_blank" rel="noopener noreferrer">'
            '⚡ 前往开通 Pro 全托管（特惠 ¥69 · 原价 ¥99 · 一次买断无月费） →</a>'
            '</div>'
        )
    is_auto_discover = (tier_cls != "badge-free") and str(config.get("mode_discover", "semi")) == "auto"
    is_auto_reply = (tier_cls != "badge-free") and str(config.get("mode_reply", "semi")) == "auto"
    discovered_html = (
        "\n".join(_render_job_card(j, highlight_icebreaker=True) for j in discovered_jobs)
        if discovered_jobs
        else (
            f'<div class="empty-box">当前已开启<strong>主动找岗：全自动模式</strong>{"与<strong>消息答疑：全自动模式</strong>" if is_auto_reply else ""}——强匹配自研岗将在配额内直接发起沟通并记入「开场已送达」，无需逐条手动确认（仅冷却期或满当日配额时暂存于此）。</div>'
            if is_auto_discover
            else '<div class="empty-box">当前暂无半自动待确认候选岗位（复制上方口令发给桌面 AI 即可生成首轮候选审阅单）。</div>'
        )
    )
    all_jobs_html = (
        "\n".join(_render_job_card(j, highlight_icebreaker=(j.get("status") == "discovered")) for j in jobs)
        if jobs
        else (
            '<div class="empty-box">本地账本暂无岗位记录。回到桌面 AI 启动首轮全自动搜岗或开启定时巡检后自动入库生成。</div>'
            if is_auto_discover
            else '<div class="empty-box">本地账本暂无岗位记录。回到桌面 AI 发送「帮我在 BOSS 跑第一轮搜岗深筛预览」后自动入库生成。</div>'
        )
    )

    # 筛选胶囊
    filter_defs = [
        ("all", "全部", len(jobs)),
        ("handed_off", "待本人约面/决策", by_status.get("handed_off", 0)),
        ("interview_scheduled", "已约面/跟进中", by_status.get("interview_scheduled", 0)),
        ("discovered", "待确认候选", by_status.get("discovered", 0)),
        ("resume_delivered", "PDF 简历已送达", by_status.get("resume_delivered", 0)),
        ("resume_requested", "待同意请求", by_status.get("resume_requested", 0)),
        ("connected", "双方已建联", by_status.get("connected", 0)),
        ("opening_sent", "开场已送达", by_status.get("opening_sent", 0)),
        ("screened_skip", "不匹配跳过", by_status.get("screened_skip", 0)),
        ("closed_rejected", "不合适归档", by_status.get("closed_rejected", 0)),
    ]
    filter_pills_html = "\n            ".join(
        f'<button type="button" class="filter-pill{" active" if key == "all" else ""}" '
        f'data-filter="{key}">{_esc(label)} <span class="pill-count">{cnt}</span></button>'
        for key, label, cnt in filter_defs
    )

    # 策略与档案页
    sched = config.get("schedule") or {}
    windows_text = "、".join(sched.get("active_windows") or ["09:00-12:00", "14:00-18:00"])
    routes = config.get("resume_routes") or []
    default_badge = ' <span class="mini-tag">默认兜底</span>'
    routes_rows = "\n".join(
        f'<tr><td><strong>{_esc(r.get("name", ""))}</strong>'
        f'{default_badge if r.get("is_default") else ""}</td>'
        f'<td>{_esc("、".join(r.get("keywords") or []) or "通用方向兜底")}</td></tr>'
        for r in routes
    )
    past_emps = config.get("past_employers") or []
    blocked_all = config.get("blocked_companies") or []
    exclude_kws = config.get("exclude_keywords") or []

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>求职助手 · 投递看板</title>
  <style>
    :root {{
      --bg-page: #f4f6f9;
      --bg-card: #ffffff;
      --bg-subtle: #f8fafc;
      --border: #e2e8f0;
      --border-strong: #cbd5e1;
      --text-main: #0f172a;
      --text-secondary: #475569;
      --text-muted: #64748b;
      --primary: #1d4ed8;
      --primary-soft: #eff6ff;
      --accent-amber: #b45309;
      --accent-amber-bg: #fffbeb;
      --accent-amber-border: #f59e0b;
      --success: #047857;
      --success-bg: #ecfdf5;
      --danger: #b91c1c;
      --danger-bg: #fef2f2;
      --purple: #6d28d9;
      --purple-bg: #f5f3ff;
      --radius-lg: 12px;
      --radius-md: 8px;
      --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.05);
    }}

    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}

    body {{
      font-family: "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg-page);
      color: var(--text-main);
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
    }}

    .container {{
      max-width: 1360px;
      margin: 0 auto;
      padding: 24px 24px 56px;
    }}

    /* 顶部栏 */
    .topbar {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 20px 24px;
      box-shadow: var(--shadow-sm);
      margin-bottom: 18px;
    }}

    .topbar-head {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      flex-wrap: wrap;
    }}

    .brand-title {{
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.01em;
      color: var(--text-main);
    }}

    .brand-subtitle {{
      margin-top: 6px;
      font-size: 13.5px;
      color: var(--text-secondary);
      display: flex;
      flex-wrap: wrap;
      gap: 8px 16px;
      align-items: center;
    }}

    .header-badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}

    .header-pill {{
      display: inline-flex;
      align-items: center;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12.5px;
      font-weight: 600;
      white-space: nowrap;
    }}

    .badge-mode {{
      background: #f1f5f9;
      color: #1e293b;
      border: 1px solid var(--border);
    }}

    .badge-pro {{
      background: #dbeafe;
      color: #1e40af;
      border: 1px solid #93c5fd;
    }}

    .badge-free {{
      background: #f1f5f9;
      color: #475569;
      border: 1px solid var(--border-strong);
    }}

    .badge-demo {{
      background: var(--accent-amber-bg);
      color: var(--accent-amber);
      border: 1px solid #fcd34d;
    }}

    .badge-version {{
      background: #f8fafc;
      color: #64748b;
      border: 1px solid var(--border);
      font-variant-numeric: tabular-nums;
    }}

    .badge-update-ready {{
      background: #ecfdf5;
      color: #047857;
      border: 1px solid #6ee7b7;
      text-decoration: none;
    }}

    .badge-update-ready:hover {{
      background: #d1fae5;
    }}

    .release-update-banner {{
      background: linear-gradient(90deg, #ecfdf5 0%, #f0fdf4 55%, #ffffff 100%);
      border: 1px solid #86efac;
      border-radius: var(--radius-lg);
      padding: 14px 20px;
      margin-bottom: 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
      box-shadow: var(--shadow-sm);
    }}

    .release-update-banner.release-update-required {{
      background: linear-gradient(90deg, #fffbeb 0%, #fef3c7 45%, #ffffff 100%);
      border-color: #fbbf24;
    }}

    .release-update-left {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 0;
      flex: 1 1 420px;
    }}

    .release-update-head {{
      display: flex;
      align-items: center;
      gap: 8px 10px;
      flex-wrap: wrap;
    }}

    .release-update-tag {{
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 999px;
      background: #059669;
      color: #ffffff;
      font-size: 11.5px;
      font-weight: 700;
    }}

    .release-update-required .release-update-tag {{
      background: #d97706;
    }}

    .release-update-title {{
      font-size: 14.5px;
      color: #064e3b;
    }}

    .release-update-required .release-update-title {{
      color: #78350f;
    }}

    .release-update-meta {{
      font-size: 12.5px;
      color: var(--text-secondary);
    }}

    .release-update-highlights {{
      display: flex;
      flex-wrap: wrap;
      gap: 4px 16px;
      font-size: 13px;
      color: #1e293b;
    }}

    .release-update-right {{
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 5px;
      flex-shrink: 0;
    }}

    .release-copy-btn {{
      appearance: none;
      border: 1px solid #059669;
      background: #059669;
      color: #ffffff;
      font-size: 13px;
      font-weight: 600;
      padding: 7px 14px;
      border-radius: var(--radius-md);
      cursor: pointer;
      transition: background 0.15s ease;
    }}

    .release-copy-btn:hover {{
      background: #047857;
    }}

    .release-cmd-hint {{
      font-size: 12px;
      color: var(--text-muted);
    }}

    .release-cmd-hint code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      background: rgba(15, 23, 42, 0.06);
      padding: 1px 5px;
      border-radius: 4px;
      color: #0f172a;
    }}

    .snapshot-time {{
      font-size: 12.5px;
      color: var(--text-muted);
      white-space: nowrap;
    }}

    /* 6 格核心漏斗卡 */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }}

    .kpi-card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 16px;
      box-shadow: var(--shadow-sm);
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-width: 0;
    }}

    .kpi-card.kpi-highlight {{
      background: linear-gradient(180deg, #fffbeb 0%, #ffffff 100%);
      border-color: var(--accent-amber-border);
    }}

    .kpi-label {{
      font-size: 13px;
      font-weight: 600;
      color: var(--text-secondary);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .kpi-highlight .kpi-label {{
      color: var(--accent-amber);
    }}

    .kpi-value {{
      font-size: 28px;
      font-weight: 700;
      color: var(--text-main);
      margin: 6px 0 4px;
      font-variant-numeric: tabular-nums;
    }}

    .kpi-highlight .kpi-value {{
      color: #b45309;
    }}

    .kpi-sub {{
      font-size: 12px;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .kpi-sub strong {{
      color: var(--primary);
      font-weight: 600;
    }}

    /* 主工作区导航栏（隐藏移动端丑陋滚动条） */
    .tabs-bar,
    .filter-scroll {{
      display: flex;
      gap: 8px;
      overflow-x: auto;
      scrollbar-width: none;
      -ms-overflow-style: none;
    }}

    .tabs-bar::-webkit-scrollbar,
    .filter-scroll::-webkit-scrollbar {{
      display: none;
    }}

    .tabs-bar {{
      flex-wrap: wrap;
      overflow-x: visible;
      margin-bottom: 16px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 10px;
    }}

    .tab-btn {{
      appearance: none;
      border: 1px solid transparent;
      background: transparent;
      color: var(--text-secondary);
      font-size: 14px;
      font-weight: 600;
      padding: 8px 14px;
      border-radius: var(--radius-md);
      cursor: pointer;
      white-space: nowrap;
      flex-shrink: 0;
      transition: all 0.15s ease;
    }}

    .tab-btn:hover {{
      background: #e2e8f0;
      color: var(--text-main);
    }}

    .tab-btn.active {{
      background: var(--text-main);
      color: #ffffff;
    }}

    .tab-count {{
      display: inline-block;
      margin-left: 5px;
      padding: 1px 7px;
      border-radius: 999px;
      font-size: 12px;
      background: rgba(100, 116, 139, 0.16);
    }}

    .tab-btn.active .tab-count {{
      background: rgba(255, 255, 255, 0.22);
      color: #ffffff;
    }}

    .tab-panel {{
      display: none;
    }}

    .tab-panel.active {{
      display: block;
    }}

    /* 队列分组标题 */
    .section-block {{
      margin-bottom: 24px;
    }}

    .section-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
    }}

    .section-title {{
      font-size: 15px;
      font-weight: 700;
      color: var(--text-main);
    }}

    .section-hint {{
      font-size: 12.5px;
      color: var(--text-muted);
    }}

    /* 账本搜索与筛选工具栏 */
    .toolbar {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 14px 16px;
      margin-bottom: 14px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}

    .search-row {{
      display: flex;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
    }}

    .search-input {{
      flex: 1 1 280px;
      padding: 9px 12px;
      border: 1px solid var(--border-strong);
      border-radius: var(--radius-md);
      font-size: 13.5px;
      color: var(--text-main);
      background: var(--bg-subtle);
      outline: none;
    }}

    .search-input:focus {{
      border-color: var(--primary);
      background: #ffffff;
    }}

    .visible-counter {{
      font-size: 12.5px;
      color: var(--text-muted);
      white-space: nowrap;
    }}

    .filter-pill {{
      appearance: none;
      border: 1px solid var(--border);
      background: var(--bg-subtle);
      color: var(--text-secondary);
      padding: 5px 11px;
      border-radius: 999px;
      font-size: 12.5px;
      font-weight: 500;
      cursor: pointer;
      white-space: nowrap;
      flex-shrink: 0;
    }}

    .filter-pill.active {{
      background: var(--primary-soft);
      border-color: var(--primary);
      color: var(--primary);
      font-weight: 600;
    }}

    .pill-count {{
      font-variant-numeric: tabular-nums;
      opacity: 0.85;
    }}

    /* 岗位卡片 */
    .cards-stack {{
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}

    .job-card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-left: 4px solid var(--border-strong);
      border-radius: var(--radius-lg);
      padding: 16px 18px;
      box-shadow: var(--shadow-sm);
    }}

    .status-border-handed_off {{ border-left-color: #d97706; }}
    .status-border-discovered {{ border-left-color: #2563eb; }}
    .status-border-resume_delivered {{ border-left-color: #059669; }}
    .status-border-resume_requested {{ border-left-color: #7c3aed; }}
    .status-border-connected {{ border-left-color: #0284c7; }}
    .status-border-opening_sent {{ border-left-color: #64748b; }}
    .status-border-screened_skip {{ border-left-color: #dc2626; }}
    .status-border-closed_rejected {{ border-left-color: #94a3b8; }}

    .job-card-top {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      flex-wrap: wrap;
    }}

    .job-main-title {{
      min-width: 0;
      flex: 1 1 300px;
    }}

    .title-salary-line {{
      display: flex;
      align-items: baseline;
      gap: 10px;
      flex-wrap: wrap;
    }}

    .job-title {{
      font-size: 16px;
      font-weight: 700;
      color: var(--text-main);
    }}

    .job-salary {{
      font-size: 15px;
      font-weight: 700;
      color: #0f766e;
      white-space: nowrap;
    }}

    .company-line {{
      margin-top: 3px;
      font-size: 13.5px;
      color: var(--text-secondary);
      word-break: break-word;
    }}

    .job-right-meta {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}

    .status-badge {{
      padding: 3px 9px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      white-space: nowrap;
    }}

    .status-handed_off {{ background: var(--accent-amber-bg); color: var(--accent-amber); border: 1px solid #fcd34d; }}
    .status-interview_scheduled {{ background: #dcfce7; color: #166534; border: 1px solid #86efac; }}
    .status-discovered {{ background: var(--primary-soft); color: var(--primary); border: 1px solid #bfdbfe; }}
    .status-resume_delivered {{ background: var(--success-bg); color: var(--success); border: 1px solid #a7f3d0; }}
    .status-resume_requested {{ background: var(--purple-bg); color: var(--purple); border: 1px solid #ddd6fe; }}
    .status-connected {{ background: #e0f2fe; color: #0369a1; }}
    .status-opening_sent {{ background: #f1f5f9; color: #334155; }}
    .status-screened_skip {{ background: var(--danger-bg); color: var(--danger); }}
    .status-closed_rejected {{ background: #f1f5f9; color: #64748b; }}

    .recruiter-pill {{
      padding: 3px 9px;
      border-radius: 6px;
      font-size: 12px;
      background: #f8fafc;
      color: var(--text-secondary);
      border: 1px solid var(--border);
      white-space: nowrap;
    }}

    .recruiter-pill.decision-maker {{
      background: #eef2ff;
      color: #3730a3;
      border-color: #c7d2fe;
      font-weight: 600;
    }}

    .tag-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 10px 0;
    }}

    .tag {{
      font-size: 11.5px;
      padding: 2px 8px;
      border-radius: 4px;
      font-weight: 500;
      white-space: nowrap;
    }}

    .tag-channel {{ background: #f1f5f9; color: #475569; }}
    .tag-selfdev {{ background: #ecfdf5; color: #047857; }}
    .tag-info {{ background: #eff6ff; color: #1d4ed8; }}
    .tag-warn {{ background: #fffbeb; color: #b45309; }}
    .tag-danger {{ background: #fef2f2; color: #b91c1c; }}

    .job-details-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 6px;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 10px 12px;
      font-size: 13px;
    }}

    .detail-row {{
      display: flex;
      gap: 10px;
      align-items: baseline;
    }}

    .detail-label {{
      flex: 0 0 74px;
      font-weight: 600;
      color: var(--text-muted);
      font-size: 12px;
    }}

    .detail-val {{
      flex: 1 1 auto;
      color: var(--text-main);
      word-break: break-word;
      overflow-wrap: anywhere;
    }}

    .icebreaker-box .detail-val {{
      color: #1e3a8a;
    }}

    .note-row .detail-val {{
      color: #334155;
      font-weight: 500;
    }}

    .job-card-footer {{
      margin-top: 10px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      font-size: 12.5px;
      color: var(--text-muted);
    }}

    .footer-left {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px 16px;
    }}

    .resume-tag {{
      color: var(--text-secondary);
      font-weight: 500;
    }}

    .job-link {{
      color: var(--primary);
      text-decoration: none;
      font-weight: 600;
      white-space: nowrap;
    }}

    .job-link:hover {{
      text-decoration: underline;
    }}

    .job-link.muted {{
      color: var(--text-muted);
      font-weight: 400;
    }}

    /* 策略与档案页 */
    .strategy-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}

    .strategy-card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 18px;
      box-shadow: var(--shadow-sm);
    }}

    .strategy-card.full-span {{
      grid-column: 1 / -1;
    }}

    .strategy-card h3 {{
      font-size: 15px;
      font-weight: 700;
      margin-bottom: 12px;
      color: var(--text-main);
    }}

    .kv-list {{
      display: grid;
      gap: 8px;
      font-size: 13.5px;
    }}

    .kv-item {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px dashed var(--border);
      padding-bottom: 6px;
    }}

    .kv-item:last-child {{
      border-bottom: none;
      padding-bottom: 0;
    }}

    .kv-key {{
      color: var(--text-muted);
      flex-shrink: 0;
    }}

    .kv-val {{
      color: var(--text-main);
      font-weight: 500;
      text-align: right;
      word-break: break-word;
    }}

    .route-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}

    .route-table th,
    .route-table td {{
      text-align: left;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
    }}

    .route-table th {{
      color: var(--text-muted);
      font-weight: 600;
      background: var(--bg-subtle);
    }}

    .mini-tag {{
      display: inline-block;
      padding: 1px 6px;
      font-size: 11px;
      border-radius: 4px;
      background: var(--primary-soft);
      color: var(--primary);
      margin-left: 4px;
    }}

    .profile-pre {{
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 13px;
      color: var(--text-secondary);
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 12px 14px;
      max-height: 320px;
      overflow-y: auto;
    }}

    .empty-box {{
      background: var(--bg-card);
      border: 1px dashed var(--border-strong);
      border-radius: var(--radius-lg);
      padding: 28px 18px;
      text-align: center;
      color: var(--text-muted);
      font-size: 13.5px;
    }}

    .empty-box.empty-locked {{
      background: #f8fafc;
      border-color: #cbd5e1;
      color: #475569;
      line-height: 1.7;
    }}

    .empty-box.empty-locked strong {{
      color: #1e293b;
      font-size: 14px;
    }}

    /* 免费版升级说明栏 */
    .upgrade-banner {{
      background: linear-gradient(90deg, #eff6ff 0%, #f8fafc 100%);
      border: 1px solid #bfdbfe;
      border-left: 4px solid #2563eb;
      border-radius: var(--radius-lg);
      padding: 14px 18px;
      margin-bottom: 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
    }}

    .upgrade-left {{
      flex: 1 1 560px;
      min-width: 0;
    }}

    .upgrade-title-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 6px;
    }}

    .upgrade-tag {{
      background: #1d4ed8;
      color: #ffffff;
      font-size: 11.5px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 999px;
      white-space: nowrap;
    }}

    .upgrade-headline {{
      font-size: 13.5px;
      font-weight: 600;
      color: #0f172a;
    }}

    .upgrade-features {{
      font-size: 12.5px;
      color: #334155;
      display: flex;
      flex-wrap: wrap;
      gap: 4px 12px;
      line-height: 1.5;
    }}

    .upgrade-features strong {{
      color: #1e40af;
    }}

    .upgrade-right {{
      background: #ffffff;
      border: 1px solid #dbeafe;
      border-radius: var(--radius-md);
      padding: 10px 14px;
      flex: 0 1 auto;
      display: flex;
      flex-direction: column;
      gap: 8px;
      align-items: flex-start;
    }}

    .upgrade-btn-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}

    .upgrade-cta-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 8px 16px;
      border-radius: var(--radius-md);
      background: #2563eb;
      color: #ffffff;
      font-size: 13.5px;
      font-weight: 700;
      text-decoration: none;
      box-shadow: 0 2px 6px rgba(37, 99, 235, 0.22);
      transition: background 0.15s ease, transform 0.12s ease;
    }}

    .upgrade-cta-btn:hover {{
      background: #1d4ed8;
      transform: translateY(-1px);
    }}

    .upgrade-secondary-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 7px 12px;
      border-radius: var(--radius-md);
      background: #f8fafc;
      border: 1px solid #cbd5e1;
      color: #334155;
      font-size: 12.5px;
      font-weight: 600;
      text-decoration: none;
    }}

    .upgrade-secondary-btn:hover {{
      border-color: #2563eb;
      color: #1d4ed8;
      background: #ffffff;
    }}

    .badge-upgrade-cta {{
      background: #2563eb;
      color: #ffffff;
      border: 1px solid #1d4ed8;
      text-decoration: none;
      transition: background 0.15s ease;
    }}

    .badge-upgrade-cta:hover {{
      background: #1d4ed8;
    }}

    .upgrade-cmd-label {{
      font-size: 11.5px;
      color: #64748b;
      line-height: 1.5;
    }}

    .inline-copy-btn {{
      appearance: none;
      border: 1px solid #bfdbfe;
      background: #eff6ff;
      color: #1d4ed8;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
      font-weight: 600;
      padding: 1px 7px;
      border-radius: 5px;
      cursor: pointer;
    }}

    .inline-copy-btn:hover {{
      background: #dbeafe;
    }}

    .inline-pro-link {{
      display: inline-block;
      margin-top: 8px;
      color: #1d4ed8;
      font-weight: 700;
      text-decoration: none;
    }}

    .inline-pro-link:hover {{
      text-decoration: underline;
    }}

    .launchpad-card {{
      background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
      border: 1.5px solid #93c5fd;
      border-radius: var(--radius-lg);
      padding: 20px 22px;
      margin-bottom: 20px;
      box-shadow: 0 6px 20px rgba(37, 99, 235, 0.06);
    }}

    .launchpad-badge {{
      display: inline-block;
      background: #dbeafe;
      color: #1e40af;
      font-size: 12px;
      font-weight: 700;
      padding: 3px 10px;
      border-radius: 999px;
      margin-bottom: 8px;
    }}

    .launchpad-title {{
      font-size: 18px;
      font-weight: 800;
      color: var(--text-main);
      margin-bottom: 4px;
    }}

    .launchpad-sub {{
      font-size: 13.5px;
      color: var(--text-secondary);
      margin-bottom: 14px;
    }}

    .launchpad-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}

    .launchpad-step {{
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 10px;
    }}

    .launchpad-step-title {{
      font-size: 14px;
      font-weight: 700;
      color: var(--text-main);
    }}

    .launchpad-step-desc {{
      font-size: 12.5px;
      color: var(--text-secondary);
      line-height: 1.55;
    }}

    .launchpad-cmd-bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      border-radius: 7px;
      padding: 8px 10px;
      flex-wrap: wrap;
    }}

    .launchpad-cmd-bar code {{
      font-size: 12px;
      color: #1e293b;
      flex: 1 1 220px;
      word-break: break-all;
    }}

    .copy-action-btn {{
      appearance: none;
      border: 1px solid #1d4ed8;
      background: #2563eb;
      color: #ffffff;
      font-size: 12.5px;
      font-weight: 700;
      padding: 6px 12px;
      border-radius: 6px;
      cursor: pointer;
      white-space: nowrap;
    }}

    .copy-action-btn.secondary {{
      background: #ffffff;
      color: #1d4ed8;
    }}

    .copy-action-btn:hover {{
      opacity: 0.92;
    }}

    .lock-pill {{
      display: inline-block;
      padding: 1px 8px;
      font-size: 11px;
      font-weight: 600;
      border-radius: 999px;
      background: #fef3c7;
      color: #92400e;
      border: 1px solid #fde68a;
      margin-left: 6px;
      text-decoration: none;
    }}

    a.lock-pill:hover {{
      background: #fde68a;
    }}

    .pro-active-pill {{
      display: inline-block;
      padding: 1px 7px;
      font-size: 11px;
      font-weight: 600;
      border-radius: 999px;
      background: #dcfce7;
      color: #166534;
      border: 1px solid #bbf7d0;
      margin-left: 6px;
    }}

    /* 响应式断点 */
    @media (max-width: 1120px) {{
      .kpi-grid {{
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }}
    }}

    @media (max-width: 768px) {{
      .container {{
        padding: 14px 12px 40px;
      }}
      .topbar {{
        padding: 16px;
      }}
      .brand-title {{
        font-size: 19px;
      }}
      .kpi-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
      }}
      .kpi-card {{
        padding: 13px;
      }}
      .kpi-value {{
        font-size: 24px;
      }}
      .strategy-grid {{
        grid-template-columns: 1fr;
      }}
      .launchpad-grid {{
        grid-template-columns: 1fr;
      }}
      .release-update-right {{
        align-items: flex-start;
      }}
      .tab-btn {{
        padding: 7px 11px;
        font-size: 13px;
      }}
      .filter-scroll {{
        flex-wrap: wrap;
        overflow-x: visible;
      }}
      .detail-row {{
        flex-direction: column;
        gap: 2px;
      }}
      .detail-label {{
        flex: none;
      }}
      .kv-item {{
        flex-direction: column;
        gap: 2px;
      }}
      .kv-val {{
        text-align: left;
      }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <header class="topbar">
      <div class="topbar-head">
        <div>
          <h1 class="brand-title">求职助手 · 投递看板</h1>
          <div class="brand-subtitle">
            <span><strong>求职方向：</strong>{_esc(roles_str)}</span>
            <span><strong>目标城市：</strong>{_esc(cities_str)}</span>
            <span class="snapshot-time">快照更新：{_esc(snap)}</span>
          </div>
        </div>
        <div class="header-badges">
          {demo_banner}
          {version_pill_html}
          <span class="header-pill badge-mode">{_esc(mode_badge)}</span>
          <span class="header-pill {tier_cls}">{_esc(tier_badge)}</span>
          {top_upgrade_pill}
        </div>
      </div>
    </header>
    {release_update_banner_html}
    {upgrade_banner_html}

    <section class="kpi-grid" aria-label="求职转化核心漏斗">
      <div class="kpi-card">
        <span class="kpi-label">已评估岗位</span>
        <div class="kpi-value">{int(funnel.get("screened_total", 0))}</div>
        <span class="kpi-sub">逐岗比对入库总数</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">不匹配跳过</span>
        <div class="kpi-value">{int(funnel.get("screened_skip", 0))}</div>
        <span class="kpi-sub">{_esc(_skip_kpi_sub(config))}</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">开场已送达</span>
        <div class="kpi-value">{int(funnel.get("opening_delivered_total", 0))}</div>
        <span class="kpi-sub">单向待回 {int(funnel.get("opening_waiting_reply", 0))}</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">双方已建联</span>
        <div class="kpi-value">{int(funnel.get("connected_total", 0))}</div>
        <span class="kpi-sub">{"已获招聘者实质回复" if is_pro else "需手动跟进或升 Pro 巡检"}</span>
      </div>
      <div class="kpi-card">
        <span class="kpi-label">PDF 简历已送达</span>
        <div class="kpi-value">{int(funnel.get("pdf_delivered_total", 0))}</div>
        <span class="kpi-sub">{"待同意请求 <strong>" + str(int(funnel.get("resume_requested_pending", 0))) + "</strong>" if is_pro else "多版自动匹配属 Pro 能力"}</span>
      </div>
      <div class="kpi-card kpi-highlight">
        <span class="kpi-label">待本人约面/决策</span>
        <div class="kpi-value">{int(funnel.get("handed_off_pending", 0))}</div>
        <span class="kpi-sub">{f"冷却/超额暂存候选 {int(funnel.get('discovered_pending', 0))}" if is_auto_discover else f"待确认破冰候选 {int(funnel.get('discovered_pending', 0))}"}</span>
      </div>
    </section>

    <nav class="tabs-bar" role="tablist">
      <button type="button" class="tab-btn active" data-tab-target="queue">
        待处理/待确认队列 <span class="tab-count">{queue_count}</span>
      </button>
      <button type="button" class="tab-btn" data-tab-target="ledger">
        全量岗位账本 <span class="tab-count">{len(jobs)}</span>
      </button>
      <button type="button" class="tab-btn" data-tab-target="strategy">
        当前求职策略与档案摘要
      </button>
    </nav>

    <main>
      <section id="tab-queue" class="tab-panel active">
        {onboarding_launchpad_html}
        <div class="section-block">
          <div class="section-header">
            <h2 class="section-title">待本人约面 / 谈薪 / 决策接管（{len(handed_off_jobs)}）</h2>
            <span class="section-hint">{"触发面试时间、联系方式、底薪或超纲问题红线，已暂停自动回复" if is_pro else "Pro 自动消息巡检红线保护队列"}</span>
          </div>
          <div class="cards-stack">
            {handed_off_html}
          </div>
        </div>

        <div class="section-block">
          <div class="section-header">
            <h2 class="section-title">{f"全自动找岗已开启 · 冷却/超额暂存候选（{len(discovered_jobs)}）" if is_auto_discover else f"半自动待确认破冰候选（{len(discovered_jobs)}）"}</h2>
            <span class="section-hint">{"强匹配岗位已在配额内直接自动发送破冰语；仅夜间宵禁、冷却期或满当日上限时暂存于此" if is_auto_discover else "已在详情页与公司主页完成初筛，在宿主对话中回复序号即可发起沟通"}</span>
          </div>
          <div class="cards-stack">
            {discovered_html}
          </div>
        </div>
      </section>

      <section id="tab-ledger" class="tab-panel">
        <div class="toolbar">
          <div class="search-row">
            <input
              id="ledger-search"
              type="search"
              class="search-input"
              placeholder="搜索公司名、岗位、招聘者头衔、匹配点、风险标签或简历名..."
            />
            <span id="visible-count" class="visible-counter">当前显示 {len(jobs)} / {len(jobs)} 条</span>
          </div>
          <div class="filter-scroll" id="status-filters">
            {filter_pills_html}
          </div>
        </div>
        <div class="cards-stack" id="ledger-list">
          {all_jobs_html}
        </div>
      </section>

      <section id="tab-strategy" class="tab-panel">
        <div class="strategy-grid">
          <div class="strategy-card">
            <h3>自动化档位与作息护栏 {"<span class='pro-active-pill'>Pro 托管已生效</span>" if is_pro else f"<a class='lock-pill' href='{pro_checkout_url}' target='_blank' rel='noopener noreferrer'>升级 Pro 解锁 →</a>"}</h3>
            <div class="kv-list">
              <div class="kv-item"><span class="kv-key">交互运行模式</span><span class="kv-val">{"副驾驶粘贴协同（无浏览器操控权限兜底）" if str(config.get("execution_mode", "browser")) == "copilot" else ("浏览器直连自动化 · ego lite（省 Token 持久化窗口）" if str(config.get("browser_provider", "auto")) == "ego_lite" else ("浏览器直连自动化 · 宿主自带 Browser Use（有头持久化）" if str(config.get("browser_provider", "auto")) == "builtin_browser_use" else "浏览器直连自动化（复用已登录窗口）"))}</span></div>
              <div class="kv-item"><span class="kv-key">简历底座形态</span><span class="kv-val">{"暂无简历（请先准备本地简历或填写平台在线简历）" if str(config.get("resume_source", "")) == "none" else ("待导入简历（请拖入 PDF 或授权读取平台在线简历）" if str(config.get("resume_source", "")) == "pending" else ("仅平台在线简历（建议在 BOSS 在线简历页生成附件 PDF）" if (str(config.get("resume_source", "")) == "online_only" or not bool(config.get("has_attachment_resume", True))) else "平台在线 + PDF 附件简历齐全"))}</span></div>
              <div class="kv-item"><span class="kv-key">主动找岗模式</span><span class="kv-val">{_esc("全自动定额破冰（已开启）" if str(config.get("mode_discover", "semi")) == "auto" else "半自动审阅确认") if is_pro else "半自动确认（全自动破冰需 Pro）"}</span></div>
              <div class="kv-item"><span class="kv-key">消息答疑模式</span><span class="kv-val">{_esc("全自动守卫答疑（已开启）" if str(config.get("mode_reply", "semi")) == "auto" else "半自动草稿确认") if is_pro else "未启用（Pro 解锁半自动/全自动答疑）"}</span></div>
              <div class="kv-item"><span class="kv-key">自动巡检时间窗</span><span class="kv-val">{(_esc("仅工作日 " if sched.get("weekdays_only", True) else "每日 ") + _esc(windows_text)) if is_pro else "未启用（Pro 解锁工作时间自动巡检）"}</span></div>
              <div class="kv-item"><span class="kv-key">轮次最小间隔</span><span class="kv-val">{f"{int(sched.get('min_interval_minutes', 90))} 分钟" if is_pro else "手动触发单轮"}</span></div>
              <div class="kv-item"><span class="kv-key">单轮 / 单日沟通上限</span><span class="kv-val">每轮 {int(sched.get("max_per_round", 5))} 岗 / 每日 {int(sched.get("max_per_day", 20))} 岗</span></div>
              <div class="kv-item"><span class="kv-key">Pro 权益模式</span><span class="kv-val">{"买断版生效中：长期有效，持续享受常规功能更新与精准匹配词库升级" if is_pro else f"<a class='inline-pro-link' style='margin-top:0;' href='{pro_checkout_url}' target='_blank' rel='noopener noreferrer'>特惠 ¥69（原价 ¥99）一次买断解锁全自动托管与精准匹配词库更新 →</a>"}</span></div>
            </div>
          </div>

          <div class="strategy-card">
            <h3>精准匹配与渠道过滤策略</h3>
            <div class="kv-list">
              <div class="kv-item"><span class="kv-key">期望最低月薪</span><span class="kv-val">{int(config.get("min_salary_k", 0))}K{("（容忍面板标薪略低）" if config.get("salary_panel_tolerance", True) else "（严格卡线）")}</span></div>
              <div class="kv-item"><span class="kv-key">外包 / 派遣 / OD 策略</span><span class="kv-val">{_esc(_outsourcing_cn(config, is_pro=is_pro))}</span></div>
              <div class="kv-item"><span class="kv-key">驻场 / 外派办公策略</span><span class="kv-val">{_esc(_onsite_cn(config))}</span></div>
              <div class="kv-item"><span class="kv-key">猎头渠道策略</span><span class="kv-val">{_esc(_headhunter_cn(str(config.get("headhunter_policy", "inbound_only")))) if is_pro else "主动检索跳过猎头（来信非对称核验需 Pro）"}</span></div>
              <div class="kv-item"><span class="kv-key">已自动保护前东家</span><span class="kv-val">{len(past_emps)} 家（{_esc("、".join(past_emps) or "未配置")}）</span></div>
              <div class="kv-item"><span class="kv-key">屏蔽企业黑名单总数</span><span class="kv-val">{len(blocked_all)} 家（排除词：{_esc("、".join(exclude_kws) or "无")}）</span></div>
            </div>
          </div>

          <div class="strategy-card full-span">
            <h3>多版本平台 PDF 简历路由映射（被动索要策略：{_esc(_inbound_resume_cn(str(config.get("inbound_resume_policy", "generous"))))}）{"<span class='pro-active-pill'>Pro 自动路由中</span>" if is_pro else f"<a class='lock-pill' href='{pro_checkout_url}' target='_blank' rel='noopener noreferrer'>Pro 解锁自动匹配发送 →</a>"}</h3>
            <table class="route-table">
              <thead>
                <tr>
                  <th>平台附件简历名称</th>
                  <th>自动匹配岗位方向关键词</th>
                </tr>
              </thead>
              <tbody>
                {routes_rows}
              </tbody>
            </table>
          </div>

          <div class="strategy-card full-span">
            <h3>个人求职事实与答法档案摘要（仅存本机）</h3>
            <pre class="profile-pre">{_esc(profile_excerpt or "尚未填写个人求职事实档案。")}</pre>
          </div>
        </div>
      </section>
    </main>
  </div>

  <script>
    (function () {{
      const tabButtons = document.querySelectorAll("[data-tab-target]");
      const panels = {{
        queue: document.getElementById("tab-queue"),
        ledger: document.getElementById("tab-ledger"),
        strategy: document.getElementById("tab-strategy")
      }};

      tabButtons.forEach(function (btn) {{
        btn.addEventListener("click", function () {{
          const target = btn.getAttribute("data-tab-target");
          tabButtons.forEach(function (b) {{ b.classList.remove("active"); }});
          btn.classList.add("active");
          Object.keys(panels).forEach(function (k) {{
            if (panels[k]) {{
              panels[k].classList.toggle("active", k === target);
            }}
          }});
        }});
      }});

      const searchInput = document.getElementById("ledger-search");
      const filterButtons = document.querySelectorAll("#status-filters [data-filter]");
      const ledgerCards = document.querySelectorAll("#ledger-list .job-card");
      const visibleCountEl = document.getElementById("visible-count");
      let activeStatus = "all";

      function applyLedgerFilter() {{
        const q = (searchInput ? searchInput.value : "").trim().toLowerCase();
        let shown = 0;
        ledgerCards.forEach(function (card) {{
          const st = card.getAttribute("data-status") || "";
          const hay = card.getAttribute("data-search") || "";
          const matchStatus = activeStatus === "all" || st === activeStatus;
          const matchText = !q || hay.indexOf(q) !== -1;
          const visible = matchStatus && matchText;
          card.style.display = visible ? "" : "none";
          if (visible) shown += 1;
        }});
        if (visibleCountEl) {{
          visibleCountEl.textContent = "当前显示 " + shown + " / " + ledgerCards.length + " 条";
        }}
      }}

      if (searchInput) {{
        searchInput.addEventListener("input", applyLedgerFilter);
      }}

      filterButtons.forEach(function (pill) {{
        pill.addEventListener("click", function () {{
          activeStatus = pill.getAttribute("data-filter") || "all";
          filterButtons.forEach(function (p) {{ p.classList.remove("active"); }});
          pill.classList.add("active");
          applyLedgerFilter();
        }});
      }});

      const copyButtons = document.querySelectorAll("[data-copy]");
      copyButtons.forEach(function (btn) {{
        btn.addEventListener("click", function () {{
          const text = btn.getAttribute("data-copy") || "";
          if (!text) return;
          const orig = btn.getAttribute("data-orig-label") || btn.textContent;
          btn.setAttribute("data-orig-label", orig);
          const doneLabel = btn.getAttribute("data-done-label") || "✓ 已复制";
          function markCopied() {{
            btn.textContent = doneLabel;
            setTimeout(function () {{ btn.textContent = orig; }}, 2200);
          }}
          if (navigator.clipboard && navigator.clipboard.writeText) {{
            navigator.clipboard.writeText(text).then(markCopied).catch(function () {{
              const ta = document.createElement("textarea");
              ta.value = text;
              document.body.appendChild(ta);
              ta.select();
              try {{ document.execCommand("copy"); markCopied(); }} catch (e) {{}}
              document.body.removeChild(ta);
            }});
          }} else {{
            const ta = document.createElement("textarea");
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            try {{ document.execCommand("copy"); markCopied(); }} catch (e) {{}}
            document.body.removeChild(ta);
          }}
        }});
      }});
    }})();
  </script>
</body>
</html>
"""


def build_dashboard_file(
    *,
    workspace: Path,
    out_path: Path | None = None,
    demo: bool = False,
    tier: str = "free",
    demo_tier: str = "pro",
    pro_meta: dict[str, Any] | None = None,
    update_notice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从本地工作区（或虚构示例数据集）构建只读静态 HTML 看板。"""
    if demo:
        effective_tier = "free" if demo_tier == "free" else "pro"
        cfg, jobs, summary, profile_md = build_demo_dataset(demo_tier=effective_tier)
        default_filename = "demo-free-dashboard.html" if effective_tier == "free" else "demo-dashboard.html"
        resolved_out = (
            out_path.expanduser().resolve()
            if out_path
            else (Path(__file__).resolve().parent.parent / "dist" / default_filename)
        )
        effective_meta = {"email": "linyuan@example.com", "lifetime": True, "days_left": None} if effective_tier == "pro" else {}
    else:
        cfg = load_config(workspace)
        ledger = JobLedger(workspace)
        jobs = ledger.list_jobs(limit=2000)
        summary = ledger.summary()
        pf = profile_facts_path(workspace)
        profile_md = pf.read_text(encoding="utf-8")[:1500] if pf.is_file() else ""
        resolved_out = (
            out_path.expanduser().resolve()
            if out_path
            else (workspace / "dashboard.html").resolve()
        )
        effective_tier = tier
        effective_meta = pro_meta or {}

    html_content = render_dashboard_html(
        config=cfg,
        jobs=jobs,
        summary=summary,
        profile_excerpt=profile_md,
        tier=effective_tier,
        pro_meta=effective_meta,
        is_demo=demo,
        update_notice=update_notice,
    )
    resolved_out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(resolved_out, html_content)
    return {
        "ok": True,
        "demo": demo,
        "out_path": str(resolved_out),
        "jobs_count": len(jobs),
        "tier": effective_tier,
    }
