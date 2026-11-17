"""求职助手（hiremate）双源版本感知与一键无损自更新引擎（Gitee 主源 + GitHub 自动备源）。

设计原则：
1. 零隐私外传：仅向公开开源仓库发起纯静态 GET 读取 `release.json`（或浅克隆公开 Git 仓），绝不携带任何本地配置、简历或设备凭证。
2. 国内优先与自动容灾：默认优先连接 Gitee 主源，超时或异常时毫秒级自动切换 GitHub 备源；无 `git` 环境自动回退安全 Tarball 更新。
3. 硬墙钟零阻塞被动感知：日常 CLI 命令每 6 小时最多静默探测一次（单源 1.5s、总墙钟硬封顶 2.0s，断网静默退避 1 小时），绝不拖慢主命令响应。
4. 跨平台原子备份与自动回滚：自更新仅替换 `~/.hiremate/skill/hiremate` 与 `~/.hiremate/dsh-plugin` 运行时副本，兼容 Windows Junction 与只读 Git 构件，失败自动回滚，100% 保留用户本地四态账本、简历档案与登录凭证。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
from urllib.parse import urlparse
import urllib.request
from typing import Any

from . import __version__
from .workspace import atomic_write_text, get_hiremate_home, now_iso, resolve_workspace

GITEE_MANIFEST_URL = "https://gitee.com/ryunana/hiremate/raw/main/release.json"
GITHUB_MANIFEST_URL = "https://raw.githubusercontent.com/ryunana/hiremate/main/release.json"

GITEE_GIT_URL = "https://gitee.com/ryunana/hiremate.git"
GITHUB_GIT_URL = "https://github.com/ryunana/hiremate.git"
GITHUB_TARBALL_URL = "https://codeload.github.com/ryunana/hiremate/tar.gz/refs/heads/main"

TRUSTED_RELEASE_HOSTS = frozenset(
    {
        "gitee.com",
        "github.com",
        "codeload.github.com",
        "raw.githubusercontent.com",
    }
)

PASSIVE_CHECK_TTL_SECONDS = 6 * 3600
FAILED_CHECK_RETRY_SECONDS = 3600
PASSIVE_HTTP_TIMEOUT_SECONDS = 1.5
PASSIVE_HARD_WALL_TIMEOUT_SECONDS = 2.0
ACTIVE_HTTP_TIMEOUT_SECONDS = 8.0
GIT_CLONE_TIMEOUT_SECONDS = 45.0
MAX_MANIFEST_BYTES = 64 * 1024
MAX_TARBALL_BYTES = 20 * 1024 * 1024
MAX_TARBALL_ENTRIES = 2000
MAX_TARBALL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024

_SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$"
)
_INIT_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')


class _SafeUpdateRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拦截 HTTPS -> HTTP 明文降级重定向，防止中间人劫持源码包。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        orig_scheme = (urlparse(req.full_url).scheme or "").lower()
        next_parsed = urlparse(newurl)
        next_scheme = (next_parsed.scheme or "").lower()
        next_host = (next_parsed.hostname or "").lower()
        if orig_scheme == "https" and next_scheme != "https":
            raise urllib.error.URLError(f"拒绝不安全的协议降级重定向：{newurl!r}")
        if next_scheme == "http" and next_host not in ("127.0.0.1", "localhost", "::1"):
            raise urllib.error.URLError(f"拒绝重定向至非本机明文 HTTP 地址：{newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def update_state_path(home: Path) -> Path:
    return home / "update_state.json"


def parse_semver(raw: str) -> tuple[int, int, int, int, tuple[Any, ...]]:
    """解析语义化版本号为可安全比较的元组；正式版高于同主次修订号的预发布版。"""
    cleaned = (raw or "").strip()
    m = _SEMVER_RE.fullmatch(cleaned)
    if not m:
        raise ValueError(f"非法的语义化版本号：{raw!r}")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    prerelease = m.group(4)
    if not prerelease:
        return (major, minor, patch, 1, ())
    parts: list[Any] = []
    for token in prerelease.split("."):
        if token.isdigit():
            parts.append((0, int(token)))
        else:
            parts.append((1, token))
    return (major, minor, patch, 0, tuple(parts))


def normalize_version(raw: str) -> str:
    cleaned = (raw or "").strip()
    parse_semver(cleaned)
    return cleaned[1:] if cleaned.lower().startswith("v") else cleaned


def compare_versions(ver_a: str, ver_b: str) -> int:
    ta = parse_semver(ver_a)
    tb = parse_semver(ver_b)
    if ta > tb:
        return 1
    if ta < tb:
        return -1
    return 0


def is_newer_version(candidate: str, baseline: str) -> bool:
    try:
        return compare_versions(candidate, baseline) > 0
    except ValueError:
        return False


def _validate_source_url(
    raw_url: str,
    *,
    allow_test_local: bool = False,
    restrict_trusted_hosts: bool = False,
) -> str:
    """校验更新源地址安全性：生产强制 https://，杜绝 `-` 开头参数注入；仅环境变量沙盒覆盖允许本机回环与 file://。"""
    clean = (raw_url or "").strip()
    if not clean or clean.startswith("-"):
        raise ValueError(f"拒绝不安全的源地址：{raw_url!r}")

    if allow_test_local and "://" not in clean:
        local_p = Path(clean).expanduser().resolve()
        if local_p.exists():
            return str(local_p)
        raise ValueError(f"本地更新源路径不存在：{clean!r}")

    parsed = urlparse(clean)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https" and host:
        if restrict_trusted_hosts and host not in TRUSTED_RELEASE_HOSTS:
            raise ValueError(f"拒绝非官方可信代码托管域名的下载地址：{host!r}")
        return clean

    if allow_test_local:
        if scheme == "http" and host in ("127.0.0.1", "localhost", "::1"):
            return clean
        if scheme == "file":
            return clean

    raise ValueError(
        f"不安全的更新源地址 {clean!r}：远程地址必须使用可信 https:// 源。"
    )


def _split_override_urls(env_val: str) -> list[str]:
    raw_items = re.split(r"[,|\s]+", env_val.strip())
    return [item.strip() for item in raw_items if item.strip()]


def _resolve_manifest_sources(prefer_source: str = "auto") -> list[tuple[str, str, bool]]:
    env_override = (os.environ.get("HIREMATE_RELEASE_MANIFEST_URLS") or "").strip()
    if env_override:
        urls = [_validate_source_url(u, allow_test_local=True) for u in _split_override_urls(env_override)]
        return [(f"custom_{idx + 1}", u, True) for idx, u in enumerate(urls)]

    gitee_pair = ("gitee", GITEE_MANIFEST_URL, False)
    github_pair = ("github", GITHUB_MANIFEST_URL, False)
    if prefer_source == "github":
        return [github_pair, gitee_pair]
    return [gitee_pair, github_pair]


def _resolve_git_sources(prefer_source: str = "auto") -> list[tuple[str, str]]:
    env_override = (os.environ.get("HIREMATE_UPDATE_GIT_URLS") or "").strip()
    if env_override:
        urls = [_validate_source_url(u, allow_test_local=True) for u in _split_override_urls(env_override)]
        return [(f"custom_{idx + 1}", u) for idx, u in enumerate(urls)]

    gitee_pair = ("gitee", GITEE_GIT_URL)
    github_pair = ("github", GITHUB_GIT_URL)
    if prefer_source == "github":
        return [github_pair, gitee_pair]
    return [gitee_pair, github_pair]


def _resolve_tarball_url(manifest: dict[str, Any] | None = None) -> tuple[str, bool]:
    env_tar = (os.environ.get("HIREMATE_UPDATE_TARBALL_URL") or "").strip()
    if env_tar:
        return _validate_source_url(env_tar, allow_test_local=True), True
    if isinstance(manifest, dict):
        sources = manifest.get("sources")
        if isinstance(sources, dict):
            gh = sources.get("github")
            if isinstance(gh, dict) and gh.get("tarball_url"):
                validated = _validate_source_url(
                    str(gh["tarball_url"]),
                    allow_test_local=False,
                    restrict_trusted_hosts=True,
                )
                return validated, False
    return GITHUB_TARBALL_URL, False


def _fetch_bytes(url_or_path: str, *, timeout: float, max_bytes: int, allow_test_local: bool = False) -> bytes:
    validated = _validate_source_url(url_or_path, allow_test_local=allow_test_local)
    if "://" not in validated:
        data = Path(validated).read_bytes()
        if len(data) > max_bytes:
            raise ValueError(f"文件体积超出安全上限 ({len(data)} > {max_bytes})")
        return data
    if validated.startswith("file://"):
        file_path = Path(urlparse(validated).path)
        data = file_path.read_bytes()
        if len(data) > max_bytes:
            raise ValueError(f"文件体积超出安全上限 ({len(data)} > {max_bytes})")
        return data

    req = urllib.request.Request(
        url=validated,
        headers={
            "Accept": "application/json, application/octet-stream, */*",
            "User-Agent": f"hiremate-updater/{__version__}",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_SafeUpdateRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"远端响应体积超出安全上限 ({max_bytes} bytes)")
        return data


def _find_usable_git_binary() -> str | None:
    """探测本机真正可用的 git 可执行程序；规避 macOS 未安装 Xcode CLT 时运行 /usr/bin/git 桩弹出系统安装框。"""
    git_bin = shutil.which("git")
    if not git_bin:
        return None
    if sys.platform == "darwin":
        try:
            resolved_git = str(Path(git_bin).resolve())
        except OSError:
            resolved_git = git_bin
        if git_bin == "/usr/bin/git" or resolved_git == "/usr/bin/git":
            xc = subprocess.run(
                ["/usr/bin/xcode-select", "-p"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if xc.returncode != 0:
                return None
    return git_bin


def _parse_and_validate_manifest(raw_bytes: bytes) -> dict[str, Any]:
    obj = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("release.json 顶层必须为 JSON 对象")
    latest_ver = normalize_version(str(obj.get("latest_version") or ""))
    min_raw = str(obj.get("min_supported_version") or "0.1.0").strip()
    min_ver = normalize_version(min_raw) if min_raw else "0.1.0"

    raw_highlights = obj.get("highlights")
    highlights: list[str] = []
    if isinstance(raw_highlights, list):
        for item in raw_highlights[:6]:
            line = str(item or "").strip()
            if line:
                highlights.append(line[:180])

    published_at = str(obj.get("published_at") or "").strip()[:32]
    tarball_sha256 = str(obj.get("tarball_sha256") or "").strip().lower()
    if tarball_sha256 and not re.fullmatch(r"[0-9a-f]{64}", tarball_sha256):
        raise ValueError("release.json 中 tarball_sha256 格式非法")

    return {
        "schema_version": int(obj.get("schema_version") or 1),
        "latest_version": latest_ver,
        "min_supported_version": min_ver,
        "published_at": published_at,
        "highlights": highlights,
        "tarball_sha256": tarball_sha256,
        "sources": obj.get("sources") if isinstance(obj.get("sources"), dict) else {},
    }


def load_update_state(home: Path) -> dict[str, Any]:
    path = update_state_path(home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_update_state(home: Path, state: dict[str, Any]) -> None:
    try:
        atomic_write_text(update_state_path(home), json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        pass


def build_update_notice_from_state(
    state: dict[str, Any],
    *,
    current_version: str = __version__,
) -> dict[str, Any] | None:
    """当缓存记录中的 latest_version 高于当前运行版本时，构建面向 Agent 与看板的结构化升级提醒。"""
    latest_ver = str(state.get("latest_version") or "").strip()
    if not latest_ver or not is_newer_version(latest_ver, current_version):
        return None

    min_ver = str(state.get("min_supported_version") or "").strip()
    required = bool(min_ver and is_newer_version(min_ver, current_version))
    highlights = [str(x).strip() for x in (state.get("highlights") or []) if str(x).strip()][:5]
    summary_text = "；".join(highlights[:3]) if highlights else "包含最新功能优化与排雷规则升级"
    prefix = "⚠️ 发现重要必升版本" if required else "💡 发现求职助手新版本"

    return {
        "update_available": True,
        "required": required,
        "current_version": current_version,
        "latest_version": latest_ver,
        "min_supported_version": min_ver or "0.1.0",
        "published_at": str(state.get("published_at") or ""),
        "highlights": highlights,
        "source": str(state.get("source_used") or "gitee"),
        "upgrade_command": "hiremate.py update",
        "dsh_slash_command": "/hiremate-update",
        "agent_prompt_hint": (
            f"{prefix} v{latest_ver}（当前 v{current_version}）：{summary_text}。"
            "直接对助手说「升级求职助手」（DSH 可输入 /hiremate-update）即可自动完成无损更新。"
        ),
    }


def _probe_git_remote_tags(prefer_source: str = "auto", timeout: float = 6.0) -> tuple[str, str] | None:
    """当远端尚未托管 release.json 时的轻量 Git Tag 兜底探测（Gitee -> GitHub）。"""
    git_bin = _find_usable_git_binary()
    if not git_bin:
        return None
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for src_name, git_url in _resolve_git_sources(prefer_source):
        try:
            proc = subprocess.run(
                [git_bin, "ls-remote", "--tags", "--refs", "--", git_url],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode != 0:
                continue
            versions: list[str] = []
            for line in proc.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) < 2:
                    continue
                ref = parts[1]
                tag = ref.rsplit("/", 1)[-1]
                try:
                    versions.append(normalize_version(tag))
                except ValueError:
                    continue
            if versions:
                versions.sort(key=parse_semver)
                return src_name, versions[-1]
        except Exception:
            continue
    return None


def _is_cache_fresh(cached: dict[str, Any], now_ts: float) -> bool:
    if not cached:
        return False
    try:
        last_ts = float(cached.get("checked_ts") or 0.0)
    except (TypeError, ValueError):
        return False
    ttl = FAILED_CHECK_RETRY_SECONDS if cached.get("last_error") else PASSIVE_CHECK_TTL_SECONDS
    elapsed = now_ts - last_ts
    return 0.0 <= elapsed < ttl


def check_for_updates(
    home: Path | None = None,
    *,
    force: bool = False,
    prefer_source: str = "auto",
    timeout: float | None = None,
    allow_git_tag_fallback: bool = False,
) -> dict[str, Any]:
    """检查公开仓库是否有新版本（默认优先读 Gitee Raw release.json，失败自动切 GitHub Raw）。"""
    hm_home = get_hiremate_home(home)
    now_ts = time.time()
    cached = load_update_state(hm_home)

    if not force and _is_cache_fresh(cached, now_ts):
        notice = build_update_notice_from_state(cached, current_version=__version__)
        return {
            "ok": not bool(cached.get("last_error")),
            "from_cache": True,
            "current_version": __version__,
            "latest_version": cached.get("latest_version") or __version__,
            "update_available": bool(notice),
            "source_used": cached.get("source_used"),
            "checked_at": cached.get("checked_at"),
            "highlights": cached.get("highlights") or [],
            "update_notice": notice,
            **({"error": cached["last_error"]} if cached.get("last_error") else {}),
        }

    eff_timeout = timeout if timeout is not None else (ACTIVE_HTTP_TIMEOUT_SECONDS if force else PASSIVE_HTTP_TIMEOUT_SECONDS)
    errors: list[str] = []

    for src_name, manifest_url, is_local_override in _resolve_manifest_sources(prefer_source):
        try:
            raw = _fetch_bytes(
                manifest_url,
                timeout=eff_timeout,
                max_bytes=MAX_MANIFEST_BYTES,
                allow_test_local=is_local_override,
            )
            manifest = _parse_and_validate_manifest(raw)
            state = {
                "checked_at": now_iso(),
                "checked_ts": now_ts,
                "current_version": __version__,
                "latest_version": manifest["latest_version"],
                "min_supported_version": manifest["min_supported_version"],
                "published_at": manifest["published_at"],
                "highlights": manifest["highlights"],
                "tarball_sha256": manifest["tarball_sha256"],
                "sources": manifest["sources"],
                "source_used": src_name,
                "last_error": "",
            }
            save_update_state(hm_home, state)
            notice = build_update_notice_from_state(state, current_version=__version__)
            return {
                "ok": True,
                "from_cache": False,
                "current_version": __version__,
                "latest_version": manifest["latest_version"],
                "min_supported_version": manifest["min_supported_version"],
                "published_at": manifest["published_at"],
                "highlights": manifest["highlights"],
                "update_available": bool(notice),
                "source_used": src_name,
                "checked_at": state["checked_at"],
                "update_notice": notice,
                "manifest": manifest,
            }
        except Exception as exc:
            errors.append(f"{src_name}: {exc}")

    if allow_git_tag_fallback:
        tag_hit = _probe_git_remote_tags(prefer_source=prefer_source, timeout=max(eff_timeout, 5.0))
        if tag_hit:
            src_name, tag_ver = tag_hit
            state = {
                "checked_at": now_iso(),
                "checked_ts": now_ts,
                "current_version": __version__,
                "latest_version": tag_ver,
                "min_supported_version": "0.1.0",
                "published_at": "",
                "highlights": [],
                "source_used": f"{src_name}_git_tag",
                "last_error": "",
            }
            save_update_state(hm_home, state)
            notice = build_update_notice_from_state(state, current_version=__version__)
            return {
                "ok": True,
                "from_cache": False,
                "current_version": __version__,
                "latest_version": tag_ver,
                "min_supported_version": "0.1.0",
                "published_at": "",
                "highlights": [],
                "update_available": bool(notice),
                "source_used": state["source_used"],
                "checked_at": state["checked_at"],
                "update_notice": notice,
            }

    err_summary = " | ".join(errors) or "无法连接公开更新源"
    fail_state = {
        **cached,
        "checked_at": now_iso(),
        "checked_ts": now_ts,
        "current_version": __version__,
        "last_error": err_summary[:500],
    }
    save_update_state(hm_home, fail_state)
    fallback_notice = build_update_notice_from_state(cached, current_version=__version__) if cached else None
    return {
        "ok": False,
        "code": "UPDATE_CHECK_UNAVAILABLE",
        "from_cache": False,
        "current_version": __version__,
        "latest_version": cached.get("latest_version") or __version__,
        "update_available": bool(fallback_notice),
        "update_notice": fallback_notice,
        "error": f"检查更新暂不可用（已尝试 Gitee 主源与 GitHub 备源）：{err_summary}",
    }


def get_passive_update_notice(home: Path | None = None) -> dict[str, Any] | None:
    """供高频日常命令静默调用：受 6 小时缓存 + 2.0s 守护线程硬墙钟双重保护，绝不因 DNS 或多 IP 黑洞阻塞 CLI。"""
    try:
        hm_home = get_hiremate_home(home)
        cached = load_update_state(hm_home)
        if os.environ.get("HIREMATE_DISABLE_UPDATE_CHECK") == "1":
            return build_update_notice_from_state(cached, current_version=__version__) if cached else None

        if _is_cache_fresh(cached, time.time()):
            return build_update_notice_from_state(cached, current_version=__version__)

        box: list[dict[str, Any] | None] = [None]

        def _worker() -> None:
            try:
                res = check_for_updates(hm_home, force=False, timeout=PASSIVE_HTTP_TIMEOUT_SECONDS)
                notice = res.get("update_notice")
                if isinstance(notice, dict):
                    box[0] = notice
            except Exception:
                pass

        worker_thread = threading.Thread(target=_worker, daemon=True)
        worker_thread.start()
        worker_thread.join(timeout=PASSIVE_HARD_WALL_TIMEOUT_SECONDS)
        if worker_thread.is_alive():
            # 若极端 DNS/黑洞超时尚未返回，先写一个退避状态避免下次命令立即再次卡顿，并返回已有缓存提醒
            timeout_state = {
                **cached,
                "checked_at": now_iso(),
                "checked_ts": time.time(),
                "current_version": __version__,
                "last_error": "passive_wall_timeout",
            }
            save_update_state(hm_home, timeout_state)
            return build_update_notice_from_state(cached, current_version=__version__) if cached else None
        return box[0]
    except Exception:
        return None


def attach_passive_update_notice(payload: dict[str, Any], home: Path | None = None) -> dict[str, Any]:
    """当存在可用新版本时，向 CLI 输出字典平滑注入 `update_notice` 字段。"""
    if not isinstance(payload, dict) or not payload.get("ok"):
        return payload
    notice = get_passive_update_notice(home)
    if notice:
        payload["update_notice"] = notice
    return payload


def _locate_open_source_dir(extracted_root: Path) -> Path:
    """兼容独立公开开源仓根目录结构与全栈仓 `open/` 子目录结构。"""
    candidates = [extracted_root, extracted_root / "open"]
    for child in sorted(extracted_root.iterdir()) if extracted_root.is_dir() else []:
        if child.is_dir():
            candidates.append(child)
            candidates.append(child / "open")

    for cand in candidates:
        if (
            (cand / "install_plugin.py").is_file()
            and (cand / "hiremate.py").is_file()
            and (cand / "hiremate_lib" / "__init__.py").is_file()
            and (cand / "workbuddy" / "skills" / "hiremate" / "SKILL.md").is_file()
        ):
            return cand
    raise RuntimeError("下载的更新包结构不完整（缺少 install_plugin.py、hiremate.py 或 SKILL.md）")


def _read_staged_package_info(open_dir: Path) -> tuple[str, list[str], str]:
    init_text = (open_dir / "hiremate_lib" / "__init__.py").read_text(encoding="utf-8")
    m = _INIT_VERSION_RE.search(init_text)
    if not m:
        raise RuntimeError("无法从更新包 hiremate_lib/__init__.py 解析版本号")
    staged_ver = normalize_version(m.group(1))

    highlights: list[str] = []
    published_at = ""
    rel_file = open_dir / "release.json"
    if rel_file.is_file():
        try:
            manifest = _parse_and_validate_manifest(rel_file.read_bytes())
            highlights = manifest.get("highlights") or []
            published_at = manifest.get("published_at") or ""
        except Exception:
            pass
    return staged_ver, highlights, published_at


def _safe_extract_tarball(tar_bytes: bytes, dest_dir: Path) -> None:
    """安全解压 tar.gz 源码包：拦截路径穿越（Tar-Slip）、特殊设备/软链与超大解压炸弹（Tar-Bomb）。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    tar_file = dest_dir / "release.tar.gz"
    tar_file.write_bytes(tar_bytes)
    extract_root = (dest_dir / "extracted").resolve()
    extract_root.mkdir(parents=True, exist_ok=True)

    with tarfile.open(tar_file, mode="r:gz") as tf:
        members = tf.getmembers()
        if len(members) > MAX_TARBALL_ENTRIES:
            raise RuntimeError(f"拒绝解压条目数异常的压缩包 ({len(members)} > {MAX_TARBALL_ENTRIES})")
        total_uncompressed = 0
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"拒绝解压包含非普通文件/软链的压缩包条目：{member.name}")
            if member.size < 0:
                raise RuntimeError(f"压缩包条目大小异常：{member.name}")
            total_uncompressed += member.size
            if total_uncompressed > MAX_TARBALL_UNCOMPRESSED_BYTES:
                raise RuntimeError(f"拦截压缩包解压体积超限 (> {MAX_TARBALL_UNCOMPRESSED_BYTES} bytes)")
            target_path = (extract_root / member.name).resolve()
            if os.path.commonpath([str(extract_root), str(target_path)]) != str(extract_root):
                raise RuntimeError(f"拦截压缩包路径穿越攻击：{member.name}")
        if sys.version_info >= (3, 12):
            tf.extractall(path=extract_root, filter="data")
        else:
            tf.extractall(path=extract_root)
            for extracted_item in extract_root.rglob("*"):
                try:
                    if extracted_item.is_dir():
                        os.chmod(extracted_item, 0o755)
                    elif extracted_item.is_file():
                        os.chmod(extracted_item, 0o755 if extracted_item.suffix == ".py" else 0o644)
                except OSError:
                    pass


def _download_source_tree(
    stage_base: Path,
    *,
    cwd: Path,
    prefer_source: str = "auto",
    manifest: dict[str, Any] | None = None,
) -> tuple[Path, str, list[str]]:
    """拉取最新源码树：优先走 Git 浅克隆（Gitee -> GitHub），无 Git 环境自动回退 HTTPS Tarball。"""
    attempts: list[str] = []
    stage_base.mkdir(parents=True, exist_ok=True)
    git_bin = _find_usable_git_binary()
    if git_bin and os.environ.get("HIREMATE_FORCE_TARBALL_UPDATE") != "1":
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        for idx, (src_name, git_url) in enumerate(_resolve_git_sources(prefer_source)):
            clone_dir = stage_base / f"git_{idx}_{src_name}"
            try:
                clone_cmd = [git_bin, "clone", "--depth", "1", "--single-branch", "--", git_url, str(clone_dir)]
                proc = subprocess.run(
                    clone_cmd,
                    cwd=str(cwd),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=GIT_CLONE_TIMEOUT_SECONDS,
                    check=False,
                )
                if proc.returncode != 0:
                    err_tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["git clone failed"]
                    attempts.append(f"git({src_name}): {err_tail[0]}")
                    continue
                open_dir = _locate_open_source_dir(clone_dir)
                return open_dir, f"git:{src_name}", attempts
            except Exception as exc:
                attempts.append(f"git({src_name}): {exc}")

    # 兜底通道：本机未装 git 或 Git 端口受限时，通过 HTTPS 拉取源码 Tarball
    try:
        tar_url, is_local_tar = _resolve_tarball_url(manifest)
        tar_dir = stage_base / "tarball_stage"
        tar_dir.mkdir(parents=True, exist_ok=True)
        raw_tar = _fetch_bytes(
            tar_url,
            timeout=30.0,
            max_bytes=MAX_TARBALL_BYTES,
            allow_test_local=is_local_tar,
        )
        expected_sha = str((manifest or {}).get("tarball_sha256") or "").strip().lower()
        if expected_sha:
            actual_sha = hashlib.sha256(raw_tar).hexdigest()
            if actual_sha != expected_sha:
                raise RuntimeError(f"更新包 SHA-256 校验不匹配（期望 {expected_sha[:12]}...，实际 {actual_sha[:12]}...）")
        _safe_extract_tarball(raw_tar, tar_dir)
        open_dir = _locate_open_source_dir(tar_dir / "extracted")
        return open_dir, "tarball:github", attempts
    except Exception as exc:
        attempts.append(f"tarball: {exc}")

    raise RuntimeError("无法从 Gitee 主源或 GitHub 备源拉取更新包：" + " | ".join(attempts))


def _is_windows_reparse_point(path: Path) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        st = path.lstat()
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _on_rm_error(func, path_str, _exc_info) -> None:
    """清理 Windows 下只读属性（如 .git/objects/pack/*.pack）后重试删除。"""
    try:
        os.chmod(path_str, stat.S_IWRITE | stat.S_IREAD)
        func(path_str)
    except OSError:
        pass


def _safe_remove_tree_or_link(target: Path) -> None:
    if target.is_symlink():
        target.unlink()
        return
    if _is_windows_reparse_point(target):
        try:
            os.rmdir(target)
        except OSError:
            pass
        return
    if not target.exists():
        return
    if target.is_dir():
        shutil.rmtree(target, onerror=_on_rm_error)
    else:
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        target.unlink()


def _copy_dir_snapshot(src: Path, dst: Path, *, ignore_names: tuple[str, ...] = ()) -> bool:
    if not src.exists() and not src.is_symlink():
        return False
    ignore_fn = shutil.ignore_patterns(*ignore_names) if ignore_names else None
    shutil.copytree(src, dst, symlinks=True, ignore=ignore_fn)
    return True


def _restore_dir_snapshot(snapshot: Path, target: Path, existed: bool) -> None:
    try:
        _safe_remove_tree_or_link(target)
    except Exception:
        pass
    if existed and snapshot.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(snapshot, target, symlinks=True, dirs_exist_ok=True)


def _ensure_dsh_bundle_skill_link(dsh_plugin_dir: Path, canon_skill_dir: Path) -> None:
    if not dsh_plugin_dir.is_dir() or not canon_skill_dir.is_dir():
        return
    link_target = dsh_plugin_dir / "skills" / "hiremate"
    link_target.parent.mkdir(parents=True, exist_ok=True)
    _safe_remove_tree_or_link(link_target)
    try:
        os.symlink(str(canon_skill_dir), str(link_target), target_is_directory=True)
    except OSError:
        if sys.platform.startswith("win"):
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_target), str(canon_skill_dir)],
                capture_output=True,
                text=True,
                check=False,
            )


def perform_self_update(
    home: Path | None = None,
    *,
    workspace: Path | None = None,
    check_only: bool = False,
    force: bool = False,
    prefer_source: str = "auto",
    search_roots: dict[str, str | Path] | None = None,
) -> dict[str, Any]:
    """执行版本检查或一键无损自更新（自动备份回滚 + 多宿主软链刷新 + 本地数据零触碰）。"""
    hm_home = get_hiremate_home(home)
    hm_home.mkdir(parents=True, exist_ok=True)
    ws = resolve_workspace(workspace, hm_home)

    chk = check_for_updates(
        hm_home,
        force=True,
        prefer_source=prefer_source,
        timeout=ACTIVE_HTTP_TIMEOUT_SECONDS,
        allow_git_tag_fallback=True,
    )

    if check_only:
        if not chk.get("ok"):
            return chk
        up_avail = bool(chk.get("update_available"))
        latest_v = str(chk.get("latest_version") or __version__)
        return {
            "ok": True,
            "check_only": True,
            "current_version": __version__,
            "latest_version": latest_v,
            "update_available": up_avail,
            "source_used": chk.get("source_used"),
            "published_at": chk.get("published_at") or "",
            "highlights": chk.get("highlights") or [],
            "update_notice": chk.get("update_notice"),
            "message": (
                f"发现新版本 v{latest_v}（当前 v{__version__}），运行 `hiremate.py update` 即可自动完成升级。"
                if up_avail
                else f"当前已是最新版本 v{__version__}。"
            ),
        }

    canon_skill_dir = hm_home / "skill" / "hiremate"
    dsh_plugin_dir = hm_home / "dsh-plugin"
    canonical_installed = (canon_skill_dir / "hiremate.py").is_file() and (canon_skill_dir / "SKILL.md").is_file()

    if chk.get("ok") and not chk.get("update_available") and not force and canonical_installed:
        return {
            "ok": True,
            "updated": False,
            "already_latest": True,
            "current_version": __version__,
            "latest_version": str(chk.get("latest_version") or __version__),
            "source_used": chk.get("source_used"),
            "canonical_dir": str(canon_skill_dir),
            "data_preserved": True,
            "message": f"当前已是最新版本 v{__version__}，无需重复更新（如需强制重装软链可加 --force）。",
        }

    manifest = chk.get("manifest") if isinstance(chk.get("manifest"), dict) else None

    tmp_root = Path(tempfile.mkdtemp(prefix="hiremate_self_update_"))
    try:
        try:
            staged_open_dir, download_channel, fallback_logs = _download_source_tree(
                tmp_root / "download",
                cwd=hm_home,
                prefer_source=prefer_source,
                manifest=manifest,
            )
            staged_ver, staged_highlights, staged_pub = _read_staged_package_info(staged_open_dir)
        except Exception as exc:
            return {
                "ok": False,
                "code": "UPDATE_DOWNLOAD_FAILED",
                "current_version": __version__,
                "error": str(exc),
                "hint": "请检查本机网络连接，或稍后重试 `hiremate.py update`（支持 `--source gitee` 或 `--source github`）。",
            }

        cmp_res = compare_versions(staged_ver, __version__)
        if cmp_res < 0 and not force:
            return {
                "ok": False,
                "code": "UPDATE_DOWNGRADE_BLOCKED",
                "current_version": __version__,
                "remote_version": staged_ver,
                "source_used": download_channel,
                "error": f"远端仓库版本 (v{staged_ver}) 低于本机当前版本 (v{__version__})，已阻止降级覆盖。",
            }

        if cmp_res == 0 and not force and canonical_installed:
            now_state = {
                "checked_at": now_iso(),
                "checked_ts": time.time(),
                "current_version": staged_ver,
                "latest_version": staged_ver,
                "min_supported_version": str((manifest or {}).get("min_supported_version") or "0.1.0"),
                "published_at": staged_pub,
                "highlights": staged_highlights,
                "source_used": download_channel,
                "last_error": "",
            }
            save_update_state(hm_home, now_state)
            return {
                "ok": True,
                "updated": False,
                "already_latest": True,
                "current_version": __version__,
                "latest_version": staged_ver,
                "source_used": download_channel,
                "canonical_dir": str(canon_skill_dir),
                "data_preserved": True,
                "message": f"当前已是最新版本 v{__version__}，无需更新。",
            }

        backup_root = tmp_root / "rollback_backup"
        backup_root.mkdir(parents=True, exist_ok=True)
        try:
            had_skill = _copy_dir_snapshot(canon_skill_dir, backup_root / "skill_hiremate")
            # 跳过 dsh-plugin 内部的 skills 软链/Windows Junction，避免穿透复制或悬空链接报错
            had_dsh = _copy_dir_snapshot(dsh_plugin_dir, backup_root / "dsh_plugin", ignore_names=("skills",))
        except Exception as exc:
            return {
                "ok": False,
                "code": "UPDATE_BACKUP_FAILED",
                "current_version": __version__,
                "error": f"创建升级前本地备份快照失败，已中止更新（原程序未改动）：{exc}",
            }

        install_cmd = [
            sys.executable,
            str(staged_open_dir / "install_plugin.py"),
            "--target",
            "auto",
            "--canonical-home",
            str(hm_home),
        ]
        if search_roots is not None:
            serialized_roots = {k: str(Path(v).expanduser().resolve()) for k, v in search_roots.items()}
            install_cmd.extend(["--search-roots-json", json.dumps(serialized_roots, ensure_ascii=False)])

        try:
            inst_proc = subprocess.run(
                install_cmd,
                cwd=str(hm_home),
                capture_output=True,
                text=True,
                timeout=30.0,
                check=False,
            )
            if inst_proc.returncode != 0:
                raise RuntimeError((inst_proc.stderr or inst_proc.stdout or "install_plugin.py 执行失败").strip())
            inst_res = json.loads(inst_proc.stdout.strip()) if inst_proc.stdout.strip() else {}

            # 冒烟自检：确认新写入的主副本 hiremate.py 可正常启动并报告预期版本号
            smoke_proc = subprocess.run(
                [sys.executable, str(canon_skill_dir / "hiremate.py"), "--version"],
                cwd=str(hm_home),
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
            if smoke_proc.returncode != 0 or staged_ver not in (smoke_proc.stdout + smoke_proc.stderr):
                raise RuntimeError(
                    f"新版本运行自检未通过 (rc={smoke_proc.returncode}): {(smoke_proc.stderr or smoke_proc.stdout).strip()}"
                )
        except Exception as exc:
            _restore_dir_snapshot(backup_root / "skill_hiremate", canon_skill_dir, had_skill)
            _restore_dir_snapshot(backup_root / "dsh_plugin", dsh_plugin_dir, had_dsh)
            if had_dsh:
                _ensure_dsh_bundle_skill_link(dsh_plugin_dir, canon_skill_dir)
            return {
                "ok": False,
                "code": "UPDATE_INSTALL_FAILED",
                "rolled_back": True,
                "current_version": __version__,
                "target_version": staged_ver,
                "error": f"安装新版本失败，已自动无损回滚至 v{__version__}：{exc}",
            }
    finally:
        try:
            _safe_remove_tree_or_link(tmp_root)
        except Exception:
            pass

    effective_highlights = staged_highlights or (chk.get("highlights") or [])
    updated_state = {
        "checked_at": now_iso(),
        "checked_ts": time.time(),
        "current_version": staged_ver,
        "latest_version": staged_ver,
        "min_supported_version": str((manifest or {}).get("min_supported_version") or "0.1.0"),
        "published_at": staged_pub or str(chk.get("published_at") or ""),
        "highlights": effective_highlights,
        "source_used": download_channel,
        "last_updated_at": now_iso(),
        "last_error": "",
    }
    save_update_state(hm_home, updated_state)

    # 若本地工作区已有生成的静态看板，顺手用新版本刷新一次看板（自动消掉旧版升级提示条并同步新 UI）
    dashboard_refreshed = False
    dash_file = ws / "dashboard.html"
    if (ws / "config.json").is_file() and dash_file.is_file():
        try:
            sub_env = {
                **os.environ,
                "HIREMATE_DISABLE_UPDATE_CHECK": "1",
                "HIREMATE_NO_BROWSER": "1",
                "HIREMATE_DISABLE_DESKTOP_NOTIFY": "1",
            }
            dash_proc = subprocess.run(
                [
                    sys.executable,
                    str(canon_skill_dir / "hiremate.py"),
                    "--home",
                    str(hm_home),
                    "--workspace",
                    str(ws),
                    "build-dashboard",
                ],
                cwd=str(hm_home),
                env=sub_env,
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            dashboard_refreshed = dash_proc.returncode == 0
        except Exception:
            dashboard_refreshed = False

    return {
        "ok": True,
        "updated": True,
        "already_latest": False,
        "previous_version": __version__,
        "current_version": staged_ver,
        "latest_version": staged_ver,
        "source_used": download_channel,
        "fallback_attempts": fallback_logs,
        "published_at": updated_state["published_at"],
        "highlights": effective_highlights,
        "canonical_dir": str(canon_skill_dir),
        "linked_hosts": inst_res.get("linked_hosts", []),
        "dashboard_refreshed": dashboard_refreshed,
        "data_preserved": True,
        "message": (
            f"已成功将「求职助手」从 v{__version__} 自动更新至 v{staged_ver}（来源：{download_channel}）！"
            "全宿主软链已同步生效，本地简历档案、四态账本与登录凭证完整保留。"
        ),
    }
