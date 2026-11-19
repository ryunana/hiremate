"""求职助手（hiremate）云端 Pro 客户端。

仅封装轻量 HTTP 请求、OAuth 2.0 浏览器设备配对流（Device Pairing）与本地凭证管理（0600 权限），
不含任何闭源排雷词库、话术正则或离线放行逻辑。
当未登录、Pro 到期（HTTP 402）或云端不可达时，平顺降级为免费基础版提示，绝不崩溃或锁死本地数据。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
from urllib.parse import urlparse
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from . import __version__
from .ledger import JobLedger
from .normalize import deduplicate_company_list, employer_shield_match
from .workspace import (
    atomic_write_text,
    build_pro_activation_guide,
    get_effective_api_base,
    get_hiremate_home,
    inspect_onboarding_status,
    load_config,
    load_runtime_state,
    now_iso,
)

HTTP_TIMEOUT_SECONDS = 10.0

PRO_UPDATES_NOTE = "一次买断绑定个人账户长期有效，持续享受常规功能更新与精准匹配词库升级"

FREE_DOWNGRADE_MESSAGE = (
    "当前运行于「求职助手」开源基础版（未绑定设备或 Pro 权益未激活）。"
    "您仍可正常使用对话式建档、单轮预览记账、本地静态投递看板构建与 CSV 导出。"
)


def _write_private_file(path: Path, content: str) -> None:
    """以严格 0600 权限原子写入敏感凭证文件。"""
    atomic_write_text(path, content, mode=0o600)


def _default_device_name() -> str:
    try:
        raw_host = socket.gethostname().split(".", 1)[0]
    except Exception:
        raw_host = ""
    clean_host = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fa5]", "", raw_host)[:24]
    if clean_host:
        return f"hiremate-{sys.platform} ({clean_host})"
    return f"hiremate-{sys.platform}"


def validate_api_base(raw_base: str) -> str:
    """校验云端地址安全性：远程地址强制 https://，仅本机回环允许 http://。"""
    clean = (raw_base or "").strip().rstrip("/")
    parsed = urlparse(clean)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        raise ValueError(f"无效的云端服务地址：{raw_base!r}")
    if scheme == "https":
        return clean
    if scheme == "http" and host in ("127.0.0.1", "localhost", "::1"):
        return clean
    raise ValueError(
        f"不安全的云端服务地址 {clean!r}：为保护登录令牌，远程服务必须使用 https://（仅本机调试允许 http://localhost 或 127.0.0.1）。"
    )


def _resolve_authenticated_api_base(
    cfg: dict[str, Any],
    creds: dict[str, Any],
    api_base_override: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """解析带 Bearer Token 请求的目标地址：严禁通过 --api-base 或配置注入把已有登录令牌发往异源或明文 HTTP 服务器。"""
    recorded_raw = str(creds.get("api_base") or "").strip()
    try:
        if recorded_raw:
            bound_base = validate_api_base(recorded_raw)
            explicit_override = (api_base_override or os.environ.get("HIREMATE_API_BASE") or "").strip()
            if explicit_override:
                requested_base = validate_api_base(explicit_override)
                if requested_base != bound_base:
                    return (
                        None,
                        {
                            "ok": False,
                            "code": "API_BASE_MISMATCH",
                            "message": (
                                f"安全拦截：当前本机凭证绑定于 {bound_base}，拒绝向异源地址 {requested_base} 发送登录令牌。"
                                "如需切换服务端，请先运行 `hiremate.py auth logout` 后重新登录。"
                            ),
                        },
                    )
            return (bound_base, None)
        return (validate_api_base(get_effective_api_base(cfg, api_base_override)), None)
    except ValueError as exc:
        return (
            None,
            {
                "ok": False,
                "code": "INSECURE_API_BASE",
                "message": str(exc),
            },
        )


def device_id_path(home: Path) -> Path:
    return home / "device_id"


def credentials_path(home: Path) -> Path:
    return home / "credentials.json"


def pending_device_path(home: Path) -> Path:
    return home / "pending_device.json"


def get_or_create_device_id(home: Path | None = None) -> str:
    resolved_home = home or get_hiremate_home()
    path = device_id_path(resolved_home)
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    new_id = f"dev_{uuid.uuid4().hex}"
    _write_private_file(path, new_id + "\n")
    return new_id


def load_credentials(home: Path | None = None) -> dict[str, Any] | None:
    resolved_home = home or get_hiremate_home()
    path = credentials_path(resolved_home)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("token"):
            return data
    except (OSError, json.JSONDecodeError):
        return None
    return None


def save_credentials(creds: dict[str, Any], home: Path | None = None) -> Path:
    resolved_home = home or get_hiremate_home()
    path = credentials_path(resolved_home)
    payload = dict(creds)
    payload["saved_at"] = now_iso()
    _write_private_file(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path


def clear_credentials(home: Path | None = None) -> bool:
    resolved_home = home or get_hiremate_home()
    removed = False
    for p in (credentials_path(resolved_home), pending_device_path(resolved_home)):
        if p.exists():
            p.unlink()
            removed = True
    return removed


def _parse_iso_utc(val: str | None) -> datetime | None:
    if not val:
        return None
    s = val.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_pending_device(home: Path, expected_api_base: str) -> dict[str, Any] | None:
    path = pending_device_path(home)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("api_base") != expected_api_base:
        return None
    exp = _parse_iso_utc(str(data.get("expires_at") or ""))
    if not exp or exp <= datetime.now(timezone.utc):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    if not data.get("device_code") or not data.get("user_code"):
        return None
    return data


def save_pending_device(data: dict[str, Any], home: Path) -> Path:
    path = pending_device_path(home)
    _write_private_file(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def clear_pending_device(home: Path) -> None:
    path = pending_device_path(home)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def open_browser_url(url: str) -> bool:
    """在本地桌面环境自动拉起系统默认浏览器打开配对页面（测试或无头模式自动跳过）。

    注意：macOS 沙盒（如 Codex Seatbelt 沙盒）下 `/usr/bin/open` 会顺利 fork 启动但立即以非 0 退出码失败，
    因此必须使用 `subprocess.run` 校验真实退出码，严禁使用 `Popen` 盲目返回 `True` 误导 Agent。
    """
    if os.environ.get("HIREMATE_NO_BROWSER") == "1":
        return False
    if not (url.startswith("https://") or url.startswith("http://")):
        return False
    try:
        if sys.platform == "darwin":
            res = subprocess.run(
                ["open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            return res.returncode == 0
        if sys.platform.startswith("win"):
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def open_local_html_file(html_path: Path) -> bool:
    """在本地桌面环境自动拉起系统默认浏览器打开本地静态看板（dashboard.html）。"""
    if os.environ.get("HIREMATE_NO_BROWSER") == "1":
        return False
    try:
        resolved = html_path.expanduser().resolve()
        if not resolved.is_file() or resolved.suffix.lower() != ".html":
            return False
        if sys.platform == "darwin":
            res = subprocess.run(
                ["open", str(resolved)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            return res.returncode == 0
        if sys.platform.startswith("win"):
            os.startfile(str(resolved))  # type: ignore[attr-defined]
            return True
        return bool(webbrowser.open(resolved.as_uri(), new=2))
    except Exception:
        return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """防止重定向至明文 HTTP 或异源主机时泄露 Authorization Bearer Token。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        orig_p = urlparse(req.full_url)
        new_p = urlparse(new_req.full_url)
        if new_p.scheme == "http" and (new_p.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            raise urllib.error.HTTPError(req.full_url, code, "Blocked insecure HTTP redirect", headers, fp)
        if (orig_p.scheme, (orig_p.netloc or "").lower()) != (new_p.scheme, (new_p.netloc or "").lower()):
            for hdr_dict in (new_req.headers, new_req.unredirected_hdrs):
                for k in list(hdr_dict.keys()):
                    if k.lower() == "authorization":
                        del hdr_dict[k]
        return new_req


def _http_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": f"hiremate-open-cli/{__version__}",
    }
    raw_body: bytes | None = None
    if body is not None:
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    parsed_url = urlparse(url)
    if parsed_url.scheme not in ("https", "http"):
        return (0, {"ok": False, "code": "INVALID_URL_SCHEME", "message": f"不支持的请求协议：{parsed_url.scheme!r}"})
    if parsed_url.scheme == "http" and (parsed_url.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
        return (0, {"ok": False, "code": "INSECURE_HTTP_BLOCKED", "message": "拒绝向非本机明文 HTTP 地址发起云端请求。"})

    req = urllib.request.Request(url=url, data=raw_body, headers=headers, method=method)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = int(resp.status)
            resp_bytes = resp.read()
            try:
                parsed = json.loads(resp_bytes.decode("utf-8")) if resp_bytes else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return (
                    502,
                    {
                        "ok": False,
                        "code": "BAD_RESPONSE",
                        "message": "云端返回了非 JSON 页面（可能遇到网络认证门户或网关异常），已自动安全降级。",
                    },
                )
            return (status, parsed if isinstance(parsed, dict) else {"data": parsed})
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            err_bytes = exc.read()
            parsed = json.loads(err_bytes.decode("utf-8")) if err_bytes else {}
            if not isinstance(parsed, dict):
                parsed = {"message": str(parsed)}
        except Exception:
            parsed = {"ok": False, "code": f"HTTP_{status}", "message": f"云端服务暂时异常（HTTP {status}）"}
        return (status, parsed)
    except ssl.SSLError as exc:
        return (
            0,
            {
                "ok": False,
                "code": "SSL_CERT",
                "message": f"TLS/SSL 证书校验失败（请检查系统时间或代理证书配置）：{exc}",
            },
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return (
            0,
            {
                "ok": False,
                "code": "NETWORK_UNREACHABLE",
                "message": f"无法连接云端服务：{exc}",
            },
        )


def _parse_window_pairs(active_windows: list[str]) -> list[list[str]]:
    pairs: list[list[str]] = []
    for win in active_windows:
        if "-" in str(win):
            left, right = str(win).split("-", 1)
            pairs.append([left.strip(), right.strip()])
    return pairs or [["09:00", "12:00"], ["14:00", "18:00"]]


def config_to_pro_preferences(config: dict[str, Any]) -> dict[str, Any]:
    """将本地 config.json 映射为脱敏云端评估偏好（绝不上传完整简历文件）。"""
    resumes_payload: list[dict[str, Any]] = []
    for item in config.get("resume_routes") or []:
        if not isinstance(item, dict):
            continue
        resumes_payload.append(
            {
                "name": str(item.get("name") or ""),
                "match_keywords": list(item.get("keywords") or item.get("match_keywords") or []),
                "is_default": bool(item.get("is_default", False)),
            }
        )
    out_policy = str(
        config.get("outsourcing_policy")
        or ("block_all" if config.get("filter_outsourcing", True) else "allow_all")
    )
    onsite_policy = str(
        config.get("onsite_policy")
        or ("block_all" if config.get("filter_onsite", True) else "allow_all")
    )
    return {
        # 隐私保护硬边界：前东家与个人屏蔽名单已在客户端本机提前完成拦截，绝不随请求上传云端
        "blacklist_companies": [],
        "exclude_keywords": list(config.get("exclude_keywords") or []),
        "block_outsourcing": out_policy == "block_all",
        "outsourcing_policy": out_policy,
        "block_onsite": onsite_policy == "block_all",
        "onsite_policy": onsite_policy,
        "headhunter_policy": str(config.get("headhunter_policy") or "inbound_only"),
        "target_keywords": list(config.get("target_roles") or []),
        "preferred_cities": list(config.get("preferred_cities") or []),
        "allow_remote_bigtech": bool(config.get("remote_bigtech_flex", True)),
        "expected_salary_min_k": int(config.get("min_salary_k", 20)),
        "tolerate_lower_panel_salary": bool(config.get("salary_panel_tolerance", True)),
        "salary_match_mode": str(config.get("salary_match_mode") or ("ceiling_tolerant" if config.get("salary_panel_tolerance", True) else "ceiling_strict")),
        "inbound_resume_policy": str(config.get("inbound_resume_policy") or "generous"),
        "has_attachment_resume": bool(config.get("has_attachment_resume", True)),
        "resume_source": str(config.get("resume_source") or "pending"),
        "resumes": resumes_payload,
    }


def auth_device_login(
    *,
    workspace: Path,
    home: Path | None = None,
    api_base_override: str | None = None,
    wait_seconds: int = 45,
    open_browser: bool = True,
) -> dict[str, Any]:
    """浏览器免验证码一键设备配对登录（支持断点续轮询）。"""
    resolved_home = home or get_hiremate_home()
    cfg = load_config(workspace)
    api_base = validate_api_base(get_effective_api_base(cfg, api_base_override))
    dev_id = get_or_create_device_id(resolved_home)

    pending = load_pending_device(resolved_home, api_base)
    resumed = pending is not None
    if not pending:
        status, start_data = _http_json(
            "POST",
            f"{api_base}/v1/auth/device/start",
            body={
                "device_id": dev_id,
                "device_name": _default_device_name(),
                "platform": sys.platform,
            },
        )
        if status != 200 or not start_data.get("device_code"):
            start_data.setdefault("http_status", status)
            start_data["ok"] = False
            return start_data
        pending = {
            "api_base": api_base,
            "device_id": dev_id,
            "device_code": start_data["device_code"],
            "user_code": start_data["user_code"],
            "verification_uri": start_data.get("verification_uri") or f"{api_base}/activate",
            "verification_uri_complete": start_data.get("verification_uri_complete")
            or f"{api_base}/activate?code={start_data['user_code']}",
            "expires_at": start_data.get("expires_at"),
            "interval": int(start_data.get("interval") or 2),
        }
        save_pending_device(pending, resolved_home)

    verify_url = str(pending["verification_uri_complete"])
    user_code = str(pending["user_code"])
    browser_opened = False
    browser_attempted = False
    if open_browser and not resumed:
        browser_attempted = True
        browser_opened = open_browser_url(verify_url)

    # 先立即探查一次：
    # - 若 resumed=True 且用户刚刚已在浏览器点过确认授权，0 延迟直接拿回凭证，绝不重复弹多余标签页；
    # - 若首次探查仍处于 authorization_pending，且 open_browser=True 但本轮尚未弹窗（resumed=True），
    #   立即补弹确认页（修复首次因沙盒拦截/--no-wait/误关标签页后二次运行永远不弹窗的问题）。
    deadline = time.monotonic() + max(0, int(wait_seconds))
    poll_interval = max(0.4, min(float(pending.get("interval") or 2), 2.0))

    while True:
        p_status, p_data = _http_json(
            "POST",
            f"{api_base}/v1/auth/device/poll",
            body={"device_code": pending["device_code"]},
        )
        if p_status == 200 and p_data.get("token"):
            clear_pending_device(resolved_home)
            active = bool(p_data.get("active", p_data.get("tier") == "pro"))
            is_lifetime = bool(p_data.get("lifetime", p_data.get("days_left") is None and active))
            cred_file = save_credentials(
                {
                    "email": p_data.get("email"),
                    "token": p_data["token"],
                    "device_id": dev_id,
                    "api_base": api_base,
                    "tier": "pro" if active else "free",
                    "plan": p_data.get("plan", "free"),
                    "lifetime": is_lifetime,
                    "days_left": p_data.get("days_left"),
                    "expires_at": p_data.get("expires_at"),
                    "updates_included": bool(p_data.get("updates_included", active)),
                    "updates_policy": p_data.get("updates_policy") or PRO_UPDATES_NOTE,
                    "pro_guide_shown": bool(active),
                },
                home=resolved_home,
            )
            validity_desc = "买断长期有效" if is_lifetime else f"剩余 {p_data.get('days_left')} 天"
            ob = inspect_onboarding_status(workspace, cfg)
            base_msg = (
                f"设备绑定成功（{p_data.get('email')}）！Pro 专业版生效中（{validity_desc}）；{PRO_UPDATES_NOTE}。"
                if active
                else f"设备绑定成功（{p_data.get('email')}）！当前为免费基础版，可随时在官网账户中心特惠 ¥69（原价 ¥99）买断解锁 Pro。"
            )
            if not ob["onboarding_completed"]:
                base_msg += " 检测到本机尚未建立求职档案，请先向用户报喜展示已解锁的 Pro 权益，再继续引导拖入 PDF 简历完成 5 分钟建档与策略确认。"
            elif active and (cfg.get("mode_discover") == "semi" or cfg.get("mode_reply") == "semi"):
                base_msg += (
                    " 请务必告知用户已解锁的 5 项 Pro 高级权益，并明确提示：当前「主动找岗」与「消息答疑」仍处于半自动（semi），"
                    "现已支持一键切换为「全自动模式（auto）」并开启工作日自动巡检（展示 pro_activation_guide 菜单供用户选择）！"
                )
            auth_res: dict[str, Any] = {
                "ok": True,
                "status": "authorized",
                "email": p_data.get("email"),
                "tier": "pro" if active else "free",
                "plan": p_data.get("plan", "free"),
                "active": active,
                "lifetime": is_lifetime,
                "days_left": p_data.get("days_left"),
                "expires_at": p_data.get("expires_at"),
                "updates_included": bool(p_data.get("updates_included", active)),
                "updates_policy": p_data.get("updates_policy") or PRO_UPDATES_NOTE,
                "device_id": dev_id,
                "credentials_path": str(cred_file),
                "initialized": ob["initialized"],
                "onboarding_completed": ob["onboarding_completed"],
                "next_action_hint": ob["next_action_hint"],
                "message": base_msg,
            }
            if active:
                auth_res["pro_activation_guide"] = build_pro_activation_guide(
                    workspace,
                    cfg,
                    email=str(p_data.get("email") or ""),
                    validity_desc=validity_desc,
                )
            return auth_res

        err_code = str(p_data.get("code") or "")
        if err_code in ("EXPIRED_TOKEN", "TOKEN_ALREADY_CONSUMED", "INVALID_DEVICE_CODE"):
            clear_pending_device(resolved_home)
            return {
                "ok": False,
                "status": "expired",
                "code": err_code,
                "message": p_data.get("message") or "设备配对码已过期，请重新运行 auth login。",
            }

        if open_browser and not browser_attempted:
            browser_attempted = True
            browser_opened = open_browser_url(verify_url)
            deadline = time.monotonic() + max(0, int(wait_seconds))

        now_mono = time.monotonic()
        # 若浏览器未能弹出（例如沙盒拦截了系统 open 或传了 --no-browser），切勿盲等 45 秒浪费回合时间，
        # 立即返回配对链接让 Agent 用有头浏览器主动打开并直接贴给用户。
        if not browser_opened and ((not resumed) or open_browser):
            deadline = now_mono

        if now_mono >= deadline:
            pending_payload: dict[str, Any] = {
                "ok": True,
                "status": "authorization_pending",
                "user_code": user_code,
                "verification_uri": pending["verification_uri"],
                "verification_uri_complete": verify_url,
                "expires_at": pending.get("expires_at"),
                "browser_opened": browser_opened,
                "resumed": resumed,
            }
            if not browser_opened:
                pending_payload["agent_action_required"] = (
                    f"系统默认浏览器未能自动弹出确认页（可能受当前宿主沙盒限制，用户此刻尚未看到页面）！"
                    f"请立即调用可见有头浏览器（如 ego lite / 宿主内置浏览器）主动打开 {verify_url} "
                    f"（或申请宿主机非沙盒权限执行系统打开命令），并在普通回复正文中直接贴出完整链接 {verify_url} 与配对码 {user_code}；"
                    f"严禁调用 request_user_input / 问题框等阻塞式弹窗等待确认（会遮挡正文链接导致用户找不到授权页）。"
                )
                pending_payload["message"] = (
                    f"系统浏览器未能自动拉起，请立即为用户打开 {verify_url} （核对配对码 {user_code}）并点击「确认授权此设备」；"
                    "同时务必在普通回复正文中完整展示该链接（严禁先弹无链接的问题输入框阻塞回合）。点击确认后再次运行 `hiremate.py auth login` 即可秒级完成绑定。"
                )
            else:
                pending_payload["message"] = (
                    f"已在系统浏览器弹出设备授权页：{verify_url} （配对码 {user_code}），请点击「确认授权此设备」。"
                    "无需输入验证码；回复用户时请务必在普通正文中附上完整备用链接与配对码（严禁调用无链接的 request_user_input / 问题框阻塞遮挡正文），"
                    "用户确认后再次运行 `hiremate.py auth login` 即可秒级完成绑定。"
                )
            return pending_payload

        sleep_dur = min(poll_interval, max(0.05, deadline - now_mono))
        time.sleep(sleep_dur)


def auth_open_upgrade(
    *,
    plan: str = "pro_buyout",
    workspace: Path,
    home: Path | None = None,
    api_base_override: str | None = None,
    open_browser: bool = True,
) -> dict[str, Any]:
    """在浏览器打开站内 Pro 买断开通中心（在线扫码支付后秒级直充当前注册账户）。"""
    resolved_home = home or get_hiremate_home()
    creds = load_credentials(resolved_home)
    cfg = load_config(workspace)
    api_base = validate_api_base(get_effective_api_base(cfg, api_base_override or (creds.get("api_base") if creds else None)))
    clean_plan = "pro_buyout"
    target_url = f"{api_base}/activate?plan={clean_plan}#subscribe-box"
    opened = open_browser_url(target_url) if open_browser else False
    suffix = (
        "；稍后运行 `auth status` 即可同步激活。"
        if creds
        else "；支付完成后请运行 `auth login`（DSH 内运行 `/hiremate-login`）将本机绑定至该账户。"
    )
    res: dict[str, Any] = {
        "ok": True,
        "plan": clean_plan,
        "upgrade_url": target_url,
        "browser_opened": opened,
        "email": creds.get("email") if creds else None,
        "updates_policy": PRO_UPDATES_NOTE,
        "message": (
            (
                f"已打开站内 Pro 买断开通页：{target_url} 。"
                if opened
                else f"系统浏览器未自动弹出，请立即在可见有头浏览器打开或在回复正文中直接附上 Pro 买断开通页：{target_url} 。"
            )
            + "完成微信/支付宝扫码支付 ¥69 后，买断权益将永久绑定至你的账户，无需输入任何激活码"
            + suffix
        ),
    }
    if not opened:
        res["agent_action_required"] = (
            f"系统浏览器未能自动拉起（可能受宿主沙盒限制），请立即通过可见有头浏览器打开 {target_url} ，"
            f"并在普通回复正文中完整展示该链接（切勿调用无链接的阻塞式提问框）。"
        )
    return res


def auth_send_code(
    *,
    email: str,
    workspace: Path,
    home: Path | None = None,
    api_base_override: str | None = None,
) -> dict[str, Any]:
    cfg = load_config(workspace)
    api_base = validate_api_base(get_effective_api_base(cfg, api_base_override))
    status, data = _http_json(
        "POST",
        f"{api_base}/v1/auth/send-code",
        body={"email": email.strip()},
    )
    data.setdefault("http_status", status)
    data.setdefault("api_base", api_base)
    if status == 200 and data.get("ok", True):
        data["ok"] = True
        data.setdefault("message", f"6 位数字验证码已发送至 {email.strip()}，请查收邮件。")
    else:
        data["ok"] = False
    return data


def auth_verify_code(
    *,
    email: str,
    code: str,
    workspace: Path,
    home: Path | None = None,
    api_base_override: str | None = None,
) -> dict[str, Any]:
    resolved_home = home or get_hiremate_home()
    cfg = load_config(workspace)
    api_base = validate_api_base(get_effective_api_base(cfg, api_base_override))
    dev_id = get_or_create_device_id(resolved_home)

    status, data = _http_json(
        "POST",
        f"{api_base}/v1/auth/verify-code",
        body={
            "email": email.strip(),
            "code": code.strip(),
            "device_id": dev_id,
            "device_name": _default_device_name(),
            "replace_oldest": True,
        },
    )
    data.setdefault("http_status", status)
    if status == 200 and data.get("token"):
        active = bool(data.get("active", data.get("tier") == "pro"))
        cred_file = save_credentials(
            {
                "email": data.get("email") or email.strip().lower(),
                "token": data["token"],
                "device_id": dev_id,
                "api_base": api_base,
                "tier": "pro" if active else "free",
                "days_left": data.get("days_left", 0),
                "expires_at": data.get("expires_at"),
                "updates_included": bool(data.get("updates_included", active)),
                "updates_policy": data.get("updates_policy") or PRO_UPDATES_NOTE,
            },
            home=resolved_home,
        )
        vc_email = str(data.get("email") or email.strip().lower())
        is_lifetime_vc = bool(data.get("lifetime", data.get("days_left") is None and active))
        validity_vc = "买断长期有效" if is_lifetime_vc else f"剩余 {data.get('days_left')} 天"
        vc_res: dict[str, Any] = {
            "ok": True,
            "email": vc_email,
            "tier": "pro" if active else "free",
            "active": active,
            "lifetime": is_lifetime_vc,
            "expires_at": data.get("expires_at"),
            "days_left": data.get("days_left"),
            "updates_included": bool(data.get("updates_included", active)),
            "updates_policy": data.get("updates_policy") or PRO_UPDATES_NOTE,
            "device_id": dev_id,
            "credentials_path": str(cred_file),
            "message": "设备绑定成功，本地凭证已按 0600 权限安全保存。",
        }
        if active:
            vc_res["pro_activation_guide"] = build_pro_activation_guide(
                workspace,
                cfg,
                email=vc_email,
                validity_desc=validity_vc,
            )
        return vc_res

    data["ok"] = False
    return data


def auth_status(
    *,
    workspace: Path,
    home: Path | None = None,
    api_base_override: str | None = None,
) -> dict[str, Any]:
    resolved_home = home or get_hiremate_home()
    cfg = load_config(workspace)
    ob = inspect_onboarding_status(workspace, cfg)
    creds = load_credentials(resolved_home)
    if not creds:
        return {
            "ok": True,
            "authenticated": False,
            "tier": "free",
            "active": False,
            "updates_included": False,
            "updates_policy": PRO_UPDATES_NOTE,
            "initialized": ob["initialized"],
            "onboarding_completed": ob["onboarding_completed"],
            "next_action_hint": ob["next_action_hint"],
            "message": "当前未绑定 Pro 账号，运行于开源基础版（免登录即可直接建档与单轮筛岗）。运行 `hiremate.py auth login` 可打开浏览器免密绑定设备。",
        }

    api_base, mismatch_err = _resolve_authenticated_api_base(cfg, creds, api_base_override)
    if mismatch_err is not None or api_base is None:
        return {
            "ok": False,
            "authenticated": False,
            "tier": "free",
            "active": False,
            "reason_code": "API_BASE_MISMATCH",
            "message": mismatch_err["message"] if mismatch_err else "云端地址不匹配",
        }
    status, data = _http_json(
        "GET",
        f"{api_base}/v1/auth/status",
        token=str(creds["token"]),
    )
    if status == 200 and data.get("ok"):
        active = bool(data.get("active", data.get("tier") == "pro"))
        is_lifetime = bool(data.get("lifetime", data.get("days_left") is None and active))
        was_pro_guide_shown = bool(creds.get("pro_guide_shown", False))
        just_upgraded = (creds.get("tier") != "pro") and active
        show_activation_guide = active and (just_upgraded or not was_pro_guide_shown)
        refreshed = dict(creds)
        refreshed.update(
            {
                "email": data.get("email") or creds.get("email"),
                "tier": "pro" if active else "free",
                "plan": data.get("plan", "free"),
                "lifetime": is_lifetime,
                "days_left": data.get("days_left"),
                "expires_at": data.get("expires_at"),
                "updates_included": bool(data.get("updates_included", active)),
                "updates_policy": data.get("updates_policy") or PRO_UPDATES_NOTE,
                "pro_guide_shown": True if active else False,
            }
        )
        save_credentials(refreshed, home=resolved_home)
        validity_desc = "买断长期有效" if is_lifetime else f"剩余 {refreshed['days_left']} 天"
        status_msg = (
            f"Pro 专业版生效中（{validity_desc}）· {PRO_UPDATES_NOTE}。"
            if active
            else (str(data.get("message") or "").strip() or FREE_DOWNGRADE_MESSAGE)
        )
        if show_activation_guide and ob["onboarding_completed"] and (
            cfg.get("mode_discover") == "semi" or cfg.get("mode_reply") == "semi"
        ):
            status_msg += " 提示：当前主动找岗/消息答疑仍处于半自动（semi），请向用户介绍 Pro 5 项高级功能并询问是否切换为全自动模式（auto）与开启工作日自动巡检。"
        st_res: dict[str, Any] = {
            "ok": True,
            "authenticated": True,
            "email": refreshed["email"],
            "tier": refreshed["tier"],
            "plan": refreshed["plan"],
            "active": active,
            "lifetime": is_lifetime,
            "expires_at": refreshed["expires_at"],
            "days_left": refreshed["days_left"],
            "updates_included": refreshed["updates_included"],
            "updates_policy": refreshed["updates_policy"],
            "device_id": creds.get("device_id"),
            "initialized": ob["initialized"],
            "onboarding_completed": ob["onboarding_completed"],
            "next_action_hint": ob["next_action_hint"],
            "message": status_msg,
        }
        if show_activation_guide:
            st_res["pro_activation_guide"] = build_pro_activation_guide(
                workspace,
                cfg,
                email=str(refreshed.get("email") or ""),
                validity_desc=validity_desc,
            )
        return st_res

    # 离线或云端暂不可达时，若本机缓存的已验证凭证为 Pro，给出明确的离线缓存态而非误导用户权益丢失
    if status in (0, 502, 503, 504) and creds.get("tier") == "pro":
        return {
            "ok": True,
            "authenticated": True,
            "offline_cached_pro": True,
            "email": creds.get("email"),
            "tier": "pro",
            "plan": creds.get("plan", "pro_buyout"),
            "active": True,
            "lifetime": bool(creds.get("lifetime", True)),
            "expires_at": creds.get("expires_at"),
            "days_left": creds.get("days_left"),
            "updates_included": True,
            "updates_policy": creds.get("updates_policy") or PRO_UPDATES_NOTE,
            "device_id": creds.get("device_id"),
            "initialized": ob["initialized"],
            "onboarding_completed": ob["onboarding_completed"],
            "next_action_hint": ob["next_action_hint"],
            "http_status": status,
            "reason_code": data.get("code", "OFFLINE_CACHED_PRO"),
            "message": "云端暂时无法连接，本轮自动巡检暂停，本地免费功能与看板照常可用；你的已付费 Pro 权益不受影响，网络恢复后自动继续。",
        }

    return {
        "ok": True,
        "authenticated": False,
        "tier": "free",
        "active": False,
        "updates_included": False,
        "updates_policy": PRO_UPDATES_NOTE,
        "initialized": ob["initialized"],
        "onboarding_completed": ob["onboarding_completed"],
        "next_action_hint": ob["next_action_hint"],
        "http_status": status,
        "reason_code": data.get("code", "FALLBACK_FREE"),
        "message": data.get("message") or FREE_DOWNGRADE_MESSAGE,
    }


def auth_devices(
    *,
    workspace: Path,
    home: Path | None = None,
    api_base_override: str | None = None,
    revoke_device_id: str | None = None,
) -> dict[str, Any]:
    """查询当前账户已绑定的设备列表，或按 device_id 解绑指定旧设备。"""
    resolved_home = home or get_hiremate_home()
    creds = load_credentials(resolved_home)
    if not creds:
        return {
            "ok": False,
            "code": "NO_LOCAL_CREDENTIALS",
            "message": "本机尚未登录，请先运行 `hiremate.py auth login` 完成浏览器绑定。",
        }
    cfg = load_config(workspace)
    api_base, mismatch_err = _resolve_authenticated_api_base(cfg, creds, api_base_override)
    if mismatch_err is not None or api_base is None:
        return mismatch_err or {"ok": False, "code": "API_BASE_MISMATCH"}

    if revoke_device_id and revoke_device_id.strip():
        target_id = revoke_device_id.strip()
        status, data = _http_json(
            "POST",
            f"{api_base}/v1/auth/devices/revoke",
            body={"device_id": target_id},
            token=str(creds["token"]),
        )
        data.setdefault("http_status", status)
        if status == 200 and data.get("ok") and target_id == creds.get("device_id"):
            clear_credentials(resolved_home)
            data["local_logged_out"] = True
        return data

    status, data = _http_json(
        "GET",
        f"{api_base}/v1/auth/devices",
        token=str(creds["token"]),
    )
    data.setdefault("http_status", status)
    if status == 200 and data.get("ok"):
        data["current_device_id"] = creds.get("device_id")
    return data


def auth_logout(*, home: Path | None = None) -> dict[str, Any]:
    removed = clear_credentials(home)
    return {
        "ok": True,
        "removed": removed,
        "tier": "free",
        "message": "已退出登录，本地求职账本与静态看板数据完整保留。",
    }


def _graceful_pro_fallback(action: str, status: int, data: dict[str, Any]) -> dict[str, Any]:
    """Pro 云端调用未登录、到期或网络异常时统一 Fail-Closed：
    强制返回 handoff_required=True 与 allow_auto_send=False，防止全自动模式下跳过守卫盲发。
    """
    res: dict[str, Any] = {
        "ok": False,
        "downgraded": True,
        "tier": "free",
        "action": action,
        "http_status": status,
        "code": data.get("code") or ("UNAUTHENTICATED" if status == 0 else f"HTTP_{status}"),
        "message": data.get("message") or FREE_DOWNGRADE_MESSAGE,
        "handoff_required": True,
        "allow_auto_send": False,
        "updates_policy": PRO_UPDATES_NOTE,
        "free_capabilities": [
            "对话式求职事实建档与前东家同字号自动屏蔽",
            "本地 SQLite 四态求职账本与三维查重",
            "单轮岗位精准匹配与外包过滤预览",
            "本地高密度只读静态投递看板与 Excel(.xlsx)/CSV 导出",
        ],
    }
    if action == "patrol_gate":
        res["allow_unread_check"] = False
        res["discover_due"] = False
    elif action == "guard_reply":
        res["reply_approved"] = False
    elif action == "evaluate_job":
        res["decision"] = "manual_review"
    return res


def pro_patrol_gate(
    *,
    workspace: Path,
    home: Path | None = None,
    api_base_override: str | None = None,
    now_override: str | None = None,
) -> dict[str, Any]:
    cfg = load_config(workspace)
    if str(cfg.get("execution_mode") or "browser") == "copilot":
        return {
            "ok": True,
            "in_active_window": False,
            "allow_unread_check": False,
            "discover_due": False,
            "reason": "COPILOT_MODE_NO_BROWSER",
            "message": "当前处于副驾驶粘贴模式（无浏览器直连操控权限），已自动跳过后台定时巡检。",
        }

    resolved_home = home or get_hiremate_home()
    creds = load_credentials(resolved_home)
    if not creds:
        return _graceful_pro_fallback("patrol_gate", 401, {"code": "NO_LOCAL_CREDENTIALS"})
    rt = load_runtime_state(workspace)
    ledger = JobLedger(workspace)
    summary = ledger.summary()
    sched = cfg.get("schedule") or {}

    payload = {
        "now_iso": now_override or now_iso(),
        "schedule": {
            "workdays_only": bool(sched.get("weekdays_only", True)),
            "windows": _parse_window_pairs(sched.get("active_windows") or []),
            "max_deliver_per_round": int(sched.get("max_per_round", 5)),
            "max_deliver_per_day": int(sched.get("max_per_day", 20)),
            "discover_interval_minutes": int(sched.get("min_interval_minutes", 90)),
        },
        "runtime_state": {
            "delivered_today": int(summary.get("today_opening_sent", 0)),
            "main_busy": bool(rt.get("busy", False)),
            "main_busy_since": rt.get("busy_since"),
            "last_discover_at": rt.get("last_discover_at"),
        },
        "preferences": config_to_pro_preferences(cfg),
    }

    api_base, mismatch_err = _resolve_authenticated_api_base(cfg, creds, api_base_override)
    if mismatch_err is not None or api_base is None:
        return _graceful_pro_fallback("patrol_gate", 401, mismatch_err or {"code": "API_BASE_MISMATCH"})
    status, data = _http_json(
        "POST",
        f"{api_base}/v1/pro/patrol-gate",
        body=payload,
        token=str(creds["token"]),
    )
    if status == 200 and data.get("ok"):
        return data
    return _graceful_pro_fallback("patrol_gate", status, data)


def pro_evaluate_job(
    *,
    job_data: dict[str, Any],
    workspace: Path,
    home: Path | None = None,
    api_base_override: str | None = None,
) -> dict[str, Any]:
    resolved_home = home or get_hiremate_home()
    cfg = load_config(workspace)
    company_raw = str(job_data.get("company") or "").strip()
    if company_raw:
        all_shielded = deduplicate_company_list(
            list(cfg.get("blocked_companies") or []) + list(cfg.get("past_employers") or [])
        )
        for blk in all_shielded:
            if employer_shield_match(company_raw, blk):
                is_former = any(employer_shield_match(company_raw, emp) for emp in (cfg.get("past_employers") or []))
                return {
                    "ok": True,
                    "tier": "pro",
                    "decision": "skip",
                    "pass": False,
                    "blocked_employer": is_former,
                    "blocked_company_hit": blk,
                    "allow_auto_send": False,
                    "handoff_required": False,
                    "reasons": [f"本地{'前东家/现东家' if is_former else '屏蔽企业'}保护拦截（匹配：{blk}）"],
                }

    creds = load_credentials(resolved_home)
    if not creds:
        return _graceful_pro_fallback("evaluate_job", 401, {"code": "NO_LOCAL_CREDENTIALS"})

    normalized_job = dict(job_data)
    if "salary_text" not in normalized_job and "salary" in normalized_job:
        normalized_job["salary_text"] = normalized_job["salary"]
    if "jd_text" not in normalized_job and "jd" in normalized_job:
        normalized_job["jd_text"] = normalized_job["jd"]
    if "is_inbound_recruiter" not in normalized_job:
        ch = str(normalized_job.get("channel") or normalized_job.get("source") or "").strip().lower()
        if ch == "inbound":
            normalized_job["is_inbound_recruiter"] = True

    body = {
        "action": "evaluate_job",
        "job": normalized_job,
        "preferences": config_to_pro_preferences(cfg),
    }
    api_base, mismatch_err = _resolve_authenticated_api_base(cfg, creds, api_base_override)
    if mismatch_err is not None or api_base is None:
        return _graceful_pro_fallback("evaluate_job", 401, mismatch_err or {"code": "API_BASE_MISMATCH"})
    status, data = _http_json(
        "POST",
        f"{api_base}/v1/pro/evaluate-and-draft",
        body=body,
        token=str(creds["token"]),
    )
    if status == 200 and data.get("ok"):
        data.setdefault("tier", "pro")
        return data
    return _graceful_pro_fallback("evaluate_job", status, data)


def pro_guard_reply(
    *,
    input_data: dict[str, Any],
    workspace: Path,
    home: Path | None = None,
    api_base_override: str | None = None,
) -> dict[str, Any]:
    cfg = load_config(workspace)
    job_part = dict(input_data["job"]) if isinstance(input_data.get("job"), dict) else {}
    if isinstance(input_data.get("conversation"), dict):
        conv_part = dict(input_data["conversation"])
    else:
        conv_part = {
            "incoming_message": input_data.get("incoming_message", ""),
            "draft_reply": input_data.get("draft_reply", ""),
        }
    if not job_part:
        job_part = {
            "title": input_data.get("title", ""),
            "company": input_data.get("company", ""),
            "jd_text": input_data.get("jd_text", input_data.get("jd", "")),
        }
    elif "jd_text" not in job_part and "jd" in job_part:
        job_part["jd_text"] = job_part["jd"]
    if "is_inbound_recruiter" not in job_part:
        ch = str(job_part.get("channel") or job_part.get("source") or "").strip().lower()
        if ch == "inbound":
            job_part["is_inbound_recruiter"] = True

    comp_guard = str(job_part.get("company") or "").strip()
    if comp_guard:
        all_shielded = deduplicate_company_list(
            list(cfg.get("blocked_companies") or []) + list(cfg.get("past_employers") or [])
        )
        for blk in all_shielded:
            if employer_shield_match(comp_guard, blk):
                return {
                    "ok": True,
                    "code": "LOCAL_EMPLOYER_SHIELD_BLOCKED",
                    "tier": "pro",
                    "handoff_required": True,
                    "handoff_reasons": [f"本地前东家/屏蔽企业保护拦截（匹配：{blk}），禁止自动回复"],
                    "reply_approved": False,
                    "allow_auto_send": False,
                    "violations": ["命中本地屏蔽企业保护"],
                    "sanitized_suggestion": "",
                    "recommended_resume": None,
                    "is_rejection_closing": False,
                }

    resolved_home = home or get_hiremate_home()
    creds = load_credentials(resolved_home)
    if not creds:
        return _graceful_pro_fallback("guard_reply", 401, {"code": "NO_LOCAL_CREDENTIALS"})

    body = {
        "action": "guard_reply",
        "job": job_part,
        "preferences": config_to_pro_preferences(cfg),
        "conversation": conv_part,
    }
    api_base, mismatch_err = _resolve_authenticated_api_base(cfg, creds, api_base_override)
    if mismatch_err is not None or api_base is None:
        return _graceful_pro_fallback("guard_reply", 401, mismatch_err or {"code": "API_BASE_MISMATCH"})
    status, data = _http_json(
        "POST",
        f"{api_base}/v1/pro/evaluate-and-draft",
        body=body,
        token=str(creds["token"]),
    )
    if status == 200 and data.get("ok"):
        data.setdefault("tier", "pro")
        return data
    return _graceful_pro_fallback("guard_reply", status, data)


def pro_report_outsourcing(
    *,
    company: str,
    evidence: str,
    job_title: str = "",
    job_url: str = "",
    workspace: Path,
    home: Path | None = None,
    api_base_override: str | None = None,
) -> dict[str, Any]:
    """向云端候选池上报带客观页面铁证的新发现外包/派遣企业（绝不上传个人黑名单或前东家）。"""
    resolved_home = home or get_hiremate_home()
    creds = load_credentials(resolved_home)
    if not creds:
        return _graceful_pro_fallback("report_outsourcing", 401, {"code": "NO_LOCAL_CREDENTIALS"})

    cfg = load_config(workspace)
    body = {
        "company": (company or "").strip(),
        "evidence": (evidence or "").strip(),
        "job_title": (job_title or "").strip(),
        "job_url": (job_url or "").strip(),
    }
    api_base, mismatch_err = _resolve_authenticated_api_base(cfg, creds, api_base_override)
    if mismatch_err is not None or api_base is None:
        return _graceful_pro_fallback("report_outsourcing", 401, mismatch_err or {"code": "API_BASE_MISMATCH"})
    status, data = _http_json(
        "POST",
        f"{api_base}/v1/pro/outsourcing/report",
        body=body,
        token=str(creds["token"]),
    )
    if status == 200 and data.get("ok"):
        data.setdefault("tier", "pro")
        return data
    return _graceful_pro_fallback("report_outsourcing", status, data)
