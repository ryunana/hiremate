"""公司底细核查与正交二维判定引擎（纯本地零网络，同库 SQLite 存储）。

设计原则：
1. 与账本共用 ledger.sqlite3（WAL + busy_timeout），写入使用 BEGIN IMMEDIATE 事务，
   异常转为结构化错误码（如 DB_BUSY / IDENTITY_CONFLICT），不向 stdout 吐 traceback；
2. 事实列允许 NULL，二次补抓采用字段级 COALESCE(NULLIF(TRIM(...), ''), old) 合并，
   并将空白/占位词转为 None，绝不清空已有工商信息；支持显式 clear_fields；
3. 主键为归一化 company_core，附带 legal_name 与 company_clean 校验防同名异地撞车，
   同集团规模取最大值防子公司小规模覆盖母公司；
4. profile_probed_at 与 jobs_probed_at 独立计时（30 天过期独立标 stale）；
5. verdict 不落库，每次按当前 config（preferred_cities / remote_bigtech_flex /
   outsourcing_policy / target_roles）实时重算，由「可行性 × 可靠性」正交交叉决定，
   每条风险均附原始证据摘句，杜绝黑箱分与关键词一票否决误伤。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from .ledger import JobLedger
from .normalize import (
    companies_match,
    normalize_company_parts,
)
from .workspace import load_config, now_iso

STALE_DAYS = 30

_PLACEHOLDER_VALUES = frozenset({
    "",
    "[]",
    "{}",
    "暂无",
    "未完善",
    "未知",
    "无",
    "-",
    "--",
    "n/a",
    "none",
    "null",
})

# 经营范围强/弱信号
_STRONG_OUTSOURCING_SCOPE_KWS = (
    "劳务派遣",
    "劳务服务",
    "人力资源服务",
    "劳务外包",
    "人力外包",
    "人才外包",
    "职业中介",
)
_WEAK_OUTSOURCING_SCOPE_KWS = (
    "软件外包服务",
    "信息技术外包",
    "承接档案服务外包",
    "业务流程外包",
)

# 客户品牌词（仅当不出现在公司自身名称/经营范围中时才记弱风险提示，绝不单独一票否决）
_CLIENT_BRAND_KWS = ("银行", "航空", "超市", "电信", "电网", "地铁")

# 门店/基础服务岗关键词
_STORE_ROLE_KWS = (
    "店员",
    "营业员",
    "理货",
    "收银",
    "服务员",
    "茶艺",
    "客服专员",
    "配餐",
    "保安",
    "保洁",
)

# 跨界传统行业曾用名关键词
_CROSS_INDUSTRY_FORMER_KWS = (
    "建筑",
    "工程",
    "商贸",
    "贸易",
    "餐饮",
    "物流",
    "物业",
    "劳务",
    "装潢",
    "建材",
    "农业",
)

# 岗位职能细分与四大不相干簇（防止 15 人初创招 产品+算法+后端 同属 software 簇被误杀）
_ROLE_CLUSTERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("software", "ai_algo", ("算法", "nlp", "大模型", "llm", "语音", "视觉", "cv", "ai infra", "机器学习", "深度学习")),
    ("software", "eng", ("后端", "前端", "全栈", "golang", "java", "python", "c++", "研发", "开发", "测试", "运维", "架构")),
    ("software", "product_design", ("产品经理", "产品", "ui", "ux", "交互", "设计")),
    ("hardware_chip", "chip_ic", ("芯片", "ic", "fpga", "soc", "半导体", "流片", "eda")),
    ("hardware_chip", "hardware_sensor", ("激光雷达", "雷达", "硬件", "嵌入式", "pcb", "驱动", "传感器", "结构工程", "电子工程")),
    ("store_ops", "store_service", _STORE_ROLE_KWS),
    ("sales_hr", "sales_hr_admin", ("销售", "电销", "地推", "招聘", "hr", "行政", "财务", "会计")),
)

# 常见城市 -> 省份映射（用于注册地与办公地跨省粗判）
_CITY_PROVINCE: dict[str, str] = {
    "北京": "北京",
    "上海": "上海",
    "天津": "天津",
    "重庆": "重庆",
    "深圳": "广东",
    "广州": "广东",
    "东莞": "广东",
    "佛山": "广东",
    "珠海": "广东",
    "惠州": "广东",
    "中山": "广东",
    "杭州": "浙江",
    "宁波": "浙江",
    "温州": "浙江",
    "南京": "江苏",
    "苏州": "江苏",
    "无锡": "江苏",
    "常州": "江苏",
    "成都": "四川",
    "武汉": "湖北",
    "长沙": "湖南",
    "西安": "陕西",
    "郑州": "河南",
    "合肥": "安徽",
    "厦门": "福建",
    "福州": "福建",
    "济南": "山东",
    "青岛": "山东",
    "大连": "辽宁",
    "沈阳": "辽宁",
    "昆明": "云南",
    "贵阳": "贵州",
    "南宁": "广西",
    "南昌": "江西",
    "太原": "山西",
    "石家庄": "河北",
    "哈尔滨": "黑龙江",
    "长春": "吉林",
}

_CLEARABLE_FIELDS = frozenset({
    "scale",
    "scale_max_num",
    "financing_stage",
    "insured_count",
    "established",
    "former_name",
    "business_scope",
    "reg_city",
    "office_city",
    "intro",
    "jobs_json",
})

_CREDIBLE_FINANCING_RE = re.compile(
    r"(?:已上市|上市公司|港股|美股|科创板|创业板|主板上市|[defg]\s*轮|pre-?ipo)",
    re.IGNORECASE,
)

_CREDIBLE_BIGTECH_NAME_HINTS: tuple[str, ...] = (
    "腾讯",
    "阿里巴巴",
    "阿里云",
    "淘天",
    "蚂蚁集团",
    "支付宝",
    "字节跳动",
    "抖音",
    "火山引擎",
    "飞书",
    "百度",
    "美团",
    "京东",
    "拼多多",
    "快手",
    "网易",
    "小米",
    "华为",
    "荣耀",
    "中兴",
    "小红书",
    "哔哩哔哩",
    "携程",
    "滴滴",
    "顺丰",
    "丰巢",
    "菜鸟",
    "大疆",
    "影石",
    "拓竹",
    "宇树",
    "智元",
    "蔚来",
    "理想汽车",
    "小鹏",
    "比亚迪",
    "极氪",
    "吉利",
    "赛力斯",
    "广汽",
    "长安汽车",
    "上汽",
    "宁德时代",
    "海康威视",
    "大华股份",
    "科大讯飞",
    "金山",
    "传音",
    "歌尔",
    "立讯",
    "蓝思",
    "迈瑞",
    "联影",
    "oppo",
    "vivo",
    "欧珀",
    "维沃",
    "得物",
    "哈啰",
    "货拉拉",
    "满帮",
    "猿辅导",
    "作业帮",
    "米哈游",
    "莉莉丝",
    "叠纸",
    "鹰角",
    "库洛",
    "三七互娱",
    "完美世界",
    "智谱",
    "月之暗面",
    "百川智能",
    "零一万物",
    "阶跃星辰",
    "深度求索",
    "稀宇",
    "minimax",
    "商汤",
    "旷视",
    "云从",
    "依图",
    "第四范式",
    "合合信息",
    "微众银行",
    "网商银行",
    "招银网络",
    "平安科技",
    "中国移动",
    "中移",
    "中国电信",
    "天翼",
    "中国联通",
    "联通数科",
    "国家电网",
    "南方电网",
    "中科院",
    "中国科学院",
    "鹏城实验室",
    "之江实验室",
    "人工智能实验室",
    "智源研究院",
)


class CompanyIntelError(Exception):
    """结构化公司核查错误，避免向 stdout 输出 traceback。"""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "code": self.code, "error": self.message}
        out.update(self.extra)
        return out


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _PLACEHOLDER_VALUES:
        return None
    return text


def _parse_iso_utc(raw: str | None) -> datetime | None:
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


def _parse_scale_max(scale_text: str | None) -> int | None:
    text = str(scale_text or "").strip()
    if not text:
        return None
    # 兼容 "A轮 · 0-20人" / "50-99人" / "1000-9999人" / "10000人以上" / "20人以下"
    m_range = re.search(r"(\d+)\s*[-~—至]\s*(\d+)\s*人?", text)
    if m_range:
        return int(m_range.group(2))
    m_above = re.search(r"(\d+)\s*人?\s*(?:以上|\+)", text)
    if m_above:
        return int(m_above.group(1))
    m_below = re.search(r"(\d+)\s*人?\s*以下", text)
    if m_below:
        return int(m_below.group(1))
    return None


def _parse_insured_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = str(value).strip()
    if not text or text.lower() in _PLACEHOLDER_VALUES:
        return None
    m = re.search(r"(\d+)", text.replace(",", ""))
    return int(m.group(1)) if m else None


def _is_credible_bigtech(
    *,
    legal_name: str,
    company_clean: str,
    scale_max: int | None,
    scale_text: str | None,
    financing_stage: str | None,
    intro: str | None,
    has_inflated_scale: bool,
) -> bool:
    """多因子识别可信大厂 / 上市企业 / 集团研究院；若触发参保规模严重倒挂则立即剥夺豁免。"""
    if has_inflated_scale:
        return False
    if scale_max is not None and scale_max >= 1000:
        return True
    fin_blob = f"{financing_stage or ''} {scale_text or ''} {intro or ''}"
    if _CREDIBLE_FINANCING_RE.search(fin_blob):
        return True
    name_blob = f"{legal_name} {company_clean}".lower()
    if any(h.lower() in name_blob for h in _CREDIBLE_BIGTECH_NAME_HINTS):
        return True
    if ("研究院" in name_blob or "实验室" in name_blob) and (scale_max is None or scale_max >= 100):
        return True
    return False


def _parse_established_years(established: str | None, now_dt: datetime) -> float | None:
    text = str(established or "").strip()
    if not text:
        return None
    m = re.search(r"(19\d{2}|20\d{2})(?:[-/.年](\d{1,2}))?(?:[-/.月](\d{1,2}))?", text)
    if not m:
        return None
    yr = int(m.group(1))
    mo = max(1, min(12, int(m.group(2) or 6)))
    dy = max(1, min(28, int(m.group(3) or 15)))
    try:
        est_dt = datetime(yr, mo, dy, tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, (now_dt - est_dt).total_seconds() / (86400.0 * 365.25))


def _extract_cities_from_text(text: str | None) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    found: list[str] = []
    for city in _CITY_PROVINCE:
        if city in raw and city not in found:
            found.append(city)
    if not found and 1 < len(raw) <= 8:
        clean_raw = raw.split("·")[0].split("-")[0].replace("市", "").strip()
        if clean_raw:
            found.append(clean_raw)
    return found


def _province_of(location_text: str | None) -> str | None:
    text = str(location_text or "").strip()
    if not text:
        return None
    for city, prov in _CITY_PROVINCE.items():
        if city in text:
            return prov
    m_prov = re.search(r"(广东|浙江|江苏|四川|湖北|湖南|陕西|河南|安徽|福建|山东|辽宁|云南|贵州|广西|江西|山西|河北|黑龙江|吉林)", text)
    if m_prov:
        return m_prov.group(1)
    return None


def _open_db(workspace: Path) -> sqlite3.Connection:
    led = JobLedger(workspace)
    conn = sqlite3.connect(str(led.db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _merge_jobs(
    existing_json: str | None,
    new_jobs: list[dict[str, Any]] | None,
    ts: str,
) -> tuple[str | None, list[dict[str, Any]]]:
    """按 job_id（无则按 title+city）做并集合并，记录 last_seen_at。"""
    merged: dict[str, dict[str, Any]] = {}
    if existing_json:
        try:
            old_list = json.loads(existing_json)
            if isinstance(old_list, list):
                for item in old_list:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    if not title:
                        continue
                    city = str(item.get("city") or "").strip()
                    key = str(item.get("job_id") or "").strip() or f"{title}||{city}"
                    merged[key] = dict(item)
        except json.JSONDecodeError:
            pass

    if new_jobs:
        for raw in new_jobs:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            city = str(raw.get("city") or "").strip()
            salary = str(raw.get("salary") or "").strip()
            job_id = str(raw.get("job_id") or "").strip()
            key = job_id or f"{title}||{city}"
            prev = merged.get(key) or {}
            merged[key] = {
                "job_id": job_id or str(prev.get("job_id") or ""),
                "title": title,
                "city": city or str(prev.get("city") or ""),
                "salary": salary or str(prev.get("salary") or ""),
                "last_seen_at": ts,
            }

    if not merged:
        return None, []
    items = list(merged.values())
    return json.dumps(items, ensure_ascii=False), items


def _check_identity_conflict(
    existing_clean: str,
    existing_legal: str,
    incoming_clean: str,
    incoming_legal: str,
) -> bool:
    """防同名异地不同主体撞车（如「深圳星辰」与「杭州星辰」）。"""
    # 优先检查完整工商名中的跨省地域前缀冲突（normalize_company_parts 会剥离城市前缀，故须查 legal_name）
    prov_a = _province_of(existing_legal) or _province_of(existing_clean)
    prov_b = _province_of(incoming_legal) or _province_of(incoming_clean)
    if prov_a and prov_b and prov_a != prov_b:
        return True
    if not existing_clean or not incoming_clean:
        return False
    if existing_clean == incoming_clean:
        return False
    if incoming_clean in existing_clean or existing_clean in incoming_clean:
        return False
    if existing_legal and incoming_legal and existing_legal == incoming_legal:
        return False
    if companies_match(existing_legal or existing_clean, incoming_legal or incoming_clean):
        return False
    return True


def evaluate_company_record(
    record: dict[str, Any],
    *,
    config: dict[str, Any],
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """纯函数正交二维判定引擎（verdict 不落库，每次按当前 config 实时重算）。"""
    moment = now_dt or datetime.now(timezone.utc)
    preferred_cities = [str(c).strip() for c in (config.get("preferred_cities") or []) if str(c).strip()]
    remote_bigtech_flex = bool(config.get("remote_bigtech_flex", True))
    out_policy = str(config.get("outsourcing_policy") or ("block_all" if config.get("filter_outsourcing", True) else "allow_all"))
    target_roles = [str(r).strip().lower() for r in (config.get("target_roles") or []) if str(r).strip()]

    legal_name = str(record.get("legal_name") or record.get("company_clean") or record.get("company_core") or "")
    company_clean_str = str(record.get("company_clean") or "")
    scale_text = _clean_optional_text(record.get("scale"))
    scale_max = record.get("scale_max_num")
    if scale_max is None and scale_text:
        scale_max = _parse_scale_max(scale_text)
    financing_stage = _clean_optional_text(record.get("financing_stage"))
    insured_count = _parse_insured_count(record.get("insured_count"))
    established = _clean_optional_text(record.get("established"))
    former_name = _clean_optional_text(record.get("former_name"))
    business_scope = _clean_optional_text(record.get("business_scope"))
    reg_city = _clean_optional_text(record.get("reg_city"))
    office_city = _clean_optional_text(record.get("office_city"))
    intro = _clean_optional_text(record.get("intro"))

    has_inflated_scale = (
        scale_max is not None
        and scale_max >= 500
        and insured_count is not None
        and insured_count < 50
        and insured_count < scale_max * 0.1
    )

    # 解析 jobs 并过滤出 30 天内活跃岗位用于跨度与门店占比分析
    all_jobs: list[dict[str, Any]] = []
    raw_jobs = record.get("jobs_json")
    if isinstance(raw_jobs, str) and raw_jobs.strip():
        try:
            parsed = json.loads(raw_jobs)
            if isinstance(parsed, list):
                all_jobs = [x for x in parsed if isinstance(x, dict)]
        except json.JSONDecodeError:
            all_jobs = []
    elif isinstance(raw_jobs, list):
        all_jobs = [x for x in raw_jobs if isinstance(x, dict)]

    cutoff = moment - timedelta(days=STALE_DAYS)
    recent_jobs: list[dict[str, Any]] = []
    for j in all_jobs:
        seen_dt = _parse_iso_utc(j.get("last_seen_at"))
        if seen_dt is None or seen_dt >= cutoff:
            recent_jobs.append(j)

    # 1. 可行性维度（Feasibility）：动态按 preferred_cities 判定，空列表表示不限城市
    job_cities: set[str] = set()
    for j in recent_jobs:
        for c in _extract_cities_from_text(str(j.get("city") or "") + " " + str(j.get("title") or "")):
            job_cities.add(c)
    if office_city:
        for c in _extract_cities_from_text(office_city):
            job_cities.add(c)

    credible_bigtech = _is_credible_bigtech(
        legal_name=legal_name,
        company_clean=company_clean_str,
        scale_max=scale_max,
        scale_text=scale_text,
        financing_stage=financing_stage,
        intro=intro,
        has_inflated_scale=has_inflated_scale,
    )

    if not preferred_cities:
        feasibility = "local_match"
    elif not job_cities:
        feasibility = "unknown"
    elif any(any(pc in jc or jc in pc for jc in job_cities) for pc in preferred_cities):
        feasibility = "local_match"
    elif remote_bigtech_flex and credible_bigtech:
        feasibility = "remote_flex"
    else:
        feasibility = "remote_mismatch"

    # 2. 可靠性维度（Reliability）：分强/弱证据与正交累加
    risk_items: list[dict[str, Any]] = []

    def _add_risk(code: str, severity: str, summary: str, evidence: str) -> None:
        risk_items.append({
            "code": code,
            "severity": severity,  # critical | high | weak
            "summary": summary,
            "evidence": evidence,
        })

    if has_inflated_scale:
        _add_risk(
            "inflated_scale",
            "high",
            f"面板标称规模（{scale_text or f'{scale_max}人'}）与工商参保人数（{insured_count}人）严重倒挂，疑似规模注水或挂靠空壳",
            f"scale={scale_text or scale_max}, insured_count={insured_count}",
        )

    # 2a. 经营范围强/弱外包信号
    strong_scope_hits = [k for k in _STRONG_OUTSOURCING_SCOPE_KWS if business_scope and k in business_scope]
    weak_scope_hits = [k for k in _WEAK_OUTSOURCING_SCOPE_KWS if business_scope and k in business_scope]
    if strong_scope_hits:
        _add_risk(
            "strong_outsourcing_scope",
            "high",
            f"经营范围包含劳务/人力派遣强信号（{'、'.join(strong_scope_hits)}）",
            business_scope or "",
        )
    elif weak_scope_hits:
        _add_risk(
            "weak_outsourcing_scope",
            "weak",
            f"经营范围含常见外包模板词（{'、'.join(weak_scope_hits)}），需结合岗位实质核实",
            business_scope or "",
        )

    # 2b. 门店/基础岗混招（比例阈值 + 目标岗位命中 + 经营范围强信号）
    store_jobs = [
        j for j in recent_jobs
        if any(k in str(j.get("title") or "") for k in _STORE_ROLE_KWS)
    ]
    if store_jobs and recent_jobs:
        store_ratio = len(store_jobs) / float(len(recent_jobs))
        has_target_role_match = bool(
            target_roles
            and any(
                any(tr in str(j.get("title") or "").lower() for tr in target_roles)
                for j in recent_jobs
                if j not in store_jobs
            )
        )
        sample_titles = "、".join(str(j.get("title") or "") for j in store_jobs[:3])
        if strong_scope_hits or (len(recent_jobs) >= 5 and store_ratio >= 0.30 and not has_target_role_match):
            sev = "critical" if out_policy != "allow_all" else "weak"
            _add_risk(
                "mixed_labor_dispatch",
                sev,
                f"在招 {len(recent_jobs)} 个岗位混有 {len(store_jobs)} 个门店/基础岗（占比 {store_ratio:.0%}）"
                + ("且经营范围含劳务/人力服务，典型劳务派遣画像" if strong_scope_hits else "，存在劳务派遣风险"),
                f"门店岗示例：{sample_titles}" + (f"；经营范围：{business_scope}" if strong_scope_hits else ""),
            )

    # 2c. 客户品牌词（若已出现在公司名或经营范围中则完全不记风险；否则仅记 weak 提示，不一票否决）
    self_identity_blob = f"{legal_name} {business_scope or ''} {intro or ''}"
    client_brand_hits: list[str] = []
    for j in recent_jobs:
        t = str(j.get("title") or "")
        for kw in _CLIENT_BRAND_KWS:
            if kw in t and kw not in self_identity_blob:
                client_brand_hits.append(f"{t}（命中「{kw}」）")
                break
    if client_brand_hits:
        _add_risk(
            "client_site_hint",
            "weak",
            "部分岗位标题含外部行业品牌词，建议确认是否属甲方派驻（不单独一票否决）",
            "；".join(client_brand_hits[:3]),
        )

    # 2d. 岗位职能跨度（按簇计算：0-20 人且 ≥3 细分类别 且 ≥2 个不相干大簇）
    hit_subcats: set[str] = set()
    hit_clusters: set[str] = set()
    cluster_samples: dict[str, str] = {}
    for j in recent_jobs:
        tl = str(j.get("title") or "").lower()
        for cluster_name, subcat, kws in _ROLE_CLUSTERS:
            if any(k in tl for k in kws):
                hit_subcats.add(subcat)
                hit_clusters.add(cluster_name)
                cluster_samples.setdefault(cluster_name, str(j.get("title") or ""))
    if scale_max is not None and scale_max <= 20 and len(hit_subcats) >= 3 and len(hit_clusters) >= 2:
        _add_risk(
            "abnormal_role_span",
            "high",
            f"小规模企业（{scale_text or f'≤{scale_max}人'}）同时在招 {len(hit_subcats)} 类跨 {len(hit_clusters)} 大不相干职能，存在一次性空壳组队风险",
            " / ".join(f"{k}:{v}" for k, v in cluster_samples.items()),
        )

    # 2e. 规模过小 + 简介未完善
    if scale_max is not None and scale_max <= 20 and not intro:
        _add_risk(
            "tiny_company_no_intro",
            "weak",
            f"企业规模较小（{scale_text or f'≤{scale_max}人'}）且公司简介未完善，背景透明度低",
            f"scale={scale_text or scale_max}, intro=缺失",
        )

    # 2f. 曾用名跨行业（弱风险；叠加 成立 < 3 年 且 ≤20 人 升级 high）
    est_years = _parse_established_years(established, moment)
    if former_name:
        cross_kws = [k for k in _CROSS_INDUSTRY_FORMER_KWS if k in former_name]
        if cross_kws:
            upgraded = (
                est_years is not None
                and est_years < 3.0
                and scale_max is not None
                and scale_max <= 20
            )
            _add_risk(
                "cross_industry_former_name",
                "high" if upgraded else "weak",
                f"曾用名「{former_name}」与当前方向存在跨行业转向迹象"
                + ("（叠加成立不足3年且≤20人，风险升级）" if upgraded else ""),
                f"former_name={former_name}, established={established or '未知'}, scale={scale_text or '未知'}",
            )

    # 2g. 注册地与办公地跨省不一致（弱风险）
    reg_prov = _province_of(reg_city)
    off_prov = _province_of(office_city)
    if reg_prov and off_prov and reg_prov != off_prov:
        _add_risk(
            "cross_province_reg_office",
            "weak",
            f"注册地（{reg_city}）与办公地（{office_city}）跨省不一致，建议确认劳动合同签订主体",
            f"reg_city={reg_city}, office_city={office_city}",
        )

    # 3. 汇总可靠性等级
    critical_cnt = sum(1 for r in risk_items if r["severity"] == "critical")
    high_cnt = sum(1 for r in risk_items if r["severity"] == "high")
    weak_cnt = sum(1 for r in risk_items if r["severity"] == "weak")

    if critical_cnt > 0:
        reliability = "outsourcing_rejected"
    elif high_cnt > 0 or weak_cnt >= 2:
        reliability = "high_risk"
    elif weak_cnt == 1:
        reliability = "caution"
    else:
        reliability = "clean"

    # 4. 正交交叉决定最终 verdict（本地岗绝不掩盖高风险）
    if reliability == "outsourcing_rejected" or feasibility == "remote_mismatch":
        verdict = "not_feasible"
    elif reliability == "high_risk":
        verdict = "explorable_high_risk"
    elif reliability == "caution":
        verdict = "explorable_caution"
    else:
        verdict = "explorable"

    # 5. 置信度与缓存新鲜度（profile 与 jobs 分开计时）
    has_profile_facts = bool(scale_text or financing_stage or insured_count is not None or established or business_scope or intro)
    has_jobs = len(recent_jobs) > 0
    if has_profile_facts and has_jobs:
        confidence = "high"
    elif has_profile_facts or has_jobs:
        confidence = "medium"
    else:
        confidence = "low"

    prof_dt = _parse_iso_utc(record.get("profile_probed_at"))
    jobs_dt = _parse_iso_utc(record.get("jobs_probed_at"))
    profile_stale = (prof_dt is None) or ((moment - prof_dt).days >= STALE_DAYS)
    jobs_stale = (jobs_dt is None) or ((moment - jobs_dt).days >= STALE_DAYS)

    reason_parts: list[str] = []
    if feasibility == "remote_mismatch":
        reason_parts.append(
            f"在招/办公城市（{'、'.join(sorted(job_cities))}）不在优先城市（{'、'.join(preferred_cities)}）内"
        )
    elif feasibility == "remote_flex":
        reason_parts.append("异地大厂弹性放行（约面前需本人确认异地意愿）")
    elif feasibility == "local_match":
        reason_parts.append("地点满足当前城市偏好")
    for item in risk_items:
        reason_parts.append(item["summary"])

    return {
        "company_core": record.get("company_core"),
        "legal_name": legal_name,
        "company_clean": record.get("company_clean"),
        "verdict": verdict,
        "feasibility": feasibility,
        "reliability": reliability,
        "confidence": confidence,
        "risk_count": len(risk_items),
        "risk_flags": [r["code"] for r in risk_items],
        "risk_items": risk_items,
        "reason": "；".join(reason_parts) if reason_parts else "当前未发现明显异常风险",
        "freshness": {
            "profile_probed_at": record.get("profile_probed_at"),
            "jobs_probed_at": record.get("jobs_probed_at"),
            "profile_stale": profile_stale,
            "jobs_stale": jobs_stale,
            "refresh_required": profile_stale or jobs_stale,
        },
        "facts": {
            "scale": scale_text,
            "scale_max_num": scale_max,
            "financing_stage": financing_stage,
            "insured_count": insured_count,
            "established": established,
            "former_name": former_name,
            "business_scope": business_scope,
            "reg_city": reg_city,
            "office_city": office_city,
            "intro": intro,
            "recent_jobs_count": len(recent_jobs),
            "job_cities": sorted(job_cities),
        },
    }


def upsert_and_evaluate(
    workspace: Path,
    *,
    company: str,
    legal_name: str = "",
    profile: dict[str, Any] | None = None,
    jobs: list[dict[str, Any]] | None = None,
    clear_fields: list[str] | None = None,
    now_override: str | None = None,
) -> dict[str, Any]:
    """原子落库（COALESCE 字段级合并 + 身份冲突保护）并按当前 config 实时输出二维判定。"""
    raw_company = str(company or "").strip()
    if not raw_company:
        raise CompanyIntelError("INVALID_COMPANY", "必须提供非空的 company 名称")

    clean_c, core_c = normalize_company_parts(raw_company)
    if not core_c:
        raise CompanyIntelError("INVALID_COMPANY", f"无法归一化公司名：{raw_company!r}")

    incoming_legal = str(legal_name or raw_company).strip()
    ts = now_override or now_iso()
    now_dt = _parse_iso_utc(ts) or datetime.now(timezone.utc)
    prof = profile if isinstance(profile, dict) else {}

    in_scale = _clean_optional_text(prof.get("scale"))
    in_scale_max = _parse_scale_max(in_scale)
    if in_scale_max is None and prof.get("scale_max_num") is not None:
        try:
            in_scale_max = int(prof["scale_max_num"])
        except (TypeError, ValueError):
            in_scale_max = None
    in_financing = _clean_optional_text(prof.get("financing_stage"))
    in_insured_count = _parse_insured_count(prof.get("insured_count"))
    in_established = _clean_optional_text(prof.get("established"))
    in_former_name = _clean_optional_text(prof.get("former_name"))
    in_scope = _clean_optional_text(prof.get("business_scope"))
    in_reg_city = _clean_optional_text(prof.get("reg_city"))
    in_office_city = _clean_optional_text(prof.get("office_city"))
    in_intro = _clean_optional_text(prof.get("intro"))

    has_profile_input = any(
        v is not None
        for v in (
            in_scale,
            in_scale_max,
            in_financing,
            in_insured_count,
            in_established,
            in_former_name,
            in_scope,
            in_reg_city,
            in_office_city,
            in_intro,
        )
    )
    has_jobs_input = isinstance(jobs, list) and len(jobs) > 0
    clears = {f.strip() for f in (clear_fields or []) if f.strip() in _CLEARABLE_FIELDS}

    config = load_config(workspace)
    conn = _open_db(workspace)
    try:
        conn.execute("BEGIN IMMEDIATE")
        old_row = conn.execute(
            "SELECT * FROM company_intel WHERE company_core = ?",
            (core_c,),
        ).fetchone()

        if old_row is not None:
            if _check_identity_conflict(
                str(old_row["company_clean"] or ""),
                str(old_row["legal_name"] or ""),
                clean_c,
                incoming_legal,
            ):
                conn.rollback()
                raise CompanyIntelError(
                    "IDENTITY_CONFLICT",
                    f"检测到同名异地主体冲突：已有「{old_row['legal_name'] or old_row['company_clean']}」，"
                    f"新传入「{incoming_legal}」，已拒绝自动合并以防覆盖",
                    company_core=core_c,
                    existing_legal_name=str(old_row["legal_name"] or ""),
                    incoming_legal_name=incoming_legal,
                )

            # 同集团规模取最大值，防子公司 0-20 人覆盖母公司规模
            old_scale_max = old_row["scale_max_num"]
            if old_scale_max is not None and in_scale_max is not None:
                merged_scale_max = max(int(old_scale_max), int(in_scale_max))
                merged_scale = in_scale if int(in_scale_max) >= int(old_scale_max) else old_row["scale"]
            else:
                merged_scale_max = in_scale_max if in_scale_max is not None else old_scale_max
                merged_scale = in_scale if in_scale is not None else old_row["scale"]

            merged_jobs_json, _ = _merge_jobs(
                None if "jobs_json" in clears else old_row["jobs_json"],
                jobs if has_jobs_input else None,
                ts,
            )
            new_prof_ts = ts if has_profile_input else old_row["profile_probed_at"]
            new_jobs_ts = ts if has_jobs_input else old_row["jobs_probed_at"]

            # COALESCE(NULLIF(TRIM(new), ''), old) + 显式 clear_fields 支持
            def _pick(col: str, val: Any) -> Any:
                if col in clears:
                    return None
                return val if val is not None else old_row[col]

            conn.execute(
                """
                UPDATE company_intel SET
                    legal_name = CASE WHEN TRIM(?) != '' THEN ? ELSE legal_name END,
                    company_clean = CASE WHEN TRIM(?) != '' THEN ? ELSE company_clean END,
                    scale = ?,
                    scale_max_num = ?,
                    financing_stage = ?,
                    insured_count = ?,
                    established = ?,
                    former_name = ?,
                    business_scope = ?,
                    reg_city = ?,
                    office_city = ?,
                    intro = ?,
                    jobs_json = ?,
                    profile_probed_at = ?,
                    jobs_probed_at = ?,
                    updated_at = ?
                WHERE company_core = ?
                """,
                (
                    incoming_legal,
                    incoming_legal,
                    clean_c,
                    clean_c,
                    None if "scale" in clears else merged_scale,
                    None if "scale_max_num" in clears or "scale" in clears else merged_scale_max,
                    _pick("financing_stage", in_financing),
                    _pick("insured_count", in_insured_count),
                    _pick("established", in_established),
                    _pick("former_name", in_former_name),
                    _pick("business_scope", in_scope),
                    _pick("reg_city", in_reg_city),
                    _pick("office_city", in_office_city),
                    _pick("intro", in_intro),
                    merged_jobs_json,
                    new_prof_ts,
                    new_jobs_ts,
                    ts,
                    core_c,
                ),
            )
        else:
            merged_jobs_json, _ = _merge_jobs(None, jobs if has_jobs_input else None, ts)
            conn.execute(
                """
                INSERT INTO company_intel (
                    company_core, legal_name, company_clean,
                    scale, scale_max_num, financing_stage, insured_count,
                    established, former_name,
                    business_scope, reg_city, office_city, intro,
                    jobs_json, profile_probed_at, jobs_probed_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    core_c,
                    incoming_legal,
                    clean_c,
                    in_scale,
                    in_scale_max,
                    in_financing,
                    in_insured_count,
                    in_established,
                    in_former_name,
                    in_scope,
                    in_reg_city,
                    in_office_city,
                    in_intro,
                    merged_jobs_json,
                    ts if has_profile_input else None,
                    ts if has_jobs_input else None,
                    ts,
                    ts,
                ),
            )

        saved_row = conn.execute(
            "SELECT * FROM company_intel WHERE company_core = ?",
            (core_c,),
        ).fetchone()
        conn.commit()
    except sqlite3.OperationalError as exc:
        if conn.in_transaction:
            conn.rollback()
        raise CompanyIntelError("DB_BUSY", f"账本数据库繁忙，请稍后重试：{exc}") from exc
    except CompanyIntelError:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

    eval_result = evaluate_company_record(dict(saved_row), config=config, now_dt=now_dt)
    return {"ok": True, **eval_result}


def show_company_intel(
    workspace: Path,
    *,
    company: str,
    now_override: str | None = None,
) -> dict[str, Any]:
    """按归一化公司名读取缓存并按当前 config 实时重算 verdict。"""
    raw_company = str(company or "").strip()
    if not raw_company:
        raise CompanyIntelError("INVALID_COMPANY", "必须提供非空的 company 名称")
    _, core_c = normalize_company_parts(raw_company)
    now_dt = _parse_iso_utc(now_override) or datetime.now(timezone.utc)
    config = load_config(workspace)

    conn = _open_db(workspace)
    try:
        row = conn.execute(
            "SELECT * FROM company_intel WHERE company_core = ?",
            (core_c,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise CompanyIntelError("DB_BUSY", f"账本数据库繁忙，请稍后重试：{exc}") from exc
    finally:
        conn.close()

    if row is None:
        return {
            "ok": True,
            "found": False,
            "company_core": core_c,
            "refresh_required": True,
            "message": f"本地尚未缓存「{raw_company}」的公司底细记录，建议执行 company probe",
        }
    eval_result = evaluate_company_record(dict(row), config=config, now_dt=now_dt)
    return {"ok": True, "found": True, **eval_result}


__all__ = [
    "CompanyIntelError",
    "STALE_DAYS",
    "evaluate_company_record",
    "show_company_intel",
    "upsert_and_evaluate",
]
