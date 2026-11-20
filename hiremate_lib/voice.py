"""个人求职语气档案（Voice Profile）：从本机桌面 Agent 历史对话提取用户真实行文指纹，驱动破冰、答疑与回复去模板化。

设计边界：
1. 纯本机离线读写（仅存于 <workspace>/voice_profile.md 与临时语料 <workspace>/voice_corpus.md，绝不上传服务端）；
2. 零第三方依赖（兼容 Python 3.9+ 标准库；多帧 .zstd/.zst 优先走 Python 3.14 compression.zstd，低版本自动降级系统 zstd CLI 或跳过）；
3. 针对招聘沟通场景做「语域投射降噪」：硬剔除代码块、报错日志、JSON、URL 及粘贴的外部岗位 JD，按「短句对话桶 + 成段阐述桶」均衡抽样；
4. 严格服从四级优先级：云端 guard-reply 风控与求职常识红线 > profile_facts.md 真实履历事实 > voice_profile.md 本人语气习惯 > 默认话术模板。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from .workspace import atomic_write_text

SUPPORTED_VOICE_HOSTS: tuple[str, ...] = (
    "dsh",
    "claude",
    "codex",
    "antigravity",
    "workbuddy",
)

NO_VOICE_PROFILE_HINT = (
    "尚未建立个人求职语气档案（这不是错误：请照常按求职助手默认得体自然口语沟通，无需打断或追问用户；"
    "当用户希望让破冰与回复更像本人说话风格时，可运行 `hiremate.py voice collect` 从本机历史对话提炼建档，"
    "或在半自动审阅用户修改措辞时调用 `hiremate.py voice note` 持续沉淀）。"
)

VOICE_EXTRACTION_RULES_PROMPT = """请阅读 `corpus_path` 中的本机历史消息样本，为用户建立《个人求职语气档案》（控制在 500～650 字以内，便于高频搜岗与后台巡检常驻）：
1. **双证据防幻觉硬约束**：每条写入档案的习惯，必须在样本中找到 **至少 2 处真实证据**（附上简短原词/短句例证）；找不到的特征严禁凭印象脑补。样本偏少时直接注明「样本较少，置信度偏低」。
2. **语域投射过滤（切勿把对 AI 下指令的甲方姿态搬去对 HR 说话！）**：
   - 忽略祈使命令（如“帮我改下”“直接跑”）中的急躁情绪或命令口吻；
   - 重点提取跨场景稳定的**语言指纹**：单句长短与断句节奏、句末标点习惯（是否习惯省略句末句号、是否从不用波浪号 `～` 或感叹号 `！`）、中英文与数字间空格习惯、口语连接词偏好（如“其实/主要/这块/基本”）、专业术语中英混排习惯（如说 `Agent` 还是 `智能体`）、笃定 vs 留余地的表达方式、以及明显排斥从不使用的套话（如“赋能/抓手”）。
3. **映射为 BOSS 求职双档声线**（严守纯文本、<=110 字、禁 Markdown 加粗与 `1. 2.` 列表、禁“贵司/尊敬的”、禁泄露多版简历文件名等红线）：
   - **初聊礼貌档（对陌生 HR / 猎头破冰与首回）**：保留本人用词与标点习惯的同时保持得体客气；并定制一句符合本人语感的「递简历附言」（替代千篇一律的默认句）；
   - **同行交流档（建联后对业务负责人 / 总监聊实战闭环）**：回归本人讲业务/技术时最自然的平实短句节奏。
4. 按以下固定一级与二级标题结构生成 Markdown，先调用 `hiremate.py voice save --text "<档案全文>"` 保存，再展示给用户过目（用户要求修改哪条，改后重新 `save`）：

# 个人求职语气档案
更新：YYYY-MM-DD · 样本来源：<N> 条本机对话语料

## 1. 句长节奏与标点指纹
## 2. 称呼、开场与收尾习惯
## 3. 高频用词与排斥套话
## 4. 分场景求职声线（初聊 HR vs 同行聊业务 vs 递简历附言）
## 5. 审阅与改稿中学到的
## 6. 本人真实语感参照
"""

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_MAX_RAW_FILE_BYTES = 25 * 1024 * 1024
_MAX_DECOMPRESSED_BYTES = 32 * 1024 * 1024
_MAX_SESSIONS_PER_HOST = 100

_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
_INLINE_USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*([\s\S]*?)\s*</USER_REQUEST>", re.IGNORECASE)
_SYSTEM_XML_BLOCK_RE = re.compile(
    r"<(environment_context|INSTRUCTIONS|permissions|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|"
    r"SYSTEM_MESSAGE|command-message|command-name|command-args|local-command-[\w-]+|system-reminder)(?:\s[^>]*)?>[\s\S]*?</\1>",
    re.IGNORECASE,
)
_SLASH_CMD_PREFIX_RE = re.compile(r"^/[a-zA-Z0-9_:-]+\s*")
_URL_RE = re.compile(r"https?://\S+")
_ABS_PATH_RE = re.compile(r"(?:/Users/[^\s`\"')]+|/home/[^\s`\"')]+|[A-Za-z]:\\[^\s`\"')]+)")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CN_ID_RE = re.compile(
    r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?![0-9A-Za-z])"
)
_SECRET_TOKEN_RE = re.compile(
    r"(?:"
    r"\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    r"\bhm_live_[A-Za-z0-9_-]{10,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b|"
    r"\bAIza[0-9A-Za-z_-]{30,}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"
    r")"
)
_SECRET_KV_RE = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key|private[_-]?key)\s*[:=]\s*[^\s,;'\"\}]{6,}|"
    r"(?:密码|口令|密钥)\s*[:：=]\s*[^\s，,。；;'\"\}]{4,})"
)

_JD_PASTE_CUES_RE = re.compile(
    r"(?:岗位职责|职位职责|工作职责|职位描述|岗位描述)[\s\S]{10,1200}(?:任职要求|任职资格|岗位要求|职位要求)|"
    r"(?:任职要求|任职资格|岗位要求)[\s\S]{10,800}(?:加分项|福利待遇|工作地点)"
)
_RESUME_PASTE_CUES_RE = re.compile(
    r"(?:教育背景|教育经历)[\s\S]{10,1500}(?:工作经历|项目经历|个人总结)|"
    r"(?:工作经历|工作经验)[\s\S]{10,1500}(?:项目经历|项目经验)"
)
_LOG_OR_CODE_LINE_RE = re.compile(
    r"^(?:Traceback \(most recent call last\)|File \".*\", line \d+|\s*at [\w$.]+\s*\(|"
    r"\s*(?:import|from|def|class|const|let|var|function|export|return)\s+\w+|"
    r"\s*[\[{].*[\]}]\s*$|diff --git |@@ -\d+)"
)
_PURE_SHORT_DIRECTIVE_RE = re.compile(
    r"^(?:继续|好的|好|行|可以|确认|没问题|ok|yes|y|跑一下|试一下|提交吧|推上去|再试一次|"
    r"[123456789]|帮我(?:跑|看|查|改|修|写|生成|执行|测试|检查|初始化|打开)[\u4e00-\u9fa5a-zA-Z0-9_\s]{0,14})[。！!？?～~\s]*$",
    re.IGNORECASE,
)


def voice_profile_path(workspace: Path) -> Path:
    return workspace / "voice_profile.md"


def voice_corpus_path(workspace: Path) -> Path:
    return workspace / "voice_corpus.md"


def _today_str() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def read_voice_profile(workspace: Path) -> str | None:
    path = voice_profile_path(workspace)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text if text else None


def inspect_voice_status(workspace: Path) -> dict[str, Any]:
    """返回语气档案状态与精简摘要（供 config show、看板与斜杠状态栏零开销展示）。"""
    p_file = voice_profile_path(workspace)
    c_file = voice_corpus_path(workspace)
    raw = read_voice_profile(workspace)
    if not raw:
        return {
            "ready": False,
            "path": str(p_file),
            "corpus_exists": c_file.is_file(),
            "corpus_path": str(c_file),
            "updated_line": "",
            "learned_notes_count": 0,
            "summary": "",
        }

    m_upd = re.search(r"^(?:更新[:：]|Updated:\s*)(.+)$", raw, flags=re.MULTILINE)
    updated_line = m_upd.group(1).strip() if m_upd else ""

    notes_cnt = 0
    m_notes = re.search(
        r"^##\s+(?:5\.\s*)?(?:审阅与改稿中学到的|对话中学到的|Learned in chat)[^\n]*\n([\s\S]*?)(?=\n##\s+|\Z)",
        raw,
        flags=re.MULTILINE,
    )
    if m_notes:
        notes_cnt = sum(1 for ln in m_notes.group(1).splitlines() if ln.strip().startswith("- "))

    bullet_snippets: list[str] = []
    for ln in raw.splitlines():
        s = ln.strip()
        if s.startswith(("- ", "* ")) and len(s) > 4:
            clean_b = re.sub(r"\*\*([^*]+)\*\*", r"\1", s[2:].strip())
            if not re.match(r"^20\d{2}-\d{2}-\d{2}[:：]", clean_b):
                bullet_snippets.append(clean_b[:64])
            if len(bullet_snippets) >= 3:
                break

    summary_str = "；".join(bullet_snippets)[:160]
    return {
        "ready": True,
        "path": str(p_file),
        "corpus_exists": c_file.is_file(),
        "corpus_path": str(c_file),
        "updated_line": updated_line,
        "learned_notes_count": notes_cnt,
        "summary": summary_str,
        "char_count": len(raw),
    }


def save_voice_profile(workspace: Path, content: str) -> dict[str, Any]:
    cleaned = (content or "").strip()
    if not cleaned:
        raise ValueError("语气档案内容不能为空")
    if not cleaned.startswith("# "):
        raise ValueError("语气档案须以一级标题开头（例如 `# 个人求职语气档案`）")
    workspace.mkdir(parents=True, exist_ok=True)
    target = voice_profile_path(workspace)
    atomic_write_text(target, cleaned + "\n", mode=0o600)
    status = inspect_voice_status(workspace)
    return {
        "ok": True,
        "action": "save",
        "voice_profile_file": str(target),
        "voice_status": status,
        "message": "个人求职语气档案已保存至本机。请将档案要点展示给用户过目确认。",
    }


def add_voice_note(workspace: Path, note_text: str) -> dict[str, Any]:
    """在「审阅与改稿中学到的」小节追加一条用户措辞修改记录。"""
    note_clean = re.sub(r"\s+", " ", (note_text or "").strip())
    if not note_clean:
        raise ValueError("note 内容不能为空")
    day = _today_str()
    existing = read_voice_profile(workspace)
    section_header = "## 5. 审阅与改稿中学到的"

    if not existing:
        body = (
            f"# 个人求职语气档案\n"
            f"更新：{day} · 样本来源：审阅与对话实时记录\n\n"
            f"{section_header}\n"
            f"- {day}：{note_clean}\n"
        )
    else:
        sec_pat = re.compile(
            r"(^##\s+(?:5\.\s*)?(?:审阅与改稿中学到的|对话中学到的|Learned in chat)[^\n]*\n[\s\S]*?)(?=\n##\s+|\Z)",
            flags=re.MULTILINE,
        )
        m = sec_pat.search(existing)
        if m:
            sec_block = m.group(1).rstrip() + f"\n- {day}：{note_clean}\n"
            body = existing[: m.start(1)] + sec_block + existing[m.end(1) :]
        else:
            body = existing.rstrip() + f"\n\n{section_header}\n- {day}：{note_clean}\n"
        body = re.sub(
            r"^(?:更新[:：]|Updated:\s*).*$",
            f"更新：{day} · 含最新审阅改稿偏好",
            body,
            count=1,
            flags=re.MULTILINE,
        )

    workspace.mkdir(parents=True, exist_ok=True)
    target = voice_profile_path(workspace)
    atomic_write_text(target, body.rstrip() + "\n", mode=0o600)
    return {
        "ok": True,
        "action": "note",
        "voice_profile_file": str(target),
        "added_note": f"{day}：{note_clean}",
        "voice_status": inspect_voice_status(workspace),
    }


def clear_voice_data(workspace: Path, *, corpus_only: bool = False) -> dict[str, Any]:
    removed: list[str] = []
    c_file = voice_corpus_path(workspace)
    p_file = voice_profile_path(workspace)
    if c_file.is_file():
        c_file.unlink()
        removed.append(str(c_file))
    if not corpus_only and p_file.is_file():
        p_file.unlink()
        removed.append(str(p_file))
    return {
        "ok": True,
        "action": "clear",
        "corpus_only": corpus_only,
        "removed_files": removed,
    }


def _decompress_zstd_bytes(raw: bytes) -> str:
    """解压单帧或多帧拼接的 .zstd/.zst 会话日志（优先 Python 3.14 标准库，回退系统 zstd CLI，带解压体积上限）。"""
    if not raw or len(raw) > _MAX_RAW_FILE_BYTES:
        return ""
    try:
        from compression import zstd as _py_zstd  # type: ignore[import-not-found]

        starts: list[int] = []
        idx = 0
        while True:
            pos = raw.find(_ZSTD_MAGIC, idx)
            if pos == -1:
                break
            starts.append(pos)
            idx = pos + 1
        if not starts:
            dec = _py_zstd.decompress(raw)
            if len(dec) > _MAX_DECOMPRESSED_BYTES:
                dec = dec[:_MAX_DECOMPRESSED_BYTES]
            return dec.decode("utf-8", errors="ignore")
        out_parts: list[str] = []
        total_bytes = 0
        from_pos = starts[0]
        for k in range(1, len(starts) + 1):
            to_pos = starts[k] if k < len(starts) else len(raw)
            try:
                chunk = _py_zstd.decompress(raw[from_pos:to_pos])
                total_bytes += len(chunk)
                if total_bytes > _MAX_DECOMPRESSED_BYTES:
                    keep = max(0, _MAX_DECOMPRESSED_BYTES - (total_bytes - len(chunk)))
                    if keep > 0:
                        out_parts.append(chunk[:keep].decode("utf-8", errors="ignore"))
                    break
                out_parts.append(chunk.decode("utf-8", errors="ignore"))
                from_pos = to_pos
            except Exception:
                if to_pos == len(raw):
                    break
        if out_parts:
            return "".join(out_parts)
    except Exception:
        pass

    zstd_bin = shutil.which("zstd")
    if zstd_bin:
        try:
            proc = subprocess.Popen(
                [zstd_bin, "-dc", "-q"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                if proc.stdin:
                    proc.stdin.write(raw)
                    proc.stdin.close()
                out_buf = proc.stdout.read(_MAX_DECOMPRESSED_BYTES) if proc.stdout else b""
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
                proc.wait(timeout=2)
                return ""
            finally:
                if proc.stdout:
                    proc.stdout.close()
            if out_buf:
                return out_buf.decode("utf-8", errors="ignore")
        except Exception:
            pass
    return ""


def _read_session_text_bounded(path: Path) -> tuple[float, str]:
    """安全读取单份会话日志（跳过符号链接与超大文件，防止磁盘扫描或解压炸弹阻塞）。"""
    try:
        if path.is_symlink():
            return 0.0, ""
        st = path.stat()
        if st.st_size <= 0 or st.st_size > _MAX_RAW_FILE_BYTES:
            return 0.0, ""
        mtime = st.st_mtime
        if path.name.endswith((".zstd", ".zst")):
            return mtime, _decompress_zstd_bytes(path.read_bytes())
        return mtime, path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0.0, ""


def _collect_bounded_files(
    base_dir: Path,
    *,
    suffixes: tuple[str, ...],
    cutoff_ts: float,
    max_depth: int = 4,
    limit: int = _MAX_SESSIONS_PER_HOST,
) -> list[Path]:
    """不跟随符号链接、带目录深度与最新数量上限的会话文件枚举器。"""
    if not base_dir.is_dir() or base_dir.is_symlink():
        return []
    found: list[tuple[float, Path]] = []
    base_parts = len(base_dir.parts)
    skip_dirs = {"node_modules", ".git", "__pycache__", "cache", "tmp"}
    for cur_root, dirs, files in os.walk(base_dir, topdown=True, followlinks=False):
        cur_path = Path(cur_root)
        rel_depth = len(cur_path.parts) - base_parts
        if rel_depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".") and d not in skip_dirs and not (cur_path / d).is_symlink()
            ]
        for fname in files:
            if not fname.endswith(suffixes):
                continue
            fpath = cur_path / fname
            try:
                if fpath.is_symlink():
                    continue
                st = fpath.stat()
                if st.st_mtime >= cutoff_ts and 0 < st.st_size <= _MAX_RAW_FILE_BYTES:
                    found.append((st.st_mtime, fpath))
            except OSError:
                continue
    found.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in found[:limit]]


def _clean_user_utterance(raw_text: str) -> str | None:
    """清洗单条用户历史输入，剔除代码、系统注入、粘贴的岗位 JD/简历及纯机械短指令。"""
    if not raw_text:
        return None
    text = raw_text.strip()
    m_req = _INLINE_USER_REQ_RE.search(text)
    if m_req:
        text = m_req.group(1).strip()

    text = _SYSTEM_XML_BLOCK_RE.sub(" ", text)
    if not text or text.startswith(("<environment_context>", "<INSTRUCTIONS>", "<task-notification>")):
        return None

    text = _FENCED_CODE_RE.sub(" ", text)
    text = _SLASH_CMD_PREFIX_RE.sub("", text).strip()
    if not text:
        return None

    # 排除用户直接粘贴进聊天框的招聘 JD 或整份简历原文，防止他人公文腔污染本人语气
    if _JD_PASTE_CUES_RE.search(text) or _RESUME_PASTE_CUES_RE.search(text):
        return None

    kept_lines: list[str] = []
    code_like_lines = 0
    raw_lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not raw_lines:
        return None

    for ln in raw_lines:
        if _LOG_OR_CODE_LINE_RE.search(ln):
            code_like_lines += 1
            continue
        if ln.lstrip().startswith(("> ", "| ")):
            continue
        kept_lines.append(ln.strip())

    if not kept_lines or (len(raw_lines) >= 4 and code_like_lines / len(raw_lines) > 0.45):
        return None

    cleaned = "\n".join(kept_lines)
    cleaned = _SECRET_TOKEN_RE.sub("[密钥]", cleaned)
    cleaned = _SECRET_KV_RE.sub("[敏感凭证]", cleaned)
    cleaned = _URL_RE.sub("", cleaned)
    cleaned = _ABS_PATH_RE.sub("[路径]", cleaned)
    cleaned = _CN_ID_RE.sub("[身份证号]", cleaned)
    cleaned = _PHONE_RE.sub("[手机号]", cleaned)
    cleaned = _EMAIL_RE.sub("[邮箱]", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    if len(cleaned) < 16 or len(cleaned) > 1200:
        return None
    if _PURE_SHORT_DIRECTIVE_RE.match(cleaned):
        return None

    # 要求包含基本中文字符或自然英文句子，排除纯符号/纯配置串
    cjk_cnt = len(re.findall(r"[\u4e00-\u9fa5]", cleaned))
    alpha_words = len(re.findall(r"[A-Za-z]{2,}", cleaned))
    if cjk_cnt < 8 and alpha_words < 6:
        return None

    return cleaned


def _parse_epoch_ms_or_iso(val: Any, fallback_ts: float) -> float:
    if isinstance(val, (int, float)) and val > 0:
        return float(val) / 1000.0 if val > 1e11 else float(val)
    if isinstance(val, str) and val.strip():
        s = val.strip()
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            pass
    return fallback_ts


def _scan_dsh_messages(root: Path, cutoff_ts: float) -> tuple[int, list[dict[str, Any]]]:
    sessions_dir = root / "sessions"
    if not sessions_dir.is_dir() or sessions_dir.is_symlink():
        return 0, []
    dated_files: list[tuple[float, Path]] = []
    for ws in sorted(sessions_dir.iterdir()):
        if not ws.is_dir() or ws.is_symlink():
            continue
        for sdir in sorted(ws.iterdir()):
            if not sdir.is_dir() or sdir.is_symlink() or not sdir.name.startswith("session-"):
                continue
            candidates: list[tuple[int, float, Path]] = []
            for f in sdir.iterdir():
                m = re.match(r"^session\.v(\d+)\.jsonl(?:\.zstd)?$", f.name)
                if m and f.is_file() and not f.is_symlink():
                    try:
                        st = f.stat()
                        if st.st_mtime >= cutoff_ts and 0 < st.st_size <= _MAX_RAW_FILE_BYTES:
                            candidates.append((int(m.group(1)), st.st_mtime, f))
                    except OSError:
                        continue
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                dated_files.append((candidates[0][1], candidates[0][2]))

    dated_files.sort(key=lambda x: x[0], reverse=True)
    session_files = [f for _, f in dated_files[:_MAX_SESSIONS_PER_HOST]]

    out: list[dict[str, Any]] = []
    for sf in session_files:
        mtime, raw_text = _read_session_text_bounded(sf)
        if not raw_text:
            continue
        for line in raw_text.splitlines():
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "user/message":
                continue
            data = ev.get("data")
            if not isinstance(data, dict):
                continue
            src = data.get("source")
            if not isinstance(src, dict) or src.get("kind") != "user":
                continue
            blocks = data.get("content")
            if not isinstance(blocks, list):
                continue
            txt = "\n".join(
                str(b.get("text") or "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
            cleaned = _clean_user_utterance(txt)
            if cleaned:
                ts = _parse_epoch_ms_or_iso(ev.get("time"), mtime)
                if ts >= cutoff_ts:
                    out.append({"host": "dsh", "text": cleaned, "ts": ts})
    return len(session_files), out


def _scan_claude_messages(root: Path, cutoff_ts: float) -> tuple[int, list[dict[str, Any]]]:
    out: list[dict[str, Any]] = []
    proj_files = _collect_bounded_files(
        root / "projects",
        suffixes=(".jsonl",),
        cutoff_ts=cutoff_ts,
        max_depth=2,
        limit=_MAX_SESSIONS_PER_HOST,
    )
    files_scanned = 0
    for jf in proj_files:
        mtime, raw_text = _read_session_text_bounded(jf)
        if not raw_text:
            continue
        files_scanned += 1
        for line in raw_text.splitlines():
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "user" or ev.get("isSidechain") is True:
                continue
            msg = ev.get("message")
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                txt = content
            elif isinstance(content, list):
                txt = "\n".join(
                    str(b.get("text") or "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                continue
            cleaned = _clean_user_utterance(txt)
            if cleaned:
                ts = _parse_epoch_ms_or_iso(ev.get("timestamp"), mtime)
                if ts >= cutoff_ts:
                    out.append({"host": "claude", "text": cleaned, "ts": ts})

    hist_file = root / "history.jsonl"
    if hist_file.is_file() and len(out) < 40:
        mtime, hist_text = _read_session_text_bounded(hist_file)
        if hist_text:
            files_scanned += 1
            for line in hist_text.splitlines():
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                disp = ev.get("display")
                if not isinstance(disp, str):
                    continue
                ts = _parse_epoch_ms_or_iso(ev.get("timestamp"), mtime)
                if ts < cutoff_ts:
                    continue
                cleaned = _clean_user_utterance(disp)
                if cleaned:
                    out.append({"host": "claude", "text": cleaned, "ts": ts})
    return files_scanned, out


def _scan_codex_messages(root: Path, cutoff_ts: float) -> tuple[int, list[dict[str, Any]]]:
    candidates = _collect_bounded_files(
        root / "sessions",
        suffixes=(".jsonl",),
        cutoff_ts=cutoff_ts,
        max_depth=4,
        limit=_MAX_SESSIONS_PER_HOST,
    )
    if len(candidates) < 60:
        arch_extra = _collect_bounded_files(
            root / "archived_sessions",
            suffixes=(".jsonl", ".jsonl.zst"),
            cutoff_ts=cutoff_ts,
            max_depth=2,
            limit=max(0, _MAX_SESSIONS_PER_HOST - len(candidates)),
        )
        candidates.extend(arch_extra[:40])

    out: list[dict[str, Any]] = []
    for cf in candidates:
        mtime, raw_text = _read_session_text_bounded(cf)
        if not raw_text:
            continue
        for line in raw_text.splitlines():
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "response_item":
                continue
            payload = ev.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "user":
                continue
            blocks = payload.get("content")
            if not isinstance(blocks, list):
                continue
            txt = "\n".join(
                str(b.get("text") or "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") in ("input_text", "text")
            )
            cleaned = _clean_user_utterance(txt)
            if cleaned:
                ts = _parse_epoch_ms_or_iso(ev.get("timestamp"), mtime)
                if ts >= cutoff_ts:
                    out.append({"host": "codex", "text": cleaned, "ts": ts})
    return len(candidates), out


def _scan_antigravity_messages(root: Path, cutoff_ts: float) -> tuple[int, list[dict[str, Any]]]:
    brain_dir = root / "brain"
    if not brain_dir.is_dir() or brain_dir.is_symlink():
        return 0, []
    t_files: list[tuple[float, Path]] = []
    for conv_dir in brain_dir.iterdir():
        if not conv_dir.is_dir() or conv_dir.is_symlink():
            continue
        tf = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
        if tf.is_file() and not tf.is_symlink():
            try:
                st = tf.stat()
                if st.st_mtime >= cutoff_ts and 0 < st.st_size <= _MAX_RAW_FILE_BYTES:
                    t_files.append((st.st_mtime, tf))
            except OSError:
                continue
    t_files.sort(key=lambda x: x[0], reverse=True)
    selected_files = [f for _, f in t_files[:80]]

    out: list[dict[str, Any]] = []
    for tf in selected_files:
        mtime, raw_text = _read_session_text_bounded(tf)
        if not raw_text:
            continue
        for line in raw_text.splitlines():
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "USER_INPUT" or ev.get("source") != "USER_EXPLICIT":
                continue
            content = ev.get("content")
            if not isinstance(content, str):
                continue
            cleaned = _clean_user_utterance(content)
            if cleaned:
                ts = _parse_epoch_ms_or_iso(ev.get("created_at"), mtime)
                if ts >= cutoff_ts:
                    out.append({"host": "antigravity", "text": cleaned, "ts": ts})
    return len(selected_files), out


def _scan_workbuddy_messages(root: Path, cutoff_ts: float) -> tuple[int, list[dict[str, Any]]]:
    out: list[dict[str, Any]] = []
    scanned = 0
    for sub_rel in ("sessions", "history", "conversations"):
        wb_files = _collect_bounded_files(
            root / sub_rel,
            suffixes=(".jsonl",),
            cutoff_ts=cutoff_ts,
            max_depth=4,
            limit=_MAX_SESSIONS_PER_HOST,
        )
        for jf in wb_files:
            mtime, raw_text = _read_session_text_bounded(jf)
            if not raw_text:
                continue
            scanned += 1
            for line in raw_text.splitlines():
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("role") != "user":
                    continue
                content = ev.get("content")
                if isinstance(content, str):
                    cleaned = _clean_user_utterance(content)
                    if cleaned:
                        out.append({"host": "workbuddy", "text": cleaned, "ts": mtime})
    return scanned, out


def _default_host_roots() -> dict[str, Path]:
    home = Path.home()
    return {
        "dsh": Path(os.environ.get("DSH_HOME") or (home / ".dsh")).expanduser(),
        "claude": Path(os.environ.get("CLAUDE_HOME") or (home / ".claude")).expanduser(),
        "codex": Path(os.environ.get("CODEX_HOME") or (home / ".codex")).expanduser(),
        "antigravity": Path(os.environ.get("AGY_DATA_HOME") or (home / ".gemini" / "antigravity")).expanduser(),
        "workbuddy": Path(os.environ.get("WORKBUDDY_HOME") or (home / ".workbuddy")).expanduser(),
    }


def collect_voice_corpus(
    workspace: Path,
    *,
    hosts: list[str] | None = None,
    days: int = 120,
    max_chars: int = 36000,
    host_roots_override: dict[str, str | Path] | None = None,
) -> dict[str, Any]:
    """跨宿主扫描本机历史对话记录，按「短句沟通桶 + 成段阐述桶」去重抽样写入 voice_corpus.md。"""
    target_hosts = (
        [h.strip().lower() for h in hosts if h.strip().lower() in SUPPORTED_VOICE_HOSTS]
        if hosts and hosts != ["auto"]
        else list(SUPPORTED_VOICE_HOSTS)
    )
    if not target_hosts:
        raise ValueError(f"不支持的宿主名称，可选：{', '.join(SUPPORTED_VOICE_HOSTS)} 或 auto")

    roots = _default_host_roots()
    if host_roots_override:
        roots = {
            str(k).lower(): Path(v).expanduser().resolve()
            for k, v in host_roots_override.items()
        }

    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).timestamp()

    scanners = {
        "dsh": _scan_dsh_messages,
        "claude": _scan_claude_messages,
        "codex": _scan_codex_messages,
        "antigravity": _scan_antigravity_messages,
        "workbuddy": _scan_workbuddy_messages,
    }

    per_host_stats: dict[str, dict[str, int]] = {}
    all_raw_msgs: list[dict[str, Any]] = []

    for h_name in target_hosts:
        h_root = roots.get(h_name)
        if not h_root or not h_root.exists():
            continue
        sess_cnt, items = scanners[h_name](h_root, cutoff_ts)
        if sess_cnt > 0 or items:
            per_host_stats[h_name] = {
                "sessions_scanned": sess_cnt,
                "valid_messages": len(items),
            }
            all_raw_msgs.extend(items)

    # 最新优先去重
    all_raw_msgs.sort(key=lambda m: float(m.get("ts", 0.0)), reverse=True)
    seen_norm: set[str] = set()
    unique_msgs: list[dict[str, Any]] = []
    for item in all_raw_msgs:
        norm_key = re.sub(r"\s+", "", str(item["text"])).lower()
        if norm_key in seen_norm:
            continue
        seen_norm.add(norm_key)
        unique_msgs.append(item)

    # 双桶均衡抽样：
    # - 短句自然对话桶（16～140 字，贴合 BOSS 15～85 字日常回复节奏）分配约 55% 预算
    # - 成段业务/观点阐述桶（141～1200 字，体现术语与逻辑结构）分配约 45% 预算
    short_bucket = [m for m in unique_msgs if 16 <= len(m["text"]) <= 140]
    long_bucket = [m for m in unique_msgs if len(m["text"]) > 140]

    # 短句桶优先取信息密度适中（25～110 字）且靠新的消息；长段桶按适度长文+时间新近排序
    short_bucket.sort(key=lambda m: (0 if 24 <= len(m["text"]) <= 110 else 1, -float(m["ts"])))
    long_bucket.sort(key=lambda m: (-min(len(m["text"]), 500), -float(m["ts"])))

    short_budget = int(max_chars * 0.55)
    picked_short: list[dict[str, Any]] = []
    short_chars = 0
    for m in short_bucket:
        ln = len(m["text"])
        if short_chars + ln > short_budget and picked_short:
            continue
        picked_short.append(m)
        short_chars += ln

    remaining_budget = max(0, max_chars - short_chars)
    picked_long: list[dict[str, Any]] = []
    long_chars = 0
    for m in long_bucket:
        ln = len(m["text"])
        if long_chars + ln > remaining_budget and picked_long:
            continue
        picked_long.append(m)
        long_chars += ln

    # 若长段不足，剩余预算回补给短句桶
    extra_budget = max_chars - short_chars - long_chars
    if extra_budget > 100 and len(picked_short) < len(short_bucket):
        picked_ids = {id(x) for x in picked_short}
        for m in short_bucket:
            if id(m) in picked_ids:
                continue
            ln = len(m["text"])
            if short_chars + long_chars + ln > max_chars:
                continue
            picked_short.append(m)
            short_chars += ln

    picked_short.sort(key=lambda m: float(m["ts"]))
    picked_long.sort(key=lambda m: float(m["ts"]))
    total_picked = len(picked_short) + len(picked_long)
    total_chars = short_chars + long_chars

    out_file = voice_corpus_path(workspace)
    if total_picked > 0:
        workspace.mkdir(parents=True, exist_ok=True)
        host_summary = "、".join(
            f"{k}({v['valid_messages']}条)" for k, v in per_host_stats.items() if v["valid_messages"] > 0
        )
        md_lines = [
            "# 本机 Agent 历史对话个人语感样本集（求职语气建档专用）",
            "",
            f"- 抽样统计：共筛选出 { len(unique_msgs) } 条去噪消息，精选 **{total_picked}** 条（短句表达 {len(picked_short)} 条 + 成段阐述 {len(picked_long)} 条，共约 {total_chars} 字）",
            f"- 来源分布：{host_summary or '本机会话'}（已自动剔除代码块、报错堆栈、命令前缀与粘贴的招聘 JD，仅保留在本机）",
            "",
            "## A. 短句沟通与即时表达样本（16～140 字 · 重点观察断句、标点收尾、连接词与语气助词）",
            "",
        ]
        for idx, m in enumerate(picked_short, 1):
            dt_str = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
            md_lines.append(f"### S{idx} [{dt_str} · {m['host']}]")
            md_lines.append(m["text"])
            md_lines.append("")

        md_lines.append("## B. 成段业务与逻辑阐述样本（>140 字 · 重点观察术语混排、叙事切入方式与排斥套话）")
        md_lines.append("")
        for idx, m in enumerate(picked_long, 1):
            dt_str = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
            md_lines.append(f"### L{idx} [{dt_str} · {m['host']}]")
            md_lines.append(m["text"])
            md_lines.append("")

        atomic_write_text(out_file, "\n".join(md_lines), mode=0o600)

    existing_status = inspect_voice_status(workspace)
    return {
        "ok": True,
        "action": "collect",
        "corpus_ready": total_picked > 0,
        "corpus_path": str(out_file) if total_picked > 0 else None,
        "total_unique_found": len(unique_msgs),
        "picked_messages": total_picked,
        "picked_short_count": len(picked_short),
        "picked_long_count": len(picked_long),
        "picked_chars": total_chars,
        "hosts_scanned": per_host_stats,
        "existing_voice_profile": existing_status,
        "agent_instruction": (
            VOICE_EXTRACTION_RULES_PROMPT
            if total_picked > 0
            else (
                "本机历史会话中暂未扫到足够的用户自然表达段落；"
                "请请用户直接粘贴 3～5 条平时在招聘软件或微信里真实发过的工作沟通消息（或写过的一小段文字），再调用 `voice save` 建档。"
            )
        ),
    }
