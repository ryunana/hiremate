#!/usr/bin/env python3
"""求职助手（hiremate）多宿主安装器（单源 Canonical + 目录软链同步 + DSH / Codex / Antigravity 侧边栏菜单挂载）。

- 默认运行 `python3 install_plugin.py`（即 `--target auto`）：
  1. 将唯一真实运行时与技能指令写入 `~/.hiremate/skill/hiremate`；
  2. 将 DeepSeek Harness 侧边栏工作台与原生斜杠命令插件写入 `~/.hiremate/dsh-plugin` 并自动幂等挂载至已安装的 DSH Profile；
  3. 为已安装的 OpenAI Codex 自动构建本地插件源 `~/.hiremate/codex-marketplace`（含 MCP App 左侧栏全局入口）并注册 `hiremate@hiremate`；
  4. 为已安装的 Antigravity 自动挂载 `~/.gemini/config/sidecars/hiremate_board` 侧栏与分屏看板；
  5. 自动探测本机已安装的桌面 Agent 宿主（WorkBuddy、DeepSeek Harness、Antigravity、Claude Code、Codex、Cursor、Windsurf、通用 ~/.agents），
     建立目录软链接指向 `~/.hiremate/skill/hiremate`（一次升级、全端同步生效）。
- 显式传入 `--target workbuddy|dsh|...`（及可选 `--dest-root`）时，保持与定向安装模式完全兼容。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

OPEN_ROOT = Path(__file__).resolve().parent

RUNTIME_FILES = (
    "hiremate.py",
    "hiremate_lib",
    "templates",
)

OPTIONAL_RUNTIME_FILES = (
    "release.json",
)

DSH_BUNDLE_FILES = (
    "index.mjs",
    "client.js",
    "icon.svg",
    "package.json",
    "cordis.patch.yml",
)

DSH_PATCH_BEGIN = "# hiremate-dsh:begin"
DSH_PATCH_END = "# hiremate-dsh:end"

CODEX_TOML_BEGIN = "# hiremate-codex:begin"
CODEX_TOML_END = "# hiremate-codex:end"

HOST_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("workbuddy", "WORKBUDDY_HOME", "~/.workbuddy"),
    ("dsh", "DSH_HOME", "~/.dsh"),
    ("antigravity", "AGY_CONFIG_HOME", "~/.gemini/config"),
    ("agents", "AGENTS_HOME", "~/.agents"),
    ("claude", "CLAUDE_HOME", "~/.claude"),
    ("codex", "CODEX_HOME", "~/.codex"),
    ("cursor", "CURSOR_HOME", "~/.cursor"),
    ("windsurf", "WINDSURF_HOME", "~/.codeium/windsurf"),
)

DEFAULT_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none">
  <rect x="6" y="6" width="52" height="52" rx="14" fill="#2563EB"/>
  <path d="M24 22v-3a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v3" stroke="#FFFFFF" stroke-width="3.2" stroke-linecap="round"/>
  <rect x="15" y="22" width="34" height="25" rx="5" stroke="#FFFFFF" stroke-width="3.2"/>
  <path d="M15 32.5h34" stroke="#93C5FD" stroke-width="2.6"/>
  <circle cx="32" cy="32.5" r="4.2" fill="#FFFFFF"/>
</svg>
"""


def _read_open_version() -> str:
    init_py = OPEN_ROOT / "hiremate_lib" / "__init__.py"
    if init_py.is_file():
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', init_py.read_text(encoding="utf-8"))
        if m:
            return m.group(1).strip()
    return "0.7.1"


def _canonical_home_dir(canonical_home: str | None = None) -> Path:
    base_raw = canonical_home or os.environ.get("HIREMATE_HOME") or "~/.hiremate"
    return Path(base_raw).expanduser().resolve()


def _canonical_skill_dir(canonical_home: str | None = None) -> Path:
    return _canonical_home_dir(canonical_home) / "skill" / "hiremate"


def _canonical_dsh_plugin_dir(canonical_home: str | None = None) -> Path:
    return _canonical_home_dir(canonical_home) / "dsh-plugin"


def _canonical_codex_marketplace_dir(canonical_home: str | None = None) -> Path:
    return _canonical_home_dir(canonical_home) / "codex-marketplace"


def _host_root_dir(host: str, dest_root: str | None = None) -> Path:
    if dest_root:
        return Path(dest_root).expanduser().resolve()
    for name, env_var, default_dir in HOST_CANDIDATES:
        if name == host:
            raw = os.environ.get(env_var) or default_dir
            return Path(raw).expanduser().resolve()
    raise ValueError(f"未知目标宿主：{host}")


def _is_windows_reparse_point(path: Path) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        st = path.lstat()
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _remove_link_or_dir(path: Path) -> bool:
    """安全移除目录软链、Windows Junction 或实体目录（兼容 Python 3.9~3.14，绝不误穿透 Junction）。"""
    if path.is_symlink():
        path.unlink()
        return True
    if _is_windows_reparse_point(path):
        os.rmdir(path)
        return True
    if not path.exists():
        return False
    if path.is_dir():
        try:
            shutil.rmtree(path)
        except OSError:
            os.rmdir(path)
        return True
    path.unlink()
    return True


def _copy_runtime_into(dest_dir: Path, skill_src: Path) -> list[str]:
    if dest_dir.is_symlink() or _is_windows_reparse_point(dest_dir):
        _remove_link_or_dir(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_src, dest_dir / "SKILL.md")
    installed = [str(dest_dir / "SKILL.md")]

    for name in RUNTIME_FILES:
        src = OPEN_ROOT / name
        dst = dest_dir / name
        if src.is_dir():
            _remove_link_or_dir(dst)
            shutil.copytree(
                src,
                dst,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
            )
        else:
            if dst.is_symlink():
                dst.unlink()
            shutil.copy2(src, dst)
        installed.append(str(dst))

    for opt_name in OPTIONAL_RUNTIME_FILES:
        opt_src = OPEN_ROOT / opt_name
        if opt_src.is_file():
            opt_dst = dest_dir / opt_name
            if opt_dst.is_symlink():
                opt_dst.unlink()
            shutil.copy2(opt_src, opt_dst)
            installed.append(str(opt_dst))
    return installed


def _link_skill_dir(source_dir: Path, target_link: Path) -> str:
    """建立目录软链（若已指向同一主副本则幂等保留；无权限时回退 Junction 或拷贝）。"""
    target_link.parent.mkdir(parents=True, exist_ok=True)
    is_sym = target_link.is_symlink()
    is_junc = (not is_sym) and _is_windows_reparse_point(target_link)
    if (is_sym or is_junc) and target_link.exists():
        try:
            if target_link.resolve() == source_dir.resolve():
                return "symlink" if is_sym else "junction"
        except OSError:
            pass
    _remove_link_or_dir(target_link)

    try:
        os.symlink(str(source_dir), str(target_link), target_is_directory=True)
        return "symlink"
    except OSError:
        if sys.platform.startswith("win"):
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target_link), str(source_dir)],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0 and target_link.exists():
                return "junction"
        shutil.copytree(
            source_dir,
            target_link,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
        )
        return "copy_fallback"


def _install_dsh_bundle_files(dsh_plugin_dir: Path, canon_skill_dir: Path) -> list[str]:
    """将 open/dsh 侧边栏与原生斜杠命令包写入 ~/.hiremate/dsh-plugin 并将其内部 skills/hiremate 指向主副本。"""
    src_dsh = OPEN_ROOT / "dsh"
    if not src_dsh.is_dir():
        return []
    if dsh_plugin_dir.is_symlink():
        dsh_plugin_dir.unlink()
    dsh_plugin_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for fname in DSH_BUNDLE_FILES:
        sfile = src_dsh / fname
        if sfile.is_file():
            dfile = dsh_plugin_dir / fname
            shutil.copy2(sfile, dfile)
            written.append(str(dfile))

    bundle_skill_link = dsh_plugin_dir / "skills" / "hiremate"
    _link_skill_dir(canon_skill_dir, bundle_skill_link)
    written.append(str(bundle_skill_link))
    return written


def _merge_dsh_cordis_patch(patch_file: Path, specifier: str) -> str:
    """向 DSH cordis.patch.yml 幂等合并 `- insert:` 插件入口（支持平滑升级旧版无 insert 块，绝不破坏用户其他配置）。"""
    escaped_spec = str(specifier).replace("\\", "/").replace('"', '\\"')
    block = (
        f"{DSH_PATCH_BEGIN}\n"
        f"- insert:\n"
        f"    - id: hiremate-dsh\n"
        f'      name: "{escaped_spec}"\n'
        f"{DSH_PATCH_END}\n"
    )

    if not patch_file.exists():
        patch_file.parent.mkdir(parents=True, exist_ok=True)
        patch_file.write_text(block, encoding="utf-8")
        return "created"

    existing = patch_file.read_text(encoding="utf-8")
    stripped = existing.strip()
    if stripped == "[]":
        patch_file.write_text(block, encoding="utf-8")
        return "replaced_empty_list"
    if stripped.startswith("["):
        return "skipped_flow_list"

    b_idx = existing.find(DSH_PATCH_BEGIN)
    e_idx = existing.find(DSH_PATCH_END)
    if b_idx != -1 and e_idx != -1 and e_idx > b_idx:
        after = existing[e_idx + len(DSH_PATCH_END) :].lstrip("\r\n")
        merged = existing[:b_idx].rstrip() + "\n\n" + block
        if after:
            merged += "\n" + after
        final_text = merged.lstrip("\n")
        if final_text == existing:
            return "unchanged"
        patch_file.write_text(final_text, encoding="utf-8")
        return "updated"

    if "id: hiremate-dsh" in existing:
        # 若宿主重写 YAML 时剥离了首行注释（如仅保留 `- insert:` + `- id: hiremate-dsh`），原位升级为标准标记块
        uncommented_pat = re.compile(
            r"(?m)^-[ \t]+insert:[ \t]*\n[ \t]*-[ \t]+id:[ \t]*hiremate-dsh[ \t]*\n(?:[ \t]+[A-Za-z0-9_-]+:[^\n]*\n?)*(?:[ \t]*# hiremate-dsh:end[ \t]*\n?)?"
        )
        if uncommented_pat.search(existing):
            updated = uncommented_pat.sub(block, existing, count=1)
            if updated != existing:
                patch_file.write_text(updated, encoding="utf-8")
                return "updated"
        return "already_present"

    merged = (existing.rstrip() + "\n\n" + block) if stripped else block
    patch_file.write_text(merged, encoding="utf-8")
    return "appended"


def _register_dsh_host_plugin(dsh_root: Path, dsh_plugin_dir: Path) -> dict[str, Any]:
    """将 hiremate-dsh（含侧边栏工作台与斜杠命令）挂载至 DSH 宿主目录。"""
    index_mjs = dsh_plugin_dir / "index.mjs"
    if not index_mjs.is_file():
        return {"registered": False, "reason": "bundle_missing"}

    patched_files: list[dict[str, str]] = []
    linked_modules: list[str] = []

    profiles_dir = dsh_root / "profiles"
    if profiles_dir.is_dir():
        for prof in sorted(profiles_dir.iterdir()):
            if not prof.is_dir() or prof.name == "node_modules" or prof.name.startswith("."):
                continue
            if not ((prof / "package.json").is_file() or (prof / "cordis.patch.yml").is_file()):
                continue
            nm_dir = prof / "node_modules"
            use_pkg_spec = False
            try:
                nm_dir.mkdir(parents=True, exist_ok=True)
                pkg_link = nm_dir / "hiremate-dsh"
                _link_skill_dir(dsh_plugin_dir, pkg_link)
                linked_modules.append(str(pkg_link))
                use_pkg_spec = True
            except OSError:
                use_pkg_spec = False
            patch_path = prof / "cordis.patch.yml"
            spec = "hiremate-dsh" if use_pkg_spec else str(index_mjs)
            action = _merge_dsh_cordis_patch(patch_path, spec)
            patched_files.append({"profile": prof.name, "patch_file": str(patch_path), "action": action})

    if not patched_files:
        root_nm = dsh_root / "node_modules"
        use_root_pkg = False
        try:
            root_nm.mkdir(parents=True, exist_ok=True)
            pkg_link = root_nm / "hiremate-dsh"
            _link_skill_dir(dsh_plugin_dir, pkg_link)
            linked_modules.append(str(pkg_link))
            use_root_pkg = True
        except OSError:
            use_root_pkg = False
        root_patch = dsh_root / "cordis.patch.yml"
        action = _merge_dsh_cordis_patch(root_patch, "hiremate-dsh" if use_root_pkg else str(index_mjs))
        patched_files.append({"profile": "root", "patch_file": str(root_patch), "action": action})

    return {
        "registered": True,
        "sidebar_panel": "hiremate",
        "plugin_dir": str(dsh_plugin_dir),
        "patched_files": patched_files,
        "linked_node_modules": linked_modules,
    }


def _find_codex_cli_bin() -> str | None:
    found = shutil.which("codex")
    if found:
        return found
    candidates = [
        Path.home() / ".npm-global" / "bin" / "codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
        Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
        Path("/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex"),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _build_codex_marketplace_bundle(
    mp_dir: Path,
    canon_skill_dir: Path,
    canon_home: Path,
) -> dict[str, Any]:
    """构建符合 Codex 本地 Marketplace 规范的完整插件包（含左侧边栏 MCP App 面板与快捷启动卡片）。"""
    version = _read_open_version()
    if mp_dir.is_symlink():
        mp_dir.unlink()
    _remove_link_or_dir(mp_dir)

    mp_meta_dir = mp_dir / ".agents" / "plugins"
    plug_root = mp_dir / "plugins" / "hiremate"
    codex_plug_dir = plug_root / ".codex-plugin"
    assets_dir = plug_root / "assets"
    skills_target = plug_root / "skills" / "hiremate"

    mp_meta_dir.mkdir(parents=True, exist_ok=True)
    codex_plug_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    icon_src = OPEN_ROOT / "dsh" / "icon.svg"
    icon_svg = icon_src.read_text(encoding="utf-8") if icon_src.is_file() else DEFAULT_ICON_SVG
    (assets_dir / "logo.svg").write_text(icon_svg, encoding="utf-8")

    marketplace_json = {
        "name": "hiremate",
        "interface": {
            "displayName": "求职助手 (HireMate)",
        },
        "plugins": [
            {
                "name": "hiremate",
                "source": {
                    "source": "local",
                    "path": "./plugins/hiremate",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }
    (mp_meta_dir / "marketplace.json").write_text(
        json.dumps(marketplace_json, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    plugin_json = {
        "name": "hiremate",
        "version": version,
        "description": "求职助手（hiremate）：BOSS 直聘岗位精准匹配（自动绕开外包与前东家）、本机对话行文风格语气档案、一岗一议半自动/全自动消息答疑、多版简历对口路由与四态可视化看板。",
        "author": {
            "name": "HireMate",
        },
        "license": "MIT",
        "keywords": ["hiremate", "job-search", "resume", "求职助手", "BOSS直聘"],
        "skills": "./skills/",
        "mcpServers": "./.mcp.json",
        "interface": {
            "displayName": "求职助手",
            "shortDescription": "BOSS 岗位精准排雷、个人语气档案与四态求职看板",
            "longDescription": "拒绝海投乱投：自动拉黑保护前东家、识别外包与驻场、从历史对话提取本人说话语气拟写破冰与答疑，并在左侧边栏提供实时可视化求职看板。",
            "developerName": "HireMate",
            "logo": "./assets/logo.svg",
            "composerIcon": "./assets/logo.svg",
            "category": "Productivity",
            "capabilities": ["Read", "Write", "Interactive"],
            "defaultPrompt": [
                "帮我初始化求职助手，核对真实简历与 8 项职位筛选策略",
                "帮我在 BOSS 直聘按当前求职档案跑一轮精准搜岗深筛",
                "请从我的历史对话记录提取行文风格，建立个人求职语气档案",
            ],
            "brandColor": "#2563EB",
        },
    }
    (codex_plug_dir / "plugin.json").write_text(
        json.dumps(plugin_json, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    py_cmd = sys.executable if (sys.executable and Path(sys.executable).is_file()) else "python3"
    mcp_json = {
        "mcpServers": {
            "hiremate": {
                "command": py_cmd,
                "args": [str(canon_skill_dir / "hiremate.py"), "mcp-serve"],
                "env": {
                    "HIREMATE_HOME": str(canon_home),
                    "PYTHONUTF8": "1",
                },
                "startup_timeout_sec": 30,
            }
        }
    }
    (plug_root / ".mcp.json").write_text(
        json.dumps(mcp_json, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    shutil.copytree(
        canon_skill_dir,
        skills_target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
    )
    return {"version": version, "plugin_root": plug_root}


def _merge_codex_config_toml_fallback(config_toml: Path, mp_dir: Path) -> str:
    escaped_mp = str(mp_dir).replace("\\", "/")
    mp_section = (
        f"[marketplaces.hiremate]\n"
        f'source_type = "local"\n'
        f'source = "{escaped_mp}"\n'
    )
    plug_section = (
        f'[plugins."hiremate@hiremate"]\n'
        f"enabled = true\n"
    )
    block = f"{CODEX_TOML_BEGIN}\n{mp_section}\n{plug_section}{CODEX_TOML_END}\n"

    if not config_toml.is_file():
        config_toml.parent.mkdir(parents=True, exist_ok=True)
        config_toml.write_text(block, encoding="utf-8")
        return "created"

    existing = config_toml.read_text(encoding="utf-8")
    b_idx = existing.find(CODEX_TOML_BEGIN)
    e_idx = existing.find(CODEX_TOML_END)
    if b_idx != -1 and e_idx != -1 and e_idx > b_idx:
        after = existing[e_idx + len(CODEX_TOML_END) :].lstrip("\r\n")
        merged = existing[:b_idx].rstrip() + "\n\n" + block
        if after:
            merged += "\n" + after
        config_toml.write_text(merged.lstrip("\n"), encoding="utf-8")
        return "updated"

    has_mp = re.search(r"(?m)^\[marketplaces\.hiremate\]\s*$", existing) is not None
    has_plug = re.search(r'(?m)^\[plugins\."hiremate@hiremate"\]\s*$', existing) is not None
    if has_mp and has_plug:
        return "already_present"

    missing_parts: list[str] = []
    if not has_mp:
        missing_parts.append(mp_section.rstrip())
    if not has_plug:
        missing_parts.append(plug_section.rstrip())
    to_append = (
        block
        if (not has_mp and not has_plug)
        else ("\n\n".join(missing_parts) + "\n")
    )
    merged = (existing.rstrip() + "\n\n" + to_append) if existing.strip() else to_append
    config_toml.write_text(merged, encoding="utf-8")
    return "appended"


def _strip_codex_config_toml(config_toml: Path) -> bool:
    if not config_toml.is_file():
        return False
    existing = config_toml.read_text(encoding="utf-8")
    changed = False

    b_idx = existing.find(CODEX_TOML_BEGIN)
    e_idx = existing.find(CODEX_TOML_END)
    if b_idx != -1 and e_idx != -1 and e_idx > b_idx:
        existing = (existing[:b_idx].rstrip() + "\n\n" + existing[e_idx + len(CODEX_TOML_END) :].lstrip("\r\n")).strip()
        changed = True

    for sec_pat in (
        r'(?m)^\[marketplaces\.hiremate\]\s*\n(?:[ \t]*[A-Za-z0-9_.-]+\s*=[^\n]*\n|[ \t]*\n)*',
        r'(?m)^\[plugins\."hiremate@hiremate"\]\s*\n(?:[ \t]*[A-Za-z0-9_.-]+\s*=[^\n]*\n|[ \t]*\n)*',
    ):
        new_txt, cnt = re.subn(sec_pat, "", existing)
        if cnt > 0:
            existing = new_txt.strip()
            changed = True

    if changed:
        config_toml.write_text((existing + "\n") if existing else "", encoding="utf-8")
    return changed


def _register_codex_host_plugin(
    codex_root: Path,
    canon_home: Path,
    canon_skill_dir: Path,
) -> dict[str, Any]:
    """将求职助手安装注册为 Codex 原生插件（含插件栏卡片 + MCP App 左侧边栏看板入口）。"""
    mp_dir = canon_home / "codex-marketplace"
    built = _build_codex_marketplace_bundle(mp_dir, canon_skill_dir, canon_home)
    version = str(built["version"])
    plug_root: Path = built["plugin_root"]

    codex_bin = _find_codex_cli_bin()
    if codex_bin:
        env = {**os.environ, "CODEX_HOME": str(codex_root)}
        try:
            r_mp = subprocess.run(
                [codex_bin, "plugin", "marketplace", "add", str(mp_dir)],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            subprocess.run(
                [codex_bin, "plugin", "remove", "hiremate@hiremate"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            r_add = subprocess.run(
                [codex_bin, "plugin", "add", "hiremate@hiremate"],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            cache_dir = codex_root / "plugins" / "cache" / "hiremate" / "hiremate" / version
            if r_mp.returncode == 0 and r_add.returncode == 0 and cache_dir.is_dir():
                return {
                    "registered": True,
                    "mode": "codex_cli",
                    "plugin_id": "hiremate@hiremate",
                    "sidebar_entrypoint": "global",
                    "marketplace_dir": str(mp_dir),
                    "cache_dir": str(cache_dir),
                }
        except Exception:
            pass

    # 无 codex CLI 或离线沙盒环境确定性回退：直接写入 cache 快照并幂等合并 config.toml
    cache_dir = codex_root / "plugins" / "cache" / "hiremate" / "hiremate" / version
    _remove_link_or_dir(cache_dir)
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(plug_root, cache_dir)
    toml_action = _merge_codex_config_toml_fallback(codex_root / "config.toml", mp_dir)
    return {
        "registered": True,
        "mode": "direct_cache_fallback",
        "plugin_id": "hiremate@hiremate",
        "sidebar_entrypoint": "global",
        "marketplace_dir": str(mp_dir),
        "cache_dir": str(cache_dir),
        "config_toml_action": toml_action,
    }


def _unregister_codex_host_plugin(codex_root: Path) -> dict[str, Any]:
    codex_bin = _find_codex_cli_bin()
    if codex_bin:
        env = {**os.environ, "CODEX_HOME": str(codex_root)}
        for args in (
            [codex_bin, "plugin", "remove", "hiremate@hiremate"],
            [codex_bin, "plugin", "marketplace", "remove", "hiremate"],
        ):
            try:
                subprocess.run(args, env=env, capture_output=True, text=True, timeout=10, check=False)
            except Exception:
                pass
    removed_cache = _remove_link_or_dir(codex_root / "plugins" / "cache" / "hiremate")
    stripped_toml = _strip_codex_config_toml(codex_root / "config.toml")
    return {"removed_cache": removed_cache, "stripped_toml": stripped_toml}


def _register_antigravity_sidecar(
    agy_root: Path,
    canon_home: Path,
    cli_skill_dir: Path | None = None,
    *,
    embed_skill_link: bool = True,
) -> dict[str, Any]:
    """在 Antigravity 配置目录下封装原生 plugins/hiremate 插件包（含技能软链与侧边栏看板 Sidecar），并清理旧版散装残留。"""
    src_main = OPEN_ROOT / "antigravity" / "sidecar" / "main.mjs"
    if not src_main.is_file():
        return {"registered": False, "reason": "sidecar_source_missing"}

    effective_skill_dir = cli_skill_dir or (canon_home / "skill" / "hiremate")
    plugin_dir = agy_root / "plugins" / "hiremate"
    if plugin_dir.is_symlink():
        plugin_dir.unlink()
    assets_dir = plugin_dir / "assets"
    sidecar_dir = plugin_dir / "sidecars" / "hiremate_board"
    assets_dir.mkdir(parents=True, exist_ok=True)
    if sidecar_dir.is_symlink():
        sidecar_dir.unlink()
    sidecar_dir.mkdir(parents=True, exist_ok=True)

    # 清理旧版散装挂载残留，防止与 plugins/hiremate 产生同名技能冲突或死机副本（保留 plugins/hiremate/sidecars 下的其他自定义子目录）
    _remove_link_or_dir(agy_root / "sidecars" / "hiremate_board")
    loose_skill = agy_root / "skills" / "hiremate"
    if loose_skill != effective_skill_dir:
        _remove_link_or_dir(loose_skill)

    icon_src = OPEN_ROOT / "dsh" / "icon.svg"
    if icon_src.is_file():
        shutil.copy2(icon_src, assets_dir / "logo.svg")
    else:
        (assets_dir / "logo.svg").write_text(DEFAULT_ICON_SVG, encoding="utf-8")

    plugin_manifest = {
        "name": "hiremate",
        "displayName": "求职助手 (HireMate)",
        "version": _read_open_version(),
        "description": "求职助手（HireMate）：逐岗排雷精筛、公司底细二维核查、收藏批次管理与实时投递转化漏斗看板。",
        "logo": "assets/logo.svg",
        "suggestedPrompts": [
            "帮我初始化求职助手",
            "去 BOSS 直聘跑一轮岗位深筛并生成审阅单",
            "打开求职助手可视化漏斗看板",
        ],
    }
    (plugin_dir / "plugin.json").write_text(
        json.dumps(plugin_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    plugin_skill_dir = plugin_dir / "skills" / "hiremate"
    skill_link_mode = "embedded"
    if embed_skill_link and plugin_skill_dir != effective_skill_dir:
        skill_link_mode = _link_skill_dir(effective_skill_dir, plugin_skill_dir)

    shutil.copy2(src_main, sidecar_dir / "main.mjs")

    rw_paths = [str(canon_home)]
    if str(effective_skill_dir) not in rw_paths and not str(effective_skill_dir).startswith(str(canon_home)):
        rw_paths.append(str(effective_skill_dir))

    sidecar_manifest = {
        "command": "node",
        "args": ["main.mjs"],
        "restart_policy": "always",
        "env": {
            "HIREMATE_HOME": str(canon_home),
            "HIREMATE_CLI_PATH": str(effective_skill_dir / "hiremate.py"),
        },
        "display_name": "求职助手",
        "description": "求职助手（hiremate）实时投递转化漏斗、约面待办队列、排雷台账与个人语气档案看板。",
        "has_web_ui": True,
        "agent_permissions": {
            "allows_user_interactions": False,
        },
        "sandbox": {
            "read_write_paths": rw_paths,
        },
        "ui_config": {
            "display_name": "求职助手",
            "views": [
                {
                    "path": "/",
                    "entrypoint": "SIDECAR_UI_ENTRYPOINT_SIDEBAR",
                    "title": "求职助手",
                },
                {
                    "path": "/aux",
                    "entrypoint": "SIDECAR_UI_ENTRYPOINT_AUX_PANE",
                    "title": "求职助手看板",
                },
            ],
        },
    }
    (sidecar_dir / "sidecar.json").write_text(
        json.dumps(sidecar_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "registered": True,
        "plugin_name": "hiremate",
        "plugin_dir": str(plugin_dir),
        "skill_dir": str(plugin_skill_dir),
        "link_mode": skill_link_mode,
        "sidecar_id": "hiremate/hiremate_board",
        "sidecar_dir": str(sidecar_dir),
        "entrypoints": ["SIDECAR_UI_ENTRYPOINT_SIDEBAR", "SIDECAR_UI_ENTRYPOINT_AUX_PANE"],
    }


def detect_installed_hosts(search_roots: dict[str, str | Path] | None = None) -> list[tuple[str, Path]]:
    """自动发现本机已存在的 Agent 配置根目录；若显式传入 search_roots 则严格限定在沙盒目录内探测。"""
    detected: list[tuple[str, Path]] = []
    if search_roots is not None:
        for name, _env_var, _default_dir in HOST_CANDIDATES:
            if name in search_roots:
                root = Path(search_roots[name]).expanduser().resolve()
                if root.exists():
                    detected.append((name, root))
        return detected

    for name, env_var, default_dir in HOST_CANDIDATES:
        raw = os.environ.get(env_var) or default_dir
        root = Path(raw).expanduser().resolve()
        if root.exists():
            detected.append((name, root))

    return detected


def _strip_dsh_cordis_patch(patch_file: Path) -> bool:
    if not patch_file.is_file():
        return False
    existing = patch_file.read_text(encoding="utf-8")
    changed = False

    b_idx = existing.find(DSH_PATCH_BEGIN)
    e_idx = existing.find(DSH_PATCH_END)
    if b_idx != -1 and e_idx != -1 and e_idx > b_idx:
        existing = (existing[:b_idx].rstrip() + "\n" + existing[e_idx + len(DSH_PATCH_END) :].lstrip("\r\n")).strip()
        changed = True

    # 兼容 DSH 客户端保存设置时重序列化 YAML 剥离首行注释后的残留条目（含 `- insert:` 列表形态与旧版字典形态）
    for pat in (
        r"(?m)^-[ \t]+insert:[ \t]*\n[ \t]*-[ \t]+id:[ \t]*hiremate-dsh[ \t]*\n(?:[ \t]+[A-Za-z0-9_-]+:[^\n]*\n?)*(?:[ \t]*# hiremate-dsh:end[ \t]*\n?)?",
        r"(?m)^[ \t]*-[ \t]+id:[ \t]*hiremate-dsh[ \t]*\n(?:[ \t]+[A-Za-z0-9_-]+:[^\n]*\n?)*(?:[ \t]*# hiremate-dsh:end[ \t]*\n?)?",
        r"(?m)^([ \t]+)hiremate-dsh:[ \t]*(?:\{[^\n]*\}|\n(?:\1[ \t]+[^\n]+\n?)*)",
    ):
        new_txt, cnt = re.subn(pat, "", existing)
        if cnt > 0:
            existing = new_txt.strip()
            changed = True

    if changed:
        patch_file.write_text((existing + "\n") if existing else "", encoding="utf-8")
    return changed


def uninstall(
    *,
    canonical_home: str | None = None,
    search_roots: dict[str, str | Path] | None = None,
    purge_data: bool = False,
) -> dict[str, Any]:
    """安全卸载多宿主软链、侧边栏入口与插件代码，默认完整保留用户的本地求职账本与个人档案。"""
    home_dir = _canonical_home_dir(canonical_home)
    canon_dir = _canonical_skill_dir(canonical_home)
    dsh_plugin_dir = _canonical_dsh_plugin_dir(canonical_home)
    codex_mp_dir = _canonical_codex_marketplace_dir(canonical_home)
    hosts = detect_installed_hosts(search_roots=search_roots)

    removed_links: list[str] = []
    unpatched_files: list[str] = []
    for h_name, h_root in hosts:
        link_path = h_root / "skills" / "hiremate"
        if _remove_link_or_dir(link_path):
            removed_links.append(str(link_path))

        if h_name == "dsh":
            if _strip_dsh_cordis_patch(h_root / "cordis.patch.yml"):
                unpatched_files.append(str(h_root / "cordis.patch.yml"))
            _remove_link_or_dir(h_root / "node_modules" / "hiremate-dsh")
            _remove_link_or_dir(h_root / "dsh-plugin")
            profiles_dir = h_root / "profiles"
            if profiles_dir.is_dir():
                for prof in sorted(profiles_dir.iterdir()):
                    if not prof.is_dir():
                        continue
                    if _strip_dsh_cordis_patch(prof / "cordis.patch.yml"):
                        unpatched_files.append(str(prof / "cordis.patch.yml"))
                    pkg_link = prof / "node_modules" / "hiremate-dsh"
                    _remove_link_or_dir(pkg_link)
        elif h_name == "codex":
            _unregister_codex_host_plugin(h_root)
            _remove_link_or_dir(h_root / ".hiremate-local")
        elif h_name == "antigravity":
            _remove_link_or_dir(h_root / "plugins" / "hiremate" / "skills" / "hiremate")
            _remove_link_or_dir(h_root / "plugins" / "hiremate")
            _remove_link_or_dir(h_root / "sidecars" / "hiremate_board")

    # 先清理包含内部软链/Junction 的子包目录，再删除主副本 canon_dir，防止 Windows 悬空 Junction 导致 rmtree 报错
    inner_dsh_skill = dsh_plugin_dir / "skills" / "hiremate"
    if inner_dsh_skill.is_symlink() or _is_windows_reparse_point(inner_dsh_skill) or inner_dsh_skill.exists():
        _remove_link_or_dir(inner_dsh_skill)
    for target_dir in (dsh_plugin_dir, codex_mp_dir, canon_dir):
        _remove_link_or_dir(target_dir)

    skill_parent = home_dir / "skill"
    if skill_parent.is_dir() and not any(skill_parent.iterdir()):
        skill_parent.rmdir()

    if purge_data and home_dir.exists():
        shutil.rmtree(home_dir)

    return {
        "ok": True,
        "uninstalled": True,
        "removed_host_links": removed_links,
        "unpatched_dsh_files": unpatched_files,
        "data_preserved": not purge_data,
        "data_dir": str(home_dir),
        "message": (
            f"已卸载求职助手插件软链、侧边栏入口与运行程序；本地求职账本与档案仍保留在 {home_dir}。"
            if not purge_data
            else f"已彻底卸载求职助手并清理目录 {home_dir}。"
        ),
    }


def _detect_active_caller_host(environ: dict[str, str] | None = None) -> str:
    """探测当前正在执行安装脚本的桌面 Agent 宿主名称（无法识别时返回 'unknown'）。"""
    env = environ if environ is not None else dict(os.environ)
    explicit = str(env.get("HIREMATE_HOST") or "").strip().lower()
    if explicit in {"dsh", "deepseek", "deepseek-harness"}:
        return "dsh"
    if explicit in {"codex", "openai-codex"}:
        return "codex"
    if explicit in {"antigravity", "agy"}:
        return "antigravity"
    if explicit in {"workbuddy", "claude", "cursor", "windsurf", "agents", "none"}:
        return explicit
    if env.get("ANTIGRAVITY_AGENT") or env.get("ANTIGRAVITY_CONVERSATION_ID") or env.get("ANTIGRAVITY_APP_DATA_DIR"):
        return "antigravity"
    if any(k.startswith("WORKBUDDY_") for k in env):
        return "workbuddy"
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT") or env.get("CLAUDE_PROJECT_DIR"):
        return "claude"
    if env.get("CURSOR_TRACE_ID") or env.get("CURSOR_SESSION_ID"):
        return "cursor"
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_SANDBOX") or env.get("CODEX_CI"):
        return "codex"
    if (
        env.get("DSH_SESSION_ID")
        or env.get("DSH_PTY_SESSION_ID")
        or env.get("DSH_APP_VERSION")
        or env.get("DSH_DESKTOP_DSH_DIR")
        or env.get("DSH_HOME")
    ):
        return "dsh"
    return "unknown"


def _build_sidebar_install_guide(caller_host: str, sidebar_registrations: dict[str, Any]) -> dict[str, Any]:
    """根据当前宿主是否支持侧边栏面板生成精准提示（支持则主动告知侧边栏位置，不支持则绝不误导）。"""
    mounted_labels: list[str] = []
    if sidebar_registrations.get("dsh", {}).get("registered"):
        mounted_labels.append("DeepSeek Harness（左侧栏「求职助手」）")
    if sidebar_registrations.get("codex", {}).get("registered"):
        mounted_labels.append("OpenAI Codex（左侧栏「求职助手 / HireMate」）")
    if sidebar_registrations.get("antigravity", {}).get("registered"):
        mounted_labels.append("Antigravity（侧边栏「求职助手看板」）")

    if caller_host == "dsh":
        return {
            "current_host": "dsh",
            "current_host_supports_sidebar": True,
            "sidebar_entry": "左侧边栏「求职助手」图标",
            "sidebar_entry_tip": (
                "📌 侧边栏看板已就绪：已自动挂载至 DeepSeek Harness 左侧边栏「求职助手」入口"
                "（首次安装后重启一次客户端即可在左侧栏看到图标，随时查看实时转化漏斗、一键学习对话语气或在线调整策略；当前会话无需停下等待重启）。"
            ),
            "agent_must_tell_user": (
                "你当前运行在支持侧边栏看板的 DeepSeek Harness 中，请务必在回复中提醒新用户："
                "可以通过左侧边栏的「求职助手」入口打开可视化看板（首次刚安装完重启一次客户端后图标即显示，不必中断当前建档对话）。"
            ),
            "next_step": "安装已完成！可在左侧边栏找到「求职助手」打开看板（首次安装重启客户端后显示），或直接对 AI 助手说：『帮我初始化求职助手』",
        }
    if caller_host == "codex":
        return {
            "current_host": "codex",
            "current_host_supports_sidebar": True,
            "sidebar_entry": "左侧边栏「求职助手（HireMate）」应用面板",
            "sidebar_entry_tip": (
                "📌 侧边栏看板已就绪：已自动挂载至 OpenAI Codex 左侧边栏「求职助手（HireMate）」应用面板"
                "（首次安装后重载或重启一次 Codex 即可在左侧栏点开查看实时求职看板与在线改策略；当前会话无需停下等待重启）。"
            ),
            "agent_must_tell_user": (
                "你当前运行在支持侧边栏看板的 Codex 中，请务必在回复中提醒新用户："
                "可以通过左侧边栏的「求职助手（HireMate）」入口找到可视化看板（首次刚安装完重载/重启一次客户端后显示，不必中断当前建档对话）。"
            ),
            "next_step": "安装已完成！可在 Codex 左侧边栏找到「求职助手（HireMate）」打开看板（首次安装重载后显示），或直接对 AI 助手说：『帮我初始化求职助手』",
        }
    if caller_host == "antigravity":
        return {
            "current_host": "antigravity",
            "current_host_supports_sidebar": True,
            "sidebar_entry": "侧边栏 / 右侧辅助面板「求职助手看板」（sidecar://hiremate/hiremate_board/aux）",
            "sidebar_entry_tip": (
                "📌 原生插件与侧边栏看板已就绪：可在 Antigravity 侧边栏或点击 `[求职助手看板](sidecar://hiremate/hiremate_board/aux)` 直接展开看板，"
                "并通过 `[求职助手](customization://hiremate)` 管理插件总开关。"
            ),
            "agent_must_tell_user": (
                "你当前运行在支持原生插件面板的 Antigravity 中，请在回复中提供 `[求职助手看板](sidecar://hiremate/hiremate_board/aux)` 快捷入口，"
                "并告知可通过侧边栏或 `[求职助手](customization://hiremate)` 随时查看与管理。"
            ),
            "next_step": "安装已完成！可在侧边栏点开「求职助手看板」，或直接对 AI 助手说：『帮我初始化求职助手』",
        }
    if caller_host in {"workbuddy", "claude", "cursor", "windsurf", "agents", "none"}:
        return {
            "current_host": caller_host,
            "current_host_supports_sidebar": False,
            "sidebar_entry": "",
            "sidebar_entry_tip": "",
            "agent_must_tell_user": (
                "当前客户端不支持第三方侧边栏面板，请勿提示用户去侧边栏查找看板；"
                "建档完成后通过 `build-dashboard --open` 在浏览器自动弹开本地可视化看板即可。"
            ),
            "next_step": "安装已完成！请直接对 AI 助手说：『帮我初始化求职助手』（建档后随时说『打开求职看板』即可在浏览器查看实时转化漏斗）",
        }

    # 未知终端调用（如用户在系统终端直接跑 python3 install_plugin.py）
    if mounted_labels:
        joined = "、".join(mounted_labels)
        return {
            "current_host": "unknown",
            "current_host_supports_sidebar": None,
            "sidebar_entry": joined,
            "sidebar_entry_tip": f"📌 已同步为本机支持侧边栏的客户端挂载看板入口：{joined}（首次安装重启对应客户端后可见）。",
            "agent_must_tell_user": (
                "若你当前运行在支持侧边栏看板的客户端（DeepSeek Harness / Codex / Antigravity）中，请主动提醒用户可通过侧边栏「求职助手」打开看板（首次安装重启客户端后显示）；"
                "若当前客户端不支持侧边栏（如 WorkBuddy / Claude Code），请仅引导在浏览器打开本地看板，勿提侧边栏。"
            ),
            "next_step": f"安装已完成！支持侧边栏的客户端可在 {joined} 打开看板，或直接对 AI 助手说：『帮我初始化求职助手』",
        }
    return {
        "current_host": "unknown",
        "current_host_supports_sidebar": False,
        "sidebar_entry": "",
        "sidebar_entry_tip": "",
        "agent_must_tell_user": "请引导用户直接开始建档，建档后在浏览器打开本地可视化看板查看漏斗（本机未挂载侧边栏宿主，勿提侧边栏）。",
        "next_step": "安装已完成！请直接对 AI 助手说：『帮我初始化求职助手』",
    }


def install(
    target: str = "auto",
    dest_root: str | None = None,
    canonical_home: str | None = None,
    search_roots: dict[str, str | Path] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    primary_skill_md = OPEN_ROOT / "workbuddy" / "skills" / "hiremate" / "SKILL.md"
    if not primary_skill_md.is_file():
        raise FileNotFoundError(f"未找到核心技能文件：{primary_skill_md}")

    # 1. 自动多宿主单源软链 + 侧边栏插件挂载模式（推荐主流程）
    if target == "auto" and not dest_root:
        canon_home = _canonical_home_dir(canonical_home)
        canon_dir = _canonical_skill_dir(canonical_home)
        dsh_plugin_dir = _canonical_dsh_plugin_dir(canonical_home)
        hosts = detect_installed_hosts(search_roots=search_roots)
        planned_canonical = [str(canon_dir / "SKILL.md")] + [str(canon_dir / item) for item in RUNTIME_FILES]

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "target": "auto",
                "canonical_dir": str(canon_dir),
                "dsh_plugin_dir": str(dsh_plugin_dir),
                "planned_paths": planned_canonical,
                "planned_links": [
                    {"host": h_name, "skill_dir": str(h_root / "skills" / "hiremate")}
                    for h_name, h_root in hosts
                ],
            }

        installed_paths = _copy_runtime_into(canon_dir, primary_skill_md)
        installed_paths.extend(_install_dsh_bundle_files(dsh_plugin_dir, canon_dir))

        linked_hosts: list[dict[str, Any]] = []
        dsh_registrations: list[dict[str, Any]] = []
        sidebar_registrations: dict[str, Any] = {}
        for h_name, h_root in hosts:
            if h_name == "antigravity":
                areg = _register_antigravity_sidecar(h_root, canon_home, cli_skill_dir=canon_dir)
                sidebar_registrations["antigravity"] = areg
                linked_hosts.append(
                    {
                        "host": h_name,
                        "skill_dir": areg.get("skill_dir", str(h_root / "plugins" / "hiremate" / "skills" / "hiremate")),
                        "link_mode": areg.get("link_mode", "symlink"),
                        "antigravity_sidecar": areg,
                        "sidebar": areg,
                    }
                )
                continue

            link_path = h_root / "skills" / "hiremate"
            same_as_canon = False
            try:
                same_as_canon = link_path == canon_dir and link_path.resolve() == canon_dir.resolve()
            except OSError:
                same_as_canon = False
            if not same_as_canon:
                mode = _link_skill_dir(canon_dir, link_path)
                host_entry: dict[str, Any] = {
                    "host": h_name,
                    "skill_dir": str(link_path),
                    "link_mode": mode,
                }
                if h_name == "dsh":
                    reg = _register_dsh_host_plugin(h_root, dsh_plugin_dir)
                    host_entry["dsh_slash_commands"] = reg
                    host_entry["sidebar"] = reg
                    dsh_registrations.append(reg)
                    sidebar_registrations["dsh"] = reg
                elif h_name == "codex":
                    creg = _register_codex_host_plugin(h_root, canon_home, canon_dir)
                    host_entry["codex_plugin"] = creg
                    host_entry["sidebar"] = creg
                    sidebar_registrations["codex"] = creg
                linked_hosts.append(host_entry)

        caller_host = _detect_active_caller_host()
        sb_guide = _build_sidebar_install_guide(caller_host, sidebar_registrations)
        out_res: dict[str, Any] = {
            "ok": True,
            "dry_run": False,
            "target": "auto",
            "canonical_dir": str(canon_dir),
            "dsh_plugin_dir": str(dsh_plugin_dir),
            "dest_dir": linked_hosts[0]["skill_dir"] if linked_hosts else str(canon_dir),
            "installed_paths": installed_paths,
            "linked_hosts": linked_hosts,
            "dsh_registrations": dsh_registrations,
            "sidebar_registrations": sidebar_registrations,
            "sidebar_guide": sb_guide,
            "next_step": sb_guide["next_step"],
        }
        if not linked_hosts:
            out_res["warning"] = (
                "未在本机发现已安装的 WorkBuddy / dsh / Claude / Codex 目录，主程序已装入 ~/.hiremate/skill/hiremate；"
                "安装宿主客户端后重跑 install_plugin.py 即可自动挂载（不会凭空创建空宿主目录）。"
            )
        return out_res

    # 2. 显式单宿主定向安装模式
    effective_target = "workbuddy" if target == "auto" else target
    skill_src = (
        OPEN_ROOT / "dsh" / "skills" / "hiremate" / "SKILL.md"
        if effective_target == "dsh" and dest_root
        else primary_skill_md
    )
    host_root = _host_root_dir(effective_target, dest_root)
    dest_dir = (
        host_root / "plugins" / "hiremate" / "skills" / "hiremate"
        if effective_target == "antigravity"
        else host_root / "skills" / "hiremate"
    )
    planned = [str(dest_dir / "SKILL.md")] + [str(dest_dir / item) for item in RUNTIME_FILES]

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "target": effective_target,
            "dest_dir": str(dest_dir),
            "planned_paths": planned,
        }

    dsh_reg: dict[str, Any] | None = None
    codex_reg: dict[str, Any] | None = None
    agy_reg: dict[str, Any] | None = None
    if dest_root:
        _copy_runtime_into(dest_dir, skill_src)
        link_mode = "copy"
        canon_str = None
        if effective_target == "dsh":
            local_bundle = host_root / "dsh-plugin"
            _install_dsh_bundle_files(local_bundle, dest_dir)
            dsh_reg = _register_dsh_host_plugin(host_root, local_bundle)
        elif effective_target == "codex":
            codex_reg = _register_codex_host_plugin(host_root, host_root / ".hiremate-local", dest_dir)
        elif effective_target == "antigravity":
            agy_reg = _register_antigravity_sidecar(
                host_root,
                _canonical_home_dir(canonical_home),
                cli_skill_dir=dest_dir,
                embed_skill_link=False,
            )
    else:
        canon_home = _canonical_home_dir(canonical_home)
        canon_dir = _canonical_skill_dir(canonical_home)
        dsh_plugin_dir = _canonical_dsh_plugin_dir(canonical_home)
        _copy_runtime_into(canon_dir, primary_skill_md)
        _install_dsh_bundle_files(dsh_plugin_dir, canon_dir)
        canon_str = str(canon_dir)
        if effective_target == "antigravity":
            agy_reg = _register_antigravity_sidecar(host_root, canon_home, cli_skill_dir=canon_dir)
            link_mode = str(agy_reg.get("link_mode") or "symlink")
        else:
            link_mode = _link_skill_dir(canon_dir, dest_dir)
            if effective_target == "dsh":
                dsh_reg = _register_dsh_host_plugin(host_root, dsh_plugin_dir)
            elif effective_target == "codex":
                codex_reg = _register_codex_host_plugin(host_root, canon_home, canon_dir)

    single_sb_regs: dict[str, Any] = {}
    if dsh_reg:
        single_sb_regs["dsh"] = dsh_reg
    if codex_reg:
        single_sb_regs["codex"] = codex_reg
    if agy_reg:
        single_sb_regs["antigravity"] = agy_reg
    single_sb_guide = _build_sidebar_install_guide(effective_target, single_sb_regs)

    res: dict[str, Any] = {
        "ok": True,
        "dry_run": False,
        "target": effective_target,
        "dest_dir": str(dest_dir),
        "link_mode": link_mode,
        "installed_paths": planned,
        "sidebar_guide": single_sb_guide,
        "next_step": single_sb_guide["next_step"],
    }
    if canon_str:
        res["canonical_dir"] = canon_str
    if dsh_reg:
        res["dsh_slash_commands"] = dsh_reg
    if codex_reg:
        res["codex_plugin"] = codex_reg
    if agy_reg:
        res["antigravity_sidecar"] = agy_reg
    return res


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装「求职助手」插件与侧边栏工作台到桌面 Agent（默认自动探测全宿主同步）")
    parser.add_argument(
        "--target",
        default="auto",
        choices=("auto", "workbuddy", "dsh", "antigravity", "agents", "claude", "codex", "cursor", "windsurf"),
        help="目标宿主（默认 auto：安装一份主副本至 ~/.hiremate/skill/hiremate 并挂载至全部已安装宿主）",
    )
    parser.add_argument("--dest-root", help="可选覆盖单宿主根目录（测试或自定义目录用）")
    parser.add_argument("--canonical-home", help="可选覆盖主副本根目录（默认 ~/.hiremate）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览将要写入和软链的路径，不修改磁盘")
    parser.add_argument("--uninstall", action="store_true", help="卸载全部宿主软链、侧边栏入口与插件运行文件（默认保留本地求职数据）")
    parser.add_argument("--purge-data", action="store_true", help="配合 --uninstall 一并删除 ~/.hiremate 下的求职账本与凭证")
    parser.add_argument("--search-roots-json", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    parsed_roots: dict[str, str | Path] | None = None
    if args.search_roots_json:
        raw_roots = json.loads(args.search_roots_json)
        if not isinstance(raw_roots, dict):
            raise ValueError("--search-roots-json 必须为对象字典")
        parsed_roots = {str(k): str(v) for k, v in raw_roots.items()}

    if args.uninstall:
        result = uninstall(
            canonical_home=args.canonical_home,
            search_roots=parsed_roots,
            purge_data=args.purge_data,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    result = install(
        target=args.target,
        dest_root=args.dest_root,
        canonical_home=args.canonical_home,
        search_roots=parsed_roots,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
