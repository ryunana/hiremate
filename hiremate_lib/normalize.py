"""公司名归一化、岗位名相似度计算与 CSV 安全转义工具。"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Iterable
import xml.etree.ElementTree as ET
import zipfile

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
    "南昌市", "南昌",
    "石家庄市", "石家庄",
    "太原市", "太原",
    "沈阳市", "沈阳",
    "长春市", "长春",
    "哈尔滨市", "哈尔滨",
    "南宁市", "南宁",
    "海口市", "海口",
    "贵阳市", "贵阳",
    "兰州市", "兰州",
    "西宁市", "西宁",
    "银川市", "银川",
    "乌鲁木齐市", "乌鲁木齐",
    "常州市", "常州",
    "徐州市", "徐州",
    "南通市", "南通",
    "温州市", "温州",
    "嘉兴市", "嘉兴",
    "金华市", "金华",
    "绍兴市", "绍兴",
    "台州市", "台州",
    "烟台市", "烟台",
    "惠州市", "惠州",
    "中山市", "中山",
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
    "江西省", "江西",
    "河北省", "河北",
    "山西省", "山西",
    "辽宁省", "辽宁",
    "吉林省", "吉林",
    "黑龙江省", "黑龙江",
    "河南省", "河南",
    "海南省", "海南",
    "贵州省", "贵州",
    "云南省", "云南",
    "甘肃省", "甘肃",
    "青海省", "青海",
    "台湾省", "台湾",
    "内蒙古自治区", "内蒙古",
    "广西壮族自治区", "广西",
    "西藏自治区", "西藏",
    "宁夏回族自治区", "宁夏",
    "新疆维吾尔自治区", "新疆",
    "香港", "澳门",
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
    "研发中心", "技术中心", "交付中心", "运营中心", "共享中心", "创新中心",
    "china", "prc", "global", "international", "asia", "apac", "hk", "usa", "us", "uk", "jp", "sg",
    "group", "holding", "holdings", "inc", "ltd", "limited", "corp", "corporation", "co", "company",
    "外包", "驻场", "外派", "派遣", "直招", "自研", "代招", "猎头", "本部",
    "上市公司", "已上市", "上市", "国企", "央企", "外企", "民企", "合资", "独资", "独角兽",
    "未融资", "不需要融资", "天使轮", "种子轮", "prea轮", "prea", "a轮", "b轮", "c轮", "d轮",
    "e轮", "f轮", "preipo", "ipo", "战略融资",
})
_BRACKET_STAGE_OR_SCALE_RE = re.compile(
    r"^(?:[a-f]\+?轮|pre[\-]?[a-c]轮?|\d+\s*[-~至]\s*\d+\s*人|\d+人以上)$",
    re.IGNORECASE,
)

# 常见工商注册主体字号 <-> 招聘平台展示品牌名映射（用于前东家防社死精准关联）
CORPORATE_GROUP_ALIASES: tuple[frozenset[str], ...] = (
    frozenset({"拉扎斯", "饿了么"}),
    frozenset({"三快", "三快在线", "美团", "大众点评"}),
    frozenset({"字节跳动", "字节", "抖音", "今日头条", "飞书", "火山引擎", "字跳", "有竹居", "脸萌"}),
    frozenset({"阿里巴巴", "阿里", "阿里云", "阿里健康", "阿里国际", "阿里影业", "阿里妈妈", "淘天", "淘宝", "天猫"}),
    frozenset({"蚂蚁", "蚂蚁金服", "蚂蚁集团", "支付宝"}),
    frozenset({"腾讯", "微信", "腾讯云"}),
    frozenset({"华为", "海思", "海思半导体", "慧通", "慧通商务"}),
    frozenset({"京东", "京东世纪", "达达"}),
    frozenset({"拼多多", "寻梦", "寻梦信息"}),
    frozenset({"滴滴", "小桔", "小桔科技"}),
    frozenset({"快手", "达佳", "达佳互联"}),
    frozenset({"小红书", "行吟", "行吟信息"}),
    frozenset({"得物", "识装", "识装信息"}),
    frozenset({"看准", "boss直聘", "华品博睿"}),
    frozenset({"百度", "百度在线", "百度网讯"}),
    frozenset({"网易", "有道", "网易有道", "网之易", "博冠"}),
    frozenset({"哔哩哔哩", "bilibili", "宽娱", "宽娱数码", "幻电", "幻电信息"}),
    frozenset({"携程", "携程计算机", "去哪儿", "趣拿", "去哪儿网"}),
    frozenset({"招银", "招商银行", "招银网络"}),
    frozenset({"平安", "中国平安", "平安保险", "平安集团", "平安科技", "平安普惠", "平安产险", "平安寿险", "平安健康", "平安银行"}),
)

# 允许在集团关联匹配中向子品牌做前缀衍生的主品牌词（排除“阿里->阿里斯顿”“博冠->博冠光电”“海思->海思科医药”等跨行业重名误伤）
GROUP_PREFIX_BRANDS: frozenset[str] = frozenset({
    "华为", "腾讯", "京东", "美团", "字节跳动", "哔哩哔哩", "百度", "网易",
    "蚂蚁", "携程", "滴滴", "快手", "小米", "拼多多", "小红书", "得物", "招商银行",
})

# 同一企业工商注册名 <-> 对外招聘品牌名映射（用于账本同公司查重 companies_match）
LEGAL_ENTITY_BRAND_PAIRS: tuple[frozenset[str], ...] = (
    frozenset({"拉扎斯", "饿了么"}),
    frozenset({"三快", "三快在线", "美团"}),
    frozenset({"字跳", "有竹居", "字节跳动", "抖音"}),
    frozenset({"寻梦", "寻梦信息", "拼多多"}),
    frozenset({"行吟", "行吟信息", "小红书"}),
    frozenset({"达佳", "达佳互联", "快手"}),
    frozenset({"识装", "识装信息", "得物"}),
    frozenset({"华品博睿", "看准", "boss直聘"}),
    frozenset({"宽娱", "宽娱数码", "幻电", "幻电信息", "哔哩哔哩", "bilibili"}),
    frozenset({"小桔", "小桔科技", "滴滴"}),
    frozenset({"百度在线", "百度网讯", "百度"}),
    frozenset({"京东世纪", "京东"}),
)

_PUNCT_RE = re.compile(r"[\s·•\-—_/,，。、|｜:：;；\"'“”‘’]+")
_TITLE_NOISE_RE = re.compile(r"(急聘|诚聘|直招|双休|周末双休|高薪|六险一金|五险一金|弹性打卡|包吃住|全职|社招|校招)")
_TITLE_BRACKET_SALARY_RE = re.compile(r"\d+\s*[-~至]?\s*\d*\s*[kKwW万薪]+")
_TITLE_BRACKET_SUFFIX_NOISE_RE = re.compile(r"(?:方向|业务|岗位|团队|部门|事业部|岗|组|部|线)$")
_TITLE_RANK_RE = re.compile(r"^(?:高级|资深|初级|中级|首席|专家级|助理|实习|特聘|资深级)+")
_TITLE_GENERIC_ROLE_RE = re.compile(
    r"(?:产品经理|后端开发工程师|前端开发工程师|全栈开发工程师|客户端开发工程师|软件开发工程师|"
    r"开发工程师|研发工程师|算法工程师|测试工程师|测试开发工程师|运维工程师|安全工程师|数据工程师|"
    r"后端工程师|前端工程师|软件工程师|数据分析师|商业分析师|项目经理|运营经理|运营专员|增长运营|"
    r"产品运营|交互设计师|视觉设计师|ui设计师|设计师|产品总监|技术总监|研发总监|负责人|总监|专家|经理|工程师|专员)$"
)
_ASCII_WORD_RE = re.compile(r"[a-z0-9+#.]+")
_COMPANY_EXTRACT_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{2,20}(?:[\(（][\u4e00-\u9fa5A-Za-z]{2,10}[\)）])?[\u4e00-\u9fa5A-Za-z0-9]{0,12}"
    r"(?:集团股份有限公司|控股集团有限公司|股份有限公司|有限责任公司|集团有限公司|控股有限公司|"
    r"网络科技有限公司|信息科技有限公司|数字科技有限公司|智能科技有限公司|软件技术有限公司|信息技术有限公司|"
    r"网络技术有限公司|科技有限公司|网络有限公司|技术有限公司|软件有限公司|有限公司|科技集团|控股集团))"
)
_COMPANY_LEADING_NARRATIVE_RE = re.compile(
    r"^(?:.*?(?:曾就职于|现就职于|就职于|曾任职于|任职于|供职于|受雇于|工作于|服务于|毕业后入职|入职于|加入|入职|"
    r"曾在|我在|本人于|本人在|曾任|现任|历任|实习于|工作经历|工作单位|任职公司|所在公司)|\d+[年./\-月日至今\s~到]*)+"
)
_PARTNER_COMPANY_CLAUSE_RE = re.compile(
    r"(?:负责|主导|参与|配合|推进)?(?:与|和|同|跟|面向|对接|服务客户|合作方|客户为|甲方为)"
    r"[\u4e00-\u9fa5A-Za-z0-9()（）]{2,26}(?:有限公司|股份有限公司|集团)"
    r"(?:的?(?:合作|对接|联调|交付|项目|业务|采购))?"
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


def _extract_tagged_company_aliases(raw: str) -> list[tuple[str, bool]]:
    """提取 (候选名, 是否来自括号附加词)，防止两家不同公司因相同地域/属性括号发生交叉误匹配。"""
    if not raw or not raw.strip():
        return []
    tagged: list[tuple[str, bool]] = [(raw.strip(), False)]
    without_brackets = _BRACKET_RE.sub("", raw).strip()
    if len(without_brackets) >= 2 and without_brackets != raw.strip():
        tagged.append((without_brackets, False))

    for inner in _BRACKET_INNER_RE.findall(raw):
        raw_inner = inner.strip()
        token = _PUNCT_RE.sub("", raw_inner).strip().lower()
        if len(token) < 2:
            continue
        if token in REGION_PREFIXES or token in GENERIC_SOLO_WORDS or token in _BRACKET_NOISE_WORDS:
            continue
        if _BRACKET_STAGE_OR_SCALE_RE.match(raw_inner) or _BRACKET_STAGE_OR_SCALE_RE.match(token):
            continue
        if len(token) <= 5 and token.endswith(("省", "市", "区", "县", "镇", "片区", "分部", "园区")):
            continue
        if any(n in token for n in ("猎头", "代招", "独角兽", "某知名", "某头部", "外派", "驻场", "外包", "上市")):
            continue
        if any(token == suf for suf in LEGAL_SUFFIXES):
            continue
        tagged.append((raw_inner, True))
    return tagged


def extract_company_aliases(raw: str) -> list[str]:
    """提取企业主体名称及括号内有意义的品牌别名（如 '拉扎斯网络科技（上海）有限公司（饿了么）' -> 保留 '饿了么'）。"""
    return [name for name, _is_bracket in _extract_tagged_company_aliases(raw)]


def _in_same_corporate_group(core_a: str, core_b: str) -> bool:
    if not core_a or not core_b:
        return False
    for group in CORPORATE_GROUP_ALIASES:
        if core_a in group and core_b in group:
            return True
    return False


def _in_same_legal_brand_pair(core_a: str, core_b: str) -> bool:
    if not core_a or not core_b:
        return False
    for pair in LEGAL_ENTITY_BRAND_PAIRS:
        if core_a in pair and core_b in pair:
            return True
    return False


def companies_match(company_a: str, company_b: str) -> bool:
    """判断两个公司名称是否指向同一企业主体（兼容括号品牌名与常见工商注册字号互通，禁止纯括号词互撞）。"""
    tagged_a = _extract_tagged_company_aliases(company_a)
    tagged_b = _extract_tagged_company_aliases(company_b)
    if not tagged_a or not tagged_b:
        return False

    parts_a = [(*normalize_company_parts(a), is_b) for a, is_b in tagged_a]
    parts_b = [(*normalize_company_parts(b), is_b) for b, is_b in tagged_b]
    for clean_a, core_a, is_b_a in parts_a:
        if not core_a:
            continue
        for clean_b, core_b, is_b_b in parts_b:
            if not core_b:
                continue
            if is_b_a and is_b_b:
                continue
            if core_a == core_b or clean_a == clean_b:
                return True
            if _in_same_legal_brand_pair(core_a, core_b):
                return True
            # 处理如“示例云”与“示例云计算”但避免普通岗位判重短字误伤
            if len(core_a) >= 3 and not is_b_a and (core_b.startswith(core_a) or clean_b.startswith(core_a)):
                return True
            if len(core_b) >= 3 and not is_b_b and (core_a.startswith(core_b) or clean_a.startswith(core_b)):
                return True
    return False


def employer_shield_match(candidate_company: str, shielded_company: str) -> bool:
    """前东家与黑名单专用高敏拦截匹配：
    1. 支持括号内品牌别名（如 '拉扎斯（饿了么）' 拦截 '饿了么'，且禁止双侧括号附加词互撞）；
    2. 支持品牌核心词对子品牌/业务线的前缀覆盖（如 '华为' -> '华为终端'、'京东' -> '京东物流'、'腾讯' -> '腾讯音乐娱乐'）；
    3. 支持常见互联网大厂注册工商字号与展示品牌互通（如 '三快在线' <-> '美团'、'拉扎斯' <-> '饿了么'、'字跳' <-> '字节跳动'）。
    """
    cand_tagged = _extract_tagged_company_aliases(candidate_company)
    shield_tagged = _extract_tagged_company_aliases(shielded_company)
    if not cand_tagged or not shield_tagged:
        return False

    cand_parts = [(a, *normalize_company_parts(a), is_b) for a, is_b in cand_tagged]
    shield_parts = [(b, *normalize_company_parts(b), is_b) for b, is_b in shield_tagged]

    for _raw_c, clean_c, core_c, is_b_c in cand_parts:
        if not core_c:
            continue
        for raw_s, clean_s, core_s, is_b_s in shield_parts:
            if not core_s:
                continue
            if is_b_c and is_b_s:
                continue
            if core_c == core_s or clean_c == clean_s:
                return True
            if _in_same_corporate_group(core_c, core_s):
                return True
            # 3 字及以上核心名允许双向前缀匹配（排除“阿里”单前缀误杀“阿里斯顿”）；2 字核心名仅限明确品牌或简称
            raw_s_compact = _PUNCT_RE.sub("", _BRACKET_RE.sub("", raw_s)).lower()
            allow_short_s = (len(core_s) >= 3 and not is_b_s) or (
                len(core_s) == 2
                and not is_b_s
                and core_s != "阿里"
                and core_s not in GENERIC_SOLO_WORDS
                and (core_s in KNOWN_BRAND_CORES or clean_s == core_s or raw_s_compact == core_s)
            )
            if allow_short_s and (core_c.startswith(core_s) or clean_c.startswith(core_s)):
                return True
            # 若 2 字核心名来自完整工商主体（如“深圳元象信息科技有限公司” -> core_s="元象"），候选为“中文核心+英文品牌后缀”（如“元象XVERSE”），精准拦截且不误伤其他四字中文词
            if (
                len(core_s) == 2
                and not is_b_s
                and core_s not in GENERIC_SOLO_WORDS
                and core_c.startswith(core_s)
                and len(core_c) > 2
                and core_c[2:].isascii()
            ):
                return True
            allow_short_c = (len(core_c) >= 3 and not is_b_c) or (
                len(core_c) == 2
                and not is_b_c
                and core_c != "阿里"
                and core_c not in GENERIC_SOLO_WORDS
                and core_c in KNOWN_BRAND_CORES
            )
            if allow_short_c and (core_s.startswith(core_c) or clean_s.startswith(core_c)):
                return True
            # 仅允许集团组内的核心主品牌词（GROUP_PREFIX_BRANDS）做子业务前缀衍生，防止“博冠->博冠光电”“海思->海思科医药”误伤
            for group in CORPORATE_GROUP_ALIASES:
                if core_s in group:
                    if any(
                        (core_c == alias or clean_c == alias)
                        or (alias in GROUP_PREFIX_BRANDS and (core_c.startswith(alias) or clean_c.startswith(alias)))
                        for alias in group
                    ):
                        return True
    return False


def _clean_title_bracket_content(match: re.Match[str]) -> str:
    """保留岗位括号内有区分度的方向/语言限定词（如 AI方向、电商交易、Go、Java），仅剥离双休/急聘/城市行政区/薪资等噪声。"""
    inner = (match.group(0)[1:-1] or "").strip().lower()
    if not inner:
        return " "
    inner = _TITLE_NOISE_RE.sub(" ", inner)
    inner = _TITLE_BRACKET_SALARY_RE.sub(" ", inner)
    parts = [p.strip() for p in _PUNCT_RE.split(inner) if p.strip()]
    kept: list[str] = []
    for p in parts:
        if p in REGION_PREFIXES or p in _BRACKET_NOISE_WORDS:
            continue
        if p != "社区" and len(p) <= 5 and p.endswith(("区", "县", "镇", "新区", "园区", "基地", "大厦")):
            continue
        cleaned = _TITLE_BRACKET_SUFFIX_NOISE_RE.sub("", p).strip()
        if not cleaned or cleaned in REGION_PREFIXES or cleaned in _BRACKET_NOISE_WORDS:
            continue
        if cleaned != "社区" and len(cleaned) <= 5 and cleaned.endswith(("区", "县", "镇", "新区", "园区", "基地")):
            continue
        kept.append(cleaned)
    return (" " + " ".join(kept) + " ") if kept else " "


def normalize_title(raw: str) -> str:
    """岗位名称归一化（保留括号内核心业务方向与技术栈限定词，防同公司不同赛道岗位误判重）。"""
    if not raw:
        return ""
    text = raw.strip().lower()
    text = _BRACKET_RE.sub(_clean_title_bracket_content, text).strip()
    text = _TITLE_NOISE_RE.sub(" ", text)
    text = _PUNCT_RE.sub("", text)
    return text


def _extract_title_specialization(raw_title: str) -> tuple[str, str]:
    """拆分岗位名的 (专业方向限定词 spec, 通用职能族 role_family)，用于识别同职能不同细分赛道（如 Go vs Java、AI vs 电商）。"""
    if not raw_title:
        return ("", "")
    # 先单独提取括号内的方向词（如“高级产品经理（AI方向）”中的“ai”）
    bracket_specs: list[str] = []
    for m in _BRACKET_RE.finditer(raw_title):
        cleaned_b = _PUNCT_RE.sub("", _clean_title_bracket_content(m)).strip()
        if cleaned_b:
            bracket_specs.append(cleaned_b)
    base = _BRACKET_RE.sub(" ", raw_title.strip().lower())
    base = _TITLE_NOISE_RE.sub(" ", base)
    base = _PUNCT_RE.sub("", base)
    base_no_rank = _TITLE_RANK_RE.sub("", base)
    role_match = _TITLE_GENERIC_ROLE_RE.search(base_no_rank)
    role_family = role_match.group(0) if role_match else ""
    stem_spec = base_no_rank[: role_match.start()] if role_match else base_no_rank
    stem_spec = _TITLE_BRACKET_SUFFIX_NOISE_RE.sub("", stem_spec).strip()
    combined_spec = "".join([stem_spec] + [b for b in bracket_specs if b not in stem_spec])
    return (combined_spec, role_family)


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
    """计算两个岗位名的相似度（0.0 ~ 1.0），区分同职能下不同专业方向/技术栈（防误杀同大厂异方向岗）。"""
    na = normalize_title(title_a)
    nb = normalize_title(title_b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    spec_a, role_a = _extract_title_specialization(title_a)
    spec_b, role_b = _extract_title_specialization(title_b)
    if spec_a and spec_b:
        if spec_a == spec_b:
            return 0.92 if (not role_a or not role_b or role_a == role_b or role_a in role_b or role_b in role_a) else 0.5
        fa_s = _title_features(spec_a)
        fb_s = _title_features(spec_b)
        spec_inter = fa_s & fb_s
        if not spec_inter and spec_a not in spec_b and spec_b not in spec_a:
            # 两个岗位均明确标注了不同细分方向/语言（如 AI vs 电商交易、Go vs Java），绝不当作同一岗位误杀
            return 0.32
        if (spec_a in spec_b or spec_b in spec_a) and (
            not role_a or not role_b or role_a == role_b or role_a in role_b or role_b in role_a
        ):
            return 0.82
    elif bool(spec_a) != bool(spec_b) and role_a and role_b and (role_a == role_b or role_a in role_b or role_b in role_a):
        # 一个是无修饰泛称（如“产品经理”），另一个有明确专业方向（如“AI产品经理”），避免泛称子串拉高至 0.82 误杀细分岗
        return 0.48

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


def _strip_company_narrative_prefix(raw_match: str) -> str:
    cleaned = (raw_match or "").strip()
    if not cleaned:
        return ""
    stripped = _COMPANY_LEADING_NARRATIVE_RE.sub("", cleaned).strip()
    # 剥离单字前导介词（如“在深圳观麦科技有限公司”“于杭州远山数字科技有限公司”）
    if len(stripped) >= 5 and stripped[0] in ("在", "于", "自", "去", "到", "进", "入"):
        stripped = stripped[1:].strip()
    return stripped if len(stripped) >= 4 else cleaned


def extract_employers_from_text(text: str) -> list[str]:
    """从简历纯文本或 Markdown 中启发式识别工商主体名（剥离时间/任职动词前缀并排除甲乙方合作描述）及显式标记雇主名。"""
    if not text:
        return []
    sanitized_text = _PARTNER_COMPANY_CLAUSE_RE.sub(" ", text)
    found: list[str] = []
    for raw_item in _COMPANY_EXTRACT_RE.findall(sanitized_text):
        cleaned_emp = _strip_company_narrative_prefix(raw_item)
        if len(cleaned_emp) >= 4:
            found.append(cleaned_emp)
    for item in _LABELED_EMPLOYER_RE.findall(sanitized_text):
        cleaned = _strip_company_narrative_prefix(item.strip())
        if len(cleaned) >= 2:
            found.append(cleaned)
    return deduplicate_company_list(found)


def _extract_docx_text(raw_bytes: bytes) -> str:
    """使用标准库 zipfile + ElementTree 提取 Word (.docx) 简历正文段落。"""
    import io

    with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
        xml_names = [n for n in ("word/document.xml", "word/header1.xml", "word/footer1.xml") if n in zf.namelist()]
        paragraphs: list[str] = []
        for name in xml_names:
            root = ET.fromstring(zf.read(name))
            for elem in root.iter():
                tag = elem.tag.rsplit("}", 1)[-1]
                if tag == "p":
                    texts = [
                        node.text
                        for node in elem.iter()
                        if node.tag.rsplit("}", 1)[-1] == "t" and node.text
                    ]
                    if texts:
                        paragraphs.append("".join(texts))
        return "\n".join(paragraphs)


def read_resume_text_best_effort(file_path: str | Path) -> dict[str, Any]:
    """安全读取本地简历文本：支持 UTF-8/GB18030 文本、Word (.docx) 标准库解包，以及 PDF 提取。"""
    p = Path(file_path).expanduser().resolve()
    raw_bytes = p.read_bytes()
    suffix = p.suffix.lower()

    # 1. Word .docx（ZIP OOXML 容器）原生零依赖解析
    if suffix == ".docx" or raw_bytes.startswith(b"PK\x03\x04"):
        try:
            docx_text = _extract_docx_text(raw_bytes)
            if docx_text.strip():
                return {
                    "text": docx_text,
                    "is_pdf": False,
                    "is_docx": True,
                    "pdf_binary_notice": False,
                }
        except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError):
            if suffix == ".docx":
                return {
                    "text": "",
                    "is_pdf": False,
                    "is_docx": True,
                    "pdf_binary_notice": True,
                }

    # 2. 旧版二进制 .doc（OLE2）提示由宿主直接读取或另存为 PDF/docx
    if suffix == ".doc" or raw_bytes.startswith(b"\xd0\xcf\x11\xe0"):
        return {
            "text": raw_bytes.decode("utf-16-le", errors="ignore"),
            "is_pdf": False,
            "pdf_binary_notice": True,
        }

    # 3. 普通文本 / Markdown（兼容 UTF-8 与 Windows 常见 GB18030 编码）
    is_pdf = suffix == ".pdf" or raw_bytes.startswith(b"%PDF-")
    if not is_pdf:
        try:
            decoded_txt = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            decoded_txt = raw_bytes.decode("gb18030", errors="replace")
        return {
            "text": decoded_txt,
            "is_pdf": False,
            "pdf_binary_notice": False,
        }

    # 4. PDF：优先尝试本机已安装的 pdftotext（若有）
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
