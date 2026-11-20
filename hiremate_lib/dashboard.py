"""本地只读高信息密度静态投递看板构建器（零外部 CDN、支持桌面与移动端自适应）。"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .ledger import (
    INTERVIEW_OUTCOME_LABELS,
    INTERVIEW_STAGE_STATUSES,
    STATUS_LABELS,
    VALID_STATUSES,
    JobLedger,
)
from .workspace import (
    DEFAULT_API_BASE,
    atomic_write_text,
    get_effective_api_base,
    load_config,
    profile_facts_path,
    salary_mode_short_cn,
)

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
    if mode == "auto":
        return "全自动"
    if mode == "favorite":
        return "仅自动收藏"
    return "半自动"


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
                "favorited_pending": 0,
                "favorited_total": 0,
                "opening_delivered_total": 1,
                "opening_waiting_reply": 1,
                "connected_total": 0,
                "connected_active": 0,
                "resume_requested_pending": 0,
                "pdf_delivered_total": 0,
                "handed_off_pending": 0,
                "interview_scheduled": 0,
                "interviewing": 0,
                "interview_active_total": 0,
                "interview_total": 0,
                "upcoming_interviews_count": 0,
                "offer_pending": 0,
                "offer_accepted": 0,
                "offer_count": 0,
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
        {
            "job_id": "demo_07",
            "company": "极光引擎",
            "company_scale": "C轮 · 500-1000人",
            "business_nature": "自研产品",
            "title": "高级 Agent 应用产品经理",
            "salary": "38-55K·16薪",
            "city": "深圳·南山区",
            "recruiter_name": "周毅",
            "recruiter_title": "AI 产研总监",
            "channel": "outbound",
            "risk_tags": ["核心业务直招", "二面推进中"],
            "match_reasons": "企业级多智能体编排与评测体系经验高度对口，业务总监直接面试",
            "gaps": "二面预计深挖跨部门路线图规划与商业化 ROI 测算",
            "icebreaker": "",
            "status": "interviewing",
            "status_label": STATUS_LABELS["interviewing"],
            "resume_used": "林远-AI产品经理.pdf",
            "job_url": "https://www.zhipin.com/job_detail/demo_07.html",
            "note": "一面已通过，产研 VP 二面约在周三 14:00 腾讯会议",
            "opening_sent_at": "2026-11-10T10:00:00+08:00",
            "connected_at": "2026-11-10T11:20:00+08:00",
            "resume_requested_at": "2026-11-10T11:25:00+08:00",
            "resume_delivered_at": "2026-11-10T11:30:00+08:00",
            "handed_off_at": "2026-11-11T09:40:00+08:00",
            "interview_scheduled_at": "2026-11-11T15:00:00+08:00",
            "offer_at": None,
            "updated_at": "2026-11-15 11:30",
            "interviews": [
                {
                    "round_id": "iv_demo_01",
                    "job_id": "demo_07",
                    "round_label": "一面",
                    "scheduled_at": "2026-11-13 15:00",
                    "mode": "腾讯会议",
                    "interviewer": "周毅 · AI 产研总监",
                    "outcome": "passed",
                    "outcome_label": INTERVIEW_OUTCOME_LABELS["passed"],
                    "summary": "深挖企业知识库 RAG 切片召回调优、离线评测集构建与 Badcase 闭环，面试官对落地指标认可度很高。",
                },
                {
                    "round_id": "iv_demo_02",
                    "job_id": "demo_07",
                    "round_label": "二面",
                    "scheduled_at": "2026-11-18 14:00",
                    "mode": "腾讯会议",
                    "interviewer": "产研 VP",
                    "outcome": "pending",
                    "outcome_label": INTERVIEW_OUTCOME_LABELS["pending"],
                    "summary": "重点准备复杂 B 端客户交付转标准产品的取舍案例与商业化 ROI 数据。",
                },
            ],
        },
        {
            "job_id": "demo_08",
            "company": "青禾数科",
            "company_scale": "D轮 · 1000-2000人",
            "business_nature": "自研产品",
            "title": "AI 平台产品专家",
            "salary": "36-48K·15薪",
            "city": "深圳·科技园",
            "recruiter_name": "林婉",
            "recruiter_title": "资深招聘经理",
            "channel": "inbound",
            "risk_tags": ["已获口头Offer", "谈薪决策中"],
            "match_reasons": "SaaS 审批流引擎与 AI 员工助手双背景匹配，团队处于核心产品增长期",
            "gaps": "需在周五前答复是否接受 Offer，待对比另一家二面结果",
            "icebreaker": "",
            "status": "offer",
            "status_label": STATUS_LABELS["offer"],
            "resume_used": "林远-AI产品经理.pdf",
            "job_url": "https://www.zhipin.com/job_detail/demo_08.html",
            "note": "三轮面试全过，HR 已沟通口头 Offer（42K·15薪），预计周五发正式邮件",
            "opening_sent_at": "2026-11-05T09:30:00+08:00",
            "connected_at": "2026-11-05T09:45:00+08:00",
            "resume_requested_at": "2026-11-05T10:00:00+08:00",
            "resume_delivered_at": "2026-11-05T10:05:00+08:00",
            "handed_off_at": "2026-11-06T11:00:00+08:00",
            "interview_scheduled_at": "2026-11-06T16:00:00+08:00",
            "offer_at": "2026-11-15T10:00:00+08:00",
            "updated_at": "2026-11-15 10:00",
            "interviews": [
                {
                    "round_id": "iv_demo_03",
                    "job_id": "demo_08",
                    "round_label": "一面",
                    "scheduled_at": "2026-11-08 14:30",
                    "mode": "腾讯会议",
                    "interviewer": "产品总监",
                    "outcome": "passed",
                    "outcome_label": INTERVIEW_OUTCOME_LABELS["passed"],
                    "summary": "聊了过往多租户权限体系与企业知识库问答架构，沟通顺畅。",
                },
                {
                    "round_id": "iv_demo_04",
                    "job_id": "demo_08",
                    "round_label": "终面",
                    "scheduled_at": "2026-11-12 16:00",
                    "mode": "现场面试 · 科技园",
                    "interviewer": "CTO & HRD",
                    "outcome": "passed",
                    "outcome_label": INTERVIEW_OUTCOME_LABELS["passed"],
                    "summary": "明确汇报线与 2027 年 AI 助手商业化目标，双方预期一致。",
                },
            ],
        },
    ]

    by_status = {st: 0 for st in VALID_STATUSES}
    for j in demo_jobs:
        by_status[j["status"]] += 1

    demo_summary = {
        "total_evaluated": len(demo_jobs),
        "by_status": by_status,
        "funnel": {
            "screened_total": len(demo_jobs),
            "screened_skip": 1,
            "discovered_pending": 1,
            "favorited_pending": 0,
            "favorited_total": 0,
            "opening_delivered_total": 6,
            "opening_waiting_reply": 1,
            "connected_total": 5,
            "connected_active": 0,
            "resume_requested_pending": 1,
            "pdf_delivered_total": 4,
            "handed_off_pending": 1,
            "interview_scheduled": 0,
            "interviewing": 1,
            "interview_active_total": 1,
            "interview_total": 2,
            "upcoming_interviews_count": 1,
            "offer_pending": 1,
            "offer_accepted": 0,
            "offer_count": 1,
            "closed_rejected": 0,
        },
    }

    demo_profile_md = (
        "【虚构示例档案：林远】5 年企业级 SaaS 与 AI 应用产品经验。"
        "主导过千万级文档企业知识库 RAG 检索重构（首屏答案采纳率由 41% 提至 73%）与多租户审批流引擎；"
        "明确无硬件嵌入式及 C 端游戏买量经验；前东家「示例科技」「远山数字」已自动同步至屏蔽黑名单。"
    )
    return demo_config, demo_jobs, demo_summary, demo_profile_md


def _render_interview_timeline(interviews: list[dict[str, Any]]) -> str:
    if not interviews:
        return ""
    items_html: list[str] = []
    for rd in interviews:
        r_label = _esc(rd.get("round_label") or "面试")
        oc = str(rd.get("outcome") or "pending")
        oc_label = _esc(rd.get("outcome_label") or INTERVIEW_OUTCOME_LABELS.get(oc, oc))
        sched = _esc(rd.get("scheduled_at") or "时间待定")
        mode = _esc(rd.get("mode") or "")
        interviewer = _esc(rd.get("interviewer") or "")
        summary = _esc(rd.get("summary") or "")
        meta_parts = [f"<span>🕒 {sched}</span>"]
        if mode:
            meta_parts.append(f"<span>📍 {mode}</span>")
        if interviewer:
            meta_parts.append(f"<span>👤 {interviewer}</span>")
        summary_html = f'<div class="iv-round-summary">{summary}</div>' if summary else ""
        items_html.append(
            f"""
              <div class="iv-round-item iv-outcome-border-{_esc(oc)}">
                <div class="iv-round-head">
                  <div class="iv-round-title-group">
                    <span class="iv-round-badge">{r_label}</span>
                    <span class="iv-outcome-pill iv-outcome-{_esc(oc)}">{oc_label}</span>
                  </div>
                  <div class="iv-round-meta">{"".join(meta_parts)}</div>
                </div>
                {summary_html}
              </div>"""
        )
    return f"""
          <div class="iv-timeline">
            <div class="iv-timeline-head">面试轮次记录（{len(interviews)} 轮）</div>
            <div class="iv-timeline-list">
              {"".join(items_html)}
            </div>
          </div>"""


def _render_upcoming_schedule(upcoming_items: list[dict[str, Any]]) -> str:
    if not upcoming_items:
        return (
            '<div class="empty-box">当前暂无待进行的面试日程。'
            'BOSS 聊天里敲定约面后助手会自动同步；后续二面、终面、面经复盘与 Offer 在对话里直接口述即可随时记入。</div>'
        )
    cards: list[str] = []
    for it in upcoming_items:
        sched = _esc(it.get("scheduled_at") or "时间待定")
        company = _esc(it.get("company") or "未知企业")
        title = _esc(it.get("title") or "未命名岗位")
        r_label = _esc(it.get("round_label") or "一面")
        mode = _esc(it.get("mode") or "方式待确认")
        interviewer = _esc(it.get("interviewer") or "")
        summary = _esc(it.get("summary") or "")
        iv_html = f' · 面试官：{interviewer}' if interviewer else ""
        prep_html = f'<div class="schedule-prep">备忘：{summary}</div>' if summary else ""
        cards.append(
            f"""
            <div class="schedule-item">
              <div class="schedule-time-pill">{sched}</div>
              <div class="schedule-main">
                <div class="schedule-title-row">
                  <strong class="schedule-company">{company}</strong>
                  <span class="schedule-role">{title}</span>
                  <span class="iv-round-badge">{r_label}</span>
                </div>
                <div class="schedule-meta">方式：{mode}{iv_html}</div>
                {prep_html}
              </div>
            </div>"""
        )
    return f'<div class="schedule-strip">{"".join(cards)}</div>'


def _fmt_rate(num: int, den: int) -> str:
    if den <= 0:
        return "—"
    pct = (num / den) * 100.0
    if pct >= 100.0:
        return "100%"
    if pct == 0.0:
        return "0%"
    return f"{pct:.0f}%" if pct >= 10.0 else f"{pct:.1f}%"


def _render_funnel_chart(
    funnel: dict[str, Any],
    *,
    config: dict[str, Any],
    interview_total_val: int,
    offer_count_val: int,
    upcoming_iv_val: int,
) -> str:
    """渲染零依赖、可交互的居中渐缩转化漏斗图 + 旁路分流/待办速览。"""
    screened = int(funnel.get("screened_total", 0))
    skipped = int(funnel.get("screened_skip", 0))
    opening = int(funnel.get("opening_delivered_total", 0))
    connected = int(funnel.get("connected_total", 0))
    pdf_deliv = int(funnel.get("pdf_delivered_total", 0))
    handed_off = int(funnel.get("handed_off_pending", 0))
    favorited = int(funnel.get("favorited_pending", 0))
    discovered = int(funnel.get("discovered_pending", 0))
    resume_req = int(funnel.get("resume_requested_pending", 0))
    interviews = interview_total_val
    offers = offer_count_val

    base = max(screened, opening, connected, pdf_deliv, interviews, offers, 1)

    # 6 级单调递进主转化漏斗
    stages = [
        {
            "key": "screened",
            "label": "已评估岗位",
            "count": screened,
            "prev": None,
            "step_name": "全量基准",
            "desc": "逐岗比对入库总数（点击查看全量岗位账本）",
            "tab": "ledger",
            "filter": "all",
            "color_cls": "fv-stage-1",
        },
        {
            "key": "opening",
            "label": "开场已送达",
            "count": opening,
            "prev": screened,
            "step_name": "打招呼转化",
            "desc": "已发出破冰或被动建联的岗位（点击筛选开场已送达）",
            "tab": "ledger",
            "filter": "opening_sent",
            "color_cls": "fv-stage-2",
        },
        {
            "key": "connected",
            "label": "双方已建联",
            "count": connected,
            "prev": opening,
            "step_name": "回复建联率",
            "desc": "已获招聘者实质回复的会话（点击筛选双方已建联）",
            "tab": "ledger",
            "filter": "connected",
            "color_cls": "fv-stage-3",
        },
        {
            "key": "pdf",
            "label": "PDF 简历已送达",
            "count": pdf_deliv,
            "prev": connected,
            "step_name": "简历投递率",
            "desc": "确认已接收附件 PDF 简历（点击筛选 PDF 已送达）",
            "tab": "ledger",
            "filter": "resume_delivered",
            "color_cls": "fv-stage-4",
        },
        {
            "key": "interview",
            "label": "进入面试",
            "count": interviews,
            "prev": pdf_deliv if pdf_deliv > 0 else connected,
            "step_name": "约面进面率",
            "desc": f"累计进入面试阶段（当前待面 {upcoming_iv_val} 场，点击进入面试与 Offer 视图）",
            "tab": "interviews",
            "filter": "",
            "color_cls": "fv-stage-5",
        },
        {
            "key": "offer",
            "label": "已获 Offer",
            "count": offers,
            "prev": interviews,
            "step_name": "面试拿 Offer 率",
            "desc": "已收到口头/正式 Offer 或已接 Offer（点击查看 Offer 跟进）",
            "tab": "interviews",
            "filter": "",
            "color_cls": "fv-stage-6",
        },
    ]

    rows_html: list[str] = []
    for idx, st in enumerate(stages):
        cnt = int(st["count"])
        total_rate = _fmt_rate(cnt, screened) if screened > 0 else "—"
        if st["prev"] is None:
            step_rate_str = "基准 100%" if screened > 0 else "暂无数据"
        else:
            step_rate_str = f"{st['step_name']} {_fmt_rate(cnt, int(st['prev']))}"

        # 居中渐缩宽度：有数据时按比例映射到 28%..100%，零数据时保持优雅最小占位 24%
        if screened > 0 and cnt > 0:
            ratio = min(1.0, cnt / base)
            width_pct = round(28.0 + ratio * 72.0, 1)
            # 为了保持视觉上的漏斗梯形感，给后置阶段微调视觉收口
            max_cap = 100.0 - idx * 3.0
            width_pct = min(width_pct, max_cap) if idx > 0 else 100.0
        else:
            width_pct = max(24.0, round(68.0 - idx * 8.0, 1))

        zero_cls = " is-zero" if cnt == 0 else ""
        filter_attr = f' data-jump-filter="{_esc(st["filter"])}"' if st["filter"] else ""
        detail_tip = (
            f'{st["label"]}：{cnt} 个 ｜ {step_rate_str} ｜ 占总评估 {total_rate} — {st["desc"]}'
        )
        rows_html.append(
            f"""
            <button type="button" class="fv-row{zero_cls}" data-jump-tab="{_esc(st['tab'])}"{filter_attr} data-funnel-tip="{_esc(detail_tip)}">
              <div class="fv-stage-label">
                <span class="fv-stage-name">{_esc(st["label"])}</span>
                <span class="fv-stage-rate">{_esc(step_rate_str)}</span>
              </div>
              <div class="fv-bar-track">
                <div class="fv-bar {st['color_cls']}" style="width: {width_pct:.1f}%;">
                  <span class="fv-bar-count">{cnt}</span>
                  <span class="fv-bar-pct">{_esc(total_rate)}</span>
                </div>
              </div>
            </button>"""
        )

    skip_rate = _fmt_rate(skipped, screened) if screened > 0 else "—"
    side_cards = [
        (
            "不匹配跳过",
            skipped,
            f"拦截率 {skip_rate} · {_skip_kpi_sub(config)}",
            "ledger",
            "screened_skip",
            "fv-side-danger",
        ),
        (
            "待本人约面/决策",
            handed_off,
            "触发红线已暂停自动回复，置顶待接管",
            "queue",
            "",
            "fv-side-amber",
        ),
        *(
            [
                (
                    "已收藏·待本人投递",
                    favorited,
                    "已在 BOSS 点感兴趣，看完合适再投递",
                    "ledger",
                    "favorited",
                    "fv-side-indigo",
                )
            ]
            if (favorited > 0 or str(config.get("mode_discover", "semi")) == "favorite")
            else []
        ),
        (
            "待确认破冰候选",
            discovered,
            "深筛通过待确认或冷却期暂存",
            "ledger",
            "discovered",
            "fv-side-blue",
        ),
        (
            "待同意简历请求",
            resume_req,
            "已发起附件简历请求，待对方点同意",
            "ledger",
            "resume_requested",
            "fv-side-purple",
        ),
    ]
    side_html_parts: list[str] = []
    for s_label, s_cnt, s_sub, s_tab, s_filter, s_cls in side_cards:
        f_attr = f' data-jump-filter="{_esc(s_filter)}"' if s_filter else ""
        tip = f"{s_label}：{s_cnt} 个 — {s_sub}（点击直接定位）"
        side_html_parts.append(
            f"""
            <button type="button" class="fv-side-card {s_cls}" data-jump-tab="{_esc(s_tab)}"{f_attr} data-funnel-tip="{_esc(tip)}">
              <div class="fv-side-top">
                <span class="fv-side-label">{_esc(s_label)}</span>
                <strong class="fv-side-val">{s_cnt}</strong>
              </div>
              <div class="fv-side-sub">{_esc(s_sub)}</div>
            </button>"""
        )

    return f"""
      <div class="funnel-viz" id="funnel-viz-panel" hidden>
        <div class="fv-layout">
          <div class="fv-main">
            <div class="fv-head-row">
              <span class="fv-col-title">转化阶段与环比转化率</span>
              <span class="fv-col-title">转化漏斗（数量 · 占总评估比）</span>
            </div>
            <div class="fv-rows">
              {"".join(rows_html)}
            </div>
          </div>
          <aside class="fv-side">
            <div class="fv-side-title">旁路分流与实时待办</div>
            <div class="fv-side-list">
              {"".join(side_html_parts)}
            </div>
          </aside>
        </div>
        <div class="fv-inspector" id="fv-inspector">
          悬停任意漏斗层查看转化率详情，点击可直接跳转并筛选对应岗位列表。
        </div>
      </div>"""


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

    interviews = job.get("interviews") or []
    interview_search_bits = " ".join(
        f"{rd.get('round_label', '')} {rd.get('scheduled_at', '')} {rd.get('mode', '')} {rd.get('interviewer', '')} {rd.get('summary', '')}"
        for rd in interviews
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
            interview_search_bits,
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
    timeline_block = _render_interview_timeline(interviews)

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
          {timeline_block}
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


def _build_strategy_editor_html(
    config: dict[str, Any],
    *,
    is_pro: bool,
    is_demo: bool,
) -> str:
    """构建可在侧边栏看板内直接点选修改并一键落盘保存的策略表单卡片。"""
    is_copilot = str(config.get("execution_mode") or "browser") == "copilot"
    mode_d = str(config.get("mode_discover") or "semi")
    mode_r = str(config.get("mode_reply") or "semi")
    roles_val = ", ".join(config.get("target_roles") or [])
    cities_val = ", ".join(config.get("preferred_cities") or [])
    remote_flex = bool(config.get("remote_bigtech_flex", True))
    min_sal = int(config.get("min_salary_k", 20) or 20)
    sal_mode = str(config.get("salary_match_mode") or "ceiling_tolerant")
    out_pol = str(config.get("outsourcing_policy") or "block_all")
    onsite_pol = str(config.get("onsite_policy") or "block_all")
    hh_pol = str(config.get("headhunter_policy") or "inbound_only")
    inb_pol = str(config.get("inbound_resume_policy") or "generous")
    already_acked = bool(config.get("auto_risk_acknowledged", False))

    past_emps = [str(x).strip() for x in (config.get("past_employers") or []) if str(x).strip()]
    past_lower = {x.lower() for x in past_emps}
    extra_blocked = [
        str(c).strip()
        for c in (config.get("blocked_companies") or [])
        if str(c).strip() and str(c).strip().lower() not in past_lower
    ]
    ex_kws = [str(x).strip() for x in (config.get("exclude_keywords") or []) if str(x).strip()]

    sched = config.get("schedule") or {}
    max_round = int(sched.get("max_per_round", 5) or 5)
    max_day = int(sched.get("max_per_day", 20) or 20)
    weekdays_only = bool(sched.get("weekdays_only", True))

    def sel(cur: str, target: str) -> str:
        return " selected" if cur == target else ""

    fav_disabled = " disabled" if is_copilot else ""
    fav_note = "（当前为副驾驶粘贴模式，需先开启浏览器直连）" if is_copilot else ""
    auto_disabled = " disabled" if (not is_pro or is_copilot) else ""
    auto_suffix = "（需浏览器直连模式）" if (is_pro and is_copilot) else ("" if is_pro else "（需解锁 Pro）")

    demo_Attr = " disabled" if is_demo else ""
    demo_hint = (
        '<span class="se-demo-pill">示例预览模式（不会覆盖本机真实配置）</span>'
        if is_demo
        else '<span class="se-live-pill">保存后 Agent 下次搜岗与巡检立即按新配置执行</span>'
    )

    return f"""
          <div class="strategy-card full-span strategy-editor-card" id="hm-strategy-editor">
            <div class="se-header">
              <div>
                <h3>🎛️ 求职策略与动作模式在线配置</h3>
                <p class="se-sub">直接修改下方选项并点击保存，配置将实时写入本机 <code>config.json</code>，后续对话搜岗与后台自动巡检立即按最新规则执行。</p>
              </div>
              {demo_hint}
            </div>
            <form id="hm-strategy-form" class="se-form" data-already-acked="{"1" if already_acked else "0"}" data-is-demo="{"1" if is_demo else "0"}">
              <div class="se-grid">
                <label class="se-field">
                  <span class="se-label">1. 筛出好岗后的默认动作（主动找岗模式）</span>
                  <select id="hm-cfg-mode-discover" class="se-control"{demo_Attr}>
                    <option value="semi"{sel(mode_d, "semi")}>半自动审阅单 · 先看破冰草稿，回序号再发</option>
                    <option value="favorite"{sel(mode_d, "favorite")}{fav_disabled}>仅自动收藏 · 只点 BOSS「感兴趣」不主动投递{fav_note}</option>
                    <option value="auto"{sel(mode_d, "auto")}{auto_disabled}>全自动定额破冰 · 强匹配自研岗直接打招呼{auto_suffix}</option>
                  </select>
                </label>

                <label class="se-field">
                  <span class="se-label">2. HR / 猎头消息答疑模式</span>
                  <select id="hm-cfg-mode-reply" class="se-control"{demo_Attr}>
                    <option value="semi"{sel(mode_r, "semi")}>半自动确认 · 生成草稿经本人确认后再发</option>
                    <option value="auto"{sel(mode_r, "auto")}{auto_disabled}>全自动守卫答疑 · 常规问题自动回，约面/谈薪停手弹窗{auto_suffix}</option>
                  </select>
                </label>

                <label class="se-field">
                  <span class="se-label">3. 目标岗位方向关键词（逗号分隔）</span>
                  <input type="text" id="hm-cfg-roles" class="se-control" value="{_esc(roles_val)}" placeholder="例如：AI产品经理, Agent产品经理"{demo_Attr} />
                </label>

                <div class="se-field se-inline-pair">
                  <label class="se-sub-field">
                    <span class="se-label">4. 优先城市（逗号分隔）</span>
                    <input type="text" id="hm-cfg-cities" class="se-control" value="{_esc(cities_val)}" placeholder="深圳, 杭州（留空不限）"{demo_Attr} />
                  </label>
                  <label class="se-sub-field">
                    <span class="se-label">异地知名大厂弹性</span>
                    <select id="hm-cfg-remote-flex" class="se-control"{demo_Attr}>
                      <option value="true"{" selected" if remote_flex else ""}>开启 · 异地大厂可先聊</option>
                      <option value="false"{" selected" if not remote_flex else ""}>关闭 · 严格仅看所选城市</option>
                    </select>
                  </label>
                </div>

                <div class="se-field se-inline-pair">
                  <label class="se-sub-field se-narrow">
                    <span class="se-label">5. 期望底薪（K）</span>
                    <input type="number" id="hm-cfg-min-salary" class="se-control" min="0" max="300" step="1" value="{min_sal}"{demo_Attr} />
                  </label>
                  <label class="se-sub-field">
                    <span class="se-label">区间标薪匹配口径</span>
                    <select id="hm-cfg-salary-mode" class="se-control"{demo_Attr}>
                      <option value="ceiling_tolerant"{sel(sal_mode, "ceiling_tolerant")}>看上限 · 宽松（容忍面板标薪略低约20%先聊）</option>
                      <option value="ceiling_strict"{sel(sal_mode, "ceiling_strict")}>看上限 · 严格达标（区间上限须 ≥ 期望底薪）</option>
                      <option value="floor_strict"{sel(sal_mode, "floor_strict")}>看下限 · 严格卡起薪（起薪下限须 ≥ 期望底薪）</option>
                    </select>
                  </label>
                </div>

                <label class="se-field">
                  <span class="se-label">6. 外包 / 劳务派遣 / OD 编制策略</span>
                  <select id="hm-cfg-outsourcing" class="se-control"{demo_Attr}>
                    <option value="block_all"{sel(out_pol, "block_all")}>全部排除过滤（主动搜岗与来信均跳过）</option>
                    <option value="inbound_only"{sel(out_pol, "inbound_only")}>主动不投 · 仅接被动来信评估</option>
                    <option value="allow_all"{sel(out_pol, "allow_all")}>完全开放接受</option>
                  </select>
                </label>

                <label class="se-field">
                  <span class="se-label">7. 驻场 / 客户现场外派策略</span>
                  <select id="hm-cfg-onsite" class="se-control"{demo_Attr}>
                    <option value="block_all"{sel(onsite_pol, "block_all")}>全部排除过滤（穿透主页与 JD 拦截驻场）</option>
                    <option value="inbound_only"{sel(onsite_pol, "inbound_only")}>主动不投 · 仅接被动来信评估</option>
                    <option value="allow_all"{sel(onsite_pol, "allow_all")}>完全开放接受</option>
                  </select>
                </label>

                <div class="se-field se-inline-pair">
                  <label class="se-sub-field">
                    <span class="se-label">8. 猎头渠道策略</span>
                    <select id="hm-cfg-headhunter" class="se-control"{demo_Attr}>
                      <option value="inbound_only"{sel(hh_pol, "inbound_only")}>主动不投 · 仅接猎头来信</option>
                      <option value="block_all"{sel(hh_pol, "block_all")}>全部屏蔽猎头</option>
                      <option value="allow_all"{sel(hh_pol, "allow_all")}>完全开放猎头</option>
                    </select>
                  </label>
                  <label class="se-sub-field">
                    <span class="se-label">被动索要简历尺度</span>
                    <select id="hm-cfg-inbound-resume" class="se-control"{demo_Attr}>
                      <option value="generous"{sel(inb_pol, "generous")}>宽松配合 · 未踩雷尽量给</option>
                      <option value="strict"{sel(inb_pol, "strict")}>严格把关 · 仅高匹配才给</option>
                    </select>
                  </label>
                </div>

                <label class="se-field">
                  <span class="se-label">9. 过往/当前雇主防撞保护（自动同字号拉黑防社死）</span>
                  <input type="text" id="hm-cfg-employers" class="se-control" value="{_esc(', '.join(past_emps))}" placeholder="逗号分隔，自动同步至防误投黑名单"{demo_Attr} />
                </label>

                <div class="se-field se-inline-pair">
                  <label class="se-sub-field">
                    <span class="se-label">额外屏蔽企业（逗号分隔）</span>
                    <input type="text" id="hm-cfg-blocked" class="se-control" value="{_esc(', '.join(extra_blocked))}" placeholder="想额外避开的竞对公司"{demo_Attr} />
                  </label>
                  <label class="se-sub-field">
                    <span class="se-label">岗位排除词（逗号分隔）</span>
                    <input type="text" id="hm-cfg-exclude-kws" class="se-control" value="{_esc(', '.join(ex_kws))}" placeholder="例如：电销, 催收, 保险"{demo_Attr} />
                  </label>
                </div>

                <div class="se-field se-inline-pair se-full-row">
                  <label class="se-sub-field se-narrow">
                    <span class="se-label">单轮上限（1-8 岗）</span>
                    <input type="number" id="hm-cfg-max-round" class="se-control" min="1" max="8" step="1" value="{max_round}"{demo_Attr} />
                  </label>
                  <label class="se-sub-field se-narrow">
                    <span class="se-label">单日上限（1-30 岗）</span>
                    <input type="number" id="hm-cfg-max-day" class="se-control" min="1" max="30" step="1" value="{max_day}"{demo_Attr} />
                  </label>
                  <label class="se-sub-field">
                    <span class="se-label">巡检开放日期范围</span>
                    <select id="hm-cfg-weekdays" class="se-control"{demo_Attr}>
                      <option value="true"{" selected" if weekdays_only else ""}>仅周一至周五工作日</option>
                      <option value="false"{" selected" if not weekdays_only else ""}>每日均可运行</option>
                    </select>
                  </label>
                </div>
              </div>

              <div id="hm-auto-risk-box" class="se-risk-box" hidden>
                <label class="se-risk-check">
                  <input type="checkbox" id="hm-cfg-ack-risk" />
                  <span>本人已知晓：开启全自动档位（auto）后将在工作时段自动点击沟通或发送回复，建议保持默认定额护栏（每轮≤5岗、约面/谈薪自动停手），确认授权开启。</span>
                </label>
              </div>

              <div class="se-footer">
                <div class="se-btn-group">
                  <button type="submit" id="hm-save-strategy-btn" class="copy-action-btn"{demo_Attr}>💾 保存策略设置（立即生效）</button>
                  <button type="reset" id="hm-reset-strategy-btn" class="copy-action-btn secondary"{demo_Attr}>↺ 恢复已存值</button>
                </div>
                <div id="hm-save-strategy-status" class="se-status" role="status" aria-live="polite"></div>
              </div>
            </form>
          </div>"""


def render_dashboard_html(
    *,
    config: dict[str, Any],
    jobs: list[dict[str, Any]],
    summary: dict[str, Any],
    profile_excerpt: str,
    voice_excerpt: str = "",
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

    is_copilot_mode_hdr = str(config.get("execution_mode", "browser")) == "copilot"
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
        mode_badge = (
            "当前可用：单轮自动收藏（不主动投递）"
            if (not is_copilot_mode_hdr and str(config.get("mode_discover", "semi")) == "favorite")
            else "当前可用：手动单轮找岗（半自动确认）"
        )
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
    favorited_jobs = [j for j in jobs if j.get("status") == "favorited"]
    discovered_jobs = [j for j in jobs if j.get("status") == "discovered"]
    queue_count = len(handed_off_jobs) + len(favorited_jobs) + len(discovered_jobs)

    past_emps_for_guide = config.get("past_employers") or []
    is_copilot_mode = str(config.get("execution_mode", "browser")) == "copilot"
    mode_discover_val = str(config.get("mode_discover", "semi"))
    is_favorite_discover = (not is_copilot_mode) and mode_discover_val == "favorite"
    pro_auto_discover = (not is_copilot_mode) and is_pro and mode_discover_val == "auto"
    pro_patrol_discover = (not is_copilot_mode) and is_pro and mode_discover_val in ("auto", "favorite")
    if is_copilot_mode:
        first_search_cmd = "帮我评估下面这个 BOSS 岗位靠不靠谱（排外包+核对前东家+看匹配缺口），并拟一句真人感打招呼短句："
        step1_title = "粘贴首个岗位试跑排雷"
        step1_desc = "直接把看中的岗位 JD 发给助手，立即体验前东家防撞、外包识别与破冰话术草稿。"
    elif pro_auto_discover:
        first_search_cmd = "帮我在 BOSS 直聘按当前 Pro 全自动策略跑第一轮定额搜岗破冰并检查未读消息"
        step1_title = "跑首轮 Pro 全自动定额搜岗 + 未读检查"
        step1_desc = "自动在已登录浏览器筛除伪装外包与前东家，对强匹配自研岗直接定额发送破冰短句（每轮≤5岗）并处理未读消息。"
    elif is_favorite_discover:
        first_search_cmd = (
            "帮我在 BOSS 直聘按当前 Pro 策略跑第一轮深筛并自动收藏（只点感兴趣收藏，绝不点立即沟通）并检查未读消息"
            if is_pro
            else "帮我在 BOSS 直聘按当前求职档案跑第一轮搜岗深筛并自动收藏（只点感兴趣收藏，绝不点立即沟通）"
        )
        step1_title = "去 BOSS 跑首轮深筛并自动收藏（不主动投递）"
        step1_desc = "自动在已登录浏览器按你的条件筛除伪装外包与前东家，只点击「感兴趣」收藏进待投递列表，看完觉得合适再自己投递。"
    else:
        first_search_cmd = "帮我在 BOSS 直聘按当前求职档案跑第一轮搜岗深筛预览（先出破冰审阅单给我确认，未经确认别点发送）"
        step1_title = "去 BOSS 跑首轮搜岗深筛预览"
        step1_desc = "自动在已登录浏览器按你的条件筛出 3～5 个靠谱自研岗，先出审阅单给你过目，绝不乱发。"

    if pro_patrol_discover:
        second_reply_cmd = "帮我挂载求职助手工作日工作时间自动巡检定时任务"
        step2_title = "方式 2 · 一键挂载工作日工作时间自动巡检"
        step2_desc = (
            "周一至周五 09:00–12:00、14:00–18:00 定时自动盯未读消息与静默收藏新岗（不主动打招呼），遇到约面/谈薪立即桌面弹窗提醒。"
            if is_favorite_discover
            else "周一至周五 09:00–12:00、14:00–18:00 定时自动盯未读消息与定额找岗，遇到约面/谈薪立即桌面弹窗提醒本人接管。"
        )
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
    favorited_section_html = ""
    if favorited_jobs or is_favorite_discover:
        fav_cards_html = (
            "\n".join(_render_job_card(j, highlight_icebreaker=True) for j in favorited_jobs)
            if favorited_jobs
            else '<div class="empty-box">当前暂无已自动收藏待投递的岗位（复制上方口令发给桌面 AI，筛出好岗后将自动在 BOSS 点击「感兴趣」收藏并同步至此，绝不主动点立即沟通）。</div>'
        )
        favorited_section_html = f"""
        <div class="section-block">
          <div class="section-header">
            <h2 class="section-title">已自动收藏 · 待本人审阅投递（{len(favorited_jobs)}）</h2>
            <span class="section-hint">已在招聘网页点击「感兴趣」收藏（未主动发消息）· 觉得合适可在网页直接点沟通，或发序号让助手代发下方参考破冰语</span>
          </div>
          <div class="cards-stack">
            {fav_cards_html}
          </div>
        </div>
"""
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
        "\n".join(_render_job_card(j, highlight_icebreaker=(j.get("status") in ("discovered", "favorited"))) for j in jobs)
        if jobs
        else (
            '<div class="empty-box">本地账本暂无岗位记录。回到桌面 AI 启动首轮全自动搜岗或开启定时巡检后自动入库生成。</div>'
            if is_auto_discover
            else (
                '<div class="empty-box">本地账本暂无岗位记录。回到桌面 AI 发送「帮我在 BOSS 跑第一轮搜岗深筛并自动收藏」后自动入库生成。</div>'
                if is_favorite_discover
                else '<div class="empty-box">本地账本暂无岗位记录。回到桌面 AI 发送「帮我在 BOSS 跑第一轮搜岗深筛预览」后自动入库生成。</div>'
            )
        )
    )

    # 面试与 Offer 追踪视图数据准备
    offer_jobs = [j for j in jobs if j.get("status") in ("offer", "offer_accepted")]
    active_interview_jobs = [j for j in jobs if j.get("status") in ("interviewing", "interview_scheduled")]
    archived_interview_jobs = [
        j for j in jobs if j.get("status") == "closed_rejected" and (j.get("interviews") or j.get("interview_scheduled_at"))
    ]
    interview_tab_count = len(offer_jobs) + len(active_interview_jobs)

    upcoming_schedule_items: list[dict[str, Any]] = []
    for j in jobs:
        if j.get("status") in ("screened_skip", "closed_rejected"):
            continue
        for rd in (j.get("interviews") or []):
            if str(rd.get("outcome") or "pending") == "pending":
                upcoming_schedule_items.append({
                    "scheduled_at": rd.get("scheduled_at") or "",
                    "company": j.get("company") or "",
                    "title": j.get("title") or "",
                    "round_label": rd.get("round_label") or "一面",
                    "mode": rd.get("mode") or "",
                    "interviewer": rd.get("interviewer") or "",
                    "summary": rd.get("summary") or "",
                })
    upcoming_schedule_items.sort(key=lambda x: (x["scheduled_at"] == "", x["scheduled_at"]))
    upcoming_schedule_html = _render_upcoming_schedule(upcoming_schedule_items)

    offer_jobs_html = "\n".join(_render_job_card(j) for j in offer_jobs)
    active_interview_jobs_html = (
        "\n".join(_render_job_card(j) for j in active_interview_jobs)
        if active_interview_jobs
        else '<div class="empty-box">当前暂无进行中的面试岗位。敲定约面后，助手会同步「已约面」状态，或你也可以直接在对话里口述「把 XX 公司的一面记到周四下午 2 点」。</div>'
    )
    archived_interview_jobs_html = "\n".join(_render_job_card(j) for j in archived_interview_jobs)

    offer_section_html = (
        f"""
        <div class="section-block">
          <div class="section-header">
            <h2 class="section-title">Offer 与录用跟进（{len(offer_jobs)}）</h2>
            <span class="section-hint">已收到口头/正式 Offer 或已接 Offer 待入职岗位</span>
          </div>
          <div class="cards-stack">
            {offer_jobs_html}
          </div>
        </div>"""
        if offer_jobs
        else ""
    )
    archived_interview_section_html = (
        f"""
        <div class="section-block">
          <div class="section-header">
            <h2 class="section-title">历史面试复盘归档（{len(archived_interview_jobs)}）</h2>
            <span class="section-hint">已结束但保留完整面经与轮次记录，便于后续复盘</span>
          </div>
          <div class="cards-stack">
            {archived_interview_jobs_html}
          </div>
        </div>"""
        if archived_interview_jobs
        else ""
    )

    interview_report_cmd = "帮我更新面试进展：【公司名】一面已通过，二面约在周四 14:00 腾讯会议（面试官：产品总监），一面主要聊了..."
    interview_total_val = int(funnel.get("interview_total", len(offer_jobs) + len(active_interview_jobs)))
    interview_active_val = int(funnel.get("interview_active_total", len(active_interview_jobs)))
    upcoming_iv_val = int(funnel.get("upcoming_interviews_count", len(upcoming_schedule_items)))
    offer_count_val = int(funnel.get("offer_count", len(offer_jobs)))
    interview_kpi_sub = (
        f"待面 <strong>{upcoming_iv_val}</strong> 场 · Offer <strong>{offer_count_val}</strong>"
        if offer_count_val > 0
        else f"进行中 {interview_active_val} · 待面 <strong>{upcoming_iv_val}</strong> 场"
    )
    funnel_chart_html = _render_funnel_chart(
        funnel,
        config=config,
        interview_total_val=interview_total_val,
        offer_count_val=offer_count_val,
        upcoming_iv_val=upcoming_iv_val,
    )

    # 筛选胶囊
    filter_defs = [
        ("all", "全部", len(jobs)),
        ("offer_accepted", "已接 Offer", by_status.get("offer_accepted", 0)),
        ("offer", "已获 Offer", by_status.get("offer", 0)),
        ("interviewing", "面试推进中", by_status.get("interviewing", 0)),
        ("interview_scheduled", "已约面", by_status.get("interview_scheduled", 0)),
        ("handed_off", "待本人约面/决策", by_status.get("handed_off", 0)),
        ("favorited", "已收藏·待本人投递", by_status.get("favorited", 0)),
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
    strategy_editor_html = _build_strategy_editor_html(config, is_pro=is_pro, is_demo=is_demo)
    eff_voice_excerpt = str(locals().get("voice_excerpt") or "").strip()
    voice_learn_cmd = "请从我的历史对话记录提取行文风格，建立个人求职语气档案"
    if eff_voice_excerpt:
        voice_card_html = (
            '<div class="strategy-card full-span">'
            '<h3>个人求职语气档案（本人行文风格指纹 · 仅存本机） <span class="mini-tag">已启用</span></h3>'
            f'<pre class="profile-pre">{_esc(eff_voice_excerpt)}</pre>'
            "</div>"
        )
    else:
        voice_card_html = (
            '<div class="strategy-card full-span">'
            "<h3>个人求职语气档案（本人行文风格指纹 · 仅存本机）</h3>"
            '<div class="interview-sync-bar" style="margin-bottom:0;">'
            '<div class="interview-sync-text">'
            "<strong>尚未建立语气档案：</strong>当前按默认得体口语沟通。可一键从本机历史对话记录提取你的句长断句、标点收尾与常用词习惯，让破冰、答疑与回复更像你本人亲手输入。"
            "</div>"
            f'<button type="button" class="copy-action-btn secondary" data-copy="{_esc(voice_learn_cmd)}" data-done-label="✓ 已复制口令，发给助手即可自动提炼建档">复制提炼语气口令</button>'
            "</div>"
            "</div>"
        )

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

    /* 核心漏斗区（支持卡片 / 漏斗图切换） */
    .funnel-section {{
      margin-bottom: 20px;
    }}

    .funnel-section-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }}

    .funnel-title-group {{
      display: flex;
      align-items: baseline;
      gap: 10px;
      flex-wrap: wrap;
    }}

    .funnel-heading {{
      font-size: 14px;
      font-weight: 700;
      color: var(--text-main);
    }}

    .funnel-sub-hint {{
      font-size: 12px;
      color: var(--text-muted);
    }}

    .view-switcher {{
      display: inline-flex;
      align-items: center;
      background: #e2e8f0;
      padding: 3px;
      border-radius: 9px;
      gap: 2px;
    }}

    .view-switch-btn {{
      appearance: none;
      border: none;
      background: transparent;
      color: var(--text-secondary);
      font-size: 12.5px;
      font-weight: 600;
      padding: 5px 12px;
      border-radius: 7px;
      cursor: pointer;
      transition: all 0.15s ease;
      white-space: nowrap;
    }}

    .view-switch-btn:hover {{
      color: var(--text-main);
    }}

    .view-switch-btn.active {{
      background: #ffffff;
      color: var(--text-main);
      box-shadow: 0 1px 3px rgba(15, 23, 42, 0.1);
    }}

    /* 7 格核心漏斗卡 */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 12px;
    }}

    .kpi-grid[hidden],
    .funnel-viz[hidden] {{
      display: none !important;
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
      cursor: pointer;
      transition: transform 0.12s ease, box-shadow 0.12s ease, border-color 0.12s ease;
    }}

    .kpi-card:hover {{
      transform: translateY(-1px);
      border-color: var(--border-strong);
      box-shadow: 0 4px 12px rgba(15, 23, 42, 0.08);
    }}

    /* 可交互漏斗图 */
    .funnel-viz {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 16px 18px 14px;
      box-shadow: var(--shadow-sm);
    }}

    .fv-layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 290px;
      gap: 18px;
      align-items: start;
    }}

    .fv-head-row {{
      display: grid;
      grid-template-columns: 168px minmax(0, 1fr);
      gap: 12px;
      padding: 0 6px 8px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 8px;
    }}

    .fv-col-title {{
      font-size: 11.5px;
      font-weight: 600;
      color: var(--text-muted);
    }}

    .fv-rows {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}

    .fv-row {{
      appearance: none;
      width: 100%;
      border: 1px solid transparent;
      background: transparent;
      border-radius: 9px;
      padding: 6px 8px;
      display: grid;
      grid-template-columns: 160px minmax(0, 1fr);
      gap: 12px;
      align-items: center;
      cursor: pointer;
      text-align: left;
      transition: background 0.14s ease, border-color 0.14s ease, transform 0.12s ease;
    }}

    .fv-row:hover {{
      background: #f8fafc;
      border-color: #cbd5e1;
      transform: translateX(2px);
    }}

    .fv-stage-label {{
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 0;
    }}

    .fv-stage-name {{
      font-size: 13px;
      font-weight: 700;
      color: var(--text-main);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .fv-stage-rate {{
      font-size: 11.5px;
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
    }}

    .fv-bar-track {{
      display: flex;
      justify-content: center;
      align-items: center;
      width: 100%;
      min-height: 34px;
    }}

    .fv-bar {{
      height: 34px;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 0 12px;
      color: #ffffff;
      font-variant-numeric: tabular-nums;
      box-shadow: 0 2px 6px rgba(15, 23, 42, 0.08);
      transition: filter 0.15s ease, transform 0.15s ease;
      clip-path: polygon(0 0, 100% 0, calc(100% - 7px) 100%, 7px 100%);
    }}

    .fv-row:hover .fv-bar {{
      filter: brightness(1.06);
      transform: scaleX(1.01);
    }}

    .fv-row.is-zero .fv-bar {{
      background: #f1f5f9 !important;
      color: #94a3b8;
      box-shadow: none;
      border: 1px dashed #cbd5e1;
      clip-path: none;
    }}

    .fv-stage-1 {{ background: linear-gradient(90deg, #334155 0%, #475569 100%); }}
    .fv-stage-2 {{ background: linear-gradient(90deg, #1d4ed8 0%, #2563eb 100%); }}
    .fv-stage-3 {{ background: linear-gradient(90deg, #0284c7 0%, #0ea5e9 100%); }}
    .fv-stage-4 {{ background: linear-gradient(90deg, #0d9488 0%, #14b8a6 100%); }}
    .fv-stage-5 {{ background: linear-gradient(90deg, #047857 0%, #10b981 100%); }}
    .fv-stage-6 {{ background: linear-gradient(90deg, #6d28d9 0%, #8b5cf6 100%); }}

    .fv-bar-count {{
      font-size: 15px;
      font-weight: 800;
    }}

    .fv-bar-pct {{
      font-size: 11.5px;
      font-weight: 600;
      opacity: 0.9;
    }}

    .fv-side {{
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 12px;
    }}

    .fv-side-title {{
      font-size: 12px;
      font-weight: 700;
      color: var(--text-secondary);
      margin-bottom: 8px;
    }}

    .fv-side-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}

    .fv-side-card {{
      appearance: none;
      width: 100%;
      background: #ffffff;
      border: 1px solid var(--border);
      border-left: 3px solid var(--border-strong);
      border-radius: 8px;
      padding: 8px 10px;
      text-align: left;
      cursor: pointer;
      transition: transform 0.12s ease, box-shadow 0.12s ease;
    }}

    .fv-side-card:hover {{
      transform: translateY(-1px);
      box-shadow: 0 3px 8px rgba(15, 23, 42, 0.07);
    }}

    .fv-side-danger {{ border-left-color: #dc2626; }}
    .fv-side-amber {{ border-left-color: #d97706; background: #fffdf5; }}
    .fv-side-indigo {{ border-left-color: #4f46e5; background: #f8faff; }}
    .fv-side-blue {{ border-left-color: #2563eb; }}
    .fv-side-purple {{ border-left-color: #7c3aed; }}

    .fv-side-top {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 8px;
    }}

    .fv-side-label {{
      font-size: 12.5px;
      font-weight: 600;
      color: var(--text-main);
    }}

    .fv-side-val {{
      font-size: 17px;
      font-weight: 800;
      color: var(--text-main);
      font-variant-numeric: tabular-nums;
    }}

    .fv-side-amber .fv-side-val {{ color: #b45309; }}
    .fv-side-danger .fv-side-val {{ color: #b91c1c; }}

    .fv-side-sub {{
      margin-top: 2px;
      font-size: 11.5px;
      color: var(--text-muted);
      line-height: 1.4;
    }}

    .fv-inspector {{
      margin-top: 12px;
      padding: 8px 12px;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      border-radius: 8px;
      font-size: 12.5px;
      color: var(--text-secondary);
      line-height: 1.5;
    }}

    .kpi-card.kpi-highlight {{
      background: linear-gradient(180deg, #fffbeb 0%, #ffffff 100%);
      border-color: var(--accent-amber-border);
    }}

    .kpi-card.kpi-interview {{
      background: linear-gradient(180deg, #ecfdf5 0%, #ffffff 100%);
      border-color: #34d399;
      cursor: pointer;
      transition: transform 0.12s ease, box-shadow 0.12s ease;
    }}

    .kpi-card.kpi-interview:hover {{
      transform: translateY(-1px);
      box-shadow: 0 4px 12px rgba(4, 120, 87, 0.12);
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

    .kpi-interview .kpi-label {{
      color: var(--success);
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

    .kpi-interview .kpi-value {{
      color: var(--success);
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

    .status-border-offer_accepted {{ border-left-color: #047857; }}
    .status-border-offer {{ border-left-color: #7c3aed; }}
    .status-border-interviewing {{ border-left-color: #059669; }}
    .status-border-interview_scheduled {{ border-left-color: #0d9488; }}
    .status-border-handed_off {{ border-left-color: #d97706; }}
    .status-border-favorited {{ border-left-color: #4f46e5; }}
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

    .status-offer_accepted {{ background: #047857; color: #ffffff; border: 1px solid #065f46; }}
    .status-offer {{ background: #f5f3ff; color: #6d28d9; border: 1px solid #c4b5fd; }}
    .status-interviewing {{ background: #d1fae5; color: #047857; border: 1px solid #6ee7b7; }}
    .status-handed_off {{ background: var(--accent-amber-bg); color: var(--accent-amber); border: 1px solid #fcd34d; }}
    .status-interview_scheduled {{ background: #ccfbf1; color: #0f766e; border: 1px solid #5eead4; }}
    .status-favorited {{ background: #eef2ff; color: #3730a3; border: 1px solid #c7d2fe; }}
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

    /* 交互式策略配置面板 */
    .strategy-editor-card {{
      border: 1px solid #bfdbfe;
      box-shadow: 0 4px 14px rgba(37, 99, 235, 0.06);
    }}

    .se-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 14px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border);
    }}

    .se-header h3 {{
      margin-bottom: 4px;
      border-bottom: 0;
      padding-bottom: 0;
    }}

    .se-sub {{
      margin: 0;
      font-size: 12.5px;
      color: var(--text-muted);
    }}

    .se-live-pill,
    .se-demo-pill {{
      font-size: 12px;
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 999px;
      white-space: nowrap;
    }}

    .se-live-pill {{
      background: #ecfdf5;
      color: #047857;
      border: 1px solid #a7f3d0;
    }}

    .se-demo-pill {{
      background: #fffbeb;
      color: #b45309;
      border: 1px solid #fde68a;
    }}

    .se-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px 16px;
    }}

    .se-field {{
      display: flex;
      flex-direction: column;
      gap: 5px;
      min-width: 0;
    }}

    .se-full-row {{
      grid-column: 1 / -1;
    }}

    .se-inline-pair {{
      flex-direction: row;
      gap: 10px;
      align-items: flex-end;
    }}

    .se-sub-field {{
      display: flex;
      flex-direction: column;
      gap: 5px;
      flex: 1 1 0;
      min-width: 0;
    }}

    .se-sub-field.se-narrow {{
      flex: 0 0 132px;
    }}

    .se-label {{
      font-size: 12.5px;
      font-weight: 600;
      color: var(--text-secondary);
    }}

    .se-control {{
      width: 100%;
      padding: 7px 11px;
      font-size: 13px;
      line-height: 1.4;
      color: var(--text-main);
      background-color: #ffffff;
      border: 1px solid var(--border-strong);
      border-radius: 8px;
      outline: none;
      transition: border-color 0.14s ease, box-shadow 0.14s ease;
    }}

    select.se-control {{
      -webkit-appearance: none;
      appearance: none;
      padding-right: 36px;
      cursor: pointer;
      background-image: url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNCIgaGVpZ2h0PSIxNCIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9IiM2NDc0OGIiIHN0cm9rZS13aWR0aD0iMi4xIiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiPjxwb2x5bGluZSBwb2ludHM9IjYgOSAxMiAxNSAxOCA5Ii8+PC9zdmc+");
      background-repeat: no-repeat;
      background-position: right 12px center;
      background-size: 14px 14px;
    }}

    .se-control:focus {{
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
    }}

    .se-control:disabled {{
      background-color: #f1f5f9;
      color: #94a3b8;
      cursor: not-allowed;
    }}

    .se-risk-box {{
      margin-top: 14px;
      padding: 10px 12px;
      background: #fffbeb;
      border: 1px solid #fcd34d;
      border-radius: 8px;
    }}

    .se-risk-check {{
      display: flex;
      align-items: flex-start;
      gap: 8px;
      font-size: 12.5px;
      color: #92400e;
      cursor: pointer;
      line-height: 1.5;
    }}

    .se-risk-check input {{
      margin-top: 3px;
    }}

    .se-footer {{
      margin-top: 16px;
      padding-top: 12px;
      border-top: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }}

    .se-btn-group {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}

    .se-status {{
      font-size: 12.5px;
      font-weight: 600;
      color: #059669;
    }}

    .se-status.is-error {{
      color: #dc2626;
    }}

    @media (max-width: 860px) {{
      .se-grid {{
        grid-template-columns: 1fr;
      }}
      .se-inline-pair {{
        flex-wrap: wrap;
      }}
      .se-sub-field.se-narrow {{
        flex: 1 1 120px;
      }}
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

    /* 面试与 Offer 追踪视图 */
    .interview-sync-bar {{
      background: linear-gradient(90deg, #ecfdf5 0%, #f8fafc 100%);
      border: 1px solid #a7f3d0;
      border-left: 4px solid #059669;
      border-radius: var(--radius-lg);
      padding: 12px 16px;
      margin-bottom: 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }}

    .interview-sync-text {{
      font-size: 13px;
      color: #1e293b;
      flex: 1 1 360px;
    }}

    .interview-sync-text strong {{
      color: #047857;
    }}

    .schedule-strip {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      gap: 10px;
    }}

    .schedule-item {{
      background: var(--bg-card);
      border: 1px solid #a7f3d0;
      border-left: 4px solid #0d9488;
      border-radius: var(--radius-md);
      padding: 12px 14px;
      box-shadow: var(--shadow-sm);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}

    .schedule-time-pill {{
      display: inline-flex;
      align-items: center;
      align-self: flex-start;
      padding: 2px 9px;
      border-radius: 999px;
      background: #ccfbf1;
      color: #0f766e;
      font-size: 12px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }}

    .schedule-title-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      font-size: 14px;
    }}

    .schedule-company {{
      color: var(--text-main);
    }}

    .schedule-role {{
      color: var(--text-secondary);
      font-size: 13px;
    }}

    .schedule-meta {{
      font-size: 12.5px;
      color: var(--text-muted);
    }}

    .schedule-prep {{
      font-size: 12.5px;
      color: #334155;
      background: var(--bg-subtle);
      border-radius: 6px;
      padding: 6px 8px;
    }}

    .iv-timeline {{
      margin-top: 10px;
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 10px 12px;
    }}

    .iv-timeline-head {{
      font-size: 12px;
      font-weight: 700;
      color: var(--text-secondary);
      margin-bottom: 8px;
    }}

    .iv-timeline-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}

    .iv-round-item {{
      background: #ffffff;
      border: 1px solid var(--border);
      border-left: 3px solid var(--border-strong);
      border-radius: 6px;
      padding: 8px 10px;
    }}

    .iv-outcome-border-pending {{ border-left-color: #0284c7; }}
    .iv-outcome-border-passed {{ border-left-color: #059669; }}
    .iv-outcome-border-failed {{ border-left-color: #dc2626; }}
    .iv-outcome-border-cancelled {{ border-left-color: #94a3b8; }}

    .iv-round-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}

    .iv-round-title-group {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}

    .iv-round-badge {{
      display: inline-block;
      padding: 1px 7px;
      border-radius: 4px;
      font-size: 11.5px;
      font-weight: 700;
      background: #e2e8f0;
      color: #1e293b;
    }}

    .iv-outcome-pill {{
      display: inline-block;
      padding: 1px 7px;
      border-radius: 999px;
      font-size: 11.5px;
      font-weight: 600;
    }}

    .iv-outcome-pending {{ background: #e0f2fe; color: #0369a1; }}
    .iv-outcome-passed {{ background: #dcfce7; color: #166534; }}
    .iv-outcome-failed {{ background: #fef2f2; color: #b91c1c; }}
    .iv-outcome-cancelled {{ background: #f1f5f9; color: #64748b; }}

    .iv-round-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px 12px;
      font-size: 12px;
      color: var(--text-muted);
    }}

    .iv-round-summary {{
      margin-top: 6px;
      font-size: 12.5px;
      color: #334155;
      line-height: 1.5;
      word-break: break-word;
    }}

    /* 响应式断点 */
    @media (max-width: 1180px) {{
      .kpi-grid {{
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }}
      .fv-layout {{
        grid-template-columns: 1fr;
      }}
      .fv-side-list {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
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
      .fv-head-row {{
        display: none;
      }}
      .fv-row {{
        grid-template-columns: 1fr;
        gap: 6px;
      }}
      .fv-side-list {{
        grid-template-columns: 1fr;
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
          <button type="button" class="header-pill badge-mode" data-jump-tab="strategy" style="cursor:pointer;" title="点击打开策略配置面板直接修改选项">{_esc(mode_badge)} · 改策略</button>
          <span class="header-pill {tier_cls}">{_esc(tier_badge)}</span>
          {top_upgrade_pill}
        </div>
      </div>
    </header>
    {release_update_banner_html}
    {upgrade_banner_html}

    <section class="funnel-section" aria-label="求职转化核心漏斗">
      <div class="funnel-section-head">
        <div class="funnel-title-group">
          <span class="funnel-heading">求职转化漏斗</span>
          <span class="funnel-sub-hint">点击任意阶段可快速定位对应岗位列表</span>
        </div>
        <div class="view-switcher" role="group" aria-label="漏斗视图切换">
          <button type="button" class="view-switch-btn active" data-funnel-view="cards" aria-pressed="true">卡片视图</button>
          <button type="button" class="view-switch-btn" data-funnel-view="funnel" aria-pressed="false">漏斗图</button>
        </div>
      </div>

      <div class="kpi-grid" id="kpi-cards-panel">
        <div class="kpi-card" data-jump-tab="ledger" data-jump-filter="all" title="点击查看全量岗位账本">
          <span class="kpi-label">已评估岗位</span>
          <div class="kpi-value">{int(funnel.get("screened_total", 0))}</div>
          <span class="kpi-sub">逐岗比对入库总数</span>
        </div>
        <div class="kpi-card" data-jump-tab="ledger" data-jump-filter="screened_skip" title="点击筛选不匹配跳过岗位">
          <span class="kpi-label">不匹配跳过</span>
          <div class="kpi-value">{int(funnel.get("screened_skip", 0))}</div>
          <span class="kpi-sub">{_esc(_skip_kpi_sub(config))}</span>
        </div>
        <div class="kpi-card" data-jump-tab="ledger" data-jump-filter="opening_sent" title="点击筛选开场已送达岗位">
          <span class="kpi-label">开场已送达</span>
          <div class="kpi-value">{int(funnel.get("opening_delivered_total", 0))}</div>
          <span class="kpi-sub">单向待回 {int(funnel.get("opening_waiting_reply", 0))}</span>
        </div>
        <div class="kpi-card" data-jump-tab="ledger" data-jump-filter="connected" title="点击筛选双方已建联岗位">
          <span class="kpi-label">双方已建联</span>
          <div class="kpi-value">{int(funnel.get("connected_total", 0))}</div>
          <span class="kpi-sub">{"已获招聘者实质回复" if is_pro else "需手动跟进或升 Pro 巡检"}</span>
        </div>
        <div class="kpi-card" data-jump-tab="ledger" data-jump-filter="resume_delivered" title="点击筛选 PDF 简历已送达岗位">
          <span class="kpi-label">PDF 简历已送达</span>
          <div class="kpi-value">{int(funnel.get("pdf_delivered_total", 0))}</div>
          <span class="kpi-sub">{"待同意请求 <strong>" + str(int(funnel.get("resume_requested_pending", 0))) + "</strong>" if is_pro else "多版自动匹配属 Pro 能力"}</span>
        </div>
        <div class="kpi-card kpi-highlight" data-jump-tab="queue" title="点击查看待本人约面与决策队列">
          <span class="kpi-label">待本人约面/决策</span>
          <div class="kpi-value">{int(funnel.get("handed_off_pending", 0))}</div>
          <span class="kpi-sub">{f"冷却/超额暂存候选 {int(funnel.get('discovered_pending', 0))}" if is_auto_discover else (f"已收藏待投 {int(funnel.get('favorited_pending', 0))} · 待确认 {int(funnel.get('discovered_pending', 0))}" if (int(funnel.get('favorited_pending', 0)) > 0 or is_favorite_discover) else f"待确认破冰候选 {int(funnel.get('discovered_pending', 0))}")}</span>
        </div>
        <div class="kpi-card kpi-interview" data-jump-tab="interviews" title="点击切换到面试与 Offer 追踪视图">
          <span class="kpi-label">面试与 Offer</span>
          <div class="kpi-value">{interview_total_val}</div>
          <span class="kpi-sub">{interview_kpi_sub}</span>
        </div>
      </div>
      {funnel_chart_html}
    </section>

    <nav class="tabs-bar" role="tablist">
      <button type="button" class="tab-btn active" data-tab-target="queue">
        待处理/待确认队列 <span class="tab-count">{queue_count}</span>
      </button>
      <button type="button" class="tab-btn" data-tab-target="interviews">
        面试与 Offer <span class="tab-count">{interview_tab_count}</span>
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

{favorited_section_html}        <div class="section-block">
          <div class="section-header">
            <h2 class="section-title">{f"全自动找岗已开启 · 冷却/超额暂存候选（{len(discovered_jobs)}）" if is_auto_discover else f"半自动待确认破冰候选（{len(discovered_jobs)}）"}</h2>
            <span class="section-hint">{"强匹配岗位已在配额内直接自动发送破冰语；仅夜间宵禁、冷却期或满当日上限时暂存于此" if is_auto_discover else "已在详情页与公司主页完成初筛，在宿主对话中回复序号即可发起沟通"}</span>
          </div>
          <div class="cards-stack">
            {discovered_html}
          </div>
        </div>
      </section>

      <section id="tab-interviews" class="tab-panel">
        <div class="interview-sync-bar">
          <div class="interview-sync-text">
            <strong>如何同步面试进度：</strong>「已约面」可由助手从 BOSS 约面消息自动同步；后续的一面结果、二面/终面安排、面经复盘与 Offer 进展，在对话里直接口述告诉助手即可记入台账。
          </div>
          <button type="button" class="copy-action-btn secondary" data-copy="{_esc(interview_report_cmd)}" data-done-label="✓ 已复制口述模板，贴给助手改公司名即可">复制口述记录模板</button>
        </div>

        <div class="section-block">
          <div class="section-header">
            <h2 class="section-title">即将到来的面试日程（{len(upcoming_schedule_items)}）</h2>
            <span class="section-hint">按时间顺序列出所有待面试场次</span>
          </div>
          {upcoming_schedule_html}
        </div>

        {offer_section_html}

        <div class="section-block">
          <div class="section-header">
            <h2 class="section-title">进行中面试（{len(active_interview_jobs)}）</h2>
            <span class="section-hint">已约面及多轮推进中的岗位与各轮时间线</span>
          </div>
          <div class="cards-stack">
            {active_interview_jobs_html}
          </div>
        </div>

        {archived_interview_section_html}
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
          {strategy_editor_html}
          <div class="strategy-card">
            <h3>自动化档位与作息护栏 {"<span class='pro-active-pill'>Pro 托管已生效</span>" if is_pro else f"<a class='lock-pill' href='{pro_checkout_url}' target='_blank' rel='noopener noreferrer'>升级 Pro 解锁 →</a>"}</h3>
            <div class="kv-list">
              <div class="kv-item"><span class="kv-key">交互运行模式</span><span class="kv-val">{"副驾驶粘贴协同（无浏览器操控权限兜底）" if str(config.get("execution_mode", "browser")) == "copilot" else ("浏览器直连自动化 · ego lite（省 Token 持久化窗口）" if str(config.get("browser_provider", "auto")) == "ego_lite" else ("浏览器直连自动化 · 宿主自带 Browser Use（有头持久化）" if str(config.get("browser_provider", "auto")) == "builtin_browser_use" else "浏览器直连自动化（复用已登录窗口）"))}</span></div>
              <div class="kv-item"><span class="kv-key">简历底座形态</span><span class="kv-val">{"暂无简历（请先准备本地简历或填写平台在线简历）" if str(config.get("resume_source", "")) == "none" else ("待导入简历（请拖入 PDF 或授权读取平台在线简历）" if str(config.get("resume_source", "")) == "pending" else ("仅平台在线简历（建议在 BOSS 在线简历页生成附件 PDF）" if (str(config.get("resume_source", "")) == "online_only" or not bool(config.get("has_attachment_resume", True))) else "平台在线 + PDF 附件简历齐全"))}</span></div>
              <div class="kv-item"><span class="kv-key">主动找岗模式</span><span class="kv-val">{_esc("全自动定额破冰（已开启）" if pro_auto_discover else ("仅自动收藏·不主动投递（巡检自动收藏）" if is_favorite_discover else "半自动审阅确认")) if is_pro else ("仅自动收藏·不主动投递（单轮收藏，定时巡检需 Pro）" if is_favorite_discover else "半自动确认（可选仅自动收藏；全自动破冰需 Pro）")}</span></div>
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
              <div class="kv-item"><span class="kv-key">期望最低月薪</span><span class="kv-val">{int(config.get("min_salary_k", 0))}K（{_esc(salary_mode_short_cn(config))}）</span></div>
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

          {voice_card_html}

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
        interviews: document.getElementById("tab-interviews"),
        ledger: document.getElementById("tab-ledger"),
        strategy: document.getElementById("tab-strategy")
      }};

      function activateTab(target) {{
        tabButtons.forEach(function (b) {{
          b.classList.toggle("active", b.getAttribute("data-tab-target") === target);
        }});
        Object.keys(panels).forEach(function (k) {{
          if (panels[k]) {{
            panels[k].classList.toggle("active", k === target);
          }}
        }});
      }}

      tabButtons.forEach(function (btn) {{
        btn.addEventListener("click", function () {{
          activateTab(btn.getAttribute("data-tab-target"));
        }});
      }});

      window.hmOpenStrategyTab = function () {{
        activateTab("strategy");
        const ed = document.getElementById("hm-strategy-editor");
        if (ed && ed.scrollIntoView) {{
          ed.scrollIntoView({{ behavior: "smooth", block: "start" }});
        }}
      }};

      window.addEventListener("message", function (ev) {{
        if (ev && ev.data && ev.data.type === "hm-open-strategy") {{
          window.hmOpenStrategyTab();
        }}
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

      function setLedgerFilter(statusKey) {{
        activeStatus = statusKey || "all";
        filterButtons.forEach(function (p) {{
          p.classList.toggle("active", (p.getAttribute("data-filter") || "all") === activeStatus);
        }});
        applyLedgerFilter();
      }}

      if (searchInput) {{
        searchInput.addEventListener("input", applyLedgerFilter);
      }}

      filterButtons.forEach(function (pill) {{
        pill.addEventListener("click", function () {{
          setLedgerFilter(pill.getAttribute("data-filter") || "all");
        }});
      }});

      document.querySelectorAll("[data-jump-tab]").forEach(function (el) {{
        el.addEventListener("click", function () {{
          const target = el.getAttribute("data-jump-tab");
          const jumpFilter = el.getAttribute("data-jump-filter");
          if (target) activateTab(target);
          if (target === "ledger" && jumpFilter) {{
            if (searchInput) searchInput.value = "";
            setLedgerFilter(jumpFilter);
          }}
        }});
      }});

      /* 卡片视图 / 漏斗图切换（带本地偏好记忆） */
      const funnelSwitchBtns = document.querySelectorAll("[data-funnel-view]");
      const kpiCardsPanel = document.getElementById("kpi-cards-panel");
      const funnelVizPanel = document.getElementById("funnel-viz-panel");
      const VIEW_STORAGE_KEY = "hiremate_dashboard_funnel_view";

      function setFunnelView(mode) {{
        const isFunnel = mode === "funnel";
        if (kpiCardsPanel) kpiCardsPanel.hidden = isFunnel;
        if (funnelVizPanel) funnelVizPanel.hidden = !isFunnel;
        funnelSwitchBtns.forEach(function (b) {{
          const active = b.getAttribute("data-funnel-view") === (isFunnel ? "funnel" : "cards");
          b.classList.toggle("active", active);
          b.setAttribute("aria-pressed", active ? "true" : "false");
        }});
        try {{
          window.localStorage.setItem(VIEW_STORAGE_KEY, isFunnel ? "funnel" : "cards");
        }} catch (e) {{}}
      }}

      funnelSwitchBtns.forEach(function (btn) {{
        btn.addEventListener("click", function () {{
          setFunnelView(btn.getAttribute("data-funnel-view") || "cards");
        }});
      }});

      try {{
        const savedView = window.localStorage.getItem(VIEW_STORAGE_KEY);
        if (savedView === "funnel" || savedView === "cards") {{
          setFunnelView(savedView);
        }}
      }} catch (e) {{}}

      /* 漏斗图悬停/聚焦实时解读条 */
      const inspectorEl = document.getElementById("fv-inspector");
      if (inspectorEl) {{
        const defaultTip = inspectorEl.textContent;
        document.querySelectorAll("[data-funnel-tip]").forEach(function (el) {{
          const tip = el.getAttribute("data-funnel-tip") || defaultTip;
          el.addEventListener("mouseenter", function () {{ inspectorEl.textContent = tip; }});
          el.addEventListener("focus", function () {{ inspectorEl.textContent = tip; }});
          el.addEventListener("mouseleave", function () {{ inspectorEl.textContent = defaultTip; }});
          el.addEventListener("blur", function () {{ inspectorEl.textContent = defaultTip; }});
        }});
      }}

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

      /* 在线策略配置表单保存与跨宿主自动桥接 */
      const stratForm = document.getElementById("hm-strategy-form");
      const stratStatusEl = document.getElementById("hm-save-strategy-status");
      const saveBtn = document.getElementById("hm-save-strategy-btn");
      const riskBox = document.getElementById("hm-auto-risk-box");
      const ackRiskChk = document.getElementById("hm-cfg-ack-risk");
      const modeDiscSel = document.getElementById("hm-cfg-mode-discover");
      const modeReplySel = document.getElementById("hm-cfg-mode-reply");

      function setStratStatus(msg, isErr) {{
        if (!stratStatusEl) return;
        stratStatusEl.textContent = msg || "";
        stratStatusEl.classList.toggle("is-error", Boolean(isErr));
      }}

      try {{
        const initTab = window.__HM_INITIAL_TAB__ || window.sessionStorage.getItem("hm_open_tab") || (window.location.hash === "#strategy" ? "strategy" : "");
        const initToast = window.__HM_INITIAL_TOAST__ || window.sessionStorage.getItem("hm_save_toast") || "";
        if (initTab === "strategy") {{
          activateTab("strategy");
          window.sessionStorage.removeItem("hm_open_tab");
        }}
        if (initToast) {{
          setStratStatus(initToast, false);
          window.sessionStorage.removeItem("hm_save_toast");
        }}
      }} catch (e) {{}}

      function splitCsvInput(id) {{
        const el = document.getElementById(id);
        if (!el) return [];
        return String(el.value || "")
          .replace(/[，、；;]/g, ",")
          .split(",")
          .map(function (s) {{ return s.trim(); }})
          .filter(Boolean);
      }}

      function syncAutoRiskVisibility() {{
        if (!stratForm || !riskBox) return;
        const alreadyAcked = stratForm.getAttribute("data-already-acked") === "1";
        const wantsAuto =
          (modeDiscSel && modeDiscSel.value === "auto") ||
          (modeReplySel && modeReplySel.value === "auto");
        riskBox.hidden = alreadyAcked || !wantsAuto;
      }}

      if (modeDiscSel) modeDiscSel.addEventListener("change", syncAutoRiskVisibility);
      if (modeReplySel) modeReplySel.addEventListener("change", syncAutoRiskVisibility);
      if (stratForm) {{
        stratForm.addEventListener("reset", function () {{
          setTimeout(function () {{
            syncAutoRiskVisibility();
            setStratStatus("已恢复为当前磁盘已保存值", false);
          }}, 0);
        }});
        syncAutoRiskVisibility();

        stratForm.addEventListener("submit", async function (ev) {{
          ev.preventDefault();
          if (stratForm.getAttribute("data-is-demo") === "1") {{
            setStratStatus("当前为虚构示例预览模式，不会覆盖本机真实配置。", true);
            return;
          }}
          const alreadyAcked = stratForm.getAttribute("data-already-acked") === "1";
          const mdVal = modeDiscSel ? modeDiscSel.value : "semi";
          const mrVal = modeReplySel ? modeReplySel.value : "semi";
          const wantsAuto = mdVal === "auto" || mrVal === "auto";
          const ackChecked = Boolean(ackRiskChk && ackRiskChk.checked);
          if (wantsAuto && !alreadyAcked && !ackChecked) {{
            if (riskBox) riskBox.hidden = false;
            setStratStatus("请先勾选上方的全自动风控护栏确认框后再保存。", true);
            return;
          }}

          const minSalEl = document.getElementById("hm-cfg-min-salary");
          const maxRoundEl = document.getElementById("hm-cfg-max-round");
          const maxDayEl = document.getElementById("hm-cfg-max-day");
          const weekdaysEl = document.getElementById("hm-cfg-weekdays");
          const remoteFlexEl = document.getElementById("hm-cfg-remote-flex");
          const salModeEl = document.getElementById("hm-cfg-salary-mode");
          const outEl = document.getElementById("hm-cfg-outsourcing");
          const onsiteEl = document.getElementById("hm-cfg-onsite");
          const hhEl = document.getElementById("hm-cfg-headhunter");
          const inbEl = document.getElementById("hm-cfg-inbound-resume");

          const patch = {{
            mode_discover: mdVal,
            mode_reply: mrVal,
            target_roles: splitCsvInput("hm-cfg-roles"),
            preferred_cities: splitCsvInput("hm-cfg-cities"),
            remote_bigtech_flex: !remoteFlexEl || remoteFlexEl.value === "true",
            min_salary_k: Math.max(0, parseInt(minSalEl ? minSalEl.value : "20", 10) || 0),
            salary_match_mode: salModeEl ? salModeEl.value : "ceiling_tolerant",
            outsourcing_policy: outEl ? outEl.value : "block_all",
            onsite_policy: onsiteEl ? onsiteEl.value : "block_all",
            headhunter_policy: hhEl ? hhEl.value : "inbound_only",
            inbound_resume_policy: inbEl ? inbEl.value : "generous",
            past_employers: splitCsvInput("hm-cfg-employers"),
            blocked_companies: splitCsvInput("hm-cfg-blocked"),
            exclude_keywords: splitCsvInput("hm-cfg-exclude-kws"),
            schedule: {{
              max_per_round: Math.min(8, Math.max(1, parseInt(maxRoundEl ? maxRoundEl.value : "5", 10) || 5)),
              max_per_day: Math.min(30, Math.max(1, parseInt(maxDayEl ? maxDayEl.value : "20", 10) || 20)),
              weekdays_only: !weekdaysEl || weekdaysEl.value === "true"
            }}
          }};

          if (saveBtn) saveBtn.disabled = true;
          setStratStatus("正在保存至本机 config.json…", false);

          function fallbackCopyCliCmd() {{
            const ackPart = (wantsAuto && ackChecked) ? " --ack-auto-risk" : "";
            const cmdStr = "请将我的求职助手策略配置更新为：" + JSON.stringify(patch) + (ackPart ? "（本人已确认授权开启全自动护栏）" : "");
            const okHint = "当前为离线本地文件预览，已复制策略更新口令，粘贴发给 AI 助手即可生效（在侧边栏工作台内可直接一键保存）";
            const manualHint = "当前环境无法直接写本机文件，请复制以下口令发给 AI 助手生效：" + cmdStr;
            function legacyCopy(text) {{
              try {{
                const ta = document.createElement("textarea");
                ta.value = text;
                ta.setAttribute("readonly", "");
                ta.style.position = "fixed";
                ta.style.opacity = "0";
                document.body.appendChild(ta);
                ta.select();
                const ok = Boolean(document.execCommand && document.execCommand("copy"));
                document.body.removeChild(ta);
                return ok;
              }} catch (e) {{
                return false;
              }}
            }}
            if (navigator.clipboard && navigator.clipboard.writeText) {{
              navigator.clipboard.writeText(cmdStr).then(function () {{
                setStratStatus(okHint, false);
              }}).catch(function () {{
                setStratStatus(legacyCopy(cmdStr) ? okHint : manualHint, false);
              }});
            }} else {{
              setStratStatus(legacyCopy(cmdStr) ? okHint : manualHint, false);
            }}
          }}

          try {{
            if (typeof window.__HIREMATE_SAVE_CONFIG__ === "function") {{
              const res = await window.__HIREMATE_SAVE_CONFIG__(patch, ackChecked);
              if (res && res.ok) {{
                setStratStatus(res.notice || "✅ 策略已保存！Agent 下次搜岗与巡检将自动按最新选项执行", false);
              }} else {{
                if (res && res.code === "AUTO_RISK_CONFIRMATION_REQUIRED" && riskBox) riskBox.hidden = false;
                setStratStatus((res && res.error) || "保存失败", true);
              }}
            }} else if (window.location.protocol !== "file:") {{
              const fetcher = (window.sidecar && typeof window.sidecar.fetch === "function")
                ? window.sidecar.fetch.bind(window.sidecar)
                : window.fetch.bind(window);
              const apiUrl = /^\\/hiremate(\\/|$)/.test(window.location.pathname || "")
                ? "/hiremate/api/config/save"
                : "/api/config/save";
              const resp = await fetcher(apiUrl, {{
                method: "POST",
                headers: {{
                  "Content-Type": "application/json",
                  "X-Hiremate-Same-Origin": "1"
                }},
                body: JSON.stringify({{ patch: patch, ack_auto_risk: ackChecked }})
              }});
              const data = await resp.json();
              if (data && data.ok) {{
                const okMsg = data.notice || "✅ 策略已保存至本机！Agent 下次搜岗与巡检将自动按最新选项执行";
                try {{
                  window.sessionStorage.setItem("hm_open_tab", "strategy");
                  window.sessionStorage.setItem("hm_save_toast", okMsg);
                }} catch (e) {{}}
                setStratStatus(okMsg, false);
                window.location.reload();
                return;
              }}
              if (data && data.code === "AUTO_RISK_CONFIRMATION_REQUIRED" && riskBox) {{
                riskBox.hidden = false;
              }}
              setStratStatus((data && data.error) || "保存失败", true);
            }} else {{
              fallbackCopyCliCmd();
            }}
          }} catch (err) {{
            fallbackCopyCliCmd();
          }} finally {{
            if (saveBtn) saveBtn.disabled = false;
          }}
        }});
      }}
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
        voice_md = (
            "# 个人求职语气档案（示例：林远）\n"
            "- 句长与标点：偏好 20～45 字紧凑短句，句末常省略句号，从不使用波浪号「～」与感叹号堆砌\n"
            "- 叙事与用词：讲项目直接给业务场景与量化结果，保留 RAG / Agent 等英文原词，排斥「赋能、抓手、闭环生态」\n"
            "- 递简历附言习惯：「好的，附件简历刚发您了，方便时请过目」"
        )
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
        jobs = ledger.list_jobs(limit=2000, include_interviews=True)
        summary = ledger.summary()
        pf = profile_facts_path(workspace)
        profile_md = pf.read_text(encoding="utf-8")[:1500] if pf.is_file() else ""
        vp = workspace / "voice_profile.md"
        voice_md = vp.read_text(encoding="utf-8")[:1200] if vp.is_file() else ""
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
        voice_excerpt=voice_md,
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
