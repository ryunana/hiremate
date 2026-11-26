# 求职助手 HireMate

把简历交给 AI，它帮你逐岗筛掉不匹配、外包和空壳公司，一岗一议写好真诚破冰；如果你的电脑开着已登录的浏览器，它还能按你的作息帮你收藏、到点统一投、盯未读消息，聊到约面或谈薪再叫你。

🌐 **[官方网站与实况演示](https://hiremate.chengway.wang)** ｜ 🔑 **[Pro 账户与激活中心](https://hiremate.chengway.wang/activate)** ｜ 💬 **[问题与建议反馈](https://github.com/ryunana/hiremate/issues)**

---

## 最省事的用法：把下面这段发给 AI

不用读文档，不用敲命令。复制这段，发给你的桌面 AI（WorkBuddy / Codex / DeepSeek Harness / Claude Code / Cursor 都行）：

```text
帮我装这个技能，装好之后带我走一遍：
https://github.com/ryunana/hiremate

我的简历在这里：<拖入 PDF/Word 简历，或者写「直接读我 BOSS 直聘的在线简历」>

装完先别直接投。先带我核对简历亮点、前东家屏蔽名单和筛选条件；
等我都确认没问题，再跑一轮岗位预览给我看。
```

剩下的它自己会做。你只需要回答它的问题。

---

## 它能替你做六件事

**1. 把简历变成档案，顺手拉黑前东家。** 拖入简历（或直接读 BOSS 在线简历），5 分钟提炼出你的真实项目成果与答法；自动识别所有过往雇主及同字号关联公司加入黑名单，防止误投尴尬。

**2. 逐岗筛掉水分，还能查公司底细。** 拒绝无脑海投——逐条核对 JD 诉求与四处工作地点，绕开外包、驻场和前东家；遇到 20 人以下小公司、薪资高得离谱或背景存疑的企业，自动做「公司底细核查」（识破伪装劳务派遣与跨界空壳，同时不误杀自研金融/零售/初创公司）。

**3. 晚间只收藏，工作日到点统一投。** 收藏、回复主动找上门的 HR、以及批次补投都不会再偷吃你的每日主动投递额度；你可以晚上或周末先攒一批收藏，定好时间到点串行投递，没到点代码层直接锁死，AI 想抢跑也投不出去，遇验证码暂停后还能断点续投。

**4. 一岗一议写人话，越用越像你自己。** 禁用 AI 模板腔和分点列表，紧扣对方岗位看重的业务指标引用你的真实成果；支持从你的历史对话里提炼「个人求职语气档案」，也可以挂一份自己的 `execution-rules.md` 记住特殊挑岗习惯。

**5. 聊到约面或谈薪，立刻停手叫你。** 日常推进可按需设为半自动审阅或工作日自动回复（Pro），一旦涉及面试时间、加微信电话、期望薪资、或超出简历范围的问题，马上停手并弹出系统通知，拍板权 100% 留给你。

**6. 所有进度一屏看清，7 天未回自动提醒跟进。** 本地 SQLite 精细记账（严格区分已打招呼、已建联、待同意简历、PDF 已送达、待约面、面试各轮次与 Offer），随时一键打开本地可视化看板或导出原生 Excel；明确拒绝的岗位永久跳过，投过超 7 天没回的默认允许礼貌再跟一次。

## 你可以这样问它 / Example prompts

- 帮我初始化求职助手，这是我的简历：`<拖入简历>`
- 帮我在 BOSS 搜一轮匹配的岗位，先出预览别直接发
- 晚上帮我只点收藏别发消息，建个批次明天早上 09:30 再统一投
- 这家 0-20 人的公司看着有点怪，帮我查一下公司底细再决定投不投
- 从我的历史对话里学一下我的说话风格，建个个人求职语气档案
- 打开我的求职看板 / 把投递记录导出成 Excel

## 开始之前

- 一个能装技能的桌面 AI（WorkBuddy / Codex / DeepSeek Harness / Claude Code / Cursor / Antigravity 都行）
- 本机装有 Python 3.9+（macOS 自带，纯标准库，**零第三方 `pip` 依赖**）
- **想让 AI 自动逛 BOSS 直聘**：复用你本机已登录的可见浏览器窗口（支持宿主自带的有头持久化浏览器，或防风控省 Token 的 [`ego lite`](https://lite.ego.app/)）

不想折腾浏览器权限也完全能用：**副驾驶粘贴模式（`copilot`）**——把岗位 JD 或 HR 消息直接贴进对话框，照样能排雷、查公司底细、写破冰草稿、记本地台账和看可视化看板。

## 三步

**第一步，装。** 把上面那段话发给 AI，它会自动装好并跑完环境体检。

**第二步，定规矩。** 确认你的真实履历要点、前东家黑名单和 8 项筛选策略（外包/驻场/猎头/薪资口径全透明可调）。

**第三步，开跑。** 先跑一轮半自动预览或只收藏不投，觉得匹配准、话术像你自己，再按需切全自动或定时巡检。

## 出问题了看这里

| 现象 | 怎么办 |
|---|---|
| 没有导出 PDF 简历文件 | 直接让 AI 读取你 BOSS 直聘里的「在线简历」建档，顺手点一下页面右上角「生成附件简历」即可 |
| 担心像海投脚本一样伤号 | 零爬虫、禁无头浏览器，全程复用可见登录窗口低频操作；内置 08:30–21:30 宵禁与 30–90 秒随机间隔，遇验证码立刻停手 |
| 晚上收藏把明天额度用光了 | 已做渠道分桶：只有主动找岗打招呼扣常规额度，收藏、被动来信和批次补投均不重复扣 |
| 怕 AI 瞎编经历或乱答应薪资 | 无简历不编造；约面、谈薪、加微信、超纲问题强制停手交回本人，不会替你拍板 |
| 批次投到一半出了滑块验证 | 助手会自动暂停保留断点；你在浏览器里划完验证码，说一句「继续投刚才的批次」就会接着剩下的投，绝不重复发 |
| 想换电脑或升级插件怕丢数据 | 档案、台账和 Pro 凭证全部保存在 `~/.hiremate/`，升级自动备份无损保留；Pro 买断支持本人 2 台电脑 |

---

## 开源基础版 vs Pro 全托管版

开源基础版**无需注册登录、永久免费可用**；建议先用免费版跑通建档、筛岗、查公司底细与半自动沟通，验证满意后再按需升级 Pro（**一次买断制，无按月订阅压力**）。

| 能力模块 | 开源基础版（免费） | Pro 全托管版（限时特惠 ¥69 · 原价 ¥99） |
| :--- | :---: | :---: |
| 对话式履历建档、8 项策略透明确认 & 前东家同字号自动拉黑 | ✅ | ✅ |
| BOSS 单轮精准筛岗、四源地点核验、外包/驻场独立过滤 & 半自动破冰 | ✅ | ✅ |
| **公司底细核查（二维风控引擎，识破伪装派遣与跨界空壳，本地缓存）** | ✅ | ✅（评估时自动触发建议） |
| **渠道额度分桶、7 天未回默认允许跟进 & 自定义规则文件（`--rules-file`）** | ✅ | ✅ |
| **收藏批次队列（`favorite-batch`）** | ✅（手动攒批串行投） | ✅（非工作时间收藏 + 到点自动统一投） |
| 个人求职语气档案、SQLite 精细台账、可视化看板、侧边栏工作台 & 原生 Excel 导出 | ✅ | ✅ |
| **主动找岗 / 消息答疑「全自动模式（`auto`）」与工作日自动巡检** | — | ✅ |
| **100+ 动态外包库持续更新、猎头择优评估 & 线上多版 PDF 简历自动路由** | — | ✅ |
| **一岗一议真人短句守卫 & 约面/谈薪/超纲即时停手桌面弹窗通知** | — | ✅ |
| **授权方式** | 永久免费 | ✅ **一次买断长期有效**（本人最多 2 台电脑，含后续更新） |

> 官网升级与账户管理：<https://hiremate.chengway.wang/activate>（邮箱免密一键配对绑定，支持在线支付秒级直充与兑换码激活；买断权益限本人使用，禁止共享或转让）。

---

## 安装说明（这一节是给 AI 读的）

**自动安装（推荐）：** 读取仓库内 [AGENTS.md](AGENTS.md) 并执行：

```bash
git clone https://github.com/ryunana/hiremate.git hiremate-open
cd hiremate-open

# 写入唯一主副本至 ~/.hiremate/skill/hiremate，并自动软链至本机已安装的 Agent 宿主
python3 install_plugin.py

# 无损卸载（默认 100% 保留 ~/.hiremate 下的本地求职档案、台账与凭证）
python3 install_plugin.py --uninstall
```

- **依赖**：纯 Python 3.9+ 标准库实现（推荐 3.11/3.12），**零第三方 `pip` 依赖**（Windows 若无 `python3` 可用 `python` 或 `py -3`）。
- **多端同步**：支持 `WorkBuddy`、`DeepSeek Harness (dsh)`、`Codex`、`Claude Code`、`Cursor`、`Windsurf`、`Antigravity` 及 `~/.agents`；`dsh`、`Codex`、`Antigravity` 自动挂载左侧边栏「求职助手」可视化工作台，`dsh` 额外提供 `/hiremate-status`、`/hiremate-login`、`/hiremate-dashboard`、`/hiremate-update` 原生斜杠命令。
- **数据隔离**：个人履历（`profile_facts.md`）、自定义执行规则（`execution-rules.md`）、语气档案（`voice_profile.md`）与 SQLite 台账（`ledger.sqlite3`）全部存放在 `~/.hiremate/default/`，运行 `python3 hiremate.py update` 无损更新不会覆盖用户数据。
- **完整执行规约**：见 `workbuddy/skills/hiremate/SKILL.md`（通用主规约）与 `dsh/skills/hiremate/SKILL.md`。

---

## 说明与许可

本项目当前支持 BOSS 直聘（更多平台逐步适配），与 BOSS 直聘无任何关联，未获其赞助、背书或官方认可。

This project is not affiliated with, sponsored by, endorsed by, or officially recognized by BOSS Zhipin.

MIT License，详见 [LICENSE](LICENSE)。

---

<details>
<summary>English (short version)</summary>

## HireMate — Precision Job Matching Assistant

Give your resume to your desktop AI. It screens jobs one by one against your real background, flags disguised labor-dispatch or shell companies, drafts genuine non-template icebreakers, and tracks everything on a local SQLite workbench — interrupting you only when an interview, salary discussion, or personal decision is needed.

### Easiest way: send this to your AI

```text
Install this skill and walk me through it:
https://github.com/ryunana/hiremate

My resume: <drop your PDF/Word resume, or say "read my online resume on BOSS">

Don't apply immediately. Walk me through my profile highlights, former-employer blocklist,
and screening preferences first, then run one preview round for my review.
```

### What it does

1. **Resume → structured profile & former-employer shield**: extracts real achievements and auto-blocks past employers.
2. **Precision screening & company intel probe**: checks full JD, 4-source location consistency, outsourcing/onsite filters, and runs a 2D feasibility × reliability company check to catch disguised dispatch or shell companies without false positives.
3. **Channel quota buckets & scheduled favorite batches**: favorites and inbound replies never eat your daily outbound quota; collect off-hours and dispatch serially at `apply_at` with curfew, jitter throttling, and pause/resume checkpoints.
4. **Tailored human-sounding replies**: learns your personal tone (`voice_profile.md`), supports custom `execution-rules.md`, and stops immediately for interviews, salary, or private questions.
5. **Local SQLite workbench**: full funnel dashboard (`dashboard.html`), native Excel (`.xlsx`) export, and automatic 7-day no-reply follow-up eligibility.

### Install (for the agent)

Zero third-party dependencies (Python 3.9+ stdlib only):

```bash
git clone https://github.com/ryunana/hiremate.git hiremate-open
cd hiremate-open && python3 install_plugin.py
```

MIT licensed. Not affiliated with BOSS Zhipin.

</details>
