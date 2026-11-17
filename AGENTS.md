# AGENTS.md — 求职助手（HireMate）自动安装与冷启动接手指令

> 本文件面向用户正在使用的桌面 AI Agent（`WorkBuddy`、`DeepSeek Harness (dsh)`、`Claude Code`、`Codex` 等）。当用户把本仓库地址或目录发给你并要求安装、登录或启动「求职助手」时，请直接按以下四步自动推进闭环，不要让用户自己手敲命令行。

## 第一步：幂等安装并挂载多端技能与斜杠命令

若你当前尚未在本地仓库目录中，先幂等克隆或更新仓库至 `~/.hiremate/repo`，然后运行安装器（兼容开源独立仓根目录 `install_plugin.py` 与全栈仓 `open/install_plugin.py`）：

```bash
if [ ! -f "install_plugin.py" ] && [ ! -f "open/install_plugin.py" ]; then
  if [ -d "$HOME/.hiremate/repo/.git" ]; then
    git -C "$HOME/.hiremate/repo" pull --ff-only || true
  else
    mkdir -p "$HOME/.hiremate"
    git clone <用户提供的仓库地址> "$HOME/.hiremate/repo"
  fi
  cd "$HOME/.hiremate/repo"
fi

if [ -f "install_plugin.py" ]; then
  python3 install_plugin.py
elif [ -f "open/install_plugin.py" ]; then
  python3 open/install_plugin.py
fi
```

- 安装器会将唯一主副本写入 `~/.hiremate/skill/hiremate`，并建立目录软链同步至本机已安装的 `WorkBuddy`、`dsh`、`Claude Code`、`Codex`；
- **当前会话无需等待用户重启客户端**：直接调用 `python3 ~/.hiremate/skill/hiremate/hiremate.py` 即可立即执行后续步骤（仅 `dsh` 的 `/hiremate-status`、`/hiremate-login`、`/hiremate-dashboard`、`/hiremate-update` 四个快捷斜杠命令菜单在下次重载后显示，顺带提一句即可，不要停下等重启）；
- **后续自动感知与一键升级**：日常命令每 6 小时静默向公开仓（Gitee 主源 + GitHub 备源）检查一次新版本，返回 `update_notice` 时在回复末尾提醒用户说「升级求职助手」（运行 `hiremate.py update` 或 `/hiremate-update`）即可全宿主无损自动升级；
- 安装完成后，请立即读取 `~/.hiremate/skill/hiremate/SKILL.md` 按标准规约执行。

## 第二步：先查本地状态 + 浏览器通道能力体检，生成《四项启动准备核对单》

先在本地自检你当前宿主**自带的 Browser Use 是否同时支持「有头可见窗口（Headed）」与「登录态持久化（Persistent Profile）」**，然后运行以下三条命令探查当前状态与核对单：

```bash
# 自带 Browser Use 同时具备「有头可见 + 持久化登录目录」传 true；缺任一项或无自带浏览器传 false
python3 ~/.hiremate/skill/hiremate/hiremate.py checklist --builtin-headed-persistent <true|false>
python3 ~/.hiremate/skill/hiremate/hiremate.py config show
python3 ~/.hiremate/skill/hiremate/hiremate.py auth status
```

- **【安全死红线】严禁使用 Headless 无头模式、每次新建空白临时窗口，或 `curl`/`wget`/Python `urllib`/`requests` 爬虫脚本请求 BOSS 直聘网页**（必触发反爬风控伤及用户账号）！
- **若用户明确提到“登录 / 绑定账户 / 我买了 Pro / 开通 Pro”**：运行 `auth login` 完成免验证码浏览器设备配对（未购买需打开收银台时运行 `auth upgrade`，付完款后再跑 `auth login` 绑定本机或 `auth status` 同步）：
  - **沙盒弹窗兜底与防「无链接问题框」铁律**：切勿传 `--no-browser`；在 `Codex` 等沙盒宿主中优先申请非沙盒权限运行 `auth login`。若返回 `status: "authorization_pending"` 且 `browser_opened: false`，说明沙盒拦截了系统 `open`，**用户根本没看到确认授权页**——你必须**立即用可见有头浏览器（`ego lite` 或宿主有头浏览器）主动打开返回的 `verification_uri_complete`**（或申请宿主机权限执行 `open`），并**直接在普通回复正文中贴出完整 URL `verification_uri_complete` 与配对码 `user_code`**；**严禁调用 `request_user_input` / `ask_question` 等阻塞式提问框等待网页登录或设备确认**（会在回复渲染前挂起回合、遮挡正文里的授权链接）！
  - **当返回 JSON 中包含 `pro_activation_guide`（首次绑定或刚升级为 Pro）时**，**严禁只说一句“登录成功”就停下**——必须向用户展示 `pro_activation_guide` 中的 **5 项已解锁 Pro 高级权益**，并明确告知当前「主动找岗」与「消息答疑」若仍为免费版默认的**半自动（`semi`）**，现已可切换为**全自动模式（`auto`）** + 开启工作日错峰自动巡检（若尚未建档则紧接着进入第三步建档）；日常会话中若老用户已展示过引导且无 `pro_activation_guide` 字段，则静默读取 `tier` 不重复打扰；
- **若为普通免费版新用户（未提及登录或付费）**：**绝不强制注册登录，绝不生硬推销付费**！开源免费版免登录即可完整使用。

## 第三步：向用户展示《四项启动准备核对单》与《8 项职位筛选策略表》，确认后建档

若 `onboarding_completed` 为 `false`（或 `browser_provider` 尚未选定），请用清晰友好的中文向用户展示**《启动准备核对单》**，带用户一次性核对：

1. **`[核对项 1 · 浏览器操控通道选择]`**：
   - **分支 A · 宿主自带 Browser Use【不支持】有头 + 持久化**（如无内置浏览器、仅支持 Headless 无头模式、或用完即毁的临时实例）：
     - **直接跳过自带 Browser Use，安装并使用 `ego lite`（`ego-browser`），并明确告知用户原因**：BOSS 直聘风控极严，无头或临时浏览器极易掉登录并触发滑块/封控，因此已自动跳过宿主自带 Browser Use，切换为支持有头持久化登录态的 `ego lite`；
     - 若本机已安装 `ego-browser`（`checklist` 返回 `ego_lite_installed: true`），直接运行 `config set --browser-provider ego_lite` 启用；若未安装，立即协助安装（若本机已有 `ego-browser` 技能的 `scripts/install.sh` 可直接执行；新机首次安装请引导下载 <https://lite.ego.app/> 完成安装与首次打开引导，每次操作网页前运行 `ego-browser skill` 读取当前版本使用指南），就绪后运行 `config set --browser-provider ego_lite`（仅当用户明确表示当前受限环境无法装软件时，运行 `config set --execution-mode copilot` 切入「副驾驶粘贴模式」继续）。
   - **分支 B · 宿主自带 Browser Use【支持】有头可见 + 登录态持久化**：
     - 若 `browser_provider` 仍为 `"auto"`，**把选择权交给用户**，请用户按自身 Token 情况二选一：
       1. **想节省 Token → 安装并使用 `ego lite`**：基于紧凑 DOM 引用快照，大幅降低单轮搜岗与 Pro 定时巡检 Token 消耗（未安装则顺手代装，选定后运行 `config set --browser-provider ego_lite`）；
       2. **Token 多随便用 → 直接使用宿主自带 Browser Use**：免额外装任何软件开箱即用（底层多走视觉截图推理，Token 消耗较高，选定后运行 `config set --browser-provider builtin_browser_use`）。
2. **`[核对项 2 · 招聘平台本人登录态]`**：确认日常浏览器已登录本人 BOSS 直聘账号。
3. **`[核对项 3 · 必选硬门槛：至少一份真实简历（二选一，无简历绝不往下走）]`**：
   - **情况 A（有本地简历文件）**：请用户直接把 **1～3 份 PDF / Word / 文本简历**拖进对话（`--resume-source attachment_pdf`）；
   - **情况 B（无本地文件，但已填好 BOSS 直聘「在线简历」）**：
     - **你有浏览器权限时**：直接打开 `https://www.zhipin.com/web/geek/resume` 自动读取用户的**在线简历全文**（提取全部过往雇主、项目成果与求职期望）用于建档！同时顺手看一眼右侧「附件简历」栏：若附件数为 0，提醒用户可点在线简历右上角「生成附件简历」一键转存线上 PDF，或先传 `--resume-source online_only` 记录为仅在线简历模式；
     - **你无浏览器权限时（副驾驶模式）**：请用户在 BOSS 在线简历页点导出 PDF 拖进对话，或直接全选复制在线简历文字发进对话；
   - **情况 C（既无本地简历，平台也没填在线简历）**：**【硬性红线停步】** 运行 `config set --resume-source none`，明确温和告知用户：没有真实简历底座无法继续（严禁无中生有编造经历或漏防前东家），请先去 BOSS 填好在线简历或准备一份基础简历后再随时喊你继续。
4. **`[核对项 4 · 全部 8 项职位筛选策略透明确认（严禁暗箱静默套用默认值！）]`**：
   - **必须把以下 8 项默认筛选策略完整列给用户确认是否需要修改**（绝不能让用户事后自己去翻求职看板才发现这些规则）：
     1. **目标岗位方向**（从简历提炼预填）；
     2. **优先城市 & 异地大厂弹性**（默认**开启**「异地知名大厂可先聊」，不想看异地大厂可传 `--remote-bigtech-flex false`）；
     3. **期望最低月薪 & 面板标薪容忍度**（默认**开启**「容忍面板标薪略低约 20% 先聊」，想严格卡死标薪可传 `--salary-panel-tolerance false`）；
     4. **外包 / 派遣 / OD 策略**（默认 `block_all` 全部排除；支持 `inbound_only` 仅接被动来信 / `allow_all` 开放）；
     5. **驻场 / 现场外派策略**（默认 `block_all` 全部排除；可与外包独立分开设置）；
     6. **猎头渠道策略**（默认 `inbound_only` 主动搜岗不投猎头、仅接猎头主动优质来信；可改 `block_all` 全部屏蔽 / `allow_all` 全部开放）；
     7. **自动拉黑前东家 + 额外屏蔽企业 + 岗位排除词**（简历中的全部前东家自动同字号拉黑；支持追加 `--blocked-companies` 与 `--exclude-keywords`）；
     8. **被动索要简历策略 & 自动化档位**（默认 `generous` 除非明显不符尽量给，可改 `strict`；免费版为半自动审阅，Pro 版可随时将主动找岗与消息答疑切换为 `auto` 全自动）。

拿到简历内容（本地文件或在线简历）后：
1. **先输出精简清晰的《求职建档与 8 项筛选策略确认单》**（含提炼的《个人求职事实与答法档案》摘要、自动识别的前东家名单、以及上述 **全部 8 项筛选策略的当前设定值**），明确请用户确认或指出要修改的项（**未经展示确认，严禁直接静默调 `init` 把默认策略偷偷落盘**）；
2. 用户点头确认（或按用户意见调整对应参数）后，运行 `hiremate.py init --employers "<全部前东家>" --roles "..." --cities "..." --min-salary-k ... --resume-source <attachment_pdf|online_only|both>`（附带用户修改的策略参数），并将确认后的档案写入 `profile_facts_file`；
3. 有浏览器权限时核对 BOSS 右上角本人姓名（`--account-name`）与线上「附件简历」精确文件名（写入 `resume_routes`）；副驾驶模式下口头询问确认即可。

## 第四步：【严禁建完档停步冷场】自动生成初始看板并给出 1/2/3 下一步行动菜单

完成 `init` 并将档案写入 `profile_facts_file` 后，**必须在同一条回复回合内连续做完以下两件事，绝不能只说一句“建档已完成”就结束对话**：

1. **立即顺手生成初始求职看板**：运行 `python3 ~/.hiremate/skill/hiremate/hiremate.py build-dashboard`，把生成的 `dashboard.html` 本地路径附给用户（免费版看板内自带 Pro 一键在线买断入口与首轮启动口令）；
2. **在回复末尾展示可一键回复数字 `1 / 2 / 3` 的《下一步开始菜单》**（直接复用 `init` 或 `build-dashboard` 返回 JSON 中的 `post_onboarding_guide.user_reply_menu_markdown`）：
   - **具备浏览器权限时（免费版）**：明确提示用户**直接回复 `1`（推荐）**即可立即去 BOSS 直聘跑第一轮搜岗深筛与半自动破冰预览（先出审阅单，未经确认绝不点发送）；回复 `2` 可粘贴单条岗位 JD / HR 消息参谋；回复 `3` 查看看板或微调筛选条件；
   - **具备浏览器权限时（Pro 买断版）**：提示回复 `1` 立即跑首轮 Pro 深度排雷筛岗 + 检查未读消息；回复 `2` 一键挂载**工作日错峰自动巡检定时任务**（周一至周五 `09:00–12:00`、`14:00–18:00` 每 10～15 分钟）；回复 `3` 调整 `semi` / `auto` 档位；
   - **无浏览器权限（`copilot` 副驾驶粘贴模式）时**：提示回复 `1` 或直接把 BOSS 岗位 JD / 截图贴进对话试跑排雷与破冰草稿；回复 `2` 粘贴 HR 追问消息拟真人短句回复；回复 `3` 开启浏览器自动直连模式。
