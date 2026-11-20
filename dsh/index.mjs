// hiremate-dsh：将「求职助手」注册为 DeepSeek Harness 运行时 skill + 原生 `/` 斜杠命令 + 左侧栏可视化工作台。
// 零外部依赖：不 import 任何 @deepseek-ai/* 包，避免宿主 pnpm 闭包解析冲突。
import { execFile } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)
const WEB_BASE = '/hiremate'

export const name = 'hiremate-dsh'
export const inject = ['skills']

function stripFrontmatter(md) {
  if (!md.startsWith('---')) return md
  const end = md.indexOf('\n---', 4)
  if (end === -1) return md
  return md.slice(end + 4).replace(/^\n+/, '')
}

function resolveCliPath(skillDir) {
  const hmHome = process.env.HIREMATE_HOME || join(homedir(), '.hiremate')
  const dshHome = process.env.DSH_HOME || join(homedir(), '.dsh')
  const candidates = [
    ...(process.env.HIREMATE_CLI_PATH ? [process.env.HIREMATE_CLI_PATH] : []),
    join(skillDir, 'hiremate.py'),
    join(skillDir, '..', '..', '..', 'hiremate.py'),
    join(hmHome, 'skill', 'hiremate', 'hiremate.py'),
    join(dshHome, 'skills', 'hiremate', 'hiremate.py'),
  ]
  for (const p of candidates) {
    if (existsSync(p)) return p
  }
  return candidates[0]
}

let cachedPythonSpec = null

async function runHiremateCli(skillDir, args, options = {}) {
  const cliPath = resolveCliPath(skillDir)
  const timeoutMs = options.timeoutMs || 15_000
  const extraEnv = options.env || {}
  const candidates = cachedPythonSpec
    ? [cachedPythonSpec]
    : [
        ...(process.env.HIREMATE_PYTHON ? [[process.env.HIREMATE_PYTHON, []]] : []),
        ['python3', []],
        ...(process.platform === 'win32' ? [['py', ['-3']], ['python', []]] : [['python', []]]),
      ]

  let lastErr = null
  for (const [bin, prefixArgs] of candidates) {
    try {
      const { stdout } = await execFileAsync(bin, [...prefixArgs, cliPath, ...args], {
        timeout: timeoutMs,
        maxBuffer: 4 * 1024 * 1024,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', ...extraEnv },
      })
      cachedPythonSpec = [bin, prefixArgs]
      return JSON.parse(stdout)
    } catch (err) {
      lastErr = err
      if (err && (err.code === 'ENOENT' || /Microsoft Store/i.test(String(err.stderr || err.message || '')))) {
        cachedPythonSpec = null
        continue
      }
      if (err?.stdout) {
        try {
          return JSON.parse(err.stdout)
        } catch {
          // ignore
        }
      }
      break
    }
  }
  throw lastErr || new Error('未找到可用的 Python 3.9+ 解释器')
}

function sendJson(res, status, body) {
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  })
  res.end(JSON.stringify(body))
}

function sendHtml(res, status, html) {
  const buf = Buffer.from(html, 'utf-8')
  res.writeHead(status, {
    'content-type': 'text/html; charset=utf-8',
    'content-length': buf.length,
    'cache-control': 'no-store',
  })
  res.end(buf)
}

function isSameOriginBrowserRequest(req, { requireCustomHeader = false } = {}) {
  // 注意：DSH 桌面端（dsh-app://app）在 Electron 协议转发层 forwardWebRequest 中已校验 origin === "dsh-app://app" 与私有 cookie，
  // 随后会主动剥离 host / origin / sec-fetch-site 再转发至本地 127.0.0.1 webServer；因此不能在无 Origin 头时强求 sec-fetch-site === "same-origin"。
  const fetchSite = String(req.headers['sec-fetch-site'] || '').toLowerCase()
  if (fetchSite && fetchSite !== 'same-origin' && fetchSite !== 'none') {
    return false
  }
  if (requireCustomHeader && req.headers['x-hiremate-same-origin'] !== '1') {
    return false
  }
  const origin = req.headers.origin
  if (origin && !/^dsh-app:\/\//i.test(String(origin))) {
    const stripped = String(origin).replace(/^https?:\/\//i, '').toLowerCase()
    const hostHeader = String(req.headers.host || '').trim().toLowerCase()
    if (hostHeader && stripped !== hostHeader) {
      return false
    }
  }
  return true
}

function readJsonBody(req, maxBytes = 16 * 1024) {
  return new Promise((resolve, reject) => {
    if (req.method !== 'POST') return reject(Object.assign(new Error('method_not_allowed'), { status: 405 }))
    if (!/^application\/json\b/i.test(req.headers['content-type'] || '')) {
      return reject(Object.assign(new Error('unsupported_media_type'), { status: 415 }))
    }
    if (!isSameOriginBrowserRequest(req, { requireCustomHeader: true })) {
      return reject(Object.assign(new Error('forbidden_origin'), { status: 403 }))
    }
    let size = 0
    let settled = false
    const chunks = []
    req.on('data', (c) => {
      if (settled) return
      size += c.length
      if (size > maxBytes) {
        settled = true
        reject(Object.assign(new Error('payload_too_large'), { status: 413 }))
        req.destroy()
      } else {
        chunks.push(c)
      }
    })
    req.on('end', () => {
      if (settled) return
      settled = true
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf-8') || '{}'))
      } catch (e) {
        reject(Object.assign(e, { status: 400 }))
      }
    })
    req.on('error', (err) => {
      if (settled) return
      settled = true
      reject(err)
    })
  })
}

const ALLOWED_UI_PATCH_KEYS = new Set([
  'mode_discover',
  'mode_reply',
  'target_roles',
  'preferred_cities',
  'remote_bigtech_flex',
  'min_salary_k',
  'salary_match_mode',
  'outsourcing_policy',
  'onsite_policy',
  'headhunter_policy',
  'inbound_resume_policy',
  'past_employers',
  'blocked_companies',
  'exclude_keywords',
  'schedule',
])

function sanitizeUiPatch(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const out = {}
  for (const [k, v] of Object.entries(raw)) {
    if (!ALLOWED_UI_PATCH_KEYS.has(k)) continue
    if (k === 'schedule') {
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        const sc = {}
        for (const sk of ['weekdays_only', 'max_per_round', 'max_per_day', 'min_interval_minutes', 'active_windows']) {
          if (sk in v) sc[sk] = v[sk]
        }
        if (Object.keys(sc).length > 0) out.schedule = sc
      }
    } else {
      out[k] = v
    }
  }
  return out
}

function injectDshSidebarToolbar(rawDashboardHtml, { onboardingCompleted = false, voiceReady = false } = {}) {
  const toolbarSnippet = `
  <style>
    .hm-dsh-studio-bar {
      position: sticky;
      top: 0;
      z-index: 60;
      background: rgba(255, 255, 255, 0.94);
      backdrop-filter: blur(8px);
      border-bottom: 1px solid #dbeafe;
      padding: 10px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px 16px;
      flex-wrap: wrap;
      box-shadow: 0 2px 10px rgba(15, 23, 42, 0.04);
    }
    .hm-dsh-bar-left {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .hm-dsh-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      font-weight: 700;
      color: #1d4ed8;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      padding: 3px 10px;
      border-radius: 999px;
    }
    .hm-dsh-hint {
      font-size: 12.5px;
      color: #475569;
    }
    .hm-dsh-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .hm-dsh-btn {
      appearance: none;
      border: 1px solid #cbd5e1;
      background: #ffffff;
      color: #0f172a;
      font-size: 12.5px;
      font-weight: 600;
      padding: 6px 12px;
      border-radius: 8px;
      cursor: pointer;
      transition: all 0.14s ease;
    }
    .hm-dsh-btn:hover {
      border-color: #2563eb;
      color: #1d4ed8;
      background: #f8fafc;
    }
    .hm-dsh-btn.primary {
      background: #2563eb;
      border-color: #1d4ed8;
      color: #ffffff;
    }
    .hm-dsh-btn.primary:hover {
      background: #1d4ed8;
      color: #ffffff;
    }
    .hm-dsh-toast {
      font-size: 12px;
      color: #059669;
      font-weight: 600;
    }
  </style>
  <div class="hm-dsh-studio-bar" role="region" aria-label="求职助手侧栏快捷操作栏">
    <div class="hm-dsh-bar-left">
      <span class="hm-dsh-pill">⚡ 侧栏工作台</span>
      <span class="hm-dsh-hint">点击动作将直接开启新对话并自动执行对应流程（调整策略将直接在下方展开可视化编辑器）</span>
      <span id="hm-dsh-toast" class="hm-dsh-toast"></span>
    </div>
    <div class="hm-dsh-actions">
      ${
        onboardingCompleted
          ? `<button type="button" class="hm-dsh-btn primary" data-hm-start="/hiremate 帮我在 BOSS 直聘按当前求职档案跑一轮精准搜岗深筛">🔍 去 BOSS 搜岗深筛</button>
             <button type="button" class="hm-dsh-btn" data-hm-start="/hiremate 帮我检查 BOSS 直聘未读消息与待接收简历回执">💬 检查未读消息</button>`
          : `<button type="button" class="hm-dsh-btn primary" data-hm-start="/hiremate 帮我初始化求职助手">📋 5 分钟对话建档</button>`
      }
      <button type="button" class="hm-dsh-btn" id="hm-voice-collect-btn">${voiceReady ? '🗣️ 更新个人语气档案' : '🗣️ 从对话历史学习我的语气'}</button>
      <button type="button" class="hm-dsh-btn" id="hm-open-strategy-btn">⚙️ 调整筛选策略</button>
      <button type="button" class="hm-dsh-btn" id="hm-refresh-board-btn">🔄 刷新实时数据</button>
    </div>
  </div>
  <script>
    (function () {
      var toastEl = document.getElementById('hm-dsh-toast');
      function flash(msg) {
        if (!toastEl) return;
        toastEl.textContent = msg;
        setTimeout(function () { if (toastEl.textContent === msg) toastEl.textContent = ''; }, 3500);
      }
      function startSessionAction(promptText, autoSend) {
        if (!promptText) return;
        var shouldSend = autoSend !== false;
        if (window.parent && window.parent !== window) {
          window.parent.postMessage(
            { type: 'hiremate:start', prompt: promptText, autoSend: shouldSend },
            location.origin
          );
          flash(shouldSend ? '✓ 正在开启新对话并启动流程…' : '✓ 已在新对话填入草稿，补充内容后回车即可');
          return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(promptText).then(
            function () { flash('✓ 已复制口令到剪贴板'); },
            function () { flash('复制失败，请手动复制口令'); }
          );
        }
      }
      document.querySelectorAll('[data-hm-start]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          startSessionAction(btn.getAttribute('data-hm-start'), true);
        });
      });
      var refreshBtn = document.getElementById('hm-refresh-board-btn');
      if (refreshBtn) {
        refreshBtn.addEventListener('click', function () { location.reload(); });
      }
      var openStratBtn = document.getElementById('hm-open-strategy-btn');
      if (openStratBtn) {
        openStratBtn.addEventListener('click', function () {
          if (typeof window.hmOpenStrategyTab === 'function') {
            window.hmOpenStrategyTab();
          } else {
            var tabBtn = document.querySelector('[data-tab-target="strategy"]');
            if (tabBtn) tabBtn.click();
          }
          flash('✓ 已切换至筛选策略编辑器');
        });
      }
      var voiceBtn = document.getElementById('hm-voice-collect-btn');
      if (voiceBtn) {
        voiceBtn.addEventListener('click', function () {
          var orig = voiceBtn.textContent;
          voiceBtn.disabled = true;
          voiceBtn.textContent = '正在抽样本机会话…';
          fetch('${WEB_BASE}/api/voice/collect', {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-hiremate-same-origin': '1' },
            body: '{}'
          })
            .then(function (r) { return r.json(); })
            .then(function (data) {
              voiceBtn.disabled = false;
              voiceBtn.textContent = orig;
              if (data && data.prompt) {
                startSessionAction(data.prompt, data.auto_send !== false);
              } else {
                flash((data && data.error) || '未提取到足够消息');
              }
            })
            .catch(function () {
              voiceBtn.disabled = false;
              voiceBtn.textContent = orig;
              startSessionAction('/hiremate 请从我的本机历史对话记录提取行文风格，建立个人求职语气档案', true);
            });
        });
      }
      window.addEventListener('message', function (e) {
        if (e.origin !== location.origin || !e.data) return;
        if (e.data.type === 'hiremate:start-failed') {
          flash('唤起对话失败：' + (e.data.message || '请手动复制口令'));
        }
      });
    })();
  </script>
`
  const bodyTagMatch = rawDashboardHtml.match(/<body[^>]*>/i)
  if (!bodyTagMatch) return toolbarSnippet + rawDashboardHtml
  const idx = bodyTagMatch.index + bodyTagMatch[0].length
  return rawDashboardHtml.slice(0, idx) + toolbarSnippet + rawDashboardHtml.slice(idx)
}

export function apply(ctx) {
  const base = new URL('./skills/hiremate/', import.meta.url)
  const skillDir = fileURLToPath(base)
  const dshHome = process.env.DSH_HOME || join(homedir(), '.dsh')
  const userSkillMd = join(dshHome, 'skills', 'hiremate', 'SKILL.md')

  // 若用户级 ~/.dsh/skills/hiremate 尚未软链挂载，则由插件兜底注册 runtime skill；已挂载则避免同名重复注册
  if (!existsSync(userSkillMd)) {
    const content = stripFrontmatter(readFileSync(new URL('SKILL.md', base), 'utf-8'))
    ctx.effect(() =>
      ctx.skills.register({
        name: 'hiremate',
        description:
          '求职助手（hiremate）：BOSS 直聘岗位精准匹配（拒绝海投乱投）、个人求职语气档案、一岗一议半自动/全自动消息答疑、多版简历路由、四态投递账本与本地静态看板。',
        whenToUse:
          '当用户要初始化求职助手、筛选招聘岗位、提炼个人求职语气档案、跟进回复 HR 消息、查看求职看板或登录激活求职助手 Pro 时使用。',
        source: 'runtime',
        content,
        resourceBase: { kind: 'directory', path: skillDir },
      })
    )
  }

  // 按需注入 webServer：为左侧栏「求职助手」面板提供同源实时看板与语气语料一键抽样 API
  if (typeof ctx.inject === 'function') {
    ctx.inject(['webServer'], (webCtx) => {
      if (!webCtx?.webServer || typeof webCtx.webServer.register !== 'function') return
      webCtx.effect(() =>
        webCtx.webServer.register({
          kind: 'prefix',
          path: WEB_BASE,
          async handler(req, res) {
            if (!isSameOriginBrowserRequest(req, { requireCustomHeader: false })) {
              return sendJson(res, 403, { ok: false, error: 'forbidden_host_or_origin' })
            }
            const url = new URL(req.url || '/', 'http://localhost')
            let rest = '/'
            try {
              rest = decodeURIComponent(url.pathname.slice(WEB_BASE.length)) || '/'
            } catch {
              return sendJson(res, 400, { ok: false, error: 'malformed_uri' })
            }

            if (rest === '/api/config/save') {
              try {
                const body = await readJsonBody(req, 32 * 1024)
                const patch = sanitizeUiPatch(body?.patch)
                const cliArgs = ['config', 'set', '--patch-json', JSON.stringify(patch)]
                if (body?.ack_auto_risk) cliArgs.push('--ack-auto-risk')
                const savedRes = await runHiremateCli(skillDir, cliArgs, {
                  timeoutMs: 15_000,
                  env: { HIREMATE_DISABLE_UPDATE_CHECK: '1' },
                })
                return sendJson(res, 200, savedRes)
              } catch (err) {
                return sendJson(res, err?.status || 500, {
                  ok: false,
                  error: String(err?.message || err),
                })
              }
            }

            if (rest === '/api/voice/collect') {
              try {
                await readJsonBody(req)
                const col = await runHiremateCli(skillDir, ['voice', 'collect'], {
                  timeoutMs: 25_000,
                  env: { HIREMATE_DISABLE_UPDATE_CHECK: '1' },
                })
                const autoSend = Boolean(col?.corpus_ready)
                const prompt = autoSend
                  ? `/hiremate 请从我本机的历史对话记录提炼《个人求职语气档案》。学习素材已去噪整理好：${col.corpus_path}（共筛选出 ${col.total_unique_found} 条消息，精选 ${col.picked_messages} 条，约 ${col.picked_chars} 字）。请按求职助手的语气建档规约（双证据约束 + 区分初聊 HR 与同行交流双档声线 + 递简历附言习惯）生成并保存档案，完成后发我过目。`
                  : '/hiremate 我想建立个人求职语气档案（本机历史对话样本较少，我可以直接粘贴几段平时真实发过的沟通消息给你提炼）：\n\n'
                return sendJson(res, 200, { ok: true, ...col, prompt, auto_send: autoSend })
              } catch (err) {
                return sendJson(res, err?.status || 500, {
                  ok: false,
                  error: String(err?.message || err),
                })
              }
            }

            if (req.method !== 'GET' && req.method !== 'HEAD') {
              return sendJson(res, 404, { ok: false, error: 'not_found' })
            }

            if (rest === '/' || rest === '/index.html') {
              try {
                const noUpdOpt = { env: { HIREMATE_DISABLE_UPDATE_CHECK: '1' }, timeoutMs: 20_000 }
                const [dashRes, cfgRes] = await Promise.all([
                  runHiremateCli(skillDir, ['build-dashboard'], noUpdOpt),
                  runHiremateCli(skillDir, ['config', 'show'], noUpdOpt),
                ])
                const outPath = dashRes?.out_path
                if (!outPath || !existsSync(outPath)) {
                  throw new Error('未能定位生成的本地看板文件')
                }
                const rawHtml = readFileSync(outPath, 'utf-8')
                const fullHtml = injectDshSidebarToolbar(rawHtml, {
                  onboardingCompleted: Boolean(cfgRes?.onboarding_completed),
                  voiceReady: Boolean(cfgRes?.voice_profile_ready),
                })
                return sendHtml(res, 200, fullHtml)
              } catch (err) {
                const escMsg = String(err?.message || err).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
                const errHtml = `<!doctype html><meta charset="utf-8"><body style="font:14px system-ui;padding:32px;color:#334155"><h3>求职助手侧栏面板加载失败</h3><p>${escMsg}</p></body>`
                return sendHtml(res, 500, errHtml)
              }
            }

            return sendJson(res, 404, { ok: false, error: 'not_found' })
          },
        })
      )
    })
  }

  // 按需注入可选 commands 服务，注册零 LLM Token 消耗的原生斜杠命令
  if (typeof ctx.inject === 'function') {
    ctx.inject(['commands'], (cmdCtx) => {
      if (!cmdCtx.commands || typeof cmdCtx.commands.register !== 'function') return

      cmdCtx.effect(() => {
        const disposers = []

        disposers.push(
          cmdCtx.commands.register({
            name: 'hiremate-status',
            description: '查看「求职助手」当前版本（免费版 / Pro 剩余天数与更新权益）、语气档案及本地投递漏斗汇总',
            handler: async () => {
              try {
                const noUpdOpt = { env: { HIREMATE_DISABLE_UPDATE_CHECK: '1' } }
                const [auth, sumResp, cfgResp] = await Promise.all([
                  runHiremateCli(skillDir, ['auth', 'status'], noUpdOpt),
                  runHiremateCli(skillDir, ['ledger', 'summary'], noUpdOpt),
                  runHiremateCli(skillDir, ['config', 'show']),
                ])
                const f = sumResp?.summary?.funnel || {}
                const cfg = cfgResp?.config || {}
                const policyText = (p, fallbackFilter) => {
                  const eff = p || (fallbackFilter === false ? 'allow_all' : 'block_all')
                  if (eff === 'allow_all') return '开放接受'
                  if (eff === 'inbound_only') return '仅接被动来信'
                  return '排除过滤'
                }
                const validityText = auth?.lifetime
                  ? '买断长期有效'
                  : auth?.days_left
                    ? `剩余 ${auth.days_left} 天`
                    : '生效中'
                const tierLine = auth?.active
                  ? `Pro 专业版生效中 · 账号 ${auth.email} · ${validityText}\n服务说明：${auth.updates_policy || '一次买断长期有效，持续享受常规功能更新与精准匹配词库升级'}`
                  : '当前版本：免费基础版（免登录即可建档筛岗；输入 /hiremate-login 绑定账户，输入 /hiremate-login pro 打开 ¥69 买断开通页）'
                const onboardLine = cfgResp?.onboarding_completed
                  ? null
                  : cfgResp?.hard_blocker === 'NO_RESUME_AVAILABLE'
                    ? '❌ 前置卡点：暂无任何本地或平台在线简历 — 请先在 BOSS 直聘填好在线简历或准备一份基础简历后再初始化'
                    : '📋 新手核对单：尚未完成建档 — 请对我说「帮我初始化求职助手」，核对浏览器权限并提供简历（拖入本地 PDF 或授权读取 BOSS 在线简历）'
                const modeLabel =
                  cfg.execution_mode === 'copilot'
                    ? '副驾驶粘贴模式（无需浏览器权限）'
                    : cfg.browser_provider === 'ego_lite'
                      ? '浏览器直连模式（ego lite 省Token）'
                      : cfg.browser_provider === 'builtin_browser_use'
                        ? '浏览器直连模式（宿主自带 Browser Use）'
                        : '浏览器直连模式'
                const resumeLabel =
                  cfg.resume_source === 'online_only'
                    ? '仅平台在线简历（无PDF附件）'
                    : cfgResp?.resume_ready
                      ? '真实履历与附件已就绪'
                      : '待提供简历'
                const voiceLabel = cfgResp?.voice_profile_ready
                  ? '已启用本人语气指纹'
                  : '默认得体口语（可在左侧栏「求职助手」一键从对话学习本人语气）'
                const autoTag = (m) => (m === 'auto' ? '全自动' : m === 'favorite' ? '仅自动收藏' : '半自动确认')
                const prefLine = `运行模式：${modeLabel} ｜ 档位：找岗「${autoTag(cfg.mode_discover)}」/ 回复「${autoTag(cfg.mode_reply)}」 ｜ 简历底座：${resumeLabel}\n筛选偏好：外包/派遣/OD「${policyText(cfg.outsourcing_policy, cfg.filter_outsourcing)}」 ｜ 驻场/外派「${policyText(cfg.onsite_policy, cfg.filter_onsite)}」 ｜ 猎头「${policyText(cfg.headhunter_policy, true)}」\n行文风格：${voiceLabel}`
                const proUpgradeHint =
                  auth?.active && cfgResp?.onboarding_completed && (cfg.mode_discover === 'semi' || cfg.mode_reply === 'semi')
                    ? '💡 Pro 托管提醒：当前尚有半自动项，可对助手说「开启 Pro 全自动模式（或找岗仅收藏+答疑全自动）并挂上工作日巡检」！'
                    : null
                const funnelParts2 = [
                  `累计评估 ${f.screened_total ?? 0} 岗（不匹配跳过 ${f.screened_skip ?? 0}）`,
                ]
                if ((f.favorited_pending ?? 0) > 0 || cfg.mode_discover === 'favorite') {
                  funnelParts2.push(`已收藏待投 ${f.favorited_pending ?? 0}`)
                }
                funnelParts2.push(
                  `开场已送达 ${f.opening_delivered_total ?? 0}`,
                  `双方已建联 ${f.connected_total ?? 0}`,
                  `PDF简历已送达 ${f.pdf_delivered_total ?? 0}`,
                  `待本人约面/决策 ${f.handed_off_pending ?? 0}`,
                  `面试与 Offer ${f.interview_total ?? 0}（待面 ${f.upcoming_interviews_count ?? 0} 场 · Offer ${f.offer_count ?? 0}）`
                )
                const funnelLine = funnelParts2.join(' ｜ ')
                const verTag = cfgResp?.version ? ` v${cfgResp.version}` : ''
                const upd = cfgResp?.update_notice || auth?.update_notice || sumResp?.update_notice || null
                const updateLine = upd?.update_available
                  ? `🚀 发现新版本 v${upd.latest_version}（当前 v${upd.current_version}）：${(upd.highlights || []).slice(0, 2).join('；') || '含最新功能与规则升级'}\n👉 直接输入 /hiremate-update 即可一键无损自动更新！`
                  : null
                const lines = [`【求职助手状态${verTag}】`, tierLine]
                if (updateLine) lines.push(updateLine)
                if (onboardLine) lines.push(onboardLine)
                if (proUpgradeHint) lines.push(proUpgradeHint)
                lines.push(
                  prefLine,
                  `投递漏斗：${funnelLine}`,
                  '📊 可视化求职看板：点击左侧栏「求职助手」直接打开工作台，或输入 /hiremate-dashboard 在浏览器弹开'
                )
                return { kind: 'success', text: lines.join('\n') }
              } catch (err) {
                return { kind: 'error', text: `读取求职助手状态失败：${err?.message || err}` }
              }
            },
          })
        )

        disposers.push(
          cmdCtx.commands.register({
            name: 'hiremate-login',
            description: '免验证码一键浏览器账户设备配对，或传入 pro 直接打开 Pro 买断开通页（特惠 ¥69）',
            input: { hint: '[可选：输入 pro 直接打开 ¥69 在线买断收银台]' },
            handler: async (invocation) => {
              const rawArg = String(invocation?.args || invocation?.input || '')
                .trim()
                .toLowerCase()
              try {
                if (rawArg === 'pro' || rawArg === 'upgrade' || rawArg === 'buyout') {
                  const upg = await runHiremateCli(skillDir, ['auth', 'upgrade', '--plan', 'pro_buyout'])
                  const bindHint = upg?.email
                    ? '支付完成后买断权益永久绑定至当前账户；随后运行 /hiremate-status 即可同步生效。'
                    : '支付完成后请再运行一次 /hiremate-login 将本机绑定至你的账户（无需输入任何激活码）。'
                  return {
                    kind: 'success',
                    text: [
                      '已在浏览器打开 Pro 买断开通中心（特惠买断 ¥69 / 原价 ¥99，支持微信支付 / 支付宝秒级直充账户）：',
                      `🔗 ${upg.upgrade_url}`,
                      bindHint,
                    ].join('\n'),
                  }
                }

                const pair = await runHiremateCli(skillDir, ['auth', 'login', '--no-wait'])
                if (pair?.status === 'authorized') {
                  const guideMd = pair?.pro_activation_guide?.user_reply_menu_markdown
                  const outLines = [pair.message || `设备已绑定（${pair.email}）`]
                  if (guideMd) {
                    outLines.push(
                      '',
                      guideMd,
                      '',
                      '💬 也可直接在对话框对助手说：「帮我开启 Pro 全自动模式并挂上工作日巡检」'
                    )
                  }
                  return { kind: 'success', text: outLines.join('\n') }
                }
                return {
                  kind: 'success',
                  text: [
                    '已在浏览器打开免验证码设备配对页（若未自动弹出请点击下方链接）：',
                    `🔗 ${pair.verification_uri_complete}`,
                    `🔑 核对设备配对码：${pair.user_code}`,
                    '在网页点击「确认授权此设备」后，再运行一次 /hiremate-login 或对助手说“完成求职助手登录”即可生效。',
                  ].join('\n'),
                }
              } catch (err) {
                return { kind: 'error', text: `发起设备配对失败：${err?.message || err}` }
              }
            },
          })
        )

        disposers.push(
          cmdCtx.commands.register({
            name: 'hiremate-dashboard',
            description: '一键在浏览器打开并刷新本地可视化求职投递看板（dashboard.html）',
            handler: async () => {
              try {
                const res = await runHiremateCli(skillDir, ['build-dashboard', '--open'])
                const openedNote = res?.browser_opened
                  ? '已在系统默认浏览器为你自动打开！'
                  : '已刷新生成！（若未自动弹出浏览器，请直接点击下方链接打开，或点击左侧栏「求职助手」）'
                const outLines = [
                  `📊 本地可视化求职看板${openedNote}（${res.tier === 'pro' ? 'Pro 专业版视角' : '免费基础版视角'} · 共 ${res.jobs_count} 条记录）`,
                  `🔗 浏览器直达：${res.file_url || res.out_path}`,
                  `📂 本地路径：${res.out_path}`,
                  '💡 看板四大视图：①「待处理队列」置顶约面/谈薪接管提醒与候选岗位 ｜ ②「面试与 Offer」日程置顶、各轮时间线与面经复盘 ｜ ③「全量岗位账本」搜索筛选全部投递与排雷记录 ｜ ④「当前求职策略与语气档案」',
                ]
                if (res?.update_notice?.update_available) {
                  outLines.push(`🚀 发现新版本 v${res.update_notice.latest_version}，输入 /hiremate-update 可一键自动升级。`)
                }
                return {
                  kind: 'success',
                  text: outLines.join('\n'),
                }
              } catch (err) {
                return { kind: 'error', text: `构建看板失败：${err?.message || err}` }
              }
            },
          })
        )

        disposers.push(
          cmdCtx.commands.register({
            name: 'hiremate-update',
            description: '检查新版本或一键无损自动更新「求职助手」（Gitee 主源 + GitHub 自动备源，保留全部本地求职数据）',
            input: { hint: '[留空直接升级；输入 check 仅检查新版本，输入 force 强制重装软链]' },
            handler: async (invocation) => {
              const rawArg = String(invocation?.args || invocation?.input || '')
                .trim()
                .toLowerCase()
              try {
                if (rawArg === 'check') {
                  const chk = await runHiremateCli(skillDir, ['update', '--check'], { timeoutMs: 20_000 })
                  if (!chk?.ok) {
                    return { kind: 'error', text: chk?.error || '检查更新失败，请稍后重试。' }
                  }
                  if (!chk.update_available) {
                    return {
                      kind: 'success',
                      text: `✅ 当前已是最新版本 v${chk.current_version}（检查源：${chk.source_used || 'gitee'}）。`,
                    }
                  }
                  const hl = (chk.highlights || []).map((h) => `• ${h}`).join('\n')
                  return {
                    kind: 'success',
                    text: [
                      `🚀 发现新版本 v${chk.latest_version}（当前 v${chk.current_version}）`,
                      ...(hl ? [hl] : []),
                      '👉 直接输入 /hiremate-update（不带参数）即可一键完成自动升级！',
                    ].join('\n'),
                  }
                }

                const cliArgs = rawArg === 'force' ? ['update', '--force'] : ['update']
                const res = await runHiremateCli(skillDir, cliArgs, { timeoutMs: 60_000 })
                if (!res?.ok) {
                  return { kind: 'error', text: res?.error || '自动更新未成功，本地版本与求职数据均完整保留。' }
                }
                if (res.already_latest) {
                  return { kind: 'success', text: `✅ ${res.message || `当前已是最新版本 v${res.current_version}。`}` }
                }
                const hl = (res.highlights || []).map((h) => `• ${h}`).join('\n')
                return {
                  kind: 'success',
                  text: [
                    `🎉 ${res.message || `已从 v${res.previous_version} 自动升级至 v${res.current_version}！`}`,
                    ...(hl ? ['本次更新亮点：', hl] : []),
                  ].join('\n'),
                }
              } catch (err) {
                return { kind: 'error', text: `执行自动更新失败：${err?.message || err}` }
              }
            },
          })
        )

        return () => {
          for (const d of disposers) d?.()
        }
      })
    })
  }
}
