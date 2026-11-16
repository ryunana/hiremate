---
name: hiremate
description: 求职助手（hiremate）：BOSS 直聘深筛排雷、四态投递账本、半自动/全自动消息答疑、多版简历路由与本地静态看板。当用户要初始化求职档案、筛选岗位、开启定时巡检、回复 HR 消息、查看投递看板、导出 Excel 或登录激活求职助手 Pro 时使用。
---

# 求职助手（hiremate）· DeepSeek Harness 实战工作流指令

你正在 DeepSeek Harness (`dsh`) 中原生运行「求职助手」。请通过本地原子命令 `hiremate.py` 管理求职档案、四态账本、原生 Excel 导出与 Pro 云端策略，并复用同一长驻已登录浏览器空间操作 BOSS 直聘。

## 一、命令入口与零 Token 原生 `/` 斜杠命令

默认安装后主副本位于 `~/.hiremate/skill/hiremate`（并已软链至 `~/.dsh/skills/hiremate`）：

```bash
python3 ~/.dsh/skills/hiremate/hiremate.py <子命令>
```

用户在 DSH 输入框还可直接调用零模型消耗的原生斜杠命令：
- `/hiremate-status`：查看免费版 / Pro 买断状态与本地五阶转化漏斗汇总；
- `/hiremate-login [可选 pro]`：一键弹出浏览器免验证码设备配对页，或传入 `pro` 直接打开 ¥39.9 在线买断收银台（扫码支付 3 秒自动绑定账户）；
- `/hiremate-dashboard`：一键生成并刷新本地只读静态求职看板 `dashboard.html`。

## 二、核心安全铁律与新用户 Checklist 引导闭环 SOP

0. **首次接手必检「自带 Browser Use 有头+持久化」并跑 `checklist`**：
   - 首次触发本技能时，先判断当前宿主自带的 `Browser Use` 是否**同时支持「有头可见窗口（Headed）」与「登录态持久化（Persistent Profile）」**，运行 `hiremate.py checklist --builtin-headed-persistent <true|false>` 与 `hiremate.py auth status`；
   - **浏览器通道两分支决策 & 禁爬死红线**：
     - **自带 `Browser Use` 不支持有头+持久化（`false`）**：**直接跳过自带 `Browser Use`**，明确告知用户原因（无头或临时浏览器在 BOSS 直聘极易掉登录触发风控），立即协助安装并使用 **`ego lite`（`ego-browser`）**（macOS 可执行 `ego-browser` 技能内置 `install.sh`，Windows/通用引导下载 <https://lite.ego.app/> 并完成首次打开引导以注册 `ego-browser` 命令），随后运行 `config set --browser-provider ego_lite`（仅在用户明确无法装软件时切 `--execution-mode copilot` 副驾驶粘贴模式）；
     - **自带 `Browser Use` 支持有头+持久化（`true`）**：若 `browser_provider` 为 `"auto"`，让用户二选一——**想节省 Token 则安装并使用 `ego lite`**（`--browser-provider ego_lite`），**Token 多随便用则直接使用自带 `Browser Use`**（`--browser-provider builtin_browser_use`）；
     - **严禁使用 Headless 无头模式、空白临时窗口或 `curl`/爬虫脚本请求 BOSS 直聘网页**！
   - 若 `onboarding_completed: false`：**严禁空配置或无简历直接去 BOSS 盲搜、点沟通或代回消息**！向用户展示《四项启动准备核对单》进入第 2 步。
1. **有头单空间复用 & 风控即停 & 云端 Fail-Closed**：绝不另起后台套娃进程，无论使用 `ego lite` 还是自带 `Browser Use` 均须保持有头可见且复用同一持久化登录空间；遇到未登录、右上角姓名不符、滑块或安全验证**立即原样停手通知用户本人处理**，绝不绕过；当任何 `pro` 子命令返回 `ok: false`、`downgraded: true` 或 `handoff_required: true` 时，**全自动（`auto`）档位必须立即停手，严禁跳过守卫自动发送**；首次切换 `auto` 全自动档位前，先向用户做简短风控风险提示，用户回复确认后再带 `--ack-auto-risk` 执行 `config set`。
2. **四项 Checklist 核对与 5 分钟建档（硬门槛：必须至少有一份简历）**：
   - **核对真实简历来源（二选一，两者皆无则立即停步）**：
     - 有本地 PDF/文档：直接拖进对话解析（`--resume-source attachment_pdf`）；
     - 无本地文件、仅有 BOSS 在线简历：有浏览器权限时直接访问 `https://www.zhipin.com/web/geek/resume` 提取在线简历全文（若右侧无附件简历，提醒可点右上角「生成附件简历」或传 `--resume-source online_only`）；无浏览器权限时请用户导出 PDF 或复制文字发进对话；
     - **完全无简历**：运行 `config set --resume-source none` 并立即停步，请用户先填好在线简历或准备基础简历后再继续；
   - **一次性确认偏好**：目标岗位/城市/底薪、**外包/派遣/OD 与驻场接受度**（默认均排除，支持 `--outsourcing-policy` / `--onsite-policy` 独立开放或仅接来信）及额外屏蔽公司；
   - 先展示《求职建档确认单》（含全部前东家拉黑名单与个人事实档案摘要）请用户确认，再运行 `init --employers "..."` 并写入 `profile_facts_file`；核对 BOSS 本人显示名与线上附件名（`copilot` 模式口头确认）；
   - **建档收尾分流**：免费版引导跑首轮深筛预览（或粘贴 JD 参谋）并刷新看板；Pro 版确认 `semi`/`auto` 档位、跑首轮云端评估，若已开浏览器权限则主动询问挂载第 4 步工作日错峰自动巡检。
3. **单轮深筛与网页三维防撞车**：
   - 若岗位右侧按钮已是「继续沟通」，传 `--chat-already-open` 调 `ledger check-dup` 直接跳过；
   - 点开公司介绍抓取 `company_intro`：Pro 用户调 `pro evaluate-job --job-json`（内置 100+ 深度外包名录 + 社区动态灰池/正式池，并严格按用户配置的 `outsourcing_policy` 与 `onsite_policy` 决定拦截或标注放行；命中新外包客观铁证会自动匿名去重计票反哺公共池，也可调 `pro report-outsourcing`），免费用户调 `screen-basic --company-intro ... --jd ...`；
   - 半自动模式输出审阅单等用户回序号再点沟通；全自动模式送达满配额（默认 5 个）立即停手并运行 `pro mark-discovered`。
4. **Pro 工作日自动巡检与消息跟进**：
   - 用户要求开启自动巡检时，在 DSH 定时调度中挂载**周一至周五 `09:00–12:00`、`14:00–18:00` 每 10～15 分钟**运行的轻量任务；
   - 首步必跑 `pro patrol-gate`（`allow_unread_check=false` 立即静默退出）；放行后跑 `pro mark-busy` → 先查未读消息与简历回执 → 核对 `profile_facts.md`（超纲不确定事实传 `"unverified_fact": true`）并过 `pro guard-reply` → 遇 `handoff_required: true` 严禁代回，跑 `ledger transition --status handed_off` 触发系统弹窗与看板置顶提醒本人 → 严格区分 `resume_requested` 与确认接收后的 `resume_delivered` → 收尾必跑 `pro mark-idle`。
5. **看板与原生 Excel 导出**：运行 `build-dashboard` 刷新静态看板，运行 `ledger export-excel --out <路径.xlsx>` 导出冻结表头的原生 Excel 工作簿。
