"""求职助手（hiremate）零外部依赖 MCP App 服务端（newline-delimited JSON-RPC 2.0 over stdio）。

用途：
为支持 MCP Apps UI 扩展的桌面宿主（如 OpenAI Codex 左侧边栏 Global Entrypoint 与会话并排 Thread Tab）
提供原生侧边栏菜单入口与全屏实时可视化求职看板，支持面板内一键刷新本地四态账本与抽样个人语气语料。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any, BinaryIO

from . import __version__, client, dashboard, updater, voice, workspace

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MCP_APP_MIME = "text/html;profile=mcp-app"

_MONO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
    '<rect x="3" y="7" width="18" height="13" rx="2.5"/>'
    '<path d="M3 13h18"/>'
    '<circle cx="12" cy="13" r="1.6"/>'
    "</svg>"
)
ICON_SPEC: dict[str, Any] = {
    "src": "data:image/svg+xml;base64," + base64.b64encode(_MONO_SVG.encode("utf-8")).decode("ascii"),
    "mimeType": "image/svg+xml",
    "sizes": ["any"],
}

# Codex 标签页小图标仅接受 <=8192 字符的 PNG/WebP/GIF data URL（16x16 极简蓝色图标）
_TAB_FAVICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAAXNSR0IArs4c6QAAADtJREFUOE9j"
    "ZGBg+M9AAWAc1QBMGzZswG4AqwFNTU14DWBhYSHsAmwGkGoAqQZQBgYGxv///5PmBgBp4hER"
    "24mZ8QAAAABJRU5ErkJggg=="
)

RESOURCE_META: dict[str, Any] = {
    "ui": {
        "prefersBorder": False,
        "csp": {
            "connectDomains": [],
            "resourceDomains": ["data:", "blob:"],
        },
        "permissions": {
            "clipboardWrite": {},
        },
    }
}

READ_ONLY_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
}


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _build_shell_html() -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>求职助手</title>
  <link rel="icon" type="image/png" href="data:image/png;base64,{_TAB_FAVICON_PNG_B64}" />
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
      background: #f4f6f9;
      color: #0f172a;
    }}
    .shell {{
      display: flex;
      flex-direction: column;
      width: 100%;
      height: 100%;
    }}
    .bar {{
      flex: 0 0 auto;
      background: #ffffff;
      border-bottom: 1px solid #dbeafe;
      padding: 9px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px 14px;
      flex-wrap: wrap;
    }}
    .bar-left {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      min-width: 0;
    }}
    .pill {{
      font-size: 12px;
      font-weight: 700;
      color: #1d4ed8;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      padding: 2px 9px;
      border-radius: 999px;
      white-space: nowrap;
    }}
    .status-note {{
      font-size: 12.5px;
      color: #475569;
    }}
    .toast {{
      font-size: 12px;
      font-weight: 600;
      color: #059669;
    }}
    .actions {{
      display: flex;
      align-items: center;
      gap: 7px;
      flex-wrap: wrap;
    }}
    .btn {{
      appearance: none;
      border: 1px solid #cbd5e1;
      background: #ffffff;
      color: #0f172a;
      font-size: 12.5px;
      font-weight: 600;
      padding: 5px 11px;
      border-radius: 7px;
      cursor: pointer;
      transition: all 0.14s ease;
    }}
    .btn:hover {{
      border-color: #2563eb;
      color: #1d4ed8;
      background: #f8fafc;
    }}
    .btn.primary {{
      background: #2563eb;
      border-color: #1d4ed8;
      color: #ffffff;
    }}
    .btn.primary:hover {{
      background: #1d4ed8;
      color: #ffffff;
    }}
    .frame-wrap {{
      flex: 1 1 auto;
      position: relative;
      width: 100%;
      height: 100%;
      min-height: 0;
    }}
    iframe {{
      width: 100%;
      height: 100%;
      border: 0;
      display: block;
      background: #f4f6f9;
    }}
    .loading {{
      padding: 32px;
      font-size: 14px;
      color: #475569;
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="bar">
      <div class="bar-left">
        <span class="pill">⚡ 求职助手侧栏看板</span>
        <span id="status-note" class="status-note">正在同步本机四态求职账本…</span>
        <span id="toast" class="toast"></span>
      </div>
      <div class="actions">
        <button type="button" class="btn primary" id="btn-copy-search">🔍 复制搜岗口令</button>
        <button type="button" class="btn" id="btn-copy-unread">💬 复制查未读口令</button>
        <button type="button" class="btn" id="btn-open-strategy">⚙️ 调整筛选策略</button>
        <button type="button" class="btn" id="btn-voice">🗣️ 抽样历史对话提炼语气</button>
        <button type="button" class="btn" id="btn-refresh">🔄 刷新实时数据</button>
      </div>
    </div>
    <div class="frame-wrap">
      <div id="loading" class="loading">正在读取本地求职档案与投递看板…</div>
      <iframe id="board-frame" title="求职助手可视化看板" hidden></iframe>
    </div>
  </div>
  <script>
    (function () {{
      var nextId = 100;
      var pending = new Map();
      var toolResultListeners = new Set();
      var lastToolResult = null;
      var hostCapabilities = {{}};

      function sendMsg(payload) {{
        if (window.parent && window.parent !== window) {{
          window.parent.postMessage(Object.assign({{ jsonrpc: "2.0" }}, payload), "*");
        }}
      }}

      function requestHost(method, params, timeoutMs) {{
        var id = nextId++;
        return new Promise(function (resolve, reject) {{
          var timer = setTimeout(function () {{
            pending.delete(id);
            reject(new Error(method + " 请求超时"));
          }}, timeoutMs || 15000);
          pending.set(id, {{
            resolve: function (res) {{ clearTimeout(timer); resolve(res); }},
            reject: function (err) {{ clearTimeout(timer); reject(err); }}
          }});
          sendMsg({{ id: id, method: method, params: params || {{}} }});
        }});
      }}

      window.addEventListener("message", function (ev) {{
        if (ev.source !== window.parent) return;
        var msg = ev.data;
        if (!msg || typeof msg !== "object" || msg.jsonrpc !== "2.0") return;
        if (msg.method === undefined) {{
          var entry = pending.get(msg.id);
          if (!entry) return;
          pending.delete(msg.id);
          if (msg.error) entry.reject(new Error(msg.error.message || "请求失败"));
          else entry.resolve(msg.result);
        }} else if (msg.id !== undefined) {{
          if (msg.method === "ping" || msg.method === "ui/resource-teardown") {{
            sendMsg({{ id: msg.id, result: {{}} }});
          }} else if (msg.method === "tools/list") {{
            sendMsg({{ id: msg.id, result: {{ tools: [] }} }});
          }} else {{
            sendMsg({{ id: msg.id, error: {{ code: -32601, message: "unsupported" }} }});
          }}
        }} else if (msg.method === "ui/notifications/tool-result") {{
          lastToolResult = msg.params || {{}};
          toolResultListeners.forEach(function (fn) {{ fn(lastToolResult); }});
        }}
      }});

      var toastEl = document.getElementById("toast");
      var statusEl = document.getElementById("status-note");
      var frameEl = document.getElementById("board-frame");
      var loadingEl = document.getElementById("loading");
      var voiceBtn = document.getElementById("btn-voice");
      var searchBtn = document.getElementById("btn-copy-search");
      var currentOnboarded = false;

      function flash(text) {{
        toastEl.textContent = text;
        setTimeout(function () {{ if (toastEl.textContent === text) toastEl.textContent = ""; }}, 3800);
      }}

      function copyText(str, okMsg) {{
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          navigator.clipboard.writeText(str).then(function () {{ flash(okMsg); }}).catch(function () {{
            fallbackCopy(str, okMsg);
          }});
        }} else {{
          fallbackCopy(str, okMsg);
        }}
      }}

      function fallbackCopy(str, okMsg) {{
        var ta = document.createElement("textarea");
        ta.value = str;
        document.body.appendChild(ta);
        ta.select();
        try {{ document.execCommand("copy"); flash(okMsg); }} catch (e) {{}}
        document.body.removeChild(ta);
      }}

      var isCodexHost = false;
      function withHostMention(text) {{
        return isCodexHost ? "[@求职助手](plugin://hiremate@hiremate) " + text : text;
      }}

      function renderSnapshot(snap, opts) {{
        if (!snap || !snap.dashboard_html) return;
        opts = opts || {{}};
        currentOnboarded = Boolean(snap.onboarding_completed);
        statusEl.textContent = (snap.tier === "pro" ? "Pro 专业版" : "基础版") +
          " · 共 " + (snap.jobs_count || 0) + " 条岗位记录 · " +
          (snap.voice_ready ? "个人语气已启用" : "默认口语");
        searchBtn.textContent = currentOnboarded ? "🔍 复制搜岗口令" : "📋 复制初始化建档口令";
        voiceBtn.textContent = snap.voice_ready ? "🗣️ 重新抽样更新语气" : "🗣️ 抽样历史对话提炼语气";

        var initBoot = "";
        if (opts.openTab === "strategy") {{
          var safeToast = JSON.stringify(opts.toastMsg || "").replace(/</g, "\\u003c");
          initBoot = 'window.__HM_INITIAL_TAB__="strategy";window.__HM_INITIAL_TOAST__=' + safeToast + ';';
        }}
        var bridgePatch = '<script>(function(){{' +
          initBoot +
          'var _cbMap=new Map(),_seq=1;' +
          'window.__HIREMATE_SAVE_CONFIG__=function(patch,ackAutoRisk){{' +
          'return new Promise(function(res){{var id=_seq++;_cbMap.set(id,res);window.parent.postMessage({{type:"hm-save-config",reqId:id,patch:patch,ack_auto_risk:Boolean(ackAutoRisk)}},"*");}});' +
          '}};' +
          'window.addEventListener("message",function(ev){{' +
          'if(!ev.data)return;' +
          'if(ev.data.type==="hm-open-strategy"&&typeof window.hmOpenStrategyTab==="function"){{window.hmOpenStrategyTab();}}' +
          'if(ev.data.type==="hm-save-config-result"){{var fn=_cbMap.get(ev.data.reqId);if(fn){{_cbMap.delete(ev.data.reqId);fn(ev.data.result);}}}}' +
          '}});' +
          'document.addEventListener("click",function(e){{' +
          'var a=e.target&&e.target.closest?e.target.closest("a[href]"):null;' +
          'if(a&&/^https?:\\/\\//i.test(a.href)){{e.preventDefault();window.parent.postMessage({{type:"hm-open-url",url:a.href}},"*");}}' +
          '}},true);}})();<\\/script>';
        var rawHtml = String(snap.dashboard_html || "");
        var headIdx = rawHtml.indexOf("<head>");
        var htmlDoc;
        if (headIdx !== -1) {{
          htmlDoc = rawHtml.slice(0, headIdx + 6) + bridgePatch + rawHtml.slice(headIdx + 6);
        }} else {{
          var bodyCloseIdx = rawHtml.lastIndexOf("</body>");
          htmlDoc = bodyCloseIdx !== -1
            ? rawHtml.slice(0, bodyCloseIdx) + bridgePatch + rawHtml.slice(bodyCloseIdx)
            : bridgePatch + rawHtml;
        }}
        frameEl.srcdoc = htmlDoc;
        loadingEl.hidden = true;
        frameEl.hidden = false;
      }}

      window.addEventListener("message", function (ev) {{
        if (!ev.data) return;
        if (frameEl && frameEl.contentWindow && ev.source === frameEl.contentWindow) {{
          if (ev.data.type === "hm-open-url" && typeof ev.data.url === "string" && /^https?:\\/\\//i.test(ev.data.url)) {{
            if (hostCapabilities && hostCapabilities.openLinks) {{
              requestHost("ui/open-link", {{ url: ev.data.url }}).catch(function () {{
                window.open(ev.data.url, "_blank", "noopener,noreferrer");
              }});
            }} else {{
              window.open(ev.data.url, "_blank", "noopener,noreferrer");
            }}
          }} else if (ev.data.type === "hm-save-config") {{
            var reqId = ev.data.reqId;
            requestHost("tools/call", {{
              name: "hiremate_ui_save_config",
              arguments: {{
                patch: ev.data.patch || {{}},
                ack_auto_risk: Boolean(ev.data.ack_auto_risk)
              }}
            }}, 20000).then(function (res) {{
              var sc = (res && res.structuredContent) || {{ ok: false, error: "未收到保存响应" }};
              if (sc.ok && sc.snapshot) {{
                var okText = sc.notice || "✅ 策略已保存！下次搜岗与巡检立即按新选项执行";
                flash(okText);
                renderSnapshot(sc.snapshot, {{ openTab: "strategy", toastMsg: okText }});
              }} else if (frameEl && frameEl.contentWindow) {{
                frameEl.contentWindow.postMessage({{ type: "hm-save-config-result", reqId: reqId, result: sc }}, "*");
              }}
            }}).catch(function (err) {{
              if (frameEl && frameEl.contentWindow) {{
                frameEl.contentWindow.postMessage({{
                  type: "hm-save-config-result",
                  reqId: reqId,
                  result: {{ ok: false, error: err && err.message ? err.message : String(err) }}
                }}, "*");
              }}
            }});
          }}
        }}
      }});

      async function refreshBoard() {{
        try {{
          var res = await requestHost("tools/call", {{ name: "hiremate_ui_snapshot", arguments: {{}} }}, 20000);
          if (res && res.structuredContent) renderSnapshot(res.structuredContent);
        }} catch (err) {{
          flash("刷新失败：" + (err && err.message ? err.message : String(err)));
        }}
      }}

      searchBtn.addEventListener("click", function () {{
        var cmd = currentOnboarded
          ? withHostMention("帮我在 BOSS 直聘按当前求职档案跑一轮精准搜岗深筛")
          : withHostMention("帮我初始化求职助手");
        copyText(cmd, "✓ 已复制口令，切回对话粘贴发送即可");
      }});

      document.getElementById("btn-copy-unread").addEventListener("click", function () {{
        copyText(
          withHostMention("帮我检查 BOSS 直聘未读消息与待接收简历回执"),
          "✓ 已复制查未读口令，切回对话粘贴发送"
        );
      }});

      document.getElementById("btn-open-strategy").addEventListener("click", function () {{
        if (frameEl && frameEl.contentWindow) {{
          frameEl.contentWindow.postMessage({{ type: "hm-open-strategy" }}, "*");
        }}
      }});

      document.getElementById("btn-refresh").addEventListener("click", function () {{
        flash("正在刷新…");
        refreshBoard();
      }});

      voiceBtn.addEventListener("click", async function () {{
        var orig = voiceBtn.textContent;
        voiceBtn.disabled = true;
        voiceBtn.textContent = "正在抽样本机会话…";
        try {{
          var res = await requestHost("tools/call", {{ name: "hiremate_ui_collect_voice", arguments: {{}} }}, 25000);
          var sc = (res && res.structuredContent) || {{}};
          if (sc.prompt) {{
            copyText(sc.prompt, "✓ 语料已整理就绪！建档口令已复制，回对话粘贴发送即可");
          }} else {{
            flash("本机样本较少，可直接回对话粘贴几条聊天记录建档");
          }}
        }} catch (err) {{
          copyText(
            withHostMention("请从我的本机历史对话记录提取行文风格，建立个人求职语气档案"),
            "✓ 已复制语气建档口令，回对话粘贴发送"
          );
        }} finally {{
          voiceBtn.disabled = false;
          voiceBtn.textContent = orig;
        }}
      }});

      async function boot() {{
        toolResultListeners.add(function (tr) {{
          if (tr && tr.structuredContent && tr.structuredContent.dashboard_html) {{
            renderSnapshot(tr.structuredContent);
          }}
        }});
        var initRes = await requestHost("ui/initialize", {{
          protocolVersion: "2026-01-26",
          appInfo: {{ name: "求职助手", version: "{__version__}" }},
          appCapabilities: {{ availableDisplayModes: ["fullscreen", "inline"] }}
        }}, 15000);
        hostCapabilities = (initRes && initRes.hostCapabilities) || {{}};
        var hName = String((initRes && initRes.hostInfo && initRes.hostInfo.name) || "").toLowerCase();
        isCodexHost = !hName || hName.indexOf("codex") !== -1 || hName.indexOf("openai") !== -1;
        sendMsg({{ method: "ui/notifications/initialized" }});
        if (lastToolResult && lastToolResult.structuredContent && lastToolResult.structuredContent.dashboard_html) {{
          renderSnapshot(lastToolResult.structuredContent);
        }} else {{
          await refreshBoard();
        }}
      }}

      if (window.parent !== window) {{
        boot().catch(function (err) {{
          loadingEl.textContent = "面板初始化异常：" + (err && err.message ? err.message : String(err));
        }});
      }} else {{
        loadingEl.textContent = "此页面为求职助手 MCP 侧边栏面板，请在桌面 Agent 侧栏内打开。";
      }}
    }})();
  </script>
</body>
</html>"""


class HiremateMcpServer:
    """轻量单进程 MCP App 服务端，按需生成本地求职看板快照与本机会话语气语料。"""

    def __init__(self, *, home_dir: Path | None = None, workspace_dir: Path | None = None) -> None:
        self._home = workspace.get_hiremate_home(home_dir)
        self._ws = workspace.resolve_workspace(workspace_dir, self._home)
        self._op_lock = threading.Lock()
        self._client_name = ""
        self._shell_html = _build_shell_html()
        shell_hash = hashlib.sha256(self._shell_html.encode("utf-8")).hexdigest()[:16]
        self._page_uri = f"ui://hiremate/{__version__}/{shell_hash}/app.html"

    @property
    def page_uri(self) -> str:
        return self._page_uri

    def _owns_uri(self, uri: object) -> bool:
        return isinstance(uri, str) and uri.startswith("ui://hiremate/") and uri.endswith("/app.html")

    def _build_snapshot_payload(self) -> dict[str, Any]:
        creds = client.load_credentials(self._home)
        tier = "pro" if (creds and creds.get("tier") == "pro") else "free"
        pro_meta: dict[str, Any] = {}
        if tier == "pro" and creds:
            pro_meta = {
                "email": creds.get("email"),
                "lifetime": bool(creds.get("lifetime", creds.get("days_left") is None)),
                "days_left": creds.get("days_left"),
                "expires_at": creds.get("expires_at"),
            }
        upd = updater.build_update_notice_from_state(updater.load_update_state(self._home))
        dash = dashboard.build_dashboard_file(
            workspace=self._ws,
            tier=tier,
            pro_meta=pro_meta,
            update_notice=upd,
        )
        out_path = Path(str(dash.get("out_path") or "")).expanduser().resolve()
        dash_html = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
        cfg = workspace.load_config(self._ws)
        ob = workspace.inspect_onboarding_status(self._ws, cfg)
        v_stat = voice.inspect_voice_status(self._ws)
        return {
            "ok": True,
            "tier": tier,
            "jobs_count": int(dash.get("jobs_count", 0)),
            "onboarding_completed": bool(ob["onboarding_completed"]),
            "voice_ready": bool(v_stat["ready"]),
            "out_path": str(out_path),
            "dashboard_html": dash_html,
        }

    def tools(self) -> list[dict[str, Any]]:
        empty_schema: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
        return [
            {
                "name": "hiremate_home",
                "title": "求职助手",
                "description": "打开求职助手侧边栏工作台（查看实时投递转化漏斗、约面待办队列、排雷账本与个人语气档案）。",
                "inputSchema": empty_schema,
                "icons": [ICON_SPEC],
                "annotations": READ_ONLY_ANNOTATIONS,
                "_meta": {
                    "ui": {"resourceUri": self._page_uri, "visibility": ["app"]},
                    "openai/iconStyle": "monochrome",
                    "openai/ui": {
                        "entrypoints": [{"type": "global"}],
                        "preferredModelDisplayMode": "fullscreen",
                    },
                },
            },
            {
                "name": "hiremate_tab",
                "title": "求职看板",
                "description": "在当前会话旁并排打开求职助手投递看板标签页。",
                "inputSchema": empty_schema,
                "icons": [ICON_SPEC],
                "annotations": READ_ONLY_ANNOTATIONS,
                "_meta": {
                    "ui": {"resourceUri": self._page_uri, "visibility": ["app"]},
                    "openai/iconStyle": "monochrome",
                    "openai/ui": {
                        "entrypoints": [{"type": "thread"}],
                    },
                },
            },
            {
                "name": "hiremate_open_board",
                "title": "打开求职看板",
                "description": "在宿主分屏面板中打开求职助手实时可视化投递看板。",
                "inputSchema": empty_schema,
                "icons": [ICON_SPEC],
                "annotations": READ_ONLY_ANNOTATIONS,
                "_meta": {
                    "ui": {"resourceUri": self._page_uri},
                    "openai/iconStyle": "monochrome",
                    "openai/ui": {
                        "preferredModelDisplayMode": "fullscreen",
                    },
                },
            },
            {
                "name": "hiremate_ui_snapshot",
                "title": "读取看板快照",
                "description": "供求职助手面板内部刷新最新本地账本与看板 HTML。",
                "inputSchema": empty_schema,
                "annotations": READ_ONLY_ANNOTATIONS,
                "_meta": {"ui": {"visibility": ["app"]}},
            },
            {
                "name": "hiremate_ui_save_config",
                "title": "保存求职策略配置",
                "description": "供求职助手侧栏看板表单直接更新并保存本机 config.json，立即同步最新求职策略与看板快照。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patch": {"type": "object"},
                        "ack_auto_risk": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "_meta": {"ui": {"visibility": ["app"]}},
            },
            {
                "name": "hiremate_ui_collect_voice",
                "title": "抽样个人语气语料",
                "description": "供求职助手面板内部扫描本机历史对话记录生成语气学习素材。",
                "inputSchema": empty_schema,
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
                "_meta": {"ui": {"visibility": ["app"]}},
            },
        ]

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            client_info = params.get("clientInfo")
            if isinstance(client_info, dict):
                self._client_name = str(client_info.get("name") or "").strip().lower()
            req_ver = params.get("protocolVersion")
            chosen_ver = req_ver if req_ver in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[1]
            return {
                "protocolVersion": chosen_ver,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "hiremate",
                    "title": "求职助手",
                    "version": __version__,
                    "icons": [ICON_SPEC],
                },
                "instructions": (
                    "求职助手（hiremate）提供本地求职看板面板。日常求职建档、岗位筛选、消息答疑与语气档案管理请遵循 hiremate 技能指令调用 hiremate.py；"
                    "当用户希望直接在界面旁查看求职看板时可调用 hiremate_open_board。"
                ),
            }

        if method == "ping":
            return {}

        if method == "tools/list":
            return {"tools": self.tools()}

        if method == "resources/list":
            return {
                "resources": [
                    {
                        "uri": self._page_uri,
                        "name": "hiremate_board",
                        "title": "求职助手",
                        "mimeType": MCP_APP_MIME,
                        "_meta": RESOURCE_META,
                    }
                ]
            }

        if method == "resources/templates/list":
            return {"resourceTemplates": []}

        if method == "resources/read":
            uri = params.get("uri")
            if not self._owns_uri(uri):
                raise RpcError(RESOURCE_NOT_FOUND, f"unknown resource: {uri!r}")
            return {
                "contents": [
                    {
                        "uri": str(uri),
                        "mimeType": MCP_APP_MIME,
                        "text": self._shell_html,
                        "_meta": RESOURCE_META,
                    }
                ]
            }

        if method == "tools/call":
            name = str(params.get("name") or "")
            if name in ("hiremate_home", "hiremate_tab", "hiremate_open_board", "hiremate_ui_snapshot"):
                with self._op_lock:
                    snap = self._build_snapshot_payload()
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"已加载求职助手看板（共 {snap['jobs_count']} 条岗位记录，路径：{snap['out_path']}）。",
                        }
                    ],
                    "structuredContent": snap,
                }
            if name == "hiremate_ui_save_config":
                args_obj = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                raw_patch = args_obj.get("patch") if isinstance(args_obj.get("patch"), dict) else {}
                ack_risk = bool(args_obj.get("ack_auto_risk", False))
                with self._op_lock:
                    save_res = client.save_config_from_ui_patch(
                        workspace=self._ws,
                        home=self._home,
                        raw_patch=raw_patch,
                        ack_auto_risk=ack_risk,
                    )
                    if save_res.get("ok"):
                        save_res["snapshot"] = self._build_snapshot_payload()
                msg_text = (
                    save_res.get("notice")
                    or ("已更新并保存本机求职策略配置。" if save_res.get("ok") else str(save_res.get("error") or "保存失败"))
                )
                return {
                    "content": [{"type": "text", "text": msg_text}],
                    "structuredContent": save_res,
                }
            if name == "hiremate_ui_collect_voice":
                with self._op_lock:
                    col = voice.collect_voice_corpus(self._ws)
                mention_prefix = (
                    "[@求职助手](plugin://hiremate@hiremate) "
                    if any(k in self._client_name for k in ("codex", "chatgpt", "openai"))
                    else ""
                )
                prompt = (
                    f"{mention_prefix}请从我本机的历史对话记录提炼《个人求职语气档案》。"
                    f"学习素材已去噪整理好：{col['corpus_path']}（共筛选出 {col['total_unique_found']} 条消息，"
                    f"精选 {col['picked_messages']} 条，约 {col['picked_chars']} 字）。"
                    f"请按求职助手的语气建档规约生成并保存档案，完成后发我过目。"
                    if col.get("corpus_ready")
                    else f"{mention_prefix}我想建立个人求职语气档案，下面是我平时真实发过的沟通消息样本：\n\n"
                )
                payload = {**col, "prompt": prompt}
                return {
                    "content": [{"type": "text", "text": prompt}],
                    "structuredContent": payload,
                }
            raise RpcError(INVALID_PARAMS, f"unknown tool: {name!r}")

        raise RpcError(METHOD_NOT_FOUND, f"unsupported method: {method!r}")


_MAX_RPC_FRAME_BYTES = 4 * 1024 * 1024


def _read_next_rpc_frame(reader: BinaryIO) -> tuple[bytes | None, str]:
    """同时兼容换行分隔 JSON（NDJSON）与 LSP 风格 `Content-Length` 头帧。"""
    while True:
        first = reader.readline(_MAX_RPC_FRAME_BYTES + 1)
        if not first:
            return None, "ndjson"
        if len(first) > _MAX_RPC_FRAME_BYTES:
            return b"__OVERFLOW__", "ndjson"
        stripped = first.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            try:
                content_len = int(stripped.split(b":", 1)[1].strip())
            except ValueError:
                return b"__INVALID_HEADER__", "lsp"
            while True:
                hdr_line = reader.readline(4096)
                if not hdr_line or hdr_line in (b"\r\n", b"\n"):
                    break
            if content_len < 0 or content_len > _MAX_RPC_FRAME_BYTES:
                if 0 < content_len <= 16 * 1024 * 1024:
                    reader.read(content_len)
                return b"__OVERFLOW__", "lsp"
            body = reader.read(content_len)
            if not body:
                return None, "lsp"
            return body, "lsp"
        return stripped, "ndjson"


def _claim_clean_stdio() -> tuple[BinaryIO, BinaryIO]:
    """独占原始 stdin/stdout 描述符并将标准输出重定向到 stderr，防止意外日志污染 JSON-RPC 帧。"""
    proto_out_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    proto_in = os.fdopen(os.dup(0), "rb")
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull_fd, 0)
    os.close(devnull_fd)
    return proto_in, os.fdopen(proto_out_fd, "wb", buffering=0)


def serve_stdio(*, home_dir: Path | None = None, workspace_dir: Path | None = None) -> int:
    reader, writer = _claim_clean_stdio()
    server = HiremateMcpServer(home_dir=home_dir, workspace_dir=workspace_dir)
    write_lock = threading.Lock()
    cancel_lock = threading.Lock()
    cancelled_ids: set[str | int] = set()
    framing_mode = "ndjson"

    def send_frame(msg: dict[str, Any]) -> None:
        payload = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        raw = (
            f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
            if framing_mode == "lsp"
            else payload + b"\n"
        )
        with write_lock:
            try:
                writer.write(raw)
                writer.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def handle_request(req_id: Any, method: str, params: dict[str, Any]) -> None:
        try:
            res = server.dispatch(method, params)
            resp: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "result": res}
        except RpcError as err:
            resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": err.code, "message": err.message}}
        except Exception as exc:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": INTERNAL_ERROR, "message": f"{type(exc).__name__}: {exc}"},
            }
        was_cancelled = False
        if isinstance(req_id, (str, int)) and not isinstance(req_id, bool):
            with cancel_lock:
                was_cancelled = req_id in cancelled_ids
                cancelled_ids.discard(req_id)
        if not was_cancelled:
            send_frame(resp)

    while True:
        raw_frame, detected_mode = _read_next_rpc_frame(reader)
        if raw_frame is None:
            break
        framing_mode = detected_mode
        if raw_frame in (b"__OVERFLOW__", b"__INVALID_HEADER__"):
            send_frame({"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "invalid frame"}})
            continue
        try:
            message = json.loads(raw_frame)
        except ValueError:
            send_frame({"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "invalid JSON"}})
            continue
        if not isinstance(message, dict):
            send_frame({"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "expected object"}})
            continue
        has_id = "id" in message
        raw_id = message.get("id")
        safe_id = raw_id if (raw_id is None or (isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool))) else None
        if has_id and raw_id is not None and safe_id is None:
            send_frame({"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "id must be string, number, or null"}})
            continue
        method = message.get("method")
        if not isinstance(method, str) or not method:
            if has_id:
                send_frame({"jsonrpc": "2.0", "id": safe_id, "error": {"code": INVALID_REQUEST, "message": "missing or invalid method"}})
            continue
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if not has_id:
            if method == "notifications/cancelled":
                cid = params.get("requestId")
                if isinstance(cid, (str, int)) and not isinstance(cid, bool):
                    with cancel_lock:
                        cancelled_ids.add(cid)
            continue
        if method in ("initialize", "ping"):
            handle_request(safe_id, method, params)
        else:
            threading.Thread(
                target=handle_request,
                args=(safe_id, method, params),
                name="hiremate-mcp",
                daemon=True,
            ).start()
    return 0
