// hiremate-dsh：将「求职助手」注册为 DeepSeek Harness 运行时 skill + 原生 `/` 斜杠命令。
// 零外部依赖：不 import 任何 @deepseek-ai/* 包，避免宿主 pnpm 闭包解析冲突。
import { execFile } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

export const name = 'hiremate-dsh'
export const inject = ['skills']

function stripFrontmatter(md) {
  if (!md.startsWith('---')) return md
  const end = md.indexOf('\n---', 4)
  if (end === -1) return md
  return md.slice(end + 4).replace(/^\n+/, '')
}

function resolveCliPath(skillDir) {
  const candidates = [
    join(skillDir, 'hiremate.py'),
    join(skillDir, '..', '..', '..', 'hiremate.py'),
    join(homedir(), '.hiremate', 'skill', 'hiremate', 'hiremate.py'),
    join(homedir(), '.dsh', 'skills', 'hiremate', 'hiremate.py'),
  ]
  for (const p of candidates) {
    if (existsSync(p)) return p
  }
  return candidates[0]
}

let cachedPythonSpec = null

async function runHiremateCli(cliPath, args) {
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
        timeout: 15_000,
        maxBuffer: 2 * 1024 * 1024,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
      })
      cachedPythonSpec = [bin, prefixArgs]
      return JSON.parse(stdout)
    } catch (err) {
      lastErr = err
      if (err && (err.code === 'ENOENT' || /Microsoft Store/i.test(String(err.stderr || err.message || '')))) {
        cachedPythonSpec = null
        continue
      }
      // 若 stdout 已经是合法业务 JSON 错误，直接解析返回；否则若为启动失败继续尝试备选解释器
      if (err?.stdout) {
        try {
          return JSON.parse(err.stdout)
        } catch {
          // ignore
        }
      }
    }
  }
  throw lastErr || new Error('未找到可用的 Python 3.9+ 解释器')
}

export function apply(ctx) {
  const base = new URL('./skills/hiremate/', import.meta.url)
  const skillDir = fileURLToPath(base)
  const cliPath = resolveCliPath(skillDir)
  const dshHome = process.env.DSH_HOME || join(homedir(), '.dsh')
  const userSkillMd = join(dshHome, 'skills', 'hiremate', 'SKILL.md')

  // 若用户级 ~/.dsh/skills/hiremate 尚未软链挂载，则由插件兜底注册 runtime skill；已挂载则避免同名重复注册
  if (!existsSync(userSkillMd)) {
    const content = stripFrontmatter(readFileSync(new URL('SKILL.md', base), 'utf-8'))
    ctx.effect(() => ctx.skills.register({
      name: 'hiremate',
      description: '求职助手（hiremate）：BOSS 直聘深筛排雷、四态投递账本、半自动/全自动消息答疑、多版简历路由与本地静态看板。',
      whenToUse: '当用户要初始化求职助手、筛选招聘岗位、跟进回复 HR 消息、查看求职看板或登录激活求职助手 Pro 时使用。',
      source: 'runtime',
      content,
      resourceBase: { kind: 'directory', path: skillDir },
    }))
  }

  // 对齐 dsh-oil-ui：按需注入可选 commands 服务，注册零 LLM Token 消耗的原生斜杠命令
  if (typeof ctx.inject === 'function') {
    ctx.inject(['commands'], (cmdCtx) => {
      if (!cmdCtx.commands || typeof cmdCtx.commands.register !== 'function') return

      cmdCtx.effect(() => {
        const disposers = []

        disposers.push(cmdCtx.commands.register({
          name: 'hiremate-status',
          description: '查看「求职助手」当前版本（免费版 / Pro 剩余天数与更新权益）及本地投递漏斗汇总',
          handler: async () => {
            try {
              const [auth, sumResp, cfgResp] = await Promise.all([
                runHiremateCli(cliPath, ['auth', 'status']),
                runHiremateCli(cliPath, ['ledger', 'summary']),
                runHiremateCli(cliPath, ['config', 'show']),
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
                : (auth?.days_left ? `剩余 ${auth.days_left} 天` : '生效中')
              const tierLine = auth?.active
                ? `Pro 专业版生效中 · 账号 ${auth.email} · ${validityText}\n服务说明：${auth.updates_policy || '一次买断长期有效，持续享受常规功能更新与排雷词库升级'}`
                : '当前版本：免费基础版（免登录即可建档筛岗；输入 /hiremate-login 绑定账户，输入 /hiremate-login pro 打开 ¥69 买断开通页）'
              const onboardLine = cfgResp?.onboarding_completed
                ? null
                : (cfgResp?.hard_blocker === 'NO_RESUME_AVAILABLE'
                    ? '❌ 前置卡点：暂无任何本地或平台在线简历 — 请先在 BOSS 直聘填好在线简历或准备一份基础简历后再初始化'
                    : '📋 新手核对单：尚未完成建档 — 请对我说「帮我初始化求职助手」，核对浏览器权限并提供简历（拖入本地 PDF 或授权读取 BOSS 在线简历）')
              const modeLabel = cfg.execution_mode === 'copilot'
                ? '副驾驶粘贴模式（无需浏览器权限）'
                : (cfg.browser_provider === 'ego_lite'
                    ? '浏览器直连模式（ego lite 省Token）'
                    : (cfg.browser_provider === 'builtin_browser_use'
                        ? '浏览器直连模式（宿主自带 Browser Use）'
                        : '浏览器直连模式'))
              const resumeLabel = cfg.resume_source === 'online_only'
                ? '仅平台在线简历（无PDF附件）'
                : (cfgResp?.resume_ready ? '真实履历与附件已就绪' : '待提供简历')
              const autoTag = (m) => (m === 'auto' ? '全自动' : '半自动确认')
              const prefLine = `运行模式：${modeLabel} ｜ 档位：找岗「${autoTag(cfg.mode_discover)}」/ 回复「${autoTag(cfg.mode_reply)}」 ｜ 简历底座：${resumeLabel}\n筛选偏好：外包/派遣/OD「${policyText(cfg.outsourcing_policy, cfg.filter_outsourcing)}」 ｜ 驻场/外派「${policyText(cfg.onsite_policy, cfg.filter_onsite)}」 ｜ 猎头「${policyText(cfg.headhunter_policy, true)}」`
              const proUpgradeHint = (auth?.active && cfgResp?.onboarding_completed && (cfg.mode_discover !== 'auto' || cfg.mode_reply !== 'auto'))
                ? '💡 Pro 全自动提醒：当前主动找岗/消息答疑仍为「半自动确认」，直接对助手说「开启 Pro 全自动模式并挂上工作日巡检」即可一键升级全托管！'
                : null
              const funnelLine = [
                `累计深筛 ${f.screened_total ?? 0} 岗（排雷跳过 ${f.screened_skip ?? 0}）`,
                `开场已送达 ${f.opening_delivered_total ?? 0}`,
                `双方已建联 ${f.connected_total ?? 0}`,
                `PDF简历已送达 ${f.pdf_delivered_total ?? 0}`,
                `待本人约面/决策 ${f.handed_off_pending ?? 0}`,
              ].join(' ｜ ')
              const lines = ['【求职助手状态】', tierLine]
              if (onboardLine) lines.push(onboardLine)
              if (proUpgradeHint) lines.push(proUpgradeHint)
              lines.push(prefLine, `投递漏斗：${funnelLine}`)
              return { kind: 'success', text: lines.join('\n') }
            } catch (err) {
              return { kind: 'error', text: `读取求职助手状态失败：${err?.message || err}` }
            }
          },
        }))

        disposers.push(cmdCtx.commands.register({
          name: 'hiremate-login',
          description: '免验证码一键浏览器账户设备配对，或传入 pro 直接打开 Pro 买断开通页（特惠 ¥69）',
          input: { hint: '[可选：输入 pro 直接打开 ¥69 在线买断收银台]' },
          handler: async (invocation) => {
            const rawArg = String(invocation?.args || invocation?.input || '').trim().toLowerCase()
            try {
              if (rawArg === 'pro' || rawArg === 'upgrade' || rawArg === 'buyout') {
                const upg = await runHiremateCli(cliPath, ['auth', 'upgrade', '--plan', 'pro_buyout'])
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

              const pair = await runHiremateCli(cliPath, ['auth', 'login', '--no-wait'])
              if (pair?.status === 'authorized') {
                const guideMd = pair?.pro_activation_guide?.user_reply_menu_markdown
                const outLines = [pair.message || `设备已绑定（${pair.email}）`]
                if (guideMd) {
                  outLines.push('', guideMd, '', '💬 也可直接在对话框对助手说：「帮我开启 Pro 全自动模式并挂上工作日巡检」')
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
        }))

        disposers.push(cmdCtx.commands.register({
          name: 'hiremate-dashboard',
          description: '一键生成并刷新本地只读静态求职投递看板（dashboard.html）',
          handler: async () => {
            try {
              const res = await runHiremateCli(cliPath, ['build-dashboard'])
              return {
                kind: 'success',
                text: `已刷新本地求职投递看板（${res.tier === 'pro' ? 'Pro 专业版视角' : '免费基础版视角'}，共 ${res.jobs_count} 条记录）：\n${res.out_path}`,
              }
            } catch (err) {
              return { kind: 'error', text: `构建看板失败：${err?.message || err}` }
            }
          },
        }))

        return () => {
          for (const d of disposers) d?.()
        }
      })
    })
  }
}
