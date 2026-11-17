#!/usr/bin/env python3
"""求职助手（hiremate）多宿主安装器（oil-cli 同款单源 Canonical + 目录软链同步 + DSH 原生斜杠命令挂载）。

- 默认运行 `python3 install_plugin.py`（即 `--target auto`）：
  1. 将唯一真实运行时与技能指令写入 `~/.hiremate/skill/hiremate`；
  2. 将 DeepSeek Harness 原生斜杠命令插件写入 `~/.hiremate/dsh-plugin` 并自动幂等挂载至已安装的 DSH Profile；
  3. 自动探测本机已安装的桌面 Agent 宿主（WorkBuddy、DeepSeek Harness、Claude Code、Codex、通用 ~/.agents），
     建立目录软链接指向 `~/.hiremate/skill/hiremate`（一次升级、全端同步生效）。
- 显式传入 `--target workbuddy|dsh`（及可选 `--dest-root`）时，保持与定向安装模式完全兼容。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
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
    "package.json",
    "cordis.patch.yml",
)

DSH_PATCH_BEGIN = "# hiremate-dsh:begin"
DSH_PATCH_END = "# hiremate-dsh:end"

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


def _canonical_home_dir(canonical_home: str | None = None) -> Path:
    base_raw = canonical_home or os.environ.get("HIREMATE_HOME") or "~/.hiremate"
    return Path(base_raw).expanduser().resolve()


def _canonical_skill_dir(canonical_home: str | None = None) -> Path:
    return _canonical_home_dir(canonical_home) / "skill" / "hiremate"


def _canonical_dsh_plugin_dir(canonical_home: str | None = None) -> Path:
    return _canonical_home_dir(canonical_home) / "dsh-plugin"


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
    """安全移除目录软链、Windows Junction 或实体目录（兼容 Python 3.9~3.13，绝不误穿透 Junction）。"""
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
    """建立目录软链（若已指向同一主副本则幂等保留，防 Windows CWD 句柄占用报错；无权限时回退 Junction 或拷贝）。"""
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
    """将 open/dsh 原生斜杠命令包写入 ~/.hiremate/dsh-plugin 并将其内部 skills/hiremate 指向主副本。"""
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


def _merge_dsh_cordis_patch(patch_file: Path, index_mjs_path: Path) -> str:
    """向 DSH cordis.patch.yml 幂等合并 hiremate-dsh 插件入口（绝不破坏用户已有配置）。"""
    escaped_path = str(index_mjs_path).replace("\\", "/").replace('"', '\\"')
    block = (
        f"{DSH_PATCH_BEGIN}\n"
        f"- id: hiremate-dsh\n"
        f'  name: "{escaped_path}"\n'
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

    if "- id: hiremate-dsh" in existing:
        return "already_present"

    merged = (existing.rstrip() + "\n\n" + block) if stripped else block
    patch_file.write_text(merged, encoding="utf-8")
    return "appended"


def _register_dsh_host_plugin(dsh_root: Path, dsh_plugin_dir: Path) -> dict[str, Any]:
    """将 hiremate-dsh 原生斜杠命令挂载至 DSH 宿主目录（支持 profiles/<name> 热重载与根 patch 兜底）。"""
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
            if nm_dir.is_dir():
                pkg_link = nm_dir / "hiremate-dsh"
                _link_skill_dir(dsh_plugin_dir, pkg_link)
                linked_modules.append(str(pkg_link))
            patch_path = prof / "cordis.patch.yml"
            action = _merge_dsh_cordis_patch(patch_path, index_mjs)
            patched_files.append({"profile": prof.name, "patch_file": str(patch_path), "action": action})

    if not patched_files:
        root_patch = dsh_root / "cordis.patch.yml"
        action = _merge_dsh_cordis_patch(root_patch, index_mjs)
        patched_files.append({"profile": "root", "patch_file": str(root_patch), "action": action})

    return {
        "registered": True,
        "plugin_dir": str(dsh_plugin_dir),
        "patched_files": patched_files,
        "linked_node_modules": linked_modules,
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
    b_idx = existing.find(DSH_PATCH_BEGIN)
    e_idx = existing.find(DSH_PATCH_END)
    if b_idx == -1 or e_idx == -1 or e_idx <= b_idx:
        return False
    cleaned = (existing[:b_idx].rstrip() + "\n" + existing[e_idx + len(DSH_PATCH_END) :].lstrip("\r\n")).strip()
    patch_file.write_text((cleaned + "\n") if cleaned else "", encoding="utf-8")
    return True


def uninstall(
    *,
    canonical_home: str | None = None,
    search_roots: dict[str, str | Path] | None = None,
    purge_data: bool = False,
) -> dict[str, Any]:
    """安全卸载多宿主软链与插件代码，默认完整保留用户的本地求职账本与个人档案。"""
    home_dir = _canonical_home_dir(canonical_home)
    canon_dir = _canonical_skill_dir(canonical_home)
    dsh_plugin_dir = _canonical_dsh_plugin_dir(canonical_home)
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
            profiles_dir = h_root / "profiles"
            if profiles_dir.is_dir():
                for prof in sorted(profiles_dir.iterdir()):
                    if not prof.is_dir():
                        continue
                    if _strip_dsh_cordis_patch(prof / "cordis.patch.yml"):
                        unpatched_files.append(str(prof / "cordis.patch.yml"))
                    pkg_link = prof / "node_modules" / "hiremate-dsh"
                    _remove_link_or_dir(pkg_link)

    for target_dir in (canon_dir, dsh_plugin_dir):
        if target_dir == dsh_plugin_dir and (dsh_plugin_dir / "skills" / "hiremate").exists():
            _remove_link_or_dir(dsh_plugin_dir / "skills" / "hiremate")
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
            f"已卸载求职助手插件软链与运行程序；本地求职账本与档案仍保留在 {home_dir}。"
            if not purge_data
            else f"已彻底卸载求职助手并清理目录 {home_dir}。"
        ),
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

    # 1. 自动多宿主单源软链模式（推荐主流程）
    if target == "auto" and not dest_root:
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
        for h_name, h_root in hosts:
            link_path = h_root / "skills" / "hiremate"
            if not (link_path.resolve() == canon_dir.resolve() and link_path == canon_dir):
                mode = _link_skill_dir(canon_dir, link_path)
                host_entry: dict[str, Any] = {
                    "host": h_name,
                    "skill_dir": str(link_path),
                    "link_mode": mode,
                }
                if h_name == "dsh":
                    reg = _register_dsh_host_plugin(h_root, dsh_plugin_dir)
                    host_entry["dsh_slash_commands"] = reg
                    dsh_registrations.append(reg)
                linked_hosts.append(host_entry)

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
            "next_step": "安装已完成！请对桌面 AI 助手说：『帮我初始化求职助手』",
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
    dest_dir = host_root / "skills" / "hiremate"
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
    if dest_root:
        _copy_runtime_into(dest_dir, skill_src)
        link_mode = "copy"
        canon_str = None
        if effective_target == "dsh":
            local_bundle = host_root / "dsh-plugin"
            _install_dsh_bundle_files(local_bundle, dest_dir)
            dsh_reg = _register_dsh_host_plugin(host_root, local_bundle)
    else:
        canon_dir = _canonical_skill_dir(canonical_home)
        dsh_plugin_dir = _canonical_dsh_plugin_dir(canonical_home)
        _copy_runtime_into(canon_dir, primary_skill_md)
        _install_dsh_bundle_files(dsh_plugin_dir, canon_dir)
        link_mode = _link_skill_dir(canon_dir, dest_dir)
        canon_str = str(canon_dir)
        if effective_target == "dsh":
            dsh_reg = _register_dsh_host_plugin(host_root, dsh_plugin_dir)

    res: dict[str, Any] = {
        "ok": True,
        "dry_run": False,
        "target": effective_target,
        "dest_dir": str(dest_dir),
        "link_mode": link_mode,
        "installed_paths": planned,
    }
    if canon_str:
        res["canonical_dir"] = canon_str
    if dsh_reg:
        res["dsh_slash_commands"] = dsh_reg
    return res


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装「求职助手」插件到桌面 Agent（默认自动探测全宿主软链同步）")
    parser.add_argument(
        "--target",
        default="auto",
        choices=("auto", "workbuddy", "dsh", "antigravity", "agents", "claude", "codex", "cursor", "windsurf"),
        help="目标宿主（默认 auto：安装一份主副本至 ~/.hiremate/skill/hiremate 并软链至全部已安装宿主）",
    )
    parser.add_argument("--dest-root", help="可选覆盖单宿主根目录（测试或自定义目录用）")
    parser.add_argument("--canonical-home", help="可选覆盖主副本根目录（默认 ~/.hiremate）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览将要写入和软链的路径，不修改磁盘")
    parser.add_argument("--uninstall", action="store_true", help="卸载全部宿主软链与插件运行文件（默认保留本地求职数据）")
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
