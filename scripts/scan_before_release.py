#!/usr/bin/env python3
"""开源发布前安全扫描：确保 open/ 目录零个人隐私、零本地账本凭证、零闭源策略泄漏。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

OPEN_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_EXTENSIONS = {
    ".sqlite3",
    ".sqlite",
    ".db",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".xlsx",
    ".csv",
    ".pdf",
    ".docx",
    ".doc",
}
FORBIDDEN_COMPOUND_SUFFIXES = (
    ".sqlite3.gz",
    ".sqlite.gz",
    ".db.gz",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".db-wal",
    ".db-shm",
    ".pre_mig_bak",
)
FORBIDDEN_FILENAMES = {
    "credentials.json",
    ".env",
    ".env.local",
    ".hmac_secret",
    "policy_engine.py",
    "portal_html.py",
    "admin_cli.py",
    "billing.py",
    "mailer.py",
}
FORBIDDEN_NAME_RE = re.compile(
    r"^(?:credentials.*\.json|config\.json\.(?:corrupt|bak)-.*|.*\.bak-\d{8}.*|id_rsa.*|id_ed25519.*)$",
    re.IGNORECASE,
)

# 通用明文凭证、敏感配置与闭源模块特征扫描规则（作者本机私有词表存放于主仓 scripts/private_scan_rules.json）
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"hm_live_[0-9a-f]{24,}"),
    re.compile(r"SMTP_PASSWORD\s*=\s*\S+"),
    re.compile(r"XUNHUPAY_(?:ALIPAY_)?APPSECRET\s*=\s*\S+"),
    re.compile(r"EPAY_KEY\s*=\s*\S+"),
    re.compile(r"sk_(?:live|test)_[0-9A-Za-z]{16,}"),
    re.compile(r"whsec_[0-9A-Za-z]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?:ghp_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,})"),
    re.compile(r"def complete_payment_order\("),
    re.compile(r"def _handle_xunhupay_notify\("),
)


def _load_effective_patterns(private_rules_path: Path | None = None) -> list[re.Pattern[str]]:
    patterns = list(FORBIDDEN_PATTERNS)
    private_rules_file = (
        private_rules_path.expanduser().resolve()
        if private_rules_path
        else (OPEN_ROOT.parent / "scripts" / "private_scan_rules.json")
    )
    if private_rules_file.is_file():
        try:
            raw = json.loads(private_rules_file.read_text(encoding="utf-8"))
            for expr in raw.get("forbidden_regexes") or []:
                if isinstance(expr, str) and expr.strip():
                    patterns.append(re.compile(expr.strip()))
        except Exception as exc:
            raise RuntimeError(f"读取私有扫描规则失败 ({private_rules_file}): {exc}") from exc
    return patterns


SKIP_DIRS = {"__pycache__", ".git", "dist", ".workspace", ".pytest_cache"}
SKIP_OS_METADATA = {".DS_Store", "Thumbs.db"}


def scan(root: Path = OPEN_ROOT, *, private_rules_path: Path | None = None) -> dict:
    issues: list[str] = []
    scanned_files = 0
    effective_patterns = _load_effective_patterns(private_rules_path=private_rules_path)
    self_resolved = Path(__file__).resolve()

    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if path.name in SKIP_OS_METADATA:
            continue
        rel = str(path.relative_to(root))
        if path.is_symlink():
            issues.append(f"禁止发布的符号链接：{rel}")
            continue
        if path.is_dir():
            continue

        scanned_files += 1

        if path.name in FORBIDDEN_FILENAMES or FORBIDDEN_NAME_RE.match(path.name):
            issues.append(f"禁止发布的文件名：{rel}")
            continue
        if path.suffix.lower() in FORBIDDEN_EXTENSIONS or rel.lower().endswith(FORBIDDEN_COMPOUND_SUFFIXES):
            issues.append(f"禁止发布的数据库、简历账本或密钥后缀：{rel}")
            continue

        if path.resolve() == self_resolved or path.name == "scan_before_release.py":
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            issues.append(f"禁止发布的非 UTF-8 二进制文件：{rel}")
            continue

        for pat in effective_patterns:
            if pat.search(text):
                issues.append(f"命中隐私或违规词规则 ({pat.pattern})：{rel}")

    return {
        "ok": len(issues) == 0,
        "scanned_files": scanned_files,
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="开源发布前隐私与闭源防泄露扫描")
    parser.add_argument("--root", default=str(OPEN_ROOT), help="待扫描目录路径")
    parser.add_argument("--private-rules", help="可选外部私有正则规则 JSON 文件路径")
    args = parser.parse_args(argv)

    priv = Path(args.private_rules) if args.private_rules else None
    result = scan(Path(args.root).expanduser().resolve(), private_rules_path=priv)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
