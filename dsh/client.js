// 求职助手（hiremate）· DeepSeek Harness 浏览器侧栏插件：
// 1. 在左侧栏（sidebar.panellist）挂载「求职助手」入口；
// 2. 点击入口在主视图渲染同源实时投递看板与快捷工作台（/hiremate/）；
// 3. 页面内点击快捷动作通过 postMessage 唤起新会话、可靠写入草稿并在完备指令场景下自动发送执行
//    （显式通过 openWorkspace / 原型链 startSession 绕开第三方插件如 dsh-web-all 对实例 startSession 丢弃 options 参数的覆写）。
window.__ModuleLoader__.load({
  id: 'hiremate-dsh',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const React = require('react')
    const h = React.createElement
    const PANEL = 'hiremate'
    const SRC = '/hiremate/'

    function HiremateSidebarIcon(props) {
      const s = (props && props.size) || 18
      return h(
        'svg',
        {
          width: s,
          height: s,
          viewBox: '0 0 24 24',
          fill: 'none',
          stroke: 'currentColor',
          strokeWidth: 1.65,
          strokeLinecap: 'round',
          strokeLinejoin: 'round',
        },
        h('path', { d: 'M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2' }),
        h('rect', { x: 3, y: 7, width: 18, height: 13, rx: 2.5 }),
        h('path', { d: 'M3 13h18' }),
        h('circle', { cx: 12, cy: 13, r: 1.6 })
      )
    }

    function resolveTargetWorkspaceId(workspacesSvc, sessionsSvc) {
      if (!workspacesSvc || !workspacesSvc.list || typeof workspacesSvc.list.getSnapshot !== 'function') {
        return undefined
      }
      const wsSnap = workspacesSvc.list.getSnapshot()
      const items = (wsSnap && Array.isArray(wsSnap.items) && wsSnap.items) || []
      if (!items.length) return undefined

      const sessSnap =
        sessionsSvc && sessionsSvc.list && typeof sessionsSvc.list.getSnapshot === 'function'
          ? sessionsSvc.list.getSnapshot()
          : null
      const byId = (sessSnap && sessSnap.byId) || {}

      let mainSessionId
      for (const id of Object.keys(byId)) {
        const row = byId[id]
        if (row && row.retainedBy && row.retainedBy.mainView > 0) {
          mainSessionId = row.id || id
          break
        }
      }
      if (mainSessionId) {
        const matched = items.find((w) => Array.isArray(w.sessionIds) && w.sessionIds.includes(mainSessionId))
        if (matched && matched.workspaceId) return matched.workspaceId
      }

      let selectedId
      let selectedTime = Number.NEGATIVE_INFINITY
      for (const w of items) {
        let latest = Number.NEGATIVE_INFINITY
        if (Array.isArray(w.sessionIds)) {
          for (const sid of w.sessionIds) {
            const s = byId[sid]
            if (s && typeof s.updatedAt === 'number') latest = Math.max(latest, s.updatedAt)
          }
        }
        if (latest === Number.NEGATIVE_INFINITY && w.createdAt) {
          const parsed = Date.parse(w.createdAt)
          if (!Number.isNaN(parsed)) latest = parsed
        }
        if (selectedId === undefined || latest > selectedTime) {
          selectedId = w.workspaceId
          selectedTime = latest
        }
      }
      return selectedId || (items[0] && items[0].workspaceId)
    }

    function callOfficialStartSession(uiWorkspace, workspaceId, options) {
      if (!uiWorkspace) throw new Error('uiWorkspace 服务不可用')
      // 绕开第三方扩展（如 @linxin666/dsh-web-all auto-isolation）在实例上挂载的单参数 startSession 包装器
      const proto = Object.getPrototypeOf(uiWorkspace)
      const fn =
        proto && typeof proto.startSession === 'function' ? proto.startSession : uiWorkspace.startSession
      if (typeof fn !== 'function') throw new Error('uiWorkspace.startSession 不可用')
      return fn.call(uiWorkspace, workspaceId, options)
    }

    async function waitForSessionOpen(session, timeoutMs) {
      if (!session) return
      if (typeof session.open === 'function') {
        await Promise.race([
          session.open(),
          new Promise((resolve) => setTimeout(resolve, timeoutMs)),
        ])
        return
      }
      if (typeof session.getSnapshot !== 'function' || typeof session.subscribe !== 'function') return
      const cur = session.getSnapshot()
      if (cur && cur.openState === 'open') return
      await new Promise((resolve) => {
        let settled = false
        let off = null
        const done = () => {
          if (settled) return
          settled = true
          if (typeof off === 'function') off()
          resolve()
        }
        const timer = setTimeout(done, timeoutMs)
        off = session.subscribe(() => {
          const snap = session.getSnapshot()
          if (snap && (snap.openState === 'open' || snap.openState === 'error')) {
            clearTimeout(timer)
            done()
          }
        })
      })
    }

    function apply(ctx) {
      const frames = new Set()
      let scheme = null
      let extraServices = { workspaces: null, sessions: null, conversation: null }

      if (typeof ctx.inject === 'function') {
        ctx.inject(['workspaces', 'sessions', 'conversation'], (c) => {
          extraServices = {
            workspaces: (c && c.workspaces) || null,
            sessions: (c && c.sessions) || null,
            conversation: (c && c.conversation) || null,
          }
        })
      }

      const resolveServices = () => ({
        workspaces:
          extraServices.workspaces || (typeof ctx.get === 'function' ? ctx.get('workspaces') : null),
        sessions:
          extraServices.sessions || (typeof ctx.get === 'function' ? ctx.get('sessions') : null),
        conversation:
          extraServices.conversation || (typeof ctx.get === 'function' ? ctx.get('conversation') : null),
      })

      const postScheme = (frame) => {
        if (scheme && frame && frame.contentWindow) {
          frame.contentWindow.postMessage({ type: 'hiremate:scheme', scheme }, location.origin)
        }
      }

      if (typeof ctx.inject === 'function') {
        ctx.inject(['theme'], (c) => {
          if (!c || !c.theme || typeof c.theme.getTheme !== 'function') return
          const push = (snap) => {
            scheme = snap && snap.active ? snap.active.colorScheme : null
            frames.forEach(postScheme)
          }
          push(c.theme.getTheme())
          if (typeof c.on === 'function') c.on('theme/change', push)
        })
      }

      let lang = /^en\b/i.test(navigator.language) ? 'en' : 'zh'
      if (typeof ctx.inject === 'function') {
        ctx.inject(['locale'], (c) => {
          if (!c || !c.locale || typeof c.locale.getSnapshot !== 'function') return
          const push = (snap) => {
            if (snap && snap.active) lang = snap.active
          }
          push(c.locale.getSnapshot())
          if (typeof c.on === 'function') c.on('locale/change', push)
        })
      }

      const LABEL = { zh: '求职助手', en: 'HireMate' }

      async function launchPromptSession(promptText, autoSend) {
        const { workspaces, sessions, conversation } = resolveServices()
        const draftOptions = { prompt: promptText, whenDraftExists: 'replace' }

        if (
          ctx.uiWorkspace &&
          typeof ctx.uiWorkspace.openWorkspace === 'function' &&
          workspaces &&
          sessions &&
          typeof sessions.binding === 'function' &&
          conversation &&
          conversation.input &&
          typeof conversation.input.requestDraftInitialization === 'function'
        ) {
          const targetWorkspaceId = resolveTargetWorkspaceId(workspaces, sessions)
          if (!targetWorkspaceId) {
            callOfficialStartSession(ctx.uiWorkspace, undefined, draftOptions)
            return
          }

          let openedSessionId = null
          await ctx.uiWorkspace.openWorkspace(targetWorkspaceId, (sid) => {
            openedSessionId = sid
            const binding = sessions.binding(sid)
            if (binding) {
              conversation.input.requestDraftInitialization(binding, draftOptions)
            }
          })

          const binding = openedSessionId ? sessions.binding(openedSessionId) : null
          if (binding && typeof conversation.input.for === 'function') {
            const shell = conversation.input.for(binding.ctx)
            const snap = shell && shell.state && typeof shell.state.getSnapshot === 'function'
              ? shell.state.getSnapshot()
              : null
            if (shell && shell.actions && typeof shell.actions.setDraft === 'function') {
              if (!snap || !snap.draft || !snap.draft.trim()) {
                shell.actions.setDraft(promptText)
              }
            }
            if (autoSend && shell && shell.actions && typeof shell.actions.submit === 'function') {
              await waitForSessionOpen(binding.session, 4000)
              await new Promise((resolve) => requestAnimationFrame(() => resolve()))
              const latest =
                shell.state && typeof shell.state.getSnapshot === 'function'
                  ? shell.state.getSnapshot()
                  : null
              if (!latest || latest.phase === 'plain' || latest.phase === 'claimed') {
                shell.actions.submit()
              }
            } else if (shell && typeof shell.focus === 'function') {
              shell.focus()
            }
          }
          return
        }

        callOfficialStartSession(ctx.uiWorkspace, undefined, draftOptions)
      }

      function HirematePanel() {
        const ref = React.useRef(null)
        const [src] = React.useState(() => `${SRC}?t=${Date.now()}`)

        React.useEffect(() => {
          const self = ref.current
          if (self) frames.add(self)

          function onMessage(e) {
            const frame = ref.current
            if (!frame || e.source !== frame.contentWindow || e.origin !== location.origin) return
            const m = e.data
            if (!m || m.type !== 'hiremate:start' || typeof m.prompt !== 'string') return
            const autoSend = m.autoSend !== false
            launchPromptSession(m.prompt, autoSend).catch((err) => {
              console.error('[hiremate-dsh] 启动新对话失败', err)
              if (frame.contentWindow) {
                frame.contentWindow.postMessage(
                  { type: 'hiremate:start-failed', message: String((err && err.message) || err) },
                  location.origin
                )
              }
            })
          }

          addEventListener('message', onMessage)
          return () => {
            removeEventListener('message', onMessage)
            frames.delete(self)
          }
        }, [])

        return h('iframe', {
          ref,
          src,
          title: LABEL[lang] || LABEL.zh,
          allow: 'clipboard-write; fullscreen',
          onLoad: (e) => postScheme(e.currentTarget),
          style: { width: '100%', height: '100%', border: 0, display: 'block' },
        })
      }

      ctx.slots.inject('sidebar.panellist', () =>
        ctx.slots.register(
          { name: 'sidebar.panellist', id: PANEL, order: 7, label: () => LABEL[lang] || LABEL.zh },
          HiremateSidebarIcon
        )
      )
      ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: PANEL }, HirematePanel))
    }

    exports.isPlugin = true
    exports.inject = ['slots', 'uiWorkspace']
    exports.apply = apply
    return module.exports
  },
})
