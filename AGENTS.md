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
- **当前会话无需等待用户重启客户端**：直接调用 `python3 ~/.hiremate/skill/hiremate/hiremate.py` 即可立即执行后续步骤（仅 `dsh` 的 `/hiremate-status`、`/hiremate-login`、`/hiremate-dashboard` 三个快捷斜杠命令菜单在下次重载后显示，顺带提一句即可，不要停下等重启）；
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
- **若用户明确提到“登录 / 绑定账户 / 我买了 Pro / 开通 Pro”**：先运行 `python3 ~/.hiremate/skill/hiremate/hiremate.py auth login`（或 `auth upgrade`）弹出系统默认浏览器完成免验证码一键设备授权（此步仅弹网页，无需浏览器操控权限），确认 `tier` 状态后紧接着进入第三步（切勿登完就停下冷场）；
- **若为普通免费版新用户（未提及登录或付费）**：**绝不强制注册登录，绝不生硬推销付费**！开源免费版免登录即可完整使用。

## 第三步：向用户展示《四项启动准备核对单（Checklist）》并完成 5 分钟建档

若 `onboarding_completed` 为 `false`（或 `browser_provider` 尚未选定），请用清晰友好的中文向用户展示**《启动准备核对单》**，带用户一次性核对：

1. **`[核对项 1 · 浏览器操控通道选择]`**：
   - **分支 A · 宿主自带 Browser Use【不支持】有头 + 持久化**（如无内置浏览器、仅支持 Headless 无头模式、或用完即毁的临时实例）：
     - **直接跳过自带 Browser Use，安装并使用 `ego lite`（`ego-browser`），并明确告知用户原因**：BOSS 直聘风控极严，无头或临时浏览器极易掉登录并触发滑块/封控，因此已自动跳过宿主自带 Browser Use，切换为支持有头持久化登录态的 `ego lite`；
     - 若本机已安装 `ego-browser`（`checklist` 返回 `ego_lite_installed: true`），直接运行 `config set --browser-provider ego_lite` 启用；若未安装，立即协助安装（macOS 可执行 `ego-browser` 技能内置 `install.sh` 拉起应用，Windows/通用引导下载 <https://lite.ego.app/> 并完成首次打开引导以注册 `ego-browser` 命令），就绪后运行 `config set --browser-provider ego_lite`（仅当用户明确表示当前受限环境无法装软件时，运行 `config set --execution-mode copilot` 切入「副驾驶粘贴模式」继续）。
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
4. **`[核对项 4 · 求职边界与筛选偏好（给清默认值，只改不满意的）]`**：
   - 目标岗位词、优先城市、期望最低月薪（K）（优先从简历/在线简历自动提取供确认）；
   - **外包 / 派遣 / OD 接受度**与**驻场办公接受度**（默认均排除，支持各自独立设为「排除 / 仅接被动来信 / 完全开放」）；
   - 额外想拉黑的企业（简历/在线简历中的全部前东家会自动归一化拉黑）。

拿到简历内容（本地文件或在线简历）后：
1. 先输出精简**《求职建档确认单》**（含前东家屏蔽清单、筛选偏好、《个人求职事实与答法档案》摘要）请用户点头确认；
2. 确认后运行 `hiremate.py init --employers "<全部前东家>" --roles "..." --cities "..." --min-salary-k ... --resume-source <attachment_pdf|online_only|both>`，并将确认后的档案写入 `profile_facts_file`；
3. 有浏览器权限时核对 BOSS 右上角本人姓名（`--account-name`）与线上「附件简历」精确文件名（写入 `resume_routes`）；副驾驶模式下口头询问确认即可。

## 第四步：按「版本 × 浏览器权限」闭环交付首次体验

- **具备浏览器权限时**：
  - **免费基础版**：引导立即在 BOSS 执行**首轮搜岗深筛与半自动破冰预览**（出审阅单，回序号再发），并运行 `build-dashboard` 生成本地看板；
  - **Pro 买断版**：确认找岗与答疑档位（`semi` / `auto`），跑首轮云端深度评估与看板刷新，并主动询问是否一键挂载**工作日错峰自动巡检定时任务**（周一至周五 `09:00–12:00`、`14:00–18:00` 每 10～15 分钟）。
- **无浏览器权限（`copilot` 副驾驶粘贴模式）时**：
  - 刷新生成初始 `dashboard.html` 看板，并引导用户直接把看到的 BOSS 岗位 JD 或 HR 消息粘贴/截图发进对话——你照常调用 `screen-basic` / `pro evaluate-job` / `pro guard-reply` / `ledger` 完成排雷、拟定真人短句草稿与四态记账，由用户复制发送。
