"""公司名归一化、岗位名相似度计算与 CSV 安全转义工具。"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Iterable

REGION_PREFIXES: tuple[str, ...] = (
    "中国",
    "北京市", "北京",
    "上海市", "上海",
    "深圳市", "深圳",
    "广州市", "广州",
    "杭州市", "杭州",
    "成都市", "成都",
    "武汉市", "武汉",
    "南京市", "南京",
    "苏州市", "苏州",
    "西安市", "西安",
    "重庆市", "重庆",
    "天津市", "天津",
    "长沙市", "长沙",
    "郑州市", "郑州",
    "东莞市", "东莞",
    "佛山市", "佛山",
    "宁波市", "宁波",
    "青岛市", "青岛",
    "厦门市", "厦门",
    "珠海市", "珠海",
    "合肥市", "合肥",
    "福州市", "福州",
    "济南市", "济南",
    "大连市", "大连",
    "无锡市", "无锡",
    "昆明市", "昆明",
    "广东省", "广东",
    "浙江省", "浙江",
    "江苏省", "江苏",
    "四川省", "四川",
    "湖北省", "湖北",
    "湖南省", "湖南",
    "山东省", "山东",
    "福建省", "福建",
    "安徽省", "安徽",
    "陕西省", "陕西",
    "香港",
)

LEGAL_SUFFIXES: tuple[str, ...] = (
    "集团股份有限公司",
    "控股集团有限公司",
    "集团有限公司",
    "控股有限公司",
    "股份有限公司",
    "有限责任公司",
    "有限公司",
    "有限合伙",
    "股份公司",
    "分公司",
    "子公司",
)

INDUSTRY_SUFFIXES: tuple[str, ...] = (
    "网络科技",
    "信息科技",
    "数字科技",
    "智能科技",
    "数据科技",
    "电子科技",
    "软件科技",
    "信息技术",
    "网络技术",
    "软件技术",
    "计算机系统",
    "电子商务",
    "人工智能",
    "云计算",
    "互联网",
    "科技发展",
    "科技集团",
    "控股集团",
    "研发中心",
    "事业部",
    "科技",
    "网络",
    "信息",
    "软件",
    "技术",
    "数字",
    "智能",
    "数据",
    "系统",
    "电子",
    "咨询",
    "传媒",
    "文化",
    "实业",
    "控股",
    "集团",
    "国际",
    "发展",
    "服务",
    "商务",
)

GENERIC_SOLO_WORDS: frozenset[str] = frozenset({
    "银行", "证券", "保险", "基金", "信托", "期货", "航空", "控股", "集团",
    "科技", "网络", "软件", "信息", "技术", "数据", "智能", "数字", "电子",
    "实业", "国际", "发展", "传媒", "文化", "咨询", "服务", "大学", "医院",
    "汽车", "医药", "能源", "电信", "通信", "移动", "联通", "金融", "教育",
    "物流", "医疗", "健康", "生物", "制造", "地产", "置业", "建设", "建筑",
    "半导体", "新能源", "环保", "商务", "人力",
    "人工智能", "云计算", "大数据", "互联网", "电子商务", "人工智能实验室",
})

KNOWN_BRAND_CORES: frozenset[str] = frozenset({
    "腾讯", "阿里", "百度", "美团", "京东", "华为", "小米", "网易", "快手", "滴滴",
    "顺丰", "携程", "得物", "荣耀", "蔚来", "极氪", "大疆", "影石", "贝壳",
    "蚂蚁", "菜鸟", "钉钉", "飞书", "高德", "微盟", "知乎", "金蝶", "用友", "浪潮",
})

_BRACKET_RE = re.compile(r"[\(（\[【][^\)）\]】]*[\)）\]】]")
_BRACKET_INNER_RE = re.compile(r"[\(（\[【]([^\)）\]】]{2,30})[\)）\]】]")
_BRACKET_NOISE_WORDS: frozenset[str] = frozenset({
    "集团", "控股", "总部", "总公司", "分公司", "子公司", "有限合伙", "普通合伙",
    "中国", "大中华区", "亚太", "华南", "华东", "华北", "华中", "西南", "西北", "东北",
    "研发中心", "技术中心", "交付中心", "运营中心",
})

# 常见工商注册主体字号 <-> 招聘平台展示品牌名映射（用于前东家防社死精准关联）
CORPORATE_GROUP_ALIASES: tuple[frozenset[str], ...] = (
    frozenset({"拉扎斯", "饿了么"}),
    frozenset({"三快", "三快在线", "美团", "大众点评"}),
    frozenset({"字节跳动", "抖音", "今日头条", "飞书", "火山引擎"}),
    frozenset({"阿里巴巴", "阿里", "淘天", "淘宝", "天猫"}),
    frozenset({"蚂蚁", "蚂蚁金服", "支付宝"}),
    frozenset({"腾讯", "微信", "腾讯云"}),
    frozenset({"华为", "海思", "慧通", "慧通商务"}),
    frozenset({"京东", "京东世纪", "达达"}),
    frozenset({"拼多多", "寻梦", "寻梦信息"}),
    frozenset({"滴滴", "小桔", "小桔科技"}),
    frozenset({"快手", "达佳", "达佳互联"}),
    frozenset({"小红书", "行吟", "行吟信息"}),
    frozenset({"得物", "识装", "识装信息"}),
    frozenset({"看准", "boss直聘"}),
    frozenset({"百度", "百度在线", "百度网讯"}),
    frozenset({"网易", "有道", "网易有道"}),
)
_PUNCT_RE = re.compile(r"[\s·•\-—_/,，。、|｜:：;；\"'“”‘’]+")
_TITLE_NOISE_RE = re.compile(r"(急聘|诚聘|直招|双休|高薪|六险一金|五险一金|弹性打卡)")
_ASCII_WORD_RE = re.compile(r"[a-z0-9+#.]+")
_COMPANY_EXTRACT_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}(?:集团股份有限公司|股份有限公司|有限责任公司|集团有限公司|科技有限公司|网络科技有限公司|信息技术有限公司|软件技术有限公司|数字科技有限公司|智能科技有限公司|有限公司|科技集团|控股集团))"
)
_LABELED_EMPLOYER_RE = re.compile(
    r"(?:公司名称|任职公司|工作单位|就职企业|所在公司)[：:]\s*([\u4e00-\u9fa5A-Za-z0-9·()（）\-]{2,30})"
)
_FORMULA_CHARS = ("=", "+", "-", "@")

# 开源基础版内置的公开典型外包企业与驻场特征（Pro 云端额外提供 100+ 深度名录与社区实时沉淀池）
BASIC_OUTSOURCING_VENDORS: tuple[str, ...] = (
    "软通动力",
    "中软国际",
    "博彦科技",
    "文思海辉",
    "中电金信",
    "法本信息",
    "诚迈科技",
    "东软集团",
    "润和软件",
    "佰钧成",
    "纬创软件",
    "宇信科技",
    "汉得信息",
    "柯莱特",
    "新致软件",
    "微创软件",
    "外企德科",
    "万宝盛华",
)

_NEGATED_OUTSOURCING_ONLY_RE = re.compile(
    r"(?:(?:非|不是|拒绝|不招|不涉及|不含|并非)(?:人力|劳务|软件|it|技术|项目|研发|人才|第三方)?(?:外包|派遣)|"
    r"(?:非|不是|拒绝|不招|并非)\s*(?:华为|德科|第三方)?(?<![a-z0-9])od(?![a-z0-9])|"
    r"自研非外包|甲方直招|甲方直签|甲方面试|本部直招|外包管理|供应商管理|管理外包团队)"
)
_NEGATED_ONSITE_ONLY_RE = re.compile(
    r"(?:(?:非|不是|无需|不需要|不需|不用|无|免|拒绝|不接受|不涉及|不含|并非)(?:长期|银行|客户|现场|异地|项目)?(?:驻场|外派|派驻)|本部办公)"
)
_NEGATED_OUTSOURCING_RE = re.compile(
    rf"{_NEGATED_OUTSOURCING_ONLY_RE.pattern}|{_NEGATED_ONSITE_ONLY_RE.pattern}"
)

BASIC_ONSITE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"驻场(?:开发|测试|运维|产品|设计|办公|项目|支持|服务)?|需(?:要)?驻场|接受驻场|长期驻场|银行驻场|客户现场办公"), "驻场岗特征"),
    (
        re.compile(
            r"外派(?:至|到|驻|客户|甲方|现场|开发|测试|运维|产品|设计|项目|办公|服务|岗)|"
            r"(?:软件|it|技术|人力|研发|银行|金融|长期|项目)外派|派驻(?:客户|现场|甲方)"
        ),
        "客户现场外派特征",
    ),
)
BASIC_OUTSOURCING_EMPLOYMENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:人力|劳务|软件|it|技术|项目|研发)外包"), "外包岗特征"),
    (re.compile(r"(?:人力|劳务|人才)派遣"), "人力/劳务派遣特征"),
    (re.compile(r"(?<![a-z0-9])od(?![a-z0-9])|华为od|德科od"), "OD 外包特征"),
    (re.compile(r"与第三方(?:人力|机构|公司)?签(?:署|订)?(?:合同|劳动合同)|合同签(?:在|署在)?第三方|编制在第三方"), "第三方签署合同"),
)
BASIC_OUTSOURCING_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    BASIC_ONSITE_PATTERNS + BASIC_OUTSOURCING_EMPLOYMENT_PATTERNS
)


def normalize_company_parts(raw: str) -> tuple[str, str]:
    """返回 (去除地域与工商后缀的 clean_name, 进一步去除通用行业词的 core_name)。"""
    if not raw:
        return ("", "")
    text = raw.strip()
    without_brackets = _BRACKET_RE.sub("", text).strip()
    if len(without_brackets) >= 2:
        text = without_brackets
    text = _PUNCT_RE.sub("", text).lower()
    if not text:
        return ("", "")

    # 1. 优先去除尾部工商主体后缀
    changed = True
    while changed:
        changed = False
        for suffix in LEGAL_SUFFIXES:
            if text.endswith(suffix) and len(text) - len(suffix) >= 2:
                candidate = text[: -len(suffix)]
                if candidate not in GENERIC_SOLO_WORDS:
                    text = candidate
                    changed = True
                    break

    # 2. 循环去除多层省市前缀（如“中国广东省深圳市示例科技”，前提是剩余部分不是“银行/科技”这类纯通用词）
    changed = True
    while changed:
        changed = False
        for prefix in REGION_PREFIXES:
            if text.startswith(prefix) and len(text) - len(prefix) >= 2:
                remainder = text[len(prefix) :]
                if remainder not in GENERIC_SOLO_WORDS:
                    text = remainder
                    changed = True
                    break

    # 再跑一轮工商后缀（应对“有限公司深圳分公司”等组合）
    changed = True
    while changed:
        changed = False
        for suffix in LEGAL_SUFFIXES:
            if text.endswith(suffix) and len(text) - len(suffix) >= 2:
                candidate = text[: -len(suffix)]
                if candidate not in GENERIC_SOLO_WORDS:
                    text = candidate
                    changed = True
                    break

    clean_name = text

    # 3. 进一步剥离“科技/网络/信息/股份”等行业通用后缀得到核心字号
    core = clean_name
    changed = True
    while changed:
        changed = False
        for suffix in INDUSTRY_SUFFIXES + LEGAL_SUFFIXES:
            if core.endswith(suffix) and len(core) - len(suffix) >= 2:
                candidate = core[: -len(suffix)]
                if candidate not in GENERIC_SOLO_WORDS:
                    core = candidate
                    changed = True
                    break

    # 剥离行业后缀后再循环检查地域前缀（例如“广东深圳观麦信息” -> “观麦”）
    changed = True
    while changed:
        changed = False
        for prefix in REGION_PREFIXES:
            if core.startswith(prefix) and len(core) - len(prefix) >= 2:
                remainder = core[len(prefix) :]
                if remainder not in GENERIC_SOLO_WORDS:
                    core = remainder
                    changed = True
                    break

    return (clean_name, core)


def normalize_company(raw: str) -> str:
    """返回公司核心归一化名（如 '深圳观麦科技有限公司' -> '观麦'）。"""
    _clean, core = normalize_company_parts(raw)
    return core


def extract_company_aliases(raw: str) -> list[str]:
    """提取企业主体名称及括号内有意义的品牌别名（如 '拉扎斯网络科技（上海）有限公司（饿了么）' -> 保留 '饿了么'）。"""
    if not raw or not raw.strip():
        return []
    candidates: list[str] = [raw.strip()]
    without_brackets = _BRACKET_RE.sub("", raw).strip()
    if len(without_brackets) >= 2 and without_brackets not in candidates:
        candidates.append(without_brackets)

    for inner in _BRACKET_INNER_RE.findall(raw):
        token = _PUNCT_RE.sub("", inner).strip().lower()
        if len(token) < 2:
            continue
        if token in REGION_PREFIXES or token in GENERIC_SOLO_WORDS or token in _BRACKET_NOISE_WORDS:
            continue
        if any(token == suf for suf in LEGAL_SUFFIXES):
            continue
        candidates.append(inner.strip())
    return candidates


def _in_same_corporate_group(core_a: str, core_b: str) -> bool:
    if not core_a or not core_b:
        return False
    for group in CORPORATE_GROUP_ALIASES:
        if core_a in group and core_b in group:
            return True
    return False


def companies_match(company_a: str, company_b: str) -> bool:
    """判断两个公司名称是否指向同一企业主体。"""
    clean_a, core_a = normalize_company_parts(company_a)
    clean_b, core_b = normalize_company_parts(company_b)
    if not core_a or not core_b:
        return False
    if core_a == core_b or clean_a == clean_b:
        return True
    # 处理如“示例云”与“示例云计算”但避免普通岗位判重短字误伤
    if len(core_a) >= 3 and (core_b.startswith(core_a) or clean_b.startswith(core_a)):
        return True
    if len(core_b) >= 3 and (core_a.startswith(core_b) or clean_a.startswith(core_b)):
        return True
    return False


def employer_shield_match(candidate_company: str, shielded_company: str) -> bool:
    """前东家与黑名单专用高敏拦截匹配：
    1. 支持括号内品牌别名（如 '拉扎斯（饿了么）' 拦截 '饿了么'）；
    2. 支持 2 字及以上品牌核心词对子品牌/业务线的前缀覆盖（如 '华为' -> '华为终端'、'京东' -> '京东物流'、'腾讯' -> '腾讯音乐娱乐'）；
    3. 支持常见互联网大厂注册工商字号与展示品牌互通（如 '三快在线' <-> '美团'、'拉扎斯' <-> '饿了么'）。
    """
    cand_aliases = extract_company_aliases(candidate_company)
    shield_aliases = extract_company_aliases(shielded_company)
    if not cand_aliases or not shield_aliases:
        return False

    cand_parts = [(a, *normalize_company_parts(a)) for a in cand_aliases]
    shield_parts = [(b, *normalize_company_parts(b)) for b in shield_aliases]

    for _raw_c, clean_c, core_c in cand_parts:
        if not core_c:
            continue
        for raw_s, clean_s, core_s in shield_parts:
            if not core_s:
                continue
            if core_c == core_s or clean_c == clean_s:
                return True
            if _in_same_corporate_group(core_c, core_s):
                return True
            # 3 字及以上核心名允许双向前缀匹配；2 字核心名仅在知名大厂品牌或用户显式填写简称（clean_s == core_s）时覆盖子公司，防止“东方网络”误杀“东方财富”
            raw_s_compact = _PUNCT_RE.sub("", _BRACKET_RE.sub("", raw_s)).lower()
            allow_short_s = len(core_s) >= 3 or (
                len(core_s) == 2
                and core_s not in GENERIC_SOLO_WORDS
                and (core_s in KNOWN_BRAND_CORES or clean_s == core_s or raw_s_compact == core_s)
            )
            if allow_short_s and (core_c.startswith(core_s) or clean_c.startswith(core_s)):
                return True
            allow_short_c = len(core_c) >= 3 or (
                len(core_c) == 2 and core_c not in GENERIC_SOLO_WORDS and core_c in KNOWN_BRAND_CORES
            )
            if allow_short_c and (core_s.startswith(core_c) or clean_s.startswith(core_c)):
                return True
            # 对集团关联核心词同步允许前缀衍生（如前东家填 '北京三快在线' -> 拦截 '美团优选'）
            for group in CORPORATE_GROUP_ALIASES:
                if core_s in group:
                    if any(
                        len(alias) >= 2 and (core_c == alias or core_c.startswith(alias) or clean_c.startswith(alias))
                        for alias in group
                    ):
                        return True
    return False


def normalize_title(raw: str) -> str:
    """岗位名称归一化。"""
    if not raw:
        return ""
    text = raw.strip().lower()
    without_brackets = _BRACKET_RE.sub(" ", text).strip()
    if without_brackets:
        text = without_brackets
    text = _TITLE_NOISE_RE.sub(" ", text)
    text = _PUNCT_RE.sub("", text)
    return text


def _title_features(norm_title: str) -> set[str]:
    if not norm_title:
        return set()
    features: set[str] = set()
    for word in _ASCII_WORD_RE.findall(norm_title):
        features.add(word)
    cjk_only = _ASCII_WORD_RE.sub("", norm_title)
    if len(cjk_only) == 1:
        features.add(cjk_only)
    else:
        for idx in range(len(cjk_only) - 1):
            features.add(cjk_only[idx : idx + 2])
    return features


def title_similarity(title_a: str, title_b: str) -> float:
    """计算两个岗位名的相似度（0.0 ~ 1.0）。"""
    na = normalize_title(title_a)
    nb = normalize_title(title_b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 3 and short in long_:
        return round(max(0.82, len(short) / len(long_)), 3)
    fa = _title_features(na)
    fb = _title_features(nb)
    if not fa or not fb:
        return 0.0
    union = fa | fb
    inter = fa & fb
    return round(len(inter) / len(union), 3)


def deduplicate_company_list(items: Iterable[str] | str) -> list[str]:
    """合并公司或词条列表，保留可读原名但剔除空白与完全重复项（若误传单个字符串则按分隔符安全切分，绝不逐字拆散）。"""
    if isinstance(items, str):
        normalized_str = items.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
        iterable: Iterable[str] = [part.strip() for part in normalized_str.split(",") if part.strip()]
    else:
        iterable = items
    result: list[str] = []
    seen_exact: set[str] = set()
    for raw in iterable:
        if not raw or not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen_exact:
            continue
        seen_exact.add(key)
        result.append(cleaned)
    return result


def extract_employers_from_text(text: str) -> list[str]:
    """从简历纯文本或 Markdown 中启发式识别工商主体名与显式标记的雇主名。"""
    if not text:
        return []
    found = list(_COMPANY_EXTRACT_RE.findall(text))
    for item in _LABELED_EMPLOYER_RE.findall(text):
        cleaned = item.strip()
        if len(cleaned) >= 2:
            found.append(cleaned)
    return deduplicate_company_list(found)


def read_resume_text_best_effort(file_path: str | Path) -> dict[str, Any]:
    """安全读取本地简历文本：支持 UTF-8 文本/Markdown，遇到 PDF 二进制文件时尝试工具提取而不抛崩溃异常。"""
    p = Path(file_path).expanduser().resolve()
    raw_bytes = p.read_bytes()
    is_pdf = p.suffix.lower() == ".pdf" or raw_bytes.startswith(b"%PDF-")
    if not is_pdf:
        return {
            "text": raw_bytes.decode("utf-8", errors="replace"),
            "is_pdf": False,
            "pdf_binary_notice": False,
        }

    # 优先尝试本机已安装的 pdftotext（若有）
    pdftotext_bin = shutil.which("pdftotext")
    if pdftotext_bin:
        try:
            proc = subprocess.run(
                [pdftotext_bin, "-enc", "UTF-8", str(p), "-"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return {
                    "text": proc.stdout,
                    "is_pdf": True,
                    "pdf_binary_notice": False,
                }
        except (OSError, subprocess.SubprocessError):
            pass

    # 回退提取 PDF 中未压缩的 UTF-8 / UTF-16BE 可读片段，并附带宿主提示
    utf8_guess = raw_bytes.decode("utf-8", errors="ignore")
    utf16_guess = ""
    try:
        utf16_guess = raw_bytes.decode("utf-16-be", errors="ignore")
    except Exception:
        utf16_guess = ""
    combined = f"{utf8_guess}\n{utf16_guess}"
    return {
        "text": combined,
        "is_pdf": True,
        "pdf_binary_notice": True,
    }


def detect_basic_work_mode_risks(
    company: str,
    text: str = "",
) -> dict[str, Any]:
    """开源基础版独立识别「外包/派遣/OD」与「驻场/客户现场外派」两类属性（支持各自独立开关过滤）。"""
    comp = (company or "").strip()
    comp_core = normalize_company(comp)
    combined_lower = f"{comp}\n{text or ''}".lower()

    is_outsourcing = False
    outsourcing_reason = ""
    for vendor in BASIC_OUTSOURCING_VENDORS:
        if vendor.lower() in comp.lower() or normalize_company(vendor) == comp_core:
            is_outsourcing = True
            outsourcing_reason = f"命中典型外包/派遣企业：{vendor}"
            break

    if not is_outsourcing:
        sanitized_out = _NEGATED_OUTSOURCING_ONLY_RE.sub(" ", combined_lower)
        for pat, label in BASIC_OUTSOURCING_EMPLOYMENT_PATTERNS:
            if pat.search(sanitized_out):
                is_outsourcing = True
                outsourcing_reason = f"命中{label}"
                break

    is_onsite = False
    onsite_reason = ""
    sanitized_onsite = _NEGATED_ONSITE_ONLY_RE.sub(" ", combined_lower)
    for pat, label in BASIC_ONSITE_PATTERNS:
        if pat.search(sanitized_onsite):
            is_onsite = True
            onsite_reason = f"命中{label}"
            break

    return {
        "is_outsourcing": is_outsourcing,
        "outsourcing_reason": outsourcing_reason,
        "is_onsite": is_onsite,
        "onsite_reason": onsite_reason,
    }


def detect_basic_outsourcing(
    company: str,
    text: str = "",
) -> tuple[bool, str]:
    """兼容接口：任一命中基础外包/派遣或驻场特征即返回 (True, reason)。"""
    risks = detect_basic_work_mode_risks(company, text)
    if risks["is_outsourcing"]:
        return True, str(risks["outsourcing_reason"])
    if risks["is_onsite"]:
        return True, str(risks["onsite_reason"])
    return False, ""


def sanitize_csv_cell(value: object) -> str:
    """防护 CSV 公式注入：对以 =, +, -, @ 开头的单元格前置单引号。"""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    stripped = text.lstrip(" \t\r\n")
    if text.startswith(_FORMULA_CHARS) or (stripped and stripped.startswith(_FORMULA_CHARS)):
        return "'" + text
    return text


def parse_salary_floor_k(salary_text: str) -> int | None:
    """从面板薪资字符串（如 '25-40K·15薪'）提取上限月薪 K 数用于初筛参考。"""
    if not salary_text:
        return None
    text = salary_text.upper().replace(" ", "")
    match_range = re.search(r"(\d+)\s*[-~至]\s*(\d+)\s*K", text)
    if match_range:
        return int(match_range.group(2))
    match_single = re.search(r"(\d+)\s*K", text)
    if match_single:
        return int(match_single.group(1))
    return None
