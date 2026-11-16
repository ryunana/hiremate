"""求职助手（hiremate）开源插件 CLI、静态看板与云端 Pro 端到端联调测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

OPEN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = OPEN_ROOT.parent
PRO_SERVER_ROOT = REPO_ROOT / "pro-server"
HAS_PRO_SERVER = (PRO_SERVER_ROOT / "server.py").is_file()

if str(OPEN_ROOT) not in sys.path:
    sys.path.insert(0, str(OPEN_ROOT))
if HAS_PRO_SERVER and str(PRO_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(PRO_SERVER_ROOT))

os.environ["HIREMATE_QUIET_LOG"] = "1"

import hiremate  # noqa: E402
import install_plugin  # noqa: E402
from scripts import scan_before_release  # noqa: E402

if HAS_PRO_SERVER:
    import admin_cli  # noqa: E402
    from mailer import Mailer, SmtpSettings  # noqa: E402
    from server import build_server  # noqa: E402


def _run_cli(args: list[str]) -> tuple[int, dict]:
    from io import StringIO
    from contextlib import redirect_stdout

    buf = StringIO()
    with redirect_stdout(buf):
        rc = hiremate.main(args)
    raw = buf.getvalue().strip()
    return rc, json.loads(raw) if raw else {}


class TestOpenPluginAndE2E(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["HIREMATE_DISABLE_DESKTOP_NOTIFY"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "hm_home"
        self.ws = self.root / "ws"
        self.server_data = self.root / "server_data"
        self.srv = None

        if HAS_PRO_SERVER:
            empty_smtp = SmtpSettings(host="smtp.exmail.qq.com", port=465, user="", password="", sender="")
            dev_mailer = Mailer(settings=empty_smtp, env_mode="dev", dev_echo=True)
            self.srv = build_server(host="127.0.0.1", port=0, data_dir=self.server_data, mailer=dev_mailer)
            self.api_base = f"http://127.0.0.1:{self.srv.server_address[1]}"
            self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
            self.thread.start()
        else:
            self.api_base = "http://127.0.0.1:9"

    def tearDown(self) -> None:
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()
        self.tmp.cleanup()

    def _base_flags(self) -> list[str]:
        return ["--home", str(self.home), "--workspace", str(self.ws), "--api-base", self.api_base]

    def test_init_employer_shield_dedup_four_states_and_csv_safety(self) -> None:
        # 1. 初始化并自动拉黑前东家
        rc, init_res = _run_cli(
            self._base_flags()
            + [
                "init",
                "--account-name",
                "林远",
                "--employers",
                "深圳示例旧东家科技有限公司,星云软件",
                "--roles",
                "AI产品经理,产品经理",
                "--cities",
                "深圳,广州",
                "--min-salary-k",
                "25",
            ]
        )
        self.assertEqual(rc, 0)
        cfg = init_res["config"]
        self.assertIn("深圳示例旧东家科技有限公司", cfg["blocked_companies"])

        # 2. 前东家归一化查重直接拦截
        rc, dup_emp = _run_cli(
            self._base_flags() + ["ledger", "check-dup", "--company", "示例旧东家", "--title", "高级AI产品经理"]
        )
        self.assertEqual(rc, 0)
        self.assertTrue(dup_emp["duplicate"])

        # 3. 写入「观麦」历史沟通记录，验证“深圳观麦科技有限公司”归一化查重命中（修复旧工作台漏重投缺陷）
        job_payload = {
            "company": "观麦",
            "company_scale": "100-499人",
            "business_nature": "SaaS自研",
            "title": "高级产品经理（AI方向）",
            "salary": "25-35K",
            "city": "深圳",
            "recruiter_name": "陈女士",
            "recruiter_title": "HRBP",
            "status": "opening_sent",
            "match_reasons": "企业级工作流与 AI 产品落地匹配",
        }
        rc, up_res = _run_cli(self._base_flags() + ["ledger", "upsert-job", "--job-json", json.dumps(job_payload)])
        self.assertEqual(rc, 0)
        job_id = up_res["job"]["job_id"]

        rc, dup_gm = _run_cli(
            self._base_flags()
            + ["ledger", "check-dup", "--company", "深圳观麦科技有限公司", "--title", "高级产品经理(AI方向)", "--recruiter", "王总监"]
        )
        self.assertEqual(rc, 0)
        self.assertTrue(dup_gm["duplicate"])

        # 4. 四态精细拆账：resume_requested 绝不等同于 resume_delivered
        _run_cli(self._base_flags() + ["ledger", "transition", "--job-id", job_id, "--status", "connected"])
        _run_cli(
            self._base_flags()
            + ["ledger", "transition", "--job-id", job_id, "--status", "resume_requested", "--resume-used", "林远-AI产品经理.pdf"]
        )
        _, sum1 = _run_cli(self._base_flags() + ["ledger", "summary"])
        counts1 = sum1["summary"]["by_status"]
        self.assertEqual(counts1["resume_requested"], 1)
        self.assertEqual(counts1["resume_delivered"], 0)

        # 对方点同意接收后流转为 resume_delivered
        _run_cli(
            self._base_flags()
            + ["ledger", "transition", "--job-id", job_id, "--status", "resume_delivered", "--resume-used", "林远-AI产品经理.pdf"]
        )
        _, sum2 = _run_cli(self._base_flags() + ["ledger", "summary"])
        self.assertEqual(sum2["summary"]["by_status"]["resume_delivered"], 1)

        # 5. CSV 导出防公式注入保护
        evil_job = {
            "company": "=HYPERLINK(\"http://evil.example\",\"点我\")",
            "title": "+AI产品经理",
            "status": "discovered",
        }
        _run_cli(self._base_flags() + ["ledger", "upsert-job", "--job-json", json.dumps(evil_job)])
        csv_out = self.root / "export.csv"
        rc, csv_res = _run_cli(self._base_flags() + ["ledger", "export-csv", "--out", str(csv_out)])
        self.assertEqual(rc, 0)
        self.assertTrue(csv_res["ok"])
        csv_text = csv_out.read_text(encoding="utf-8-sig")
        self.assertIn("'=HYPERLINK", csv_text)
        self.assertIn("'+AI产品经理", csv_text)

    @unittest.skipUnless(HAS_PRO_SERVER, "独立开源发布包不含闭源 pro-server，跳过双仓全栈联调测试")
    def test_e2e_free_graceful_fallback_and_pro_unlock(self) -> None:
        _run_cli(
            self._base_flags()
            + [
                "init",
                "--account-name",
                "林远",
                "--roles",
                "AI产品经理,产品经理",
                "--cities",
                "深圳",
                "--min-salary-k",
                "25",
            ]
        )

        # 1. 未登录态调用 Pro 接口：平顺提示免费版，绝不崩溃
        rc, gate_anon = _run_cli(self._base_flags() + ["pro", "patrol-gate"])
        self.assertEqual(rc, 0)
        self.assertEqual(gate_anon.get("tier"), "free")

        # 2. 邮箱发送 6 位验证码并登录（尚未开通 Pro）
        email = "linyuan@example.com"
        _, send_res = _run_cli(self._base_flags() + ["auth", "send-code", "--email", email])
        self.assertTrue(send_res["ok"])
        code = send_res["dev_code"]

        _, ver_res = _run_cli(self._base_flags() + ["auth", "verify-code", "--email", email, "--code", code])
        self.assertTrue(ver_res["ok"])
        self.assertEqual(ver_res["tier"], "free")

        # 免费登录态调 Pro 巡检门控：收到 PRO_REQUIRED 平顺降级提示
        _, gate_free = _run_cli(self._base_flags() + ["pro", "patrol-gate"])
        self.assertFalse(gate_free.get("ok", True) and gate_free.get("tier") == "pro")
        self.assertEqual(gate_free.get("tier"), "free")

        # 3. 管理员开通买断权益，客户端立即解锁全部 Pro 接口
        admin_cli.main(["--data-dir", str(self.server_data), "grant", "--email", email, "--no-email"])

        _, st_pro = _run_cli(self._base_flags() + ["auth", "status"])
        self.assertEqual(st_pro["tier"], "pro")
        self.assertTrue(st_pro.get("lifetime"))

        _, gate_pro = _run_cli(
            self._base_flags() + ["pro", "patrol-gate", "--now", "2026-11-16T10:30:00+08:00"]
        )
        self.assertTrue(gate_pro["ok"])
        self.assertTrue(gate_pro["in_active_window"])
        self.assertTrue(gate_pro["discover_due"])

        # 4. Pro 话术守卫拦截暴露代办口吻
        guard_input = {
            "job": {"title": "AI产品经理", "jd_text": "Agent 工作流产品"},
            "conversation": {
                "incoming_message": "异地机会考虑吗？",
                "draft_reply": "外地是否考虑还需要跟本人再确认一下。",
            },
        }
        _, guard_res = _run_cli(
            self._base_flags() + ["pro", "guard-reply", "--input-json", json.dumps(guard_input)]
        )
        self.assertFalse(guard_res["reply_approved"])
        self.assertTrue(len(guard_res["violations"]) >= 1)

    def test_dashboard_demo_plugin_install_and_release_scan(self) -> None:
        # 1. 构建演示静态看板，校验无鸡汤口号、无海外字体 CDN、关键字段齐全
        demo_html_path = self.root / "demo-dashboard.html"
        rc, dash_res = _run_cli(self._base_flags() + ["build-dashboard", "--demo", "--out", str(demo_html_path)])
        self.assertEqual(rc, 0)
        self.assertTrue(dash_res["ok"])
        html = demo_html_path.read_text(encoding="utf-8")
        self.assertIn("求职助手", html)
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertNotIn("把精力留给合适的机会", html)
        self.assertNotIn("让搜索更懂你的方向", html)
        self.assertIn("PDF 简历已送达", html)

        # 2. 测试 WorkBuddy 与 DSH 双宿主安装器（dry-run + 实装到隔离目录）
        wb_root = self.root / "mock_workbuddy"
        dsh_root = self.root / "mock_dsh"
        r_dry = install_plugin.install("workbuddy", dest_root=str(wb_root), dry_run=True)
        self.assertTrue(r_dry["dry_run"])
        self.assertFalse((wb_root / "skills" / "hiremate").exists())

        r_wb = install_plugin.install("workbuddy", dest_root=str(wb_root), dry_run=False)
        self.assertTrue(r_wb["ok"])
        self.assertTrue((wb_root / "skills" / "hiremate" / "SKILL.md").is_file())
        self.assertTrue((wb_root / "skills" / "hiremate" / "hiremate.py").is_file())

        r_dsh = install_plugin.install("dsh", dest_root=str(dsh_root), dry_run=False)
        self.assertTrue(r_dsh["ok"])
        self.assertTrue((dsh_root / "skills" / "hiremate" / "SKILL.md").is_file())

        # 验证 DSH index.mjs ES Module 语法合法
        chk = subprocess.run(["node", "--check", str(OPEN_ROOT / "dsh" / "index.mjs")], capture_output=True, text=True)
        self.assertEqual(chk.returncode, 0, chk.stderr)

        # 3. 运行开源发布前防隐私/闭源泄漏扫描
        scan_res = scan_before_release.scan(OPEN_ROOT)
        self.assertTrue(scan_res["ok"], f"发布扫描发现问题: {scan_res['issues']}")

        # 3. 测试单源 Canonical + 多宿主目录软链自动同步安装（独立开源仓与主仓均必测）
        canon_home = self.root / "canon_hm"
        mock_wb = self.root / "auto_wb"
        mock_dsh = self.root / "auto_dsh"
        mock_agents = self.root / "auto_agents"
        mock_wb.mkdir()
        mock_dsh.mkdir()
        mock_agents.mkdir()

        auto_res = install_plugin.install(
            target="auto",
            canonical_home=str(canon_home),
            search_roots={"workbuddy": mock_wb, "dsh": mock_dsh, "agents": mock_agents},
        )
        self.assertTrue(auto_res["ok"])
        self.assertEqual(len(auto_res["linked_hosts"]), 3)
        for host_root in (mock_wb, mock_dsh, mock_agents):
            skill_link = host_root / "skills" / "hiremate"
            self.assertTrue(skill_link.is_symlink())
            self.assertTrue((skill_link / "hiremate.py").is_file())
            self.assertTrue((skill_link / "SKILL.md").is_file())

        # 4. 验证 DSH 插件实际注册 skills + 3 个原生斜杠命令（/hiremate-status, /hiremate-login, /hiremate-dashboard）
        empty_dsh_home = self.root / "empty_dsh_home"
        node_script = f"""
import {{ apply }} from {json.dumps((OPEN_ROOT / 'dsh' / 'index.mjs').as_uri())};
const skills = [];
const commands = [];
const fakeCtx = {{
  skills: {{ register: (s) => {{ skills.push(s.name); return () => {{}}; }} }},
  commands: {{ register: (c) => {{ commands.push(c.name); return () => {{}}; }} }},
  effect: (fn) => fn(),
  inject: (_deps, cb) => cb(fakeCtx),
}};
apply(fakeCtx);
console.log(JSON.stringify({{ skills, commands }}));
"""
        node_run = subprocess.run(
            ["node", "--input-type=module", "-e", node_script],
            env={**os.environ, "DSH_HOME": str(empty_dsh_home)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(node_run.returncode, 0, node_run.stderr)
        parsed_dsh = json.loads(node_run.stdout.strip())
        self.assertEqual(parsed_dsh["skills"], ["hiremate"])
        self.assertEqual(
            parsed_dsh["commands"],
            ["hiremate-status", "hiremate-login", "hiremate-dashboard"],
        )

        # 验证 install_plugin 自动写入了 mock_dsh/cordis.patch.yml 挂载块，且当用户级 skill 已挂载时 index.mjs 不重复注册同名 skill
        dsh_patch_file = mock_dsh / "cordis.patch.yml"
        self.assertTrue(dsh_patch_file.is_file())
        self.assertIn("- id: hiremate-dsh", dsh_patch_file.read_text(encoding="utf-8"))
        node_run_dedup = subprocess.run(
            ["node", "--input-type=module", "-e", node_script],
            env={**os.environ, "DSH_HOME": str(mock_dsh)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(node_run_dedup.returncode, 0, node_run_dedup.stderr)
        parsed_dedup = json.loads(node_run_dedup.stdout.strip())
        self.assertEqual(parsed_dedup["skills"], [])
        self.assertEqual(parsed_dedup["commands"], ["hiremate-status", "hiremate-login", "hiremate-dashboard"])

        # 5. 验证无服务端连接时客户端优雅回退免费态（不抛出未捕获异常）
        offline_flags = ["--home", str(self.home), "--workspace", str(self.ws), "--api-base", "http://127.0.0.1:9"]
        _run_cli(offline_flags + ["init", "--account-name", "林远"])
        rc_off, gate_off = _run_cli(offline_flags + ["pro", "patrol-gate"])
        self.assertEqual(rc_off, 0)
        self.assertEqual(gate_off.get("tier"), "free")

    @unittest.skipUnless(HAS_PRO_SERVER, "独立开源发布包不含闭源 pro-server，跳过设备配对服务端联调")
    def test_device_pairing_and_online_order_pro_sync_with_pro_server(self) -> None:
        os.environ["HIREMATE_NO_BROWSER"] = "1"
        _run_cli(self._base_flags() + ["init", "--account-name", "林远"])

        # 1. CLI 发起免验证码设备配对（--no-wait 立即返回配对码与链接并落盘 pending_device.json）
        rc, login_pending = _run_cli(self._base_flags() + ["auth", "login", "--no-wait"])
        self.assertEqual(rc, 0)
        self.assertEqual(login_pending["status"], "authorization_pending")
        user_code = login_pending["user_code"]
        self.assertTrue(user_code.startswith("HM-"))

        # 模拟用户在浏览器完成免密授权批准该配对码
        assert self.srv is not None
        self.srv.store.approve_device_auth(user_code=user_code, email="pair_user@example.com")

        # CLI 再次运行 auth login 自动断点续接，秒级拿到 0600 凭证
        rc2, login_done = _run_cli(self._base_flags() + ["auth", "login", "--wait-seconds", "5"])
        self.assertEqual(rc2, 0)
        self.assertEqual(login_done["status"], "authorized")
        self.assertEqual(login_done["tier"], "free")

        # 2. CLI 调用 auth upgrade 返回站内在线买断页 URL，站内订单支付完成后 auth status 自动同步 Pro 买断权益
        rc_upg, upg_res = _run_cli(self._base_flags() + ["auth", "upgrade", "--no-browser"])
        self.assertEqual(rc_upg, 0)
        self.assertTrue(upg_res["ok"])
        self.assertIn("/activate?plan=pro_buyout", upg_res["upgrade_url"])

        order = self.srv.store.create_payment_order(
            email="pair_user@example.com", plan="pro_buyout", provider="stripe", pay_channel="wechat"
        )
        self.assertEqual(order["amount_cents"], 3990)
        self.srv.store.complete_payment_order(order["order_no"], paid_amount_cents=3990)

        rc_st, st_res = _run_cli(self._base_flags() + ["auth", "status"])
        self.assertEqual(rc_st, 0)
        self.assertEqual(st_res["tier"], "pro")
        self.assertTrue(st_res["lifetime"])
        self.assertIsNone(st_res["days_left"])
        self.assertTrue(st_res["updates_included"])

        # 刷新真实工作区看板，自动识别为 Pro 买断长期有效
        real_dash = self.root / "real-pro-dash.html"
        _, d_pro = _run_cli(self._base_flags() + ["build-dashboard", "--out", str(real_dash)])
        self.assertEqual(d_pro["tier"], "pro")
        self.assertIn("买断长期有效", real_dash.read_text(encoding="utf-8"))

    def test_runtime_flags_free_outsourcing_filter_dedup_and_xlsx_export(self) -> None:
        import zipfile

        _run_cli(self._base_flags() + ["init", "--account-name", "林远", "--roles", "AI产品经理"])

        # 1. 验证运行态加锁、记录找岗时间戳、解锁全链路正常工作
        rc_b, busy_res = _run_cli(self._base_flags() + ["pro", "mark-busy", "--reason", "patrol_round"])
        self.assertEqual(rc_b, 0)
        self.assertTrue(busy_res["runtime_state"]["busy"])

        rc_d, disc_res = _run_cli(self._base_flags() + ["pro", "mark-discovered"])
        self.assertEqual(rc_d, 0)
        self.assertIsNotNone(disc_res["runtime_state"]["last_discover_at"])

        rc_i, idle_res = _run_cli(self._base_flags() + ["pro", "mark-idle"])
        self.assertEqual(rc_i, 0)
        self.assertFalse(idle_res["runtime_state"]["busy"])
        self.assertIsNotNone(idle_res["runtime_state"]["last_discover_at"])

        # 2. 验证开源免费版外包过滤开关真实生效，且不误伤“自研非外包”
        _, scr_out = _run_cli(
            self._base_flags()
            + ["screen-basic", "--company", "软通动力信息技术有限公司", "--title", "AI产品经理", "--salary", "25-35K"]
        )
        self.assertFalse(scr_out["pass_basic"])
        self.assertTrue(any("外包" in r for r in scr_out["blocked_reasons"]))

        _, scr_self = _run_cli(
            self._base_flags()
            + [
                "screen-basic",
                "--company",
                "星澜智能科技有限公司",
                "--title",
                "AI产品经理",
                "--salary",
                "30-45K",
                "--jd",
                "核心自研团队非外包，甲方直招无需驻场",
            ]
        )
        self.assertTrue(scr_self["pass_basic"])

        # 3. 验证同大厂曾跳过不相关岗位不会一刀切误杀新方向岗位，而公司级外包跳过或网页已开聊会准确拦截
        _run_cli(
            self._base_flags()
            + [
                "ledger",
                "upsert-job",
                "--job-json",
                json.dumps(
                    {
                        "company": "腾讯科技",
                        "business_nature": "互联网自研",
                        "title": "商业化广告销售运营",
                        "status": "screened_skip",
                        "note": "=SUM(1,2) 方向不符跳过",
                    }
                ),
            ]
        )
        _, dup_diff_role = _run_cli(
            self._base_flags() + ["ledger", "check-dup", "--company", "腾讯", "--title", "高级AI产品经理"]
        )
        self.assertFalse(dup_diff_role["duplicate"])
        self.assertTrue(len(dup_diff_role["warnings"]) >= 1)

        _, dup_open_chat = _run_cli(
            self._base_flags()
            + ["ledger", "check-dup", "--company", "腾讯", "--title", "高级AI产品经理", "--chat-already-open"]
        )
        self.assertTrue(dup_open_chat["duplicate"])

        # 4. 验证原生 Excel (.xlsx) 导出结构合法且带公式注入单引号转义
        xlsx_path = self.root / "export-ledger.xlsx"
        rc_x, x_res = _run_cli(self._base_flags() + ["ledger", "export-excel", "--out", str(xlsx_path)])
        self.assertEqual(rc_x, 0)
        self.assertEqual(x_res["format"], "xlsx")
        self.assertTrue(xlsx_path.is_file())
        with zipfile.ZipFile(xlsx_path, "r") as zf:
            names = set(zf.namelist())
            self.assertIn("xl/workbook.xml", names)
            sheet_xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("腾讯科技", sheet_xml)
            self.assertIn("&apos;=SUM(1,2)", sheet_xml)

        # 5. 验证二进制 PDF 传入 extract-employers 绝不抛 UnicodeDecodeError
        fake_pdf = self.root / "resume.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n")
        rc_pdf, pdf_res = _run_cli(
            self._base_flags() + ["config", "extract-employers", "--from-file", str(fake_pdf)]
        )
        self.assertEqual(rc_pdf, 0)
        self.assertTrue(pdf_res["ok"])

    def test_optional_outsourcing_and_onsite_independent_policies(self) -> None:
        # 1. 初始化时支持直接放开外包但保持拦截驻场（如接受 OD/外包编制但拒绝客户现场驻场）
        rc_init, init_res = _run_cli(
            self._base_flags()
            + [
                "init",
                "--account-name",
                "林远",
                "--roles",
                "AI产品经理,产品经理",
                "--filter-outsourcing",
                "false",
                "--filter-onsite",
                "true",
            ]
        )
        self.assertEqual(rc_init, 0)
        cfg = init_res["config"]
        self.assertFalse(cfg["filter_outsourcing"])
        self.assertEqual(cfg["outsourcing_policy"], "allow_all")
        self.assertTrue(cfg["filter_onsite"])
        self.assertEqual(cfg["onsite_policy"], "block_all")

        # 软通动力 / 华为OD（本部办公无需驻场）应顺利放行并保留透明提示
        _, scr_od = _run_cli(
            self._base_flags()
            + [
                "screen-basic",
                "--company",
                "外企德科人力资源服务有限公司",
                "--title",
                "AI产品经理（华为OD）",
                "--salary",
                "25-40K",
                "--jd",
                "华为研究所本部办公无需驻场，负责智能体评测",
            ]
        )
        self.assertTrue(scr_od["pass_basic"])
        self.assertTrue(scr_od["is_outsourcing"])
        self.assertFalse(scr_od["is_onsite"])
        self.assertTrue(any("外包/派遣性质" in w for w in scr_od["warnings"]))

        # 同配置下，要求银行驻场的岗位仍被驻场规则精准拦截
        _, scr_onsite_blocked = _run_cli(
            self._base_flags()
            + [
                "screen-basic",
                "--company",
                "软通动力信息技术有限公司",
                "--title",
                "AI产品经理（银行驻场）",
                "--salary",
                "25-35K",
                "--jd",
                "长期驻场某国有大行科技部交付",
            ]
        )
        self.assertFalse(scr_onsite_blocked["pass_basic"])
        self.assertTrue(scr_onsite_blocked["is_onsite"])
        self.assertTrue(any("驻场" in r for r in scr_onsite_blocked["blocked_reasons"]))

        # 2. 切换为排除外包、但接受乙方自研驻场（filter_outsourcing=true, filter_onsite=false）
        _, cfg_res2 = _run_cli(
            self._base_flags()
            + ["config", "set", "--filter-outsourcing", "true", "--filter-onsite", "false"]
        )
        self.assertTrue(cfg_res2["config"]["filter_outsourcing"])
        self.assertEqual(cfg_res2["config"]["outsourcing_policy"], "block_all")
        self.assertFalse(cfg_res2["config"]["filter_onsite"])
        self.assertEqual(cfg_res2["config"]["onsite_policy"], "allow_all")

        _, scr_selfdev_onsite = _run_cli(
            self._base_flags()
            + [
                "screen-basic",
                "--company",
                "星云智算科技有限公司",
                "--title",
                "AI解决方案产品经理",
                "--salary",
                "28-40K",
                "--jd",
                "核心自研非外包，甲方直签，项目初期需驻场头部车企客户现场推进落地",
            ]
        )
        self.assertTrue(scr_selfdev_onsite["pass_basic"])
        self.assertFalse(scr_selfdev_onsite["is_outsourcing"])
        self.assertTrue(scr_selfdev_onsite["is_onsite"])

        # 3. 切换为 inbound_only（主动找岗跳过，被动来信放行）
        _run_cli(
            self._base_flags()
            + ["config", "set", "--outsourcing-policy", "inbound_only", "--onsite-policy", "inbound_only"]
        )
        _, scr_outbound = _run_cli(
            self._base_flags()
            + ["screen-basic", "--company", "中软国际", "--title", "AI产品经理", "--source", "outbound"]
        )
        self.assertFalse(scr_outbound["pass_basic"])
        _, scr_inbound = _run_cli(
            self._base_flags()
            + ["screen-basic", "--company", "中软国际", "--title", "AI产品经理", "--source", "inbound"]
        )
        self.assertTrue(scr_inbound["pass_basic"])

        # 4. 完全放开外包与驻场时，账本查重不会因历史外包/驻场跳过记录连坐封杀该公司其他新方向岗位
        _run_cli(
            self._base_flags()
            + ["config", "set", "--filter-outsourcing", "false", "--filter-onsite", "false"]
        )
        _run_cli(
            self._base_flags()
            + [
                "ledger",
                "upsert-job",
                "--job-json",
                json.dumps(
                    {
                        "company": "软通动力",
                        "business_nature": "驻场外包",
                        "title": "Java初级开发工程师",
                        "risk_tags": ["外包已排雷"],
                        "status": "screened_skip",
                        "note": "早期默认排雷跳过",
                    }
                ),
            ]
        )
        _, dup_after_allow = _run_cli(
            self._base_flags()
            + ["ledger", "check-dup", "--company", "软通动力信息技术有限公司", "--title", "高级AI产品经理"]
        )
        self.assertFalse(dup_after_allow["duplicate"])

        # 刷新静态看板，确认策略面板准确展示开放状态
        dash_out = self.root / "open-pref-dashboard.html"
        _, d_res = _run_cli(self._base_flags() + ["build-dashboard", "--out", str(dash_out)])
        self.assertTrue(d_res["ok"])
        dash_html = dash_out.read_text(encoding="utf-8")
        self.assertIn("开放接受（不排除外包 / 派遣 / OD 机会）", dash_html)
        self.assertIn("开放接受（不排除驻场 / 客户现场外派机会）", dash_html)

        # 5. 回归验证：否定修饰语（“非软件外包 / 非华为OD / 不需要驻场”）绝不误杀；单条驻场跳过不连坐封杀同司本部岗；旧版单字段 config.json 兼容升级
        _run_cli(
            self._base_flags()
            + ["config", "set", "--filter-outsourcing", "true", "--filter-onsite", "true"]
        )
        _, scr_neg_rich = _run_cli(
            self._base_flags()
            + [
                "screen-basic",
                "--company",
                "星辰自研科技",
                "--title",
                "AI产品经理",
                "--jd",
                "正编自研岗位，非软件外包，非华为OD，不需要驻场，不涉及外派",
            ]
        )
        self.assertTrue(scr_neg_rich["pass_basic"])
        self.assertFalse(scr_neg_rich["is_outsourcing"])
        self.assertFalse(scr_neg_rich["is_onsite"])

        # 自研公司曾跳过 1 个驻场交付岗，后续同公司总部自研岗在 check-dup 阶段不被连坐封杀
        _run_cli(
            self._base_flags()
            + [
                "ledger",
                "upsert-job",
                "--job-json",
                json.dumps(
                    {
                        "company": "深信服科技",
                        "business_nature": "自研非外包",
                        "title": "政务项目驻场实施工程师",
                        "risk_tags": ["驻场已排雷"],
                        "status": "screened_skip",
                        "note": "驻场岗跳过",
                    }
                ),
            ]
        )
        _, dup_hq_role = _run_cli(
            self._base_flags()
            + ["ledger", "check-dup", "--company", "深信服", "--title", "总部安全大模型产品经理"]
        )
        self.assertFalse(dup_hq_role["duplicate"])

        # 旧版单开关 config.json（仅含 filter_outsourcing=false）加载时自动兼容升级为外包与驻场双放开
        legacy_ws = self.root / "legacy_ws"
        legacy_ws.mkdir(parents=True, exist_ok=True)
        (legacy_ws / "config.json").write_text(
            json.dumps({"account_display_name": "林远", "filter_outsourcing": False}),
            encoding="utf-8",
        )
        _, legacy_show = _run_cli(
            ["--home", str(self.home), "--workspace", str(legacy_ws), "config", "show"]
        )
        self.assertEqual(legacy_show["config"]["outsourcing_policy"], "allow_all")
        self.assertEqual(legacy_show["config"]["onsite_policy"], "allow_all")

    def test_new_user_onboarding_guidance_free_and_pro(self) -> None:
        os.environ["HIREMATE_NO_BROWSER"] = "1"
        fresh_ws = self.root / "brand_new_ws"
        fresh_home = self.root / "brand_new_home"
        flags = ["--home", str(fresh_home), "--workspace", str(fresh_ws), "--api-base", self.api_base]

        # 1. 全新未初始化工作区：config show 与 auth status 必须显式标记 initialized=False / onboarding_completed=False 并返回引导提示
        _, cfg_cold = _run_cli(flags + ["config", "show"])
        self.assertFalse(cfg_cold["initialized"])
        self.assertFalse(cfg_cold["onboarding_completed"])
        self.assertGreaterEqual(len(cfg_cold["missing_steps"]), 2)
        self.assertIn("简历", cfg_cold["next_action_hint"])

        _, st_cold = _run_cli(flags + ["auth", "status"])
        self.assertEqual(st_cold["tier"], "free")
        self.assertFalse(st_cold["onboarding_completed"])

        # 2. 付费新用户先在官网买断 Pro 再绑定设备（尚未建档）：绑定成功后自动提醒顺势引导完成 5 分钟建档
        if HAS_PRO_SERVER and self.srv is not None:
            self.srv.store.grant_pro(email="new_pro_buyer@example.com", days=0)
            _, p_start = _run_cli(flags + ["auth", "login", "--no-wait"])
            self.srv.store.approve_device_auth(
                user_code=p_start["user_code"], email="new_pro_buyer@example.com"
            )
            _, p_done = _run_cli(flags + ["auth", "login", "--wait-seconds", "5"])
            self.assertEqual(p_done["tier"], "pro")
            self.assertFalse(p_done["onboarding_completed"])
            self.assertIn("5 分钟建档", p_done["message"])

        # 3. 仅运行 init 但尚未把真实简历事实写入 profile_facts.md 时，仍识别为未完成建档（防止带空白模板乱答疑）
        _, init_res = _run_cli(
            flags
            + [
                "init",
                "--account-name",
                "林远",
                "--roles",
                "AI产品经理",
                "--employers",
                "前东家示例公司",
            ]
        )
        _, cfg_half = _run_cli(flags + ["config", "show"])
        self.assertTrue(cfg_half["initialized"])
        self.assertFalse(cfg_half["profile_customized"])
        self.assertFalse(cfg_half["onboarding_completed"])

        # 4. 将确认后的个人事实写入 profile_facts.md 后，onboarding_completed 立即转为 True
        Path(init_res["profile_facts_path"]).write_text(
            "# 个人求职事实与答法档案\n\n- 目标岗位：AI 产品经理\n- 核心经验：企业级 RAG 与 Agent 落地\n",
            encoding="utf-8",
        )
        _, cfg_ready = _run_cli(flags + ["config", "show"])
        self.assertTrue(cfg_ready["onboarding_completed"])
        self.assertEqual(cfg_ready["missing_steps"], [])

        # 5. 验证《四项启动准备核对单（checklist）》、无浏览器 copilot 降级流、仅平台在线简历模式与“无任何简历”硬阻断
        _, chk_no_browser = _run_cli(flags + ["checklist", "--browser-capable", "false"])
        self.assertTrue(chk_no_browser["ok"])
        self.assertEqual(chk_no_browser["items"][0]["status"], "copilot_fallback")
        self.assertIn("副驾驶粘贴模式", chk_no_browser["markdown"])
        self.assertIn("严禁", chk_no_browser["items"][0]["action"])

        # 场景 A：用户完全没有任何简历（本地与平台在线皆无） -> 硬性阻断不可放行搜岗
        _run_cli(flags + ["config", "set", "--resume-source", "none"])
        _, chk_blocked = _run_cli(flags + ["checklist", "--browser-capable", "true"])
        self.assertFalse(chk_blocked["onboarding_completed"])
        self.assertFalse(chk_blocked["can_proceed_to_search"])
        self.assertEqual(chk_blocked["hard_blocker"], "NO_RESUME_AVAILABLE")
        self.assertEqual(chk_blocked["items"][2]["status"], "blocked")

        # 场景 B：用户无本地 PDF，但从 BOSS 在线简历提取建档（online_only）且切为 copilot 模式 -> 建档放行并标记无线上 PDF 附件
        _run_cli(
            flags + ["config", "set", "--execution-mode", "copilot", "--resume-source", "online_only"]
        )
        _, chk_online = _run_cli(flags + ["checklist"])
        self.assertTrue(chk_online["onboarding_completed"])
        self.assertTrue(chk_online["can_proceed_to_search"])
        self.assertFalse(chk_online["has_attachment_resume"])
        self.assertEqual(chk_online["items"][0]["status"], "copilot_fallback")
        self.assertEqual(chk_online["items"][2]["status"], "ready")
        self.assertIn("生成附件简历", chk_online["items"][2]["action"])

        dash_copilot = self.root / "copilot-online-dash.html"
        _, d_cop = _run_cli(flags + ["build-dashboard", "--out", str(dash_copilot)])
        self.assertTrue(d_cop["ok"])
        cop_html = dash_copilot.read_text(encoding="utf-8")
        self.assertIn("副驾驶粘贴协同", cop_html)
        self.assertIn("仅平台在线简历", cop_html)

    def test_audit_hardening_employer_aliases_force_preserve_auto_confirm_and_fail_closed(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from hiremate_lib import client as hm_client
        from hiremate_lib.normalize import employer_shield_match, normalize_company

        # 1. 多重省市前缀、两字大厂子品牌、括号品牌别名与工商集团别名匹配
        self.assertEqual(normalize_company("中国广东省深圳市示例科技有限公司"), "示例")
        self.assertTrue(employer_shield_match("华为终端有限公司", "华为"))
        self.assertTrue(employer_shield_match("京东物流", "京东"))
        self.assertTrue(employer_shield_match("腾讯音乐娱乐集团", "腾讯"))
        self.assertTrue(employer_shield_match("饿了么", "拉扎斯网络科技（上海）有限公司（饿了么）"))
        self.assertTrue(employer_shield_match("美团优选", "北京三快在线科技有限公司"))
        self.assertFalse(employer_shield_match("竹报平安餐饮管理有限公司", "平安"))

        # 2. init --force 保留前东家与已填个人档案；--reset-profile 才彻底重置并备份
        ws2 = self.root / "ws_hardening"
        home2 = self.root / "home_hardening"
        flags = ["--home", str(home2), "--workspace", str(ws2), "--api-base", "http://127.0.0.1:9"]
        _run_cli(
            flags + ["init", "--account-name", "林远", "--employers", "华为,拉扎斯（饿了么）", "--roles", "AI产品经理"]
        )
        pf_file = ws2 / "profile_facts.md"
        pf_file.write_text("# 个人求职事实与答法档案\n- 真实定制内容：主导过千万级推荐系统\n", encoding="utf-8")

        _, force_res = _run_cli(flags + ["init", "--force", "--min-salary-k", "30"])
        self.assertTrue(force_res["ok"])
        self.assertTrue(force_res["profile_preserved"])
        self.assertIn("华为", force_res["config"]["past_employers"])
        self.assertIn("拉扎斯（饿了么）", force_res["config"]["blocked_companies"])
        self.assertIn("主导过千万级推荐系统", pf_file.read_text(encoding="utf-8"))

        # 验证 screen-basic 对两字前东家子公司与括号品牌准确拦截
        _, scr_hw = _run_cli(flags + ["screen-basic", "--company", "华为终端有限公司", "--title", "AI产品经理"])
        self.assertFalse(scr_hw["pass_basic"])
        _, scr_ele = _run_cli(flags + ["screen-basic", "--company", "饿了么", "--title", "AI产品经理"])
        self.assertFalse(scr_ele["pass_basic"])

        # 3. --patch-json 字符串误传给列表字段绝不拆成单字符
        _, patch_res = _run_cli(flags + ["config", "set", "--patch-json", '{"target_roles":"Java后端，Go后端"}'])
        self.assertEqual(patch_res["config"]["target_roles"], ["Java后端", "Go后端"])

        # 4. 切换全自动（auto）：免费态拒绝 -> Pro 态首次切换强制要求风险确认 -> 传 --ack-auto-risk 后生效
        rc_free_auto, res_free_auto = _run_cli(flags + ["config", "set", "--mode-discover", "auto"])
        self.assertEqual(rc_free_auto, 1)
        self.assertEqual(res_free_auto["code"], "PRO_REQUIRED_FOR_AUTO")

        hm_client.save_credentials(
            {
                "email": "pro@example.com",
                "token": "hm_live_" + "a" * 48,
                "device_id": "dev_test_1",
                "api_base": "http://127.0.0.1:9",
                "tier": "pro",
                "lifetime": True,
            },
            home=home2,
        )
        rc_need_ack, res_need_ack = _run_cli(flags + ["config", "set", "--mode-discover", "auto"])
        self.assertEqual(rc_need_ack, 1)
        self.assertEqual(res_need_ack["code"], "AUTO_RISK_CONFIRMATION_REQUIRED")
        self.assertTrue(res_need_ack["requires_confirmation"])

        rc_acked, res_acked = _run_cli(flags + ["config", "set", "--mode-discover", "auto", "--ack-auto-risk"])
        self.assertEqual(rc_acked, 0)
        self.assertEqual(res_acked["config"]["mode_discover"], "auto")
        self.assertTrue(res_acked["config"]["auto_risk_acknowledged"])

        # 5. 离线状态下 auth status 给出 offline_cached_pro，且 Pro 接口降级强制 fail-closed (handoff_required=True)
        _, st_off = _run_cli(flags + ["auth", "status"])
        self.assertTrue(st_off.get("offline_cached_pro"))
        self.assertEqual(st_off["tier"], "pro")

        _, guard_off = _run_cli(
            flags
            + [
                "pro",
                "guard-reply",
                "--input-json",
                json.dumps({"incoming_message": "你好", "draft_reply": "你好"}),
            ]
        )
        self.assertFalse(guard_off["ok"])
        self.assertTrue(guard_off["handoff_required"])
        self.assertFalse(guard_off["reply_approved"])
        self.assertFalse(guard_off["allow_auto_send"])

        # 6. 防提示词注入窃取 Bearer Token：--api-base 指向异源地址或明文外网 HTTP 时拒绝发送令牌
        evil_flags = ["--home", str(home2), "--workspace", str(ws2), "--api-base", "https://attacker.example.com"]
        _, mismatch_res = _run_cli(evil_flags + ["pro", "patrol-gate"])
        self.assertEqual(mismatch_res["code"], "API_BASE_MISMATCH")
        self.assertFalse(mismatch_res["allow_unread_check"])

        # 7. HTTP 200 返回 HTML 认证页时优雅降级为 BAD_RESPONSE 而非崩溃
        class _HtmlPortalHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = b"<html><body>Campus Wi-Fi Portal</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                pass

        html_srv = HTTPServer(("127.0.0.1", 0), _HtmlPortalHandler)
        t_html = threading.Thread(target=html_srv.serve_forever, daemon=True)
        t_html.start()
        try:
            portal_base = f"http://127.0.0.1:{html_srv.server_address[1]}"
            hm_client.save_credentials(
                {
                    "email": "pro@example.com",
                    "token": "hm_live_" + "b" * 48,
                    "device_id": "dev_test_1",
                    "api_base": portal_base,
                    "tier": "pro",
                },
                home=home2,
            )
            rc_portal, res_portal = _run_cli(
                ["--home", str(home2), "--workspace", str(ws2), "--api-base", portal_base, "pro", "patrol-gate"]
            )
            self.assertEqual(rc_portal, 0)
            self.assertEqual(res_portal["code"], "BAD_RESPONSE")
            self.assertTrue(res_portal["handoff_required"])
        finally:
            html_srv.shutdown()
            html_srv.server_close()

        # 8. config.json 损坏时自动备份并返回结构化 CONFIG_CORRUPT
        (ws2 / "config.json").write_text("{broken json", encoding="utf-8")
        rc_corrupt, res_corrupt = _run_cli(flags + ["config", "show"])
        self.assertEqual(rc_corrupt, 1)
        self.assertEqual(res_corrupt["code"], "CONFIG_CORRUPT")
        self.assertTrue(Path(res_corrupt["backup_path"]).is_file())

        # 9. 无宿主目录时 install_plugin 不凭空创建空宿主目录；uninstall 清理软链但保留用户数据
        no_host_canon = self.root / "no_host_canon"
        missing_wb = self.root / "nonexistent_wb"
        no_host_res = install_plugin.install(
            target="auto",
            canonical_home=str(no_host_canon),
            search_roots={"workbuddy": missing_wb},
        )
        self.assertTrue(no_host_res["ok"])
        self.assertEqual(no_host_res["linked_hosts"], [])
        self.assertIn("warning", no_host_res)
        self.assertFalse(missing_wb.exists())

        (no_host_canon / "default").mkdir(parents=True, exist_ok=True)
        (no_host_canon / "default" / "profile_facts.md").write_text("keep me", encoding="utf-8")
        un_res = install_plugin.uninstall(
            canonical_home=str(no_host_canon),
            search_roots={"workbuddy": missing_wb},
            purge_data=False,
        )
        self.assertTrue(un_res["ok"])
        self.assertTrue(un_res["data_preserved"])
        self.assertFalse((no_host_canon / "skill" / "hiremate").exists())
        self.assertTrue((no_host_canon / "default" / "profile_facts.md").is_file())


if __name__ == "__main__":
    unittest.main()
