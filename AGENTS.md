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

## 第二步：先查本地状态 + 浏览器权限体检，生成《四项启动准备核对单》

先在本地自检你当前会话**是否具备正规浏览器操控工具**（如 `ego-browser`、宿主内置浏览器或 Chrome/Playwright MCP），然后运行以下三条命令探查当前状态与核对单：

```bash
# 若你具备可用浏览器操控工具传 true，没有则传 false
python3 ~/.hiremate/skill/hiremate/hiremate.py checklist --browser-capable <true|false>
python3 ~/.hiremate/skill/hiremate/hiremate.py config show
python3 ~/.hiremate/skill/hiremate/hiremate.py auth status
```

- **【安全死红线】无浏览器操控工具时，绝对禁止使用 `curl`、`wget`、Python `urllib`/`requests` 或任何爬虫脚本请求 BOSS 直聘网页**（会秒触发反爬风控伤及用户账号）！
- **若用户明确提到“登录 / 绑定账户 / 我买了 Pro / 开通 Pro”**：先运行 `python3 ~/.hiremate/skill/hiremate/hiremate.py auth login`（或 `auth upgrade`）弹出系统默认浏览器完成免验证码一键设备授权（此步仅弹网页，无需你有浏览器操控权限），确认 `tier` 状态后紧接着进入第三步（切勿登完就停下冷场）；
- **若为普通免费版新用户（未提及登录或付费）**：**绝不强制注册登录，绝不生硬推销付费**！开源免费版免登录即可完整使用。

## 第三步：向用户展示《四项启动准备核对单（Checklist）》并完成 5 分钟建档

若 `onboarding_completed` 为 `false`，请用清晰友好的中文向用户展示**《启动准备核对单》**，带用户一次性逐项核对：

1. **`[核对项 1 · 浏览器操控权限]`**：
   - **有权限**：告知已就绪，可直接复用已登录浏览器自动读在线简历、筛岗与收发消息；
   - **无权限**：当场提前挑明，给出当前宿主的一句话开通方法（`WorkBuddy` 打开内置网页/`ego-browser` 授权；`dsh` / `Claude Code` / `Codex` 挂载 `ego-browser` 或 Chrome MCP；**提醒 Pro 用户自动盯盘必须开启此权限**）；若用户暂不开启，运行 `config set --execution-mode copilot` 切入**「副驾驶粘贴模式」**照常继续建档，绝不卡死。
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
