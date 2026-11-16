#!/usr/bin/env python3
"""求职助手（hiremate）开源原子命令行入口 —— 供 WorkBuddy 与 DeepSeek Harness 原生调用。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

if sys.version_info < (3, 9):
    sys.stdout.write(
        json.dumps(
            {
                "ok": False,
                "code": "PYTHON_TOO_OLD",
                "python_version": sys.version.split()[0],
                "error": "检测到 Python 版本过低（需 Python 3.9+，推荐 3.11/3.12）。",
                "hint": "macOS 可运行 `brew install python@3.12`，Windows 可从 python.org 安装新版 Python 后重试。",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    sys.exit(3)

OPEN_ROOT = Path(__file__).resolve().parent
if str(OPEN_ROOT) not in sys.path:
    sys.path.insert(0, str(OPEN_ROOT))

from hiremate_lib import client, dashboard, ledger, workspace  # noqa: E402


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    raw = value.replace("，", ",").replace("、", ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _load_json_arg(raw_or_path: str) -> dict[str, Any]:
    text = raw_or_path.strip()
    if text.startswith("@"):
        data = json.loads(Path(text[1:]).expanduser().read_text(encoding="utf-8"))
    elif text.startswith("{"):
        data = json.loads(text)
    else:
        data = json.loads(Path(text).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON 输入必须为对象字典")
    return data


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hiremate",
        description="求职助手（hiremate）原子 CLI：档案管理、四态账本、查重、静态看板与 Pro 云端接口。",
    )
    parser.add_argument("--workspace", help="工作区数据目录（默认 ~/.hiremate/default）")
    parser.add_argument("--home", help="全局凭证目录（默认 ~/.hiremate）")
    parser.add_argument("--api-base", help="覆盖云端服务地址")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON（默认即输出 JSON）")

    sub = parser.add_subparsers(dest="cmd", required=True)

    # 1. init
    p_init = sub.add_parser("init", help="初始化求职工作区、偏好配置与空白个人事实档案")
    p_init.add_argument("--account-name", help="BOSS 直聘右上角显示的本人账号名")
    p_init.add_argument("--employers", help="历史/当前就职公司（逗号分隔，自动填入屏蔽黑名单防误投前东家）")
    p_init.add_argument("--roles", help="目标岗位方向关键词（逗号分隔）")
    p_init.add_argument("--cities", help="优先城市（逗号分隔）")
    p_init.add_argument("--min-salary-k", type=int, help="期望最低月薪（单位 K）")
    p_init.add_argument(
        "--filter-outsourcing",
        choices=("true", "false"),
        help="是否过滤外包/派遣/OD 岗位（默认 true；传 false 表示接受外包机会）",
    )
    p_init.add_argument(
        "--outsourcing-policy",
        choices=workspace.VALID_CHANNEL_POLICIES,
        help="外包/派遣/OD 策略：block_all=全部排除，inbound_only=仅接被动来信，allow_all=全部开放",
    )
    p_init.add_argument(
        "--filter-onsite",
        choices=("true", "false"),
        help="是否过滤驻场/客户现场外派岗位（默认 true；传 false 表示接受驻场机会）",
    )
    p_init.add_argument(
        "--onsite-policy",
        choices=workspace.VALID_CHANNEL_POLICIES,
        help="驻场/外派办公策略：block_all=全部排除，inbound_only=仅接被动来信，allow_all=全部开放",
    )
    p_init.add_argument(
        "--execution-mode",
        choices=workspace.VALID_EXECUTION_MODES,
        help="运行交互模式：browser=宿主具备浏览器权限自动直连，copilot=无浏览器权限由用户粘贴协同",
    )
    p_init.add_argument(
        "--browser-provider",
        choices=workspace.VALID_BROWSER_PROVIDERS,
        help="浏览器操控通道：auto=按能力自动决策或待选，ego_lite=省Token紧凑DOM长驻浏览器，builtin_browser_use=宿主自带的有头持久化Browser Use",
    )
    p_init.add_argument(
        "--resume-source",
        choices=workspace.VALID_RESUME_SOURCES,
        help="简历来源形态：attachment_pdf=本地/线上PDF附件，online_only=仅平台在线简历无附件，both=在线+附件齐全，none=暂无任何简历",
    )
    p_init.add_argument("--force", action="store_true", help="重置求职偏好配置（默认自动备份并保留前东家黑名单与已填档案）")
    p_init.add_argument(
        "--reset-profile",
        action="store_true",
        help="彻底重置：连同前东家黑名单与个人事实档案一并恢复为空白模板（会自动备份原文件）",
    )

    # 1b. checklist (冷启动四项准备核对单)
    p_chk = sub.add_parser("checklist", help="输出面向新用户的《四项启动准备核对单》（浏览器通道决策/登录态/真实简历门槛/偏好与版本）")
    p_chk.add_argument(
        "--browser-capable",
        choices=("true", "false"),
        help="兼容标记：当前桌面 Agent 是否具备可用浏览器操控工具（true / false）",
    )
    p_chk.add_argument(
        "--builtin-headed-persistent",
        choices=("true", "false"),
        help="当前宿主自带的 Browser Use 是否同时支持「有头可见窗口 + 登录态持久化」（false 则直接跳过并安装使用 ego lite；true 则让用户按 Token 偏好二选一）",
    )
    p_chk.add_argument(
        "--ego-installed",
        choices=("true", "false"),
        help="可选覆盖本机 ego lite (`ego-browser`) 安装探测结果（省略时自动探测本机 PATH 与 ~/.local/bin）",
    )

    # 2. config
    p_cfg = sub.add_parser("config", help="查看或修改求职配置与屏蔽雇主")
    cfg_sub = p_cfg.add_subparsers(dest="cfg_cmd", required=True)
    cfg_sub.add_parser("show", help="输出当前求职配置与个人档案路径")

    p_cfg_set = cfg_sub.add_parser("set", help="更新一项或多项求职偏好配置")
    p_cfg_set.add_argument("--account-name", help="招聘平台本人显示名")
    p_cfg_set.add_argument("--mode-discover", choices=("semi", "auto"), help="主动找岗档位：semi=半自动确认，auto=全自动送达")
    p_cfg_set.add_argument("--mode-reply", choices=("semi", "auto"), help="消息回复档位：semi=半自动确认，auto=全自动跟进")
    p_cfg_set.add_argument("--roles", help="目标岗位关键词（逗号分隔）")
    p_cfg_set.add_argument("--cities", help="优先城市（逗号分隔）")
    p_cfg_set.add_argument("--min-salary-k", type=int, help="期望最低月薪 K")
    p_cfg_set.add_argument("--headhunter-policy", choices=workspace.VALID_HEADHUNTER_POLICIES)
    p_cfg_set.add_argument(
        "--filter-outsourcing",
        choices=("true", "false"),
        help="开关外包/派遣/OD 过滤（true=排除外包，false=开放接受外包）",
    )
    p_cfg_set.add_argument(
        "--outsourcing-policy",
        choices=workspace.VALID_CHANNEL_POLICIES,
        help="外包/派遣/OD 细粒度策略：block_all / inbound_only / allow_all",
    )
    p_cfg_set.add_argument(
        "--filter-onsite",
        choices=("true", "false"),
        help="开关驻场/客户现场外派过滤（true=排除驻场，false=开放接受驻场）",
    )
    p_cfg_set.add_argument(
        "--onsite-policy",
        choices=workspace.VALID_CHANNEL_POLICIES,
        help="驻场/外派细粒度策略：block_all / inbound_only / allow_all",
    )
    p_cfg_set.add_argument(
        "--execution-mode",
        choices=workspace.VALID_EXECUTION_MODES,
        help="切换运行模式：browser（浏览器直连）或 copilot（无浏览器权限粘贴副驾驶）",
    )
    p_cfg_set.add_argument(
        "--browser-provider",
        choices=workspace.VALID_BROWSER_PROVIDERS,
        help="切换浏览器操控通道：auto / ego_lite（省Token）/ builtin_browser_use（宿主自带的有头持久化Browser Use）",
    )
    p_cfg_set.add_argument(
        "--resume-source",
        choices=workspace.VALID_RESUME_SOURCES,
        help="更新简历来源形态：attachment_pdf / online_only / both / none / pending",
    )
    p_cfg_set.add_argument("--inbound-resume-policy", choices=("generous", "strict"))
    p_cfg_set.add_argument(
        "--ack-auto-risk",
        action="store_true",
        help="本人已知晓全自动模式下的招聘平台风控风险并确认授权开启",
    )
    p_cfg_set.add_argument("--patch-json", help="JSON 字符串或 @文件路径，增量合并配置（如配置多版简历路由）")

    p_emp = cfg_sub.add_parser("extract-employers", help="从简历文本或直接列表提取过往雇主并自动合并至屏蔽黑名单")
    p_emp.add_argument("--employers", help="直接传入雇主名（逗号分隔）")
    p_emp.add_argument("--from-file", help="从本地简历 Markdown/文本文件中自动识别公司名")
    p_emp.add_argument("--from-text", help="从文本片段自动识别公司名")

    # 3. screen (基础免费单轮初筛)
    p_screen = sub.add_parser("screen-basic", help="开源基础版：本地黑名单、基础外包排雷与条件初筛")
    p_screen.add_argument("--company", required=True)
    p_screen.add_argument("--title", required=True)
    p_screen.add_argument("--salary", default="")
    p_screen.add_argument("--company-intro", default="", help="可选：公司主页简介文本（用于基础驻场/外包识别）")
    p_screen.add_argument("--jd", default="", help="可选：岗位职责描述文本")
    p_screen.add_argument("--headhunter", action="store_true")
    p_screen.add_argument("--source", choices=("outbound", "inbound"), default="outbound")

    # 4. ledger
    p_led = sub.add_parser("ledger", help="本地 SQLite 四态精细求职账本与三维查重")
    led_sub = p_led.add_subparsers(dest="led_cmd", required=True)

    p_dup = led_sub.add_parser("check-dup", help="投前三维查重（归一化公司名 + 岗位相似度 + 网页会话/前东家黑名单核对）")
    p_dup.add_argument("--company", required=True)
    p_dup.add_argument("--title", required=True)
    p_dup.add_argument("--recruiter", default="")
    p_dup.add_argument(
        "--chat-already-open",
        action="store_true",
        help="若 BOSS 网页按钮已显示“继续沟通”或聊天列表已有会话则传入此标记",
    )

    p_up = led_sub.add_parser("upsert-job", help="新增或更新一条岗位记录")
    p_up.add_argument("--job-json", required=True, help="岗位 JSON 字符串或 @文件路径")

    p_tr = led_sub.add_parser("transition", help="流转岗位四态（严格区分开场送达/建联/简历请求/简历送达/转人工）")
    p_tr.add_argument("--job-id", required=True)
    p_tr.add_argument("--status", required=True, choices=ledger.VALID_STATUSES)
    p_tr.add_argument("--note", help="状态变更备注")
    p_tr.add_argument("--resume-used", help="本次实际投递或请求的简历文件名")
    p_tr.add_argument("--no-notify", action="store_true", help="流转至 handed_off 时跳过桌面系统通知弹窗")

    p_ls = led_sub.add_parser("list", help="按状态或关键词列出岗位记录")
    p_ls.add_argument("--status", choices=ledger.VALID_STATUSES)
    p_ls.add_argument("--search", help="搜索公司/岗位/招聘者")
    p_ls.add_argument("--limit", type=int, default=100)

    led_sub.add_parser("summary", help="输出求职转化漏斗汇总计数")

    p_csv = led_sub.add_parser("export-csv", help="导出带防公式注入保护的 UTF-8 BOM CSV 表格（若后缀为 .xlsx 自动导出 Excel）")
    p_csv.add_argument("--out", required=True, help="导出文件路径（.csv 或 .xlsx）")

    p_xlsx = led_sub.add_parser("export-excel", help="导出原生 Excel (.xlsx) 工作簿（含冻结表头与防公式注入保护）")
    p_xlsx.add_argument("--out", required=True, help="导出 Excel (.xlsx) 文件路径")

    # 5. auth (免验证码浏览器设备配对登录 & 在线开通 Pro 账户直充)
    p_auth = sub.add_parser("auth", help="免验证码浏览器一键设备配对、在线升级 Pro 与订阅状态查询")
    auth_sub = p_auth.add_subparsers(dest="auth_cmd", required=True)

    p_login = auth_sub.add_parser("login", help="打开浏览器进行免验证码设备一键配对（推荐主流程）")
    p_login.add_argument("--wait-seconds", type=int, default=45, help="最长等待浏览器点击确认秒数（默认 45s，超时后可断点续接）")
    p_login.add_argument("--no-wait", action="store_true", help="立即返回配对码与链接，不阻塞等待（适合斜杠命令）")
    p_login.add_argument("--no-browser", action="store_true", help="仅输出配对链接，不自动拉起系统浏览器")

    p_upgrade = auth_sub.add_parser("upgrade", help="打开官网账户中心在线买断解锁 Pro（¥39.9 自动秒级直充账户）")
    p_upgrade.add_argument(
        "--plan",
        default="pro_buyout",
        choices=("pro_buyout", "buyout"),
        help="买断方案标识（默认 pro_buyout）",
    )
    p_upgrade.add_argument("--no-browser", action="store_true", help="仅输出开通链接，不自动拉起系统浏览器")

    p_sc = auth_sub.add_parser("send-code", help="兼容备用：向邮箱发送 6 位数字验证码")
    p_sc.add_argument("--email", required=True)

    p_vc = auth_sub.add_parser("verify-code", help="兼容备用：验证 6 位数字验证码并绑定设备")
    p_vc.add_argument("--email", required=True)
    p_vc.add_argument("--code", required=True)

    auth_sub.add_parser("status", help="查询当前设备登录状态、Pro 剩余天数与订阅期更新权益")
    p_devs = auth_sub.add_parser("devices", help="查看当前账号已绑定的设备席位（最多 2 台）或解绑指定旧设备")
    p_devs.add_argument("--revoke", help="要解绑的设备 device_id")
    auth_sub.add_parser("logout", help="清除本机登录凭证")

    # 6. pro (连接云端 Pro 闭源策略接口)
    p_pro = sub.add_parser("pro", help="调用 Pro 专业版云端错峰门控、深度排雷与话术守卫")
    pro_sub = p_pro.add_subparsers(dest="pro_cmd", required=True)

    p_gate = pro_sub.add_parser("patrol-gate", help="请求云端错峰作息门控与本轮 Pro 执行规约")
    p_gate.add_argument("--now", help="可选覆盖 ISO 时间（仅用于测试）")

    p_eval = pro_sub.add_parser("evaluate-job", help="调用云端外包/猎头非对称评估与多版简历路由")
    p_eval.add_argument("--job-json", required=True, help="候选岗位 JSON 或 @文件路径")

    p_guard = pro_sub.add_parser("guard-reply", help="调用云端人工接管红线检查与去 AI 代办味话术守卫")
    p_guard.add_argument("--input-json", required=True, help="包含 job 与 conversation 的 JSON 或 @文件路径")

    p_rep = pro_sub.add_parser(
        "report-outsourcing",
        help="上报实测发现的新驻场/外派企业页面铁证（匿名去重计票反哺云端外包池）",
    )
    p_rep.add_argument("--company", required=True, help="公司名称")
    p_rep.add_argument("--evidence", required=True, help="公司主页或 JD 中的客观驻场/外派原文片段")
    p_rep.add_argument("--title", default="", help="可选：关联岗位名称")
    p_rep.add_argument("--job-url", default="", help="可选：关联岗位链接")

    p_mb = pro_sub.add_parser("mark-busy", help="标记当前有主执行轮次进行中")
    p_mb.add_argument("--reason", default="executing_round")
    pro_sub.add_parser("mark-idle", help="释放主执行锁")
    pro_sub.add_parser("mark-discovered", help="记录刚刚完成一轮主动找岗的时间戳")

    # 7. build-dashboard
    p_dash = sub.add_parser("build-dashboard", help="构建本地高信息密度只读静态求职看板 HTML")
    p_dash.add_argument("--out", help="输出 HTML 文件路径（默认 <workspace>/dashboard.html）")
    p_dash.add_argument("--demo", action="store_true", help="使用虚构样例数据构建演示看板（不读取个人隐私）")
    p_dash.add_argument(
        "--demo-tier",
        choices=["free", "pro"],
        default="pro",
        help="演示看板视角：free（免费基础版）或 pro（Pro 专业版，默认）",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    home = workspace.get_hiremate_home(args.home)
    ws = workspace.resolve_workspace(args.workspace, home)

    try:
        if args.cmd == "init":
            creds = client.load_credentials(home)
            tier = "pro" if (creds and creds.get("tier") == "pro") else "free"
            res = workspace.init_workspace(
                ws,
                account_name=args.account_name,
                employers=_split_csv(args.employers) or None,
                target_roles=_split_csv(args.roles) or None,
                preferred_cities=_split_csv(args.cities) or None,
                min_salary_k=args.min_salary_k,
                filter_outsourcing=(args.filter_outsourcing == "true") if args.filter_outsourcing else None,
                outsourcing_policy=args.outsourcing_policy,
                filter_onsite=(args.filter_onsite == "true") if args.filter_onsite else None,
                onsite_policy=args.onsite_policy,
                execution_mode=args.execution_mode,
                browser_provider=args.browser_provider,
                resume_source=args.resume_source,
                tier=tier,
                force=args.force,
                reset_profile=args.reset_profile,
            )
            _emit(res)
            return 0

        if args.cmd == "checklist":
            cfg = workspace.load_config(ws)
            creds = client.load_credentials(home)
            tier = "pro" if (creds and creds.get("tier") == "pro") else "free"
            email = str(creds.get("email") or "") if creds else None
            b_cap = None if args.browser_capable is None else (args.browser_capable == "true")
            b_hp = None if args.builtin_headed_persistent is None else (args.builtin_headed_persistent == "true")
            e_inst = None if args.ego_installed is None else (args.ego_installed == "true")
            _emit(
                workspace.build_startup_checklist(
                    ws,
                    cfg=cfg,
                    tier=tier,
                    authenticated_email=email,
                    browser_capable=b_cap,
                    builtin_headed_persistent=b_hp,
                    ego_installed=e_inst,
                )
            )
            return 0

        if args.cmd == "config":
            if args.cfg_cmd == "show":
                cfg = workspace.load_config(ws)
                ob = workspace.inspect_onboarding_status(ws, cfg)
                creds = client.load_credentials(home)
                tier = "pro" if (creds and creds.get("tier") == "pro") else "free"
                show_payload: dict[str, Any] = {
                    "ok": True,
                    "workspace": str(ws),
                    "tier": tier,
                    "initialized": ob["initialized"],
                    "onboarding_completed": ob["onboarding_completed"],
                    "profile_customized": ob["profile_customized"],
                    "resume_ready": ob["resume_ready"],
                    "resume_source": ob["resume_source"],
                    "has_attachment_resume": ob["has_attachment_resume"],
                    "execution_mode": ob["execution_mode"],
                    "browser_provider": ob["browser_provider"],
                    "hard_blocker": ob["hard_blocker"],
                    "missing_steps": ob["missing_steps"],
                    "recommendations": ob["recommendations"],
                    "next_action_hint": ob["next_action_hint"],
                    "config_file": str(workspace.config_path(ws)),
                    "profile_facts_file": str(workspace.profile_facts_path(ws)),
                    "config": cfg,
                }
                if ob["onboarding_completed"]:
                    show_payload["post_onboarding_guide"] = workspace.build_post_onboarding_guide(
                        cfg,
                        tier=tier,
                        dashboard_path=str((ws / "dashboard.html").resolve()),
                    )
                _emit(show_payload)
                return 0

            if args.cfg_cmd == "set":
                cfg = workspace.load_config(ws)
                was_already_auto = cfg.get("mode_discover") == "auto" or cfg.get("mode_reply") == "auto"
                patch: dict[str, Any] = {}
                if args.patch_json:
                    patch = _load_json_arg(args.patch_json)
                    workspace.apply_config_updates(cfg, patch)
                cli_patch: dict[str, Any] = {}
                if args.account_name is not None:
                    cli_patch["account_display_name"] = args.account_name.strip()
                if args.mode_discover:
                    cli_patch["mode_discover"] = args.mode_discover
                if args.mode_reply:
                    cli_patch["mode_reply"] = args.mode_reply
                if args.roles is not None:
                    cli_patch["target_roles"] = _split_csv(args.roles)
                if args.cities is not None:
                    cli_patch["preferred_cities"] = _split_csv(args.cities)
                if args.min_salary_k is not None:
                    cli_patch["min_salary_k"] = args.min_salary_k
                if args.headhunter_policy:
                    cli_patch["headhunter_policy"] = args.headhunter_policy
                if args.filter_outsourcing:
                    cli_patch["filter_outsourcing"] = args.filter_outsourcing == "true"
                if args.outsourcing_policy:
                    cli_patch["outsourcing_policy"] = args.outsourcing_policy
                if args.filter_onsite:
                    cli_patch["filter_onsite"] = args.filter_onsite == "true"
                if args.onsite_policy:
                    cli_patch["onsite_policy"] = args.onsite_policy
                if args.execution_mode:
                    cli_patch["execution_mode"] = args.execution_mode
                if args.browser_provider:
                    cli_patch["browser_provider"] = args.browser_provider
                if args.resume_source:
                    cli_patch["resume_source"] = args.resume_source
                if args.inbound_resume_policy:
                    cli_patch["inbound_resume_policy"] = args.inbound_resume_policy
                if args.ack_auto_risk:
                    cli_patch["auto_risk_acknowledged"] = True
                workspace.apply_config_updates(cfg, cli_patch)

                explicit_wants_auto = (
                    args.mode_discover == "auto"
                    or args.mode_reply == "auto"
                    or patch.get("mode_discover") == "auto"
                    or patch.get("mode_reply") == "auto"
                )
                wants_auto = cfg.get("mode_discover") == "auto" or cfg.get("mode_reply") == "auto"
                auto_downgrade_note = ""
                if wants_auto:
                    if cfg.get("execution_mode") == "copilot":
                        if explicit_wants_auto:
                            _emit(
                                {
                                    "ok": False,
                                    "code": "AUTO_REQUIRES_BROWSER_MODE",
                                    "error": "当前为副驾驶粘贴模式（copilot），无浏览器操控权限无法执行全自动点击发送；请先切换 `--execution-mode browser` 或保持半自动（semi）。",
                                }
                            )
                            return 1
                        cfg["mode_discover"] = "semi"
                        cfg["mode_reply"] = "semi"
                        auto_downgrade_note = "已切为副驾驶粘贴模式（copilot），档位已自动同步回半自动（semi）。"
                    else:
                        creds = client.load_credentials(home)
                        if not (creds and creds.get("tier") == "pro"):
                            if explicit_wants_auto:
                                _emit(
                                    {
                                        "ok": False,
                                        "code": "PRO_REQUIRED_FOR_AUTO",
                                        "error": "全自动档位（auto）属于 Pro 专业版权益；当前免费基础版支持半自动审阅（semi），可运行 `hiremate.py auth upgrade` 解锁 Pro。",
                                    }
                                )
                                return 1
                            cfg["mode_discover"] = "semi"
                            cfg["mode_reply"] = "semi"
                            auto_downgrade_note = "当前未处于激活的 Pro 状态，档位已自动回退为半自动（semi）。"
                        else:
                            explicit_ack = bool(args.ack_auto_risk or patch.get("auto_risk_acknowledged") is True)
                            if explicit_ack:
                                cfg["auto_risk_acknowledged"] = True
                            elif not cfg.get("auto_risk_acknowledged", False) and not was_already_auto:
                                _emit(
                                    {
                                        "ok": False,
                                        "code": "AUTO_RISK_CONFIRMATION_REQUIRED",
                                        "requires_confirmation": True,
                                        "risk_notice": (
                                            "风险提示：开启全自动档位（auto）后，求职助手将在工作时段内自动点击沟通或发送回复。"
                                            "招聘平台对高频自动化操作存在风控限制风险，建议保持默认定额（每轮≤5个）并确认由本人授权开启。"
                                        ),
                                        "confirm_hint": "请向用户展示上述简短风险提示；用户回复确认或授权后，追加 `--ack-auto-risk` 重新运行本命令即可生效。",
                                    }
                                )
                                return 1

                saved = workspace.save_config(ws, cfg)
                creds_now = client.load_credentials(home)
                tier_now = "pro" if (creds_now and creds_now.get("tier") == "pro") else "free"
                ob_now = workspace.inspect_onboarding_status(ws, saved)
                out_payload: dict[str, Any] = {"ok": True, "config": saved}
                if auto_downgrade_note:
                    out_payload["notice"] = auto_downgrade_note
                if ob_now["onboarding_completed"] or saved.get("target_roles"):
                    out_payload["post_onboarding_guide"] = workspace.build_post_onboarding_guide(
                        saved,
                        tier=tier_now,
                        dashboard_path=str((ws / "dashboard.html").resolve()),
                    )
                _emit(out_payload)
                return 0

            if args.cfg_cmd == "extract-employers":
                res = workspace.merge_employers(
                    ws,
                    employers=_split_csv(args.employers) or None,
                    from_file=args.from_file,
                    from_text=args.from_text,
                )
                _emit(res)
                return 0

        if args.cmd == "screen-basic":
            cfg = workspace.load_config(ws)
            res = workspace.basic_screen_candidate(
                cfg,
                company=args.company,
                title=args.title,
                salary_text=args.salary,
                company_intro=args.company_intro,
                jd_text=args.jd,
                is_headhunter=args.headhunter,
                source=args.source,
            )
            _emit(res)
            return 0

        if args.cmd == "ledger":
            led = ledger.JobLedger(ws)
            if args.led_cmd == "check-dup":
                cfg = workspace.load_config(ws)
                res = led.check_duplicate(
                    company=args.company,
                    title=args.title,
                    recruiter=args.recruiter,
                    blocked_companies=cfg.get("blocked_companies"),
                    past_employers=cfg.get("past_employers"),
                    chat_already_open=bool(args.chat_already_open),
                    filter_outsourcing=(cfg.get("outsourcing_policy", "block_all") != "allow_all"),
                    filter_onsite=(cfg.get("onsite_policy", "block_all") != "allow_all"),
                )
                _emit(res)
                return 0
            if args.led_cmd == "upsert-job":
                job_data = _load_json_arg(args.job_json)
                _emit(led.upsert_job(job_data))
                return 0
            if args.led_cmd == "transition":
                _emit(
                    led.transition(
                        job_id=args.job_id,
                        to_status=args.status,
                        note=args.note,
                        resume_used=args.resume_used,
                        notify_desktop=not bool(args.no_notify),
                    )
                )
                return 0
            if args.led_cmd == "list":
                items = led.list_jobs(status=args.status, search=args.search, limit=args.limit)
                _emit({"ok": True, "count": len(items), "items": items})
                return 0
            if args.led_cmd == "summary":
                _emit({"ok": True, "summary": led.summary()})
                return 0
            if args.led_cmd == "export-csv":
                res = led.export_csv(Path(args.out).expanduser().resolve())
                _emit(res)
                return 0
            if args.led_cmd == "export-excel":
                res = led.export_xlsx(Path(args.out).expanduser().resolve())
                _emit(res)
                return 0

        if args.cmd == "auth":
            if args.auth_cmd == "login":
                wait_sec = 0 if args.no_wait else args.wait_seconds
                _emit(
                    client.auth_device_login(
                        workspace=ws,
                        home=home,
                        api_base_override=args.api_base,
                        wait_seconds=wait_sec,
                        open_browser=not args.no_browser,
                    )
                )
                return 0
            if args.auth_cmd == "upgrade":
                res = client.auth_open_upgrade(
                    plan=args.plan,
                    workspace=ws,
                    home=home,
                    api_base_override=args.api_base,
                    open_browser=not args.no_browser,
                )
                _emit(res)
                return 0
            if args.auth_cmd == "send-code":
                _emit(
                    client.auth_send_code(
                        email=args.email,
                        workspace=ws,
                        home=home,
                        api_base_override=args.api_base,
                    )
                )
                return 0
            if args.auth_cmd == "verify-code":
                _emit(
                    client.auth_verify_code(
                        email=args.email,
                        code=args.code,
                        workspace=ws,
                        home=home,
                        api_base_override=args.api_base,
                    )
                )
                return 0
            if args.auth_cmd == "status":
                _emit(client.auth_status(workspace=ws, home=home, api_base_override=args.api_base))
                return 0
            if args.auth_cmd == "devices":
                res = client.auth_devices(
                    workspace=ws,
                    home=home,
                    api_base_override=args.api_base,
                    revoke_device_id=args.revoke,
                )
                _emit(res)
                return 0 if res.get("ok") else 1
            if args.auth_cmd == "logout":
                _emit(client.auth_logout(home=home))
                return 0

        if args.cmd == "pro":
            if args.pro_cmd == "patrol-gate":
                _emit(
                    client.pro_patrol_gate(
                        workspace=ws,
                        home=home,
                        api_base_override=args.api_base,
                        now_override=args.now,
                    )
                )
                return 0
            if args.pro_cmd == "evaluate-job":
                job_data = _load_json_arg(args.job_json)
                _emit(
                    client.pro_evaluate_job(
                        job_data=job_data,
                        workspace=ws,
                        home=home,
                        api_base_override=args.api_base,
                    )
                )
                return 0
            if args.pro_cmd == "guard-reply":
                inp = _load_json_arg(args.input_json)
                _emit(
                    client.pro_guard_reply(
                        input_data=inp,
                        workspace=ws,
                        home=home,
                        api_base_override=args.api_base,
                    )
                )
                return 0
            if args.pro_cmd == "report-outsourcing":
                _emit(
                    client.pro_report_outsourcing(
                        company=args.company,
                        evidence=args.evidence,
                        job_title=args.title,
                        job_url=args.job_url,
                        workspace=ws,
                        home=home,
                        api_base_override=args.api_base,
                    )
                )
                return 0
            if args.pro_cmd == "mark-busy":
                st = workspace.update_runtime_flag(ws, "mark-busy", reason=args.reason)
                _emit({"ok": True, "runtime_state": st["runtime_state"]})
                return 0
            if args.pro_cmd == "mark-idle":
                st = workspace.update_runtime_flag(ws, "mark-idle")
                _emit({"ok": True, "runtime_state": st["runtime_state"]})
                return 0
            if args.pro_cmd == "mark-discovered":
                st = workspace.update_runtime_flag(ws, "mark-discovered")
                _emit({"ok": True, "runtime_state": st["runtime_state"]})
                return 0

        if args.cmd == "build-dashboard":
            out = Path(args.out).expanduser().resolve() if args.out else None
            tier = args.demo_tier if args.demo else "free"
            pro_meta: dict[str, Any] = {}
            if not args.demo:
                creds = client.load_credentials(home)
                if creds and creds.get("tier") == "pro":
                    tier = "pro"
                    pro_meta = {
                        "email": creds.get("email"),
                        "lifetime": bool(creds.get("lifetime", creds.get("days_left") is None)),
                        "days_left": creds.get("days_left"),
                        "expires_at": creds.get("expires_at"),
                    }
            res = dashboard.build_dashboard_file(
                workspace=ws,
                out_path=out,
                demo=args.demo,
                tier=tier,
                demo_tier=args.demo_tier,
                pro_meta=pro_meta,
            )
            if not args.demo and res.get("jobs_count", 0) == 0:
                cfg_now = workspace.load_config(ws)
                res["first_run_empty"] = True
                res["post_onboarding_guide"] = workspace.build_post_onboarding_guide(
                    cfg_now,
                    tier=tier,
                    dashboard_path=str(res.get("out_path") or ""),
                )
            _emit(res)
            return 0

    except workspace.ConfigCorruptError as exc:
        _emit(
            {
                "ok": False,
                "code": exc.code,
                "error": str(exc),
                "corrupt_path": exc.corrupt_path,
                "backup_path": exc.backup_path,
                "hint": exc.hint,
            }
        )
        return 1
    except Exception as exc:
        _emit({"ok": False, "code": "CLI_ERROR", "error": str(exc)})
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
