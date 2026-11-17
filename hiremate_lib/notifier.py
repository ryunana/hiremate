"""桌面系统级人工接管通知工具（零第三方依赖，失败静默降级不阻塞主流程）。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any


def _escape_applescript(text: str) -> str:
    sliced = (text or "").replace("\n", " ").replace("\r", " ")[:160]
    return sliced.replace("\\", "\\\\").replace('"', '\\"')


def send_handoff_notification(
    *,
    company: str,
    title: str,
    reason: str = "",
) -> dict[str, Any]:
    """当岗位流转至 handed_off（约面/谈薪/加微信/超纲问题）时触发本机桌面通知弹窗。"""
    if os.environ.get("HIREMATE_DISABLE_DESKTOP_NOTIFY", "").strip() in ("1", "true", "yes"):
        return {"sent": False, "channel": "none", "reason": "disabled_by_env"}

    headline = f"求职助手 · 待本人接管：{company or '新进展'}"
    body = f"{title or '目标岗位'} — {reason or '已推进至约面/关键决策节点，请打开 BOSS 直聘处理'}"

    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            script = (
                f'display notification "{_escape_applescript(body)}" '
                f'with title "{_escape_applescript(headline)}"'
            )
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return {"sent": proc.returncode == 0, "channel": "macos_osascript"}

        if sys.platform.startswith("linux") and shutil.which("notify-send"):
            proc = subprocess.run(
                ["notify-send", headline[:80], body[:180]],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return {"sent": proc.returncode == 0, "channel": "linux_notify_send"}

        if sys.platform.startswith("win") and shutil.which("powershell"):
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                "$n.Visible = $true; "
                "$n.ShowBalloonTip(4000, $env:HM_NOTIFY_TITLE, $env:HM_NOTIFY_BODY, "
                "[System.Windows.Forms.ToolTipIcon]::Info); "
                "Start-Sleep -Milliseconds 2500; $n.Dispose()"
            )
            env = {
                **os.environ,
                "HM_NOTIFY_TITLE": headline[:63],
                "HM_NOTIFY_BODY": body[:200],
            }
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                env=env,
                capture_output=True,
                text=True,
                timeout=6,
            )
            return {"sent": proc.returncode == 0, "channel": "windows_balloon"}
    except (OSError, subprocess.SubprocessError):
        return {"sent": False, "channel": "error_fallback"}

    return {"sent": False, "channel": "unsupported_platform"}
