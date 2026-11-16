"""工作区目录、求职偏好配置 config.json、个人事实档案与本地运行态管理。"""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .normalize import (
    companies_match,
    deduplicate_company_list,
    detect_basic_work_mode_risks,
    employer_shield_match,
    extract_employers_from_text,
    parse_salary_floor_k,
    read_resume_text_best_effort,
)

DEFAULT_API_BASE = "https://hiremate.chengway.wang"
VALID_MODES = ("semi", "auto")
VALID_EXECUTION_MODES = ("browser", "copilot")
VALID_BROWSER_PROVIDERS = ("auto", "ego_lite", "builtin_browser_use")
VALID_RESUME_SOURCES = ("pending", "attachment_pdf", "online_only", "both", "none")
VALID_CHANNEL_POLICIES = ("block_all", "inbound_only", "allow_all")
VALID_HEADHUNTER_POLICIES = VALID_CHANNEL_POLICIES
VALID_INBOUND_RESUME_POLICIES = ("generous", "strict")

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PROFILE_TEMPLATE_PATH = PACKAGE_ROOT / "templates" / "profile_facts.md"

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "account_display_name": "",
    "execution_mode": "browser",
    "browser_provider": "auto",
    "resume_source": "pending",
    "has_attachment_resume": True,
    "mode_discover": "semi",
    "mode_reply": "semi",
    "auto_risk_acknowledged": False,
    "target_roles": [],
    "preferred_cities": [],
    "remote_bigtech_flex": True,
    "min_salary_k": 20,
    "salary_panel_tolerance": True,
    "filter_outsourcing": True,
    "outsourcing_policy": "block_all",
    "filter_onsite": True,
    "onsite_policy": "block_all",
    "headhunter_policy": "inbound_only",
    "past_employers": [],
    "blocked_companies": [],
    "exclude_keywords": [],
    "resume_routes": [
        {
            "name": "默认主简历.pdf",
            "keywords": [],
            "is_default": True,
        }
    ],
    "inbound_resume_policy": "generous",
    "reply_style": "concise_natural",
    "schedule": {
        "weekdays_only": True,
        "active_windows": ["09:00-12:00", "14:00-18:00"],
        "min_interval_minutes": 90,
        "max_per_round": 5,
        "max_per_day": 20,
    },
    "api_base": DEFAULT_API_BASE,
}

DEFAULT_RUNTIME_STATE: dict[str, Any] = {
    "busy": False,
    "busy_since": None,
    "busy_reason": None,
    "last_discover_at": None,
    "last_patrol_at": None,
    "updated_at": None,
}


def now_iso() -> str:
    """返回带本地时区偏移的 ISO-8601 时间字符串。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_hiremate_home(home_override: str | Path | None = None) -> Path:
    if home_override:
        return Path(home_override).expanduser().resolve()
    env_home = os.environ.get("HIREMATE_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".hiremate").resolve()


def resolve_workspace(
    workspace_override: str | Path | None = None,
    home_override: str | Path | None = None,
) -> Path:
    if workspace_override:
        return Path(workspace_override).expanduser().resolve()
    env_ws = os.environ.get("HIREMATE_WORKSPACE")
    if env_ws:
        return Path(env_ws).expanduser().resolve()
    return get_hiremate_home(home_override) / "default"


def config_path(workspace: Path) -> Path:
    return workspace / "config.json"


def profile_facts_path(workspace: Path) -> Path:
    return workspace / "profile_facts.md"


def runtime_state_path(workspace: Path) -> Path:
    return workspace / "runtime_state.json"


def ledger_db_path(workspace: Path) -> Path:
    return workspace / "ledger.sqlite3"


def get_effective_api_base(config: dict[str, Any] | None = None, override: str | None = None) -> str:
    if override and override.strip():
        return override.strip().rstrip("/")
    env_base = os.environ.get("HIREMATE_API_BASE", "").strip()
    if env_base:
        return env_base.rstrip("/")
    if config and isinstance(config.get("api_base"), str) and config["api_base"].strip():
        return config["api_base"].strip().rstrip("/")
    return DEFAULT_API_BASE


def _resolve_policy_and_flag(
    raw_cfg: dict[str, Any],
    *,
    policy_key: str,
    flag_key: str,
    default_policy: str = "block_all",
) -> tuple[str, bool]:
    has_policy = policy_key in raw_cfg and raw_cfg[policy_key] is not None
    has_flag = flag_key in raw_cfg and raw_cfg[flag_key] is not None

    if has_policy:
        pol = str(raw_cfg[policy_key]).strip()
        if pol not in VALID_CHANNEL_POLICIES:
            raise ValueError(f"{policy_key} 仅支持 {VALID_CHANNEL_POLICIES}，收到: {raw_cfg[policy_key]!r}")
        return (pol, pol == "block_all")

    if has_flag:
        flag = bool(raw_cfg[flag_key])
        return ("block_all" if flag else "allow_all", flag)

    return (default_policy, default_policy == "block_all")


def apply_config_updates(cfg: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """增量合并配置字典，确保布尔过滤开关与三态渠道策略保持双向同步。"""
    for key, val in patch.items():
        if key == "schedule" and isinstance(val, dict) and isinstance(cfg.get("schedule"), dict):
            cfg["schedule"].update(val)
        else:
            cfg[key] = val

    if "outsourcing_policy" in patch and patch["outsourcing_policy"] is not None:
        pol = str(patch["outsourcing_policy"]).strip()
        cfg["outsourcing_policy"] = pol
        cfg["filter_outsourcing"] = pol == "block_all"
    elif "filter_outsourcing" in patch and patch["filter_outsourcing"] is not None:
        flag = bool(patch["filter_outsourcing"])
        cfg["filter_outsourcing"] = flag
        cfg["outsourcing_policy"] = "block_all" if flag else "allow_all"

    if "onsite_policy" in patch and patch["onsite_policy"] is not None:
        pol = str(patch["onsite_policy"]).strip()
        cfg["onsite_policy"] = pol
        cfg["filter_onsite"] = pol == "block_all"
    elif "filter_onsite" in patch and patch["filter_onsite"] is not None:
        flag = bool(patch["filter_onsite"])
        cfg["filter_onsite"] = flag
        cfg["onsite_policy"] = "block_all" if flag else "allow_all"

    if "resume_source" in patch and patch["resume_source"] is not None:
        rsrc = str(patch["resume_source"]).strip()
        cfg["resume_source"] = rsrc
        if rsrc in ("online_only", "none"):
            cfg["has_attachment_resume"] = False
        elif rsrc in ("attachment_pdf", "both"):
            cfg["has_attachment_resume"] = True

    if "browser_provider" in patch and patch["browser_provider"] is not None:
        bprov = str(patch["browser_provider"]).strip()
        cfg["browser_provider"] = bprov
        if bprov in ("ego_lite", "builtin_browser_use") and "execution_mode" not in patch:
            cfg["execution_mode"] = "browser"

    return cfg


def detect_ego_lite_installed() -> bool:
    """探测本机是否已安装并注册 ego lite 的 `ego-browser` 命令行入口。"""
    if shutil.which("ego-browser"):
        return True
    local_bin = Path.home() / ".local" / "bin" / "ego-browser"
    return local_bin.is_file()


def validate_and_normalize_config(raw_cfg: dict[str, Any]) -> dict[str, Any]:
    """补齐缺省配置项、校验枚举合法性，并自动将过往雇主同步进屏蔽名单。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for key, val in raw_cfg.items():
        if key == "schedule" and isinstance(val, dict):
            cfg["schedule"].update(val)
        else:
            cfg[key] = val

    if cfg["mode_discover"] not in VALID_MODES:
        raise ValueError(f"mode_discover 仅支持 {VALID_MODES}，收到: {cfg['mode_discover']!r}")
    if cfg["mode_reply"] not in VALID_MODES:
        raise ValueError(f"mode_reply 仅支持 {VALID_MODES}，收到: {cfg['mode_reply']!r}")
    if cfg["execution_mode"] not in VALID_EXECUTION_MODES:
        raise ValueError(f"execution_mode 仅支持 {VALID_EXECUTION_MODES}，收到: {cfg['execution_mode']!r}")
    if cfg["browser_provider"] not in VALID_BROWSER_PROVIDERS:
        raise ValueError(f"browser_provider 仅支持 {VALID_BROWSER_PROVIDERS}，收到: {cfg['browser_provider']!r}")
    if cfg["resume_source"] not in VALID_RESUME_SOURCES:
        raise ValueError(f"resume_source 仅支持 {VALID_RESUME_SOURCES}，收到: {cfg['resume_source']!r}")
    if cfg["resume_source"] in ("online_only", "none"):
        cfg["has_attachment_resume"] = False
    elif cfg["resume_source"] in ("attachment_pdf", "both"):
        cfg["has_attachment_resume"] = True
    else:
        cfg["has_attachment_resume"] = bool(cfg.get("has_attachment_resume", True))
    if cfg["headhunter_policy"] not in VALID_HEADHUNTER_POLICIES:
        raise ValueError(
            f"headhunter_policy 仅支持 {VALID_HEADHUNTER_POLICIES}，收到: {cfg['headhunter_policy']!r}"
        )
    if cfg["inbound_resume_policy"] not in VALID_INBOUND_RESUME_POLICIES:
        raise ValueError(
            f"inbound_resume_policy 仅支持 {VALID_INBOUND_RESUME_POLICIES}，收到: {cfg['inbound_resume_policy']!r}"
        )

    out_pol, out_flag = _resolve_policy_and_flag(
        raw_cfg,
        policy_key="outsourcing_policy",
        flag_key="filter_outsourcing",
    )
    # 兼容旧版单开关 config.json：若仅存有 filter_outsourcing 而未拆分驻场字段，默认同步沿用同一偏好
    legacy_single_switch = (
        "filter_outsourcing" in raw_cfg
        and "outsourcing_policy" not in raw_cfg
        and "onsite_policy" not in raw_cfg
        and "filter_onsite" not in raw_cfg
    )
    onsite_pol, onsite_flag = _resolve_policy_and_flag(
        raw_cfg,
        policy_key="onsite_policy",
        flag_key="filter_onsite",
        default_policy=out_pol if legacy_single_switch else "block_all",
    )
    cfg["outsourcing_policy"] = out_pol
    cfg["filter_outsourcing"] = out_flag
    cfg["onsite_policy"] = onsite_pol
    cfg["filter_onsite"] = onsite_flag

    cfg["min_salary_k"] = max(0, int(cfg.get("min_salary_k", 20)))
    cfg["remote_bigtech_flex"] = bool(cfg.get("remote_bigtech_flex", True))
    cfg["salary_panel_tolerance"] = bool(cfg.get("salary_panel_tolerance", True))
    cfg["auto_risk_acknowledged"] = bool(cfg.get("auto_risk_acknowledged", False))

    cfg["target_roles"] = deduplicate_company_list(cfg.get("target_roles") or [])
    cfg["preferred_cities"] = deduplicate_company_list(cfg.get("preferred_cities") or [])
    cfg["exclude_keywords"] = deduplicate_company_list(cfg.get("exclude_keywords") or [])
    cfg["past_employers"] = deduplicate_company_list(cfg.get("past_employers") or [])
    # 核心防社死规则：任何时候过往雇主自动合并进屏蔽名单
    cfg["blocked_companies"] = deduplicate_company_list(
        list(cfg.get("blocked_companies") or []) + list(cfg["past_employers"])
    )

    sched = cfg["schedule"]
    sched["weekdays_only"] = bool(sched.get("weekdays_only", True))
    sched["min_interval_minutes"] = max(30, int(sched.get("min_interval_minutes", 90)))
    sched["max_per_round"] = max(1, min(8, int(sched.get("max_per_round", 5))))
    sched["max_per_day"] = max(1, min(30, int(sched.get("max_per_day", 20))))
    raw_windows = sched.get("active_windows") or ["09:00-12:00", "14:00-18:00"]
    if isinstance(raw_windows, str):
        norm_win_str = raw_windows.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
        windows = [w.strip() for w in norm_win_str.split(",") if w.strip()]
    else:
        windows = [str(w).strip() for w in raw_windows if str(w).strip()]
    sched["active_windows"] = windows or ["09:00-12:00", "14:00-18:00"]

    routes = cfg.get("resume_routes")
    if not isinstance(routes, list) or not routes:
        cfg["resume_routes"] = copy.deepcopy(DEFAULT_CONFIG["resume_routes"])

    if not isinstance(cfg.get("api_base"), str) or not cfg["api_base"].strip():
        cfg["api_base"] = DEFAULT_API_BASE
    else:
        from urllib.parse import urlparse as _urlparse

        clean_api = cfg["api_base"].strip().rstrip("/")
        p_api = _urlparse(clean_api)
        scheme_ok = p_api.scheme == "https" or (
            p_api.scheme == "http" and (p_api.hostname or "").lower() in ("127.0.0.1", "localhost", "::1")
        )
        if not scheme_ok or not p_api.hostname:
            raise ValueError(
                f"api_base 必须使用 https://（仅本机调试允许 http://127.0.0.1 或 localhost），收到: {clean_api!r}"
            )
        cfg["api_base"] = clean_api

    return cfg


class ConfigCorruptError(ValueError):
    """本地 config.json 语法或结构损坏时抛出（自动备份损坏原文件并给出修复提示）。"""

    def __init__(self, message: str, *, corrupt_path: Path, backup_path: Path | None = None, hint: str = "") -> None:
        super().__init__(message)
        self.code = "CONFIG_CORRUPT"
        self.corrupt_path = str(corrupt_path)
        self.backup_path = str(backup_path) if backup_path else None
        self.hint = hint or "请检查备份文件中的前东家与偏好配置，或运行 `hiremate.py init` 重新生成有效配置。"


def _backup_file_with_tag(path: Path, tag: str = "bak") -> Path | None:
    if not path.is_file():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak_path = path.with_name(f"{path.name}.{tag}-{ts}")
    try:
        shutil.copy2(path, bak_path)
        return bak_path
    except OSError:
        return None


def atomic_write_text(path: Path, content: str, *, mode: int | None = None) -> None:
    """同目录临时文件 + fsync + os.replace 原子写入，防止并发或断电产生半截 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        if mode is not None:
            try:
                os.chmod(tmp_path, mode)
            except OSError:
                pass
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def load_config(workspace: Path) -> dict[str, Any]:
    path = config_path(workspace)
    if not path.is_file():
        return validate_and_normalize_config({})
    try:
        raw_text = path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        bak = _backup_file_with_tag(path, "corrupt")
        bak_note = f"（已自动备份损坏文件至 {bak}）" if bak else ""
        raise ConfigCorruptError(
            f"配置文件 JSON 已损坏：{path}{bak_note} — {exc}",
            corrupt_path=path,
            backup_path=bak,
        ) from exc
    if not isinstance(data, dict):
        bak = _backup_file_with_tag(path, "corrupt")
        raise ConfigCorruptError(
            f"配置文件格式无效（期望 JSON 对象）：{path}",
            corrupt_path=path,
            backup_path=bak,
        )
    return validate_and_normalize_config(data)


def save_config(workspace: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    normalized = validate_and_normalize_config(cfg)
    atomic_write_text(
        config_path(workspace),
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
    )
    return normalized


def inspect_onboarding_status(
    workspace: Path,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """检查当前工作区是否已完成新用户冷启动建档，返回给 Agent 的明确引导信号与硬门槛状态。"""
    cfg_file = config_path(workspace)
    pf_file = profile_facts_path(workspace)
    initialized = cfg_file.is_file()
    effective_cfg = cfg if cfg is not None else load_config(workspace)

    template_raw = (
        PROFILE_TEMPLATE_PATH.read_text(encoding="utf-8").strip()
        if PROFILE_TEMPLATE_PATH.is_file()
        else ""
    )
    pf_raw = pf_file.read_text(encoding="utf-8").strip() if pf_file.is_file() else ""
    profile_customized = bool(pf_raw) and pf_raw != template_raw

    resume_source = str(effective_cfg.get("resume_source") or "pending")
    execution_mode = str(effective_cfg.get("execution_mode") or "browser")
    browser_provider = str(effective_cfg.get("browser_provider") or "auto")
    has_attachment_resume = bool(effective_cfg.get("has_attachment_resume", True))
    has_roles = bool(effective_cfg.get("target_roles"))
    has_account_name = bool(str(effective_cfg.get("account_display_name") or "").strip())
    has_employers = bool(effective_cfg.get("past_employers"))

    resume_ready = profile_customized and resume_source != "none"
    hard_blocker: str | None = None
    if resume_source == "none":
        hard_blocker = "NO_RESUME_AVAILABLE"
    elif not profile_customized:
        hard_blocker = "RESUME_NOT_IMPORTED"

    missing_steps: list[str] = []
    if not initialized:
        missing_steps.append("尚未运行 init 初始化本机求职工作区")
    if resume_source == "none":
        missing_steps.append("【硬性阻断】用户当前既无本地简历文件也无平台在线简历，必须先准备至少一份简历后才能继续")
    elif not profile_customized:
        missing_steps.append(
            "【硬性前提】尚未提取真实简历（请用户拖入 PDF 简历，或授权读取/导出 BOSS 在线简历）写入 profile_facts.md"
        )
    if not has_roles:
        missing_steps.append("尚未设定目标岗位方向关键词（target_roles）")

    recommendations: list[str] = []
    if execution_mode == "browser" and not has_account_name:
        recommendations.append("建议核对并记录 BOSS 直聘右上角本人显示名（--account-name）以防串号")
    if not has_employers:
        recommendations.append("若有过往任职经历，请提取全部前东家写入屏蔽名单（--employers）防误投")
    if resume_source == "online_only" or not has_attachment_resume:
        recommendations.append(
            "当前仅使用平台在线简历（无线上 PDF 附件）：建议在 BOSS 在线简历页右上角一键「生成附件简历」，否则 HR 索要附件时需转人工处理"
        )

    onboarding_completed = initialized and has_roles and resume_ready
    if resume_source == "none":
        next_action_hint = (
            "【暂停引导】求职助手必须基于至少一份真实简历运行（严禁无简历凭空编造经历）。"
            "请先请用户在 BOSS 直聘填好在线简历或准备一份基础简历文件，完成后再继续建档。"
        )
    elif not onboarding_completed:
        next_action_hint = (
            "检测到新用户尚未完成建档：请先运行 `hiremate.py checklist --builtin-headed-persistent <true|false>` 向用户展示《四项启动准备核对单》——"
            "① 先判断宿主自带 Browser Use 能否支持「有头可见 + 持久化登录态」：若不支持则直接跳过并安装使用 ego lite（告知用户原因）；"
            "若支持则让用户二选一（想省 Token 则安装使用 ego lite，Token 充裕随便用则直接用自带 Browser Use；严禁无头/临时浏览器或脚本硬抓网页）；"
            "② 核对至少一份真实简历（拖入本地 PDF，或直接读取 BOSS 在线简历 `https://www.zhipin.com/web/geek/resume`，无简历则立即停步）；"
            "③ 确认岗位/城市/底薪及外包与驻场接受度，运行 init 并写入确认后的 profile_facts.md。"
        )
    else:
        if execution_mode == "copilot":
            next_action_hint = (
                "求职档案已就绪（当前为副驾驶粘贴模式）：请先顺手运行 `hiremate.py build-dashboard` 生成初始看板，"
                "并在回复末尾主动给出 `1 / 2 / 3` 下一步菜单（① 粘贴一条 BOSS 岗位 JD 试跑排雷与破冰草稿；② 粘贴 HR 消息拟回复；③ 开启浏览器直连自动搜岗），切勿建完档直接停步冷场。"
            )
        else:
            next_action_hint = (
                "求职档案已就绪（当前为浏览器直连模式）：请先顺手运行 `hiremate.py build-dashboard` 生成初始看板，"
                "并在回复末尾主动给出 `1 / 2 / 3` 下一步菜单（①【推荐】立即去 BOSS 直聘跑首轮搜岗深筛与半自动破冰预览；② 粘贴单条岗位 JD/HR 消息参谋；③ 打开看板或微调偏好），切勿建完档直接停步冷场。"
            )

    return {
        "initialized": initialized,
        "onboarding_completed": onboarding_completed,
        "profile_customized": profile_customized,
        "resume_ready": resume_ready,
        "resume_source": resume_source,
        "has_attachment_resume": has_attachment_resume,
        "execution_mode": execution_mode,
        "browser_provider": browser_provider,
        "hard_blocker": hard_blocker,
        "missing_steps": missing_steps,
        "recommendations": recommendations,
        "next_action_hint": next_action_hint,
    }


def build_startup_checklist(
    workspace: Path,
    *,
    cfg: dict[str, Any] | None = None,
    tier: str = "free",
    authenticated_email: str | None = None,
    browser_capable: bool | None = None,
    builtin_headed_persistent: bool | None = None,
    ego_installed: bool | None = None,
) -> dict[str, Any]:
    """构建面向新用户与桌面 Agent 的《四项启动准备核对单（Checklist）》。"""
    effective_cfg = cfg if cfg is not None else load_config(workspace)
    ob = inspect_onboarding_status(workspace, effective_cfg)
    exec_mode = ob["execution_mode"]
    browser_provider = ob["browser_provider"]
    resume_src = ob["resume_source"]
    has_ego = detect_ego_lite_installed() if ego_installed is None else bool(ego_installed)

    # 第 1 项：浏览器操控通道决策（先判宿主自带 Browser Use 有头+持久化能力，再自动切 ego lite 或交给用户二选一）
    if exec_mode == "copilot" or (browser_capable is False and builtin_headed_persistent is None):
        b_status = "copilot_fallback"
        b_icon = "⚠️"
        b_summary = "当前无浏览器操控权限（可安装启用 ego lite，或使用「副驾驶粘贴模式」继续）"
        b_action = (
            "自动直连推荐：若宿主自带 Browser Use 支持有头+持久化可直接选用，否则直接安装并使用 ego lite（https://lite.ego.app/，省 Token 且防风控，Pro 自动巡检必需）。"
            "若当前环境不便安装浏览器，可保持 `--execution-mode copilot` 由用户粘贴 JD/消息完成排雷、拟话术与记账（严禁使用无头临时浏览器或 curl/爬虫脚本抓网页）。"
        )
    elif builtin_headed_persistent is False:
        if has_ego:
            b_status = "auto_ego_ready"
            b_icon = "✅"
            b_summary = "自带 Browser Use 不支持有头+持久化，已跳过并切换为 ego lite（本机已安装就绪）"
            b_action = (
                "请明确告知用户：当前宿主自带浏览器不支持「有头可见窗口 + 登录态持久化」（在 BOSS 直聘易掉登录或触发无头反爬风控），"
                "已直接跳过自带 Browser Use 并使用本机 ego lite（`ego-browser`）；请运行 `config set --browser-provider ego_lite` 锁定通道。"
            )
        else:
            b_status = "install_ego_required"
            b_icon = "🔧"
            b_summary = "自带 Browser Use 不支持有头+持久化，已直接跳过，准备安装并使用 ego lite"
            b_action = (
                "请明确告知用户跳过自带 Browser Use 的原因（缺有头可见或持久化登录态易触发 BOSS 风控），并立即协助安装 ego lite"
                "（macOS 可执行 ego-browser 技能内置 `install.sh`，Windows/通用引导下载 https://lite.ego.app/ 并完成首次打开引导以注册 `ego-browser` 命令），"
                "就绪后运行 `config set --browser-provider ego_lite`（若用户明确不便安装，再切换 `--execution-mode copilot` 兜底；严禁使用无头浏览器或爬虫脚本）。"
            )
    elif builtin_headed_persistent is True:
        if browser_provider == "ego_lite":
            b_status = "ready" if has_ego else "install_ego_required"
            b_icon = "✅" if has_ego else "🔧"
            b_summary = "已选定省 Token 通道：ego lite（紧凑 DOM 快照）" + ("" if has_ego else " · 待完成安装")
            b_action = (
                "全程复用 `ego-browser` 持久化窗口操作 BOSS 直聘，大幅节省单轮搜岗与 Pro 定时巡检 Token。"
                if has_ego
                else "用户已选定省 Token 的 ego lite 方案，请协助安装 https://lite.ego.app/ 并完成首次打开引导注册 `ego-browser` 命令。"
            )
        elif browser_provider == "builtin_browser_use":
            b_status = "ready"
            b_icon = "✅"
            b_summary = "已选定免装直用通道：宿主自带 Browser Use（有头 + 持久化登录态）"
            b_action = "直接复用宿主自带的有头持久化 Browser Use 窗口操作 BOSS 直聘（免额外装软件；严禁切回 Headless 无头模式或新建空白无痕窗口）。"
        else:
            b_status = "choice_required"
            b_icon = "⚖️"
            b_summary = "宿主自带 Browser Use 支持「有头 + 持久化」，请让用户按 Token 预算二选一"
            ego_tag = "本机已安装，可直接用" if has_ego else "本机未装，选此项则顺手安装启用"
            b_action = (
                f"请把选择权交给用户，选定后运行 `config set --browser-provider <ego_lite|builtin_browser_use>`："
                f"① 【想省 Token】安装并使用 ego lite（紧凑 DOM 引用快照，大幅节省搜岗与 Pro 高频巡检 Token；{ego_tag}）；"
                f"② 【Token 多随便用】直接使用宿主自带 Browser Use（免额外装软件开箱即用，视觉截图推理消耗较高）。"
            )
    elif browser_provider == "ego_lite":
        b_status = "ready" if has_ego else "install_ego_required"
        b_icon = "✅" if has_ego else "🔧"
        b_summary = "已配置浏览器通道：ego lite（省 Token 持久化窗口）" + ("" if has_ego else " · 待安装 ego lite")
        b_action = (
            "直接复用已登录的 `ego-browser` 窗口读取在线简历、深筛岗位与收发消息。"
            if has_ego
            else "请先协助安装 ego lite（https://lite.ego.app/）并完成首次打开引导以启用 `ego-browser` 命令。"
        )
    elif browser_provider == "builtin_browser_use":
        b_status = "ready"
        b_icon = "✅"
        b_summary = "已配置浏览器通道：宿主自带 Browser Use（有头 + 持久化窗口）"
        b_action = "直接复用宿主自带的有头持久化 Browser Use 窗口操作 BOSS 直聘（严禁使用 Headless 无头或临时无痕实例）。"
    elif browser_capable is True:
        b_status = "ready"
        b_icon = "✅"
        b_summary = "已具备浏览器操控权限（网页自动直连模式）"
        b_action = (
            "若宿主自带 Browser Use 支持有头+持久化，请让用户选择「省 Token 装用 ego lite」或「Token 充裕直接用自带 Browser Use」；"
            "若自带 Browser Use 不支持有头+持久化，请直接跳过并安装使用 ego lite 且告知用户。"
        )
    else:
        b_status = "ready" if ob["initialized"] else "needs_check"
        b_icon = "✅" if ob["initialized"] else "⬜"
        b_summary = "默认网页直连模式（请 Agent 先自检自带 Browser Use 是否支持有头+持久化）"
        b_action = (
            "请运行 `checklist --builtin-headed-persistent <true|false>`：不支持有头+持久化则跳过并安装使用 ego lite（告知用户）；"
            "支持则让用户选择「省 Token 用 ego lite」或「Token 多直接用自带 Browser Use」。"
        )

    # 第 2 项：招聘平台本人登录态
    acct_name = str(effective_cfg.get("account_display_name") or "").strip()
    if acct_name:
        p_status = "ready"
        p_icon = "✅"
        p_summary = f"已登记招聘平台本人账号名：{acct_name}"
        p_action = "每次网页操作前自动核对右上角显示名，防止多账号串号。"
    elif b_status == "copilot_fallback":
        p_status = "optional_in_copilot"
        p_icon = "☑️"
        p_summary = "副驾驶粘贴模式：由你本人在日常浏览器保持 BOSS 直聘登录即可"
        p_action = "可通过 `config set --account-name <姓名>` 备注本人账号名。"
    else:
        p_status = "pending"
        p_icon = "⬜"
        p_summary = "待核对 BOSS 直聘网页登录态与右上角本人姓名"
        p_action = "请在常用浏览器登录 BOSS 直聘，Agent 将核对右上角姓名并写入配置。"

    # 第 3 项：真实简历底座（硬性门槛）
    if resume_src == "none":
        r_status = "blocked"
        r_icon = "❌"
        r_summary = "【硬性卡点】当前既无本地简历文件，也无平台在线简历"
        r_action = "无简历无法建档（严禁凭空编造经历）。请先在 BOSS 直聘填好在线简历或准备一份基础简历后再继续。"
    elif ob["resume_ready"]:
        r_status = "ready"
        r_icon = "✅"
        if resume_src == "online_only" or not ob["has_attachment_resume"]:
            r_summary = "已从平台在线简历提炼建档（当前无线上 PDF 附件）"
            r_action = "建议在 BOSS 在线简历页右上角点「生成附件简历」保存一份线上 PDF，方便 HR 索要时一键发送。"
        else:
            routes = [str(x.get("name") or "") for x in (effective_cfg.get("resume_routes") or []) if x.get("name")]
            r_summary = f"真实履历档案已就绪（线上附件路由：{'、'.join(routes) or '默认主简历'}）"
            r_action = "沟通答疑严格以确认过的本地档案为准，超纲问题自动停手转人工。"
    else:
        r_status = "pending"
        r_icon = "⬜"
        r_summary = "【必选前提】请提供至少一份真实简历（本地文件 或 平台在线简历 二选一）"
        r_action = (
            "方式 A：直接把 1～3 份 PDF/文档简历拖进对话；"
            "方式 B：若只有 BOSS 在线简历，告知助手直接去 `https://www.zhipin.com/web/geek/resume` 提取"
            "（无浏览器权限时可在该页点导出 PDF 或全选复制文字发进对话）；若两者皆无则无法继续。"
        )

    # 第 4 项：求职偏好、前东家防撞与版本档位
    roles = effective_cfg.get("target_roles") or []
    cities = effective_cfg.get("preferred_cities") or []
    emps = effective_cfg.get("past_employers") or []
    out_pol = effective_cfg.get("outsourcing_policy", "block_all")
    onsite_pol = effective_cfg.get("onsite_policy", "block_all")
    pol_cn = {"block_all": "排除", "inbound_only": "仅接来信", "allow_all": "开放"}
    tier_cn = f"Pro 买断版（{authenticated_email}）" if tier == "pro" and authenticated_email else (
        "Pro 买断版" if tier == "pro" else "免费基础版（免登录即用）"
    )
    if roles:
        pref_status = "ready"
        pref_icon = "✅"
        pref_summary = (
            f"方向：{'/'.join(roles)} ｜ 城市：{'/'.join(cities) or '不限'} ｜ 底薪：{effective_cfg.get('min_salary_k', 20)}K+ ｜ "
            f"外包：{pol_cn.get(out_pol, out_pol)} ｜ 驻场：{pol_cn.get(onsite_pol, onsite_pol)} ｜ 已拉黑前东家：{len(emps)} 家"
        )
        pref_action = f"当前版本：{tier_cn}。"
    else:
        pref_status = "pending"
        pref_icon = "⬜"
        pref_summary = (
            f"待确认目标岗位、城市、底薪与外包/驻场接受度（默认外包[{pol_cn.get(out_pol)}] · 驻场[{pol_cn.get(onsite_pol)}]，可独立放开）"
        )
        pref_action = f"当前版本：{tier_cn}；导入简历时将自动识别全部前东家加入屏蔽名单。"

    items = [
        {
            "step": 1,
            "id": "browser_permission",
            "required": False,
            "status": b_status,
            "title": "桌面 Agent 浏览器操控权限",
            "summary": b_summary,
            "action": b_action,
        },
        {
            "step": 2,
            "id": "platform_login",
            "required": False,
            "status": p_status,
            "title": "招聘平台本人账号登录态",
            "summary": p_summary,
            "action": p_action,
        },
        {
            "step": 3,
            "id": "resume_ready",
            "required": True,
            "status": r_status,
            "title": "真实简历底座（本地 PDF 或 平台在线简历）",
            "summary": r_summary,
            "action": r_action,
        },
        {
            "step": 4,
            "id": "preferences_and_tier",
            "required": True,
            "status": pref_status,
            "title": "求职边界、前东家屏蔽与版本档位",
            "summary": pref_summary,
            "action": pref_action,
        },
    ]

    md_lines = [
        "### 📋 求职助手 · 启动准备核对单（Checklist）",
        f"1. {b_icon} **浏览器操控权限**：{b_summary}",
        f"   - {b_action}",
        f"2. {p_icon} **招聘网页登录态**：{p_summary}",
        f"   - {p_action}",
        f"3. {r_icon} **真实简历底座（必选）**：{r_summary}",
        f"   - {r_action}",
        f"4. {pref_icon} **求职偏好与版本**：{pref_summary}",
        f"   - {pref_action}",
    ]

    res_chk: dict[str, Any] = {
        "ok": True,
        "tier": tier,
        "onboarding_completed": ob["onboarding_completed"],
        "can_proceed_to_search": ob["onboarding_completed"],
        "hard_blocker": ob["hard_blocker"],
        "execution_mode": exec_mode,
        "browser_provider": browser_provider,
        "ego_lite_installed": has_ego,
        "builtin_headed_persistent": builtin_headed_persistent,
        "resume_source": resume_src,
        "has_attachment_resume": ob["has_attachment_resume"],
        "items": items,
        "markdown": "\n".join(md_lines),
    }
    if ob["onboarding_completed"]:
        res_chk["post_onboarding_guide"] = build_post_onboarding_guide(
            effective_cfg,
            tier=tier,
            dashboard_path=str((workspace / "dashboard.html").resolve()),
        )
    return res_chk


def build_post_onboarding_guide(
    cfg: dict[str, Any],
    *,
    tier: str = "free",
    dashboard_path: str | None = None,
) -> dict[str, Any]:
    """构建建档完成后的强制下一步引导（供 Agent 直接展示给用户，杜绝建完档停步冷场）。"""
    exec_mode = str(cfg.get("execution_mode") or "browser")
    b_prov = str(cfg.get("browser_provider") or "auto")
    roles = " / ".join(cfg.get("target_roles") or []) or "目标岗位"
    cities = " / ".join(cfg.get("preferred_cities") or []) or "不限城市"
    min_k = int(cfg.get("min_salary_k", 20) or 20)
    shield_cnt = len(cfg.get("past_employers") or [])
    dash_note = f"（本地看板路径：`{dashboard_path}`）" if dashboard_path else ""

    if exec_mode == "copilot":
        recommended_action = "paste_first_jd_or_enable_browser"
        options = [
            {
                "key": "1",
                "label": "【最常用】直接粘贴一条 BOSS 岗位 JD 或截图给我试水准",
                "detail": f"立刻帮你核对前东家（已保护 {shield_cnt} 家）与外包/驻场风险、提炼匹配亮点与缺口，并拟好 1～2 句真人感破冰话术供你复制发送",
            },
            {
                "key": "2",
                "label": "粘贴一条 HR 刚发来的追问消息让我拟回复",
                "detail": "严格对照你的真实履历档案拟定自然日常短句（绝不瞎编），并自动记入本地四态求职账本",
            },
            {
                "key": "3",
                "label": "开启浏览器自动直连（免手动复制粘贴）",
                "detail": "启用省 Token 的 ego lite 或宿主自带的有头持久化 Browser Use，让我直接去 BOSS 网页替你刷列表深筛"
                + ("与自动巡检" if tier == "pro" else ""),
            },
        ]
        menu_lines = [
            "### 🚀 本地建档已完成！接下来直接这样开始（回复数字或直接发内容即可）：",
            f"- **回复 `1` 或直接把岗位 JD 贴进来（推荐）**：我立即替你做外包/前东家排雷、分析匹配点与缺口，并拟好破冰短句供你复制发送",
            "- **回复 `2` 或粘贴 HR 消息**：对照你的真实履历拟定 1～2 句自然回复草稿，并同步记入本地台账",
            f"- **回复 `3`**：开启浏览器自动直连模式（免手动粘贴，直接去 BOSS 替你刷 `{roles}` 列表深筛）{dash_note}",
        ]
    elif tier == "pro":
        recommended_action = "run_pro_first_round_and_schedule_patrol"
        prov_label = (
            "ego lite 省 Token 通道"
            if b_prov == "ego_lite"
            else ("宿主自带 Browser Use" if b_prov == "builtin_browser_use" else "已登录浏览器")
        )
        options = [
            {
                "key": "1",
                "label": "【推荐】立即去 BOSS 跑首轮 Pro 深度排雷筛岗 + 检查未读消息",
                "detail": f"通过{prov_label}按「{roles} · {cities} · {min_k}K+」筛出高匹配自研岗（100+ 深度外包名录自动避坑、跳过 {shield_cnt} 家前东家）",
            },
            {
                "key": "2",
                "label": "一键挂载工作日错峰自动巡检定时任务",
                "detail": "每周一至周五 09:00–12:00、14:00–18:00 每 10～15 分钟自动盯未读消息、递对应简历，遇到约面/谈薪立即弹窗叫你",
            },
            {
                "key": "3",
                "label": "切换半自动 / 全自动档位或查看本地求职看板",
                "detail": f"当前可分别设定「主动找岗」与「消息答疑」为半自动确认（semi）或全自动托管（auto）{dash_note}",
            },
        ]
        menu_lines = [
            "### 🚀 Pro 求职档案已就绪！回复数字直接开工：",
            f"1. **【推荐】立即跑首轮 BOSS 深度搜岗 + 未读消息检查** — 按 `{roles}`（`{cities}` · `{min_k}K+`）穿透筛除 100+ 伪装外包并避开 {shield_cnt} 家前东家",
            "2. **一键开启工作日自动盯盘巡检** — 挂载周一至周五白天错峰定时巡检，自动接话递简历、到约面弹窗喊你",
            f"3. **调整半自动/全自动档位 或 打开本地看板**{dash_note}",
        ]
    else:
        recommended_action = "run_free_first_round_boss_preview"
        prov_label = (
            "ego lite"
            if b_prov == "ego_lite"
            else ("自带 Browser Use" if b_prov == "builtin_browser_use" else "浏览器")
        )
        options = [
            {
                "key": "1",
                "label": "【推荐】立即去 BOSS 直聘跑第一轮搜岗深筛预览（先验水准，不乱点发送）",
                "detail": f"使用{prov_label}按「{roles} · {cities} · {min_k}K+」筛出 3～5 个靠谱自研候选、自动绕开 {shield_cnt} 家前东家与外包，生成破冰话术审阅单给你过目，等你回序号才发送",
            },
            {
                "key": "2",
                "label": "发一个你看中的岗位 JD 或 HR 消息让我参谋",
                "detail": "直接把岗位文本或截图发进对话，替你查外包排雷、分析匹配依据与缺口，并记入本地四态账本",
            },
            {
                "key": "3",
                "label": "打开本地求职看板 / 微调筛选偏好",
                "detail": f"查看刚生成的静态看板{dash_note}，或随时一句话修改薪资、城市、外包/驻场开关",
            },
        ]
        menu_lines = [
            "### 🚀 本地建档已完成！接下来想怎么试跑？（直接回数字 `1` 即可开始）",
            f"1. **【推荐】立即去 BOSS 跑第一轮搜岗深筛预览** — 按 `{roles}`（`{cities}` · `{min_k}K+`）筛出 3～5 个靠谱自研岗、自动避开 {shield_cnt} 家前东家与外包，**先出破冰话术审阅单给你过目，未经你确认绝不点发送**",
            "2. **粘贴单条岗位 JD 或 HR 消息让我参谋** — 替你排雷、挑匹配亮点、拟 1～2 句真人感回复草稿并记入本地台账",
            f"3. **查看本地求职看板 / 微调筛选条件** — 随时调整薪资、城市、外包/驻场接受度{dash_note}",
        ]

    return {
        "tier": tier,
        "execution_mode": exec_mode,
        "browser_provider": b_prov,
        "recommended_action": recommended_action,
        "agent_must_do": [
            "1. 若尚未将确认后的个人求职事实写入 profile_facts_path，请立即写入，并顺手运行一次 `hiremate.py build-dashboard` 生成初始静态看板；",
            "2. 【严禁建完档停步冷场】绝不能只回复一句“建档已完成”就结束！必须在同一条回复末尾向用户清晰展示下方 `user_reply_menu_markdown` 的 1 / 2 / 3 下一步操作菜单，让用户直接回一个 `1` 就能开跑首轮实战。",
        ],
        "options": options,
        "user_reply_menu_markdown": "\n".join(menu_lines),
    }


def init_workspace(
    workspace: Path,
    *,
    account_name: str | None = None,
    employers: list[str] | None = None,
    target_roles: list[str] | None = None,
    preferred_cities: list[str] | None = None,
    min_salary_k: int | None = None,
    filter_outsourcing: bool | None = None,
    outsourcing_policy: str | None = None,
    filter_onsite: bool | None = None,
    onsite_policy: str | None = None,
    execution_mode: str | None = None,
    browser_provider: str | None = None,
    resume_source: str | None = None,
    tier: str = "free",
    force: bool = False,
    reset_profile: bool = False,
) -> dict[str, Any]:
    """初始化求职助手本地工作区。
    默认 --force 仅重置偏好字段并保留前东家/屏蔽企业与已定制个人档案（自动备份旧文件）；
    仅当显式传入 reset_profile=True 时才连同前东家黑名单与个人档案一并重置为初始模板。
    """
    workspace.mkdir(parents=True, exist_ok=True)
    cfg_file = config_path(workspace)
    backups: list[str] = []
    preserved_past: list[str] = []
    preserved_blocked: list[str] = []

    if cfg_file.is_file() and not force and not reset_profile:
        cfg = load_config(workspace)
    else:
        if cfg_file.is_file():
            bak_cfg = _backup_file_with_tag(cfg_file, "bak")
            if bak_cfg:
                backups.append(str(bak_cfg))
            if not reset_profile:
                try:
                    old_raw = json.loads(cfg_file.read_text(encoding="utf-8"))
                    if isinstance(old_raw, dict):
                        preserved_past = deduplicate_company_list(old_raw.get("past_employers") or [])
                        preserved_blocked = deduplicate_company_list(old_raw.get("blocked_companies") or [])
                except Exception:
                    pass
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        if os.environ.get("HIREMATE_API_BASE"):
            cfg["api_base"] = os.environ["HIREMATE_API_BASE"].strip().rstrip("/")
        if preserved_past:
            cfg["past_employers"] = preserved_past
        if preserved_blocked:
            cfg["blocked_companies"] = preserved_blocked

    if account_name is not None:
        cfg["account_display_name"] = account_name.strip()
    if employers:
        cfg["past_employers"] = deduplicate_company_list(list(cfg.get("past_employers", [])) + employers)
    if target_roles:
        cfg["target_roles"] = deduplicate_company_list(list(cfg.get("target_roles", [])) + target_roles)
    if preferred_cities:
        cfg["preferred_cities"] = deduplicate_company_list(
            list(cfg.get("preferred_cities", [])) + preferred_cities
        )
    if min_salary_k is not None:
        cfg["min_salary_k"] = min_salary_k

    init_patch: dict[str, Any] = {}
    if filter_outsourcing is not None:
        init_patch["filter_outsourcing"] = filter_outsourcing
    if outsourcing_policy is not None:
        init_patch["outsourcing_policy"] = outsourcing_policy
    if filter_onsite is not None:
        init_patch["filter_onsite"] = filter_onsite
    if onsite_policy is not None:
        init_patch["onsite_policy"] = onsite_policy
    if execution_mode is not None:
        init_patch["execution_mode"] = execution_mode
    if browser_provider is not None:
        init_patch["browser_provider"] = browser_provider
    if resume_source is not None:
        init_patch["resume_source"] = resume_source
    if init_patch:
        apply_config_updates(cfg, init_patch)

    saved_cfg = save_config(workspace, cfg)

    pf_path = profile_facts_path(workspace)
    template_text = (
        PROFILE_TEMPLATE_PATH.read_text(encoding="utf-8")
        if PROFILE_TEMPLATE_PATH.is_file()
        else "# 个人求职事实与答法档案\n"
    )
    profile_created = False
    profile_preserved = False
    if not pf_path.exists():
        atomic_write_text(pf_path, template_text)
        profile_created = True
    elif reset_profile:
        bak_pf = _backup_file_with_tag(pf_path, "bak")
        if bak_pf:
            backups.append(str(bak_pf))
        atomic_write_text(pf_path, template_text)
        profile_created = True
    elif force:
        existing_pf = pf_path.read_text(encoding="utf-8")
        if existing_pf.strip() == template_text.strip() or not existing_pf.strip():
            atomic_write_text(pf_path, template_text)
            profile_created = True
        else:
            profile_preserved = True

    rt_path = runtime_state_path(workspace)
    if not rt_path.exists():
        save_runtime_state(workspace, copy.deepcopy(DEFAULT_RUNTIME_STATE))

    dash_file = str((workspace / "dashboard.html").resolve())
    return {
        "ok": True,
        "workspace": str(workspace),
        "config_path": str(cfg_file),
        "profile_facts_path": str(pf_path),
        "ledger_path": str(ledger_db_path(workspace)),
        "dashboard_path": dash_file,
        "profile_created": profile_created,
        "profile_preserved": profile_preserved,
        "backups": backups,
        "config": saved_cfg,
        "post_onboarding_guide": build_post_onboarding_guide(
            saved_cfg,
            tier=tier,
            dashboard_path=dash_file,
        ),
    }


def merge_employers(
    workspace: Path,
    employers: list[str] | None = None,
    from_file: str | Path | None = None,
    from_text: str | None = None,
) -> dict[str, Any]:
    """提取或接收过往就职企业列表，自动去重合并进 past_employers 与 blocked_companies。"""
    cfg = load_config(workspace)
    before_blocked = list(cfg.get("blocked_companies", []))
    incoming: list[str] = list(employers or [])
    pdf_binary_notice = False
    if from_file:
        read_res = read_resume_text_best_effort(from_file)
        pdf_binary_notice = bool(read_res.get("pdf_binary_notice"))
        incoming.extend(extract_employers_from_text(str(read_res.get("text") or "")))
    if from_text:
        incoming.extend(extract_employers_from_text(from_text))

    cleaned_incoming = deduplicate_company_list(incoming)
    cfg["past_employers"] = deduplicate_company_list(list(cfg.get("past_employers", [])) + cleaned_incoming)
    saved = save_config(workspace, cfg)

    before_lower = {x.lower() for x in before_blocked}
    newly_added = [c for c in saved["blocked_companies"] if c.lower() not in before_lower]
    result: dict[str, Any] = {
        "ok": True,
        "added_employers": cleaned_incoming,
        "newly_blocked": newly_added,
        "past_employers": saved["past_employers"],
        "blocked_companies": saved["blocked_companies"],
    }
    if pdf_binary_notice and not cleaned_incoming:
        result["notice"] = (
            "检测到压缩流 PDF 简历，请由当前桌面 Agent 直接读取 PDF 内容后通过 --employers 传入全部过往雇主名称。"
        )
    return result


def basic_screen_candidate(
    config: dict[str, Any],
    *,
    company: str,
    title: str,
    salary_text: str = "",
    company_intro: str = "",
    jd_text: str = "",
    is_headhunter: bool = False,
    source: str = "outbound",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """开源版基础单轮初筛（支持同字号前东家拉黑、可配置的外包/派遣与驻场策略、排除词、猎头开关与底薪参考）。"""
    reasons_block: list[str] = []
    warnings: list[str] = []

    all_shielded = deduplicate_company_list(
        list(config.get("blocked_companies") or []) + list(config.get("past_employers") or [])
    )
    for blocked in all_shielded:
        if employer_shield_match(company, blocked):
            is_former = any(employer_shield_match(company, emp) for emp in config.get("past_employers", []))
            tag_label = "前东家/现东家保护拦截" if is_former else "命中自定义屏蔽企业名单"
            reasons_block.append(f"{tag_label}（匹配：{blocked}）")
            break

    tags_str = " ".join(tags or [])
    combined_text = f"{title} {tags_str} {company_intro} {jd_text}".lower()
    out_pol, _ = _resolve_policy_and_flag(
        config, policy_key="outsourcing_policy", flag_key="filter_outsourcing"
    )
    onsite_pol, _ = _resolve_policy_and_flag(
        config, policy_key="onsite_policy", flag_key="filter_onsite"
    )
    work_risks = detect_basic_work_mode_risks(company, f"{title}\n{tags_str}\n{company_intro}\n{jd_text}")
    is_out = bool(work_risks["is_outsourcing"])
    out_reason = str(work_risks["outsourcing_reason"] or "")
    is_onsite = bool(work_risks["is_onsite"])
    onsite_reason = str(work_risks["onsite_reason"] or "")

    if is_out:
        if out_pol == "block_all":
            reasons_block.append(f"基础外包排雷拦截（{out_reason}）")
        elif out_pol == "inbound_only" and source == "outbound":
            reasons_block.append(f"外包策略为“主动找岗不投、仅接被动来信”（{out_reason}）")
        else:
            warnings.append(f"识别到外包/派遣性质（{out_reason}），已按当前求职偏好保留")

    if is_onsite:
        if onsite_pol == "block_all":
            reasons_block.append(f"基础驻场/外派排雷拦截（{onsite_reason}）")
        elif onsite_pol == "inbound_only" and source == "outbound":
            reasons_block.append(f"驻场策略为“主动找岗不投、仅接被动来信”（{onsite_reason}）")
        else:
            warnings.append(f"识别到驻场/外派办公属性（{onsite_reason}），已按当前求职偏好保留")

    for kw in config.get("exclude_keywords", []):
        if kw and kw.lower() in combined_text:
            reasons_block.append(f"命中排除关键词：{kw}")

    hh_policy = config.get("headhunter_policy", "inbound_only")
    if is_headhunter:
        if hh_policy == "block_all":
            reasons_block.append("当前配置已屏蔽全部猎头岗位")
        elif hh_policy == "inbound_only" and source == "outbound":
            reasons_block.append("猎头策略为“主动找岗不投、仅接被动优质来信”")

    ceil_k = parse_salary_floor_k(salary_text)
    min_k = int(config.get("min_salary_k", 0) or 0)
    if ceil_k is not None and min_k > 0 and ceil_k < min_k:
        if config.get("salary_panel_tolerance", True) and ceil_k >= max(1, int(min_k * 0.8)):
            warnings.append(f"面板薪资上限（{ceil_k}K）略低于期望（{min_k}K），已按弹性容忍保留待核")
        else:
            reasons_block.append(f"面板薪资上限（{ceil_k}K）明显低于最低期望（{min_k}K）")

    matched_roles = [
        role for role in config.get("target_roles", []) if role.lower() in title.lower()
    ]
    return {
        "ok": True,
        "tier_scope": "open_basic",
        "pass_basic": len(reasons_block) == 0,
        "blocked_reasons": reasons_block,
        "warnings": warnings,
        "is_outsourcing": is_out,
        "is_onsite": is_onsite,
        "outsourcing_policy": out_pol,
        "onsite_policy": onsite_pol,
        "matched_target_roles": matched_roles,
    }


def select_resume_for_title(config: dict[str, Any], title: str, job_tags: str = "") -> str:
    """本地多简历基础方向提示（按配置中的方向词匹配，未命中则回退默认主简历）。"""
    routes = config.get("resume_routes") or []
    haystack = f"{title} {job_tags}".lower()
    default_name = "默认主简历.pdf"
    for route in routes:
        if route.get("is_default") and route.get("name"):
            default_name = str(route["name"])
            break
    if routes and default_name == "默认主简历.pdf" and routes[0].get("name"):
        default_name = str(routes[0]["name"])

    for route in routes:
        keywords = route.get("keywords") or []
        if any(str(kw).strip().lower() in haystack for kw in keywords if str(kw).strip()):
            return str(route.get("name") or default_name)
    return default_name


def load_runtime_state(workspace: Path) -> dict[str, Any]:
    path = runtime_state_path(workspace)
    state = copy.deepcopy(DEFAULT_RUNTIME_STATE)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update(raw)
        except (OSError, json.JSONDecodeError):
            pass
    return state


def save_runtime_state(workspace: Path, state: dict[str, Any]) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    merged = copy.deepcopy(DEFAULT_RUNTIME_STATE)
    merged.update(state)
    merged["updated_at"] = now_iso()
    atomic_write_text(
        runtime_state_path(workspace),
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
    )
    return merged


def update_runtime_flag(
    workspace: Path,
    action: str,
    *,
    reason: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    state = load_runtime_state(workspace)
    ts = timestamp or now_iso()
    canonical_action = (action or "").strip().replace("_", "-")
    if canonical_action == "mark-busy":
        state["busy"] = True
        state["busy_since"] = ts
        state["busy_reason"] = reason or "patrol_in_progress"
    elif canonical_action == "mark-idle":
        state["busy"] = False
        state["busy_since"] = None
        state["busy_reason"] = None
        state["last_patrol_at"] = ts
    elif canonical_action == "mark-discovered":
        state["last_discover_at"] = ts
    else:
        raise ValueError(f"未知运行态动作: {action}")
    saved = save_runtime_state(workspace, state)
    return {"ok": True, "action": canonical_action, "runtime_state": saved}
