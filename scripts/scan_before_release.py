#!/usr/bin/env python3
"""开源发布前安全扫描：确保 open/ 目录零个人隐私、零本地账本凭证、零闭源策略泄漏。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys

OPEN_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_EXTENSIONS = {".sqlite3", ".sqlite", ".db", ".pem", ".key"}
FORBIDDEN_COMPOUND_SUFFIXES = (".sqlite3.gz", ".sqlite.gz", ".db.gz")
FORBIDDEN_FILENAMES = {"credentials.json", ".env", ".env.local", ".hmac_secret", "policy_engine.py"}

# 通用明文凭证与敏感配置扫描规则（作者本机私有词表存放于主仓非导出目录 scripts/private_scan_rules.json）
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"hm_live_[0-9a-f]{24,}"),
    re.compile(r"SMTP_PASSWORD\s*=\s*\S+"),
    re.compile(r"sk_(?:live|test)_[0-9A-Za-z]{16,}"),
    re.compile(r"whsec_[0-9A-Za-z]{16,}"),
)


def _load_effective_patterns() -> list[re.Pattern[str]]:
    patterns = list(FORBIDDEN_PATTERNS)
    private_rules_file = OPEN_ROOT.parent / "scripts" / "private_scan_rules.json"
    if private_rules_file.is_file():
        try:
            raw = json.loads(private_rules_file.read_text(encoding="utf-8"))
            for expr in raw.get("forbidden_regexes") or []:
                if isinstance(expr, str) and expr.strip():
                    patterns.append(re.compile(expr.strip()))
        except Exception as exc:
            raise RuntimeError(f"读取私有扫描规则失败 ({private_rules_file}): {exc}") from exc
    return patterns

SKIP_DIRS = {"__pycache__", ".git", "dist", ".workspace"}


def scan(root: Path = OPEN_ROOT) -> dict:
    issues: list[str] = []
    scanned_files = 0
    effective_patterns = _load_effective_patterns()

    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if path.is_dir():
            continue

        scanned_files += 1
        rel = str(path.relative_to(root))

        if path.name in FORBIDDEN_FILENAMES:
            issues.append(f"禁止发布的文件名：{rel}")
            continue
        if path.suffix.lower() in FORBIDDEN_EXTENSIONS or rel.lower().endswith(FORBIDDEN_COMPOUND_SUFFIXES):
            issues.append(f"禁止发布的数据库或密钥后缀：{rel}")
            continue

        if path.resolve() == Path(__file__).resolve():
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for pat in effective_patterns:
            if pat.search(text):
                issues.append(f"命中隐私或违规词规则 ({pat.pattern})：{rel}")

    return {
        "ok": len(issues) == 0,
        "scanned_files": scanned_files,
        "issues": issues,
    }


def main() -> int:
    result = scan(OPEN_ROOT)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
