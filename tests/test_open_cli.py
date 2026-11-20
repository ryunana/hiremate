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


def _run_cli(args: list[str], *, env: dict[str, str] | None = None) -> tuple[int, dict]:
    from io import StringIO
    from contextlib import redirect_stdout
    from unittest import mock as _mock

    buf = StringIO()
    if env is not None:
        with _mock.patch.dict(os.environ, env):
            with redirect_stdout(buf):
                rc = hiremate.main(args)
    else:
        with redirect_stdout(buf):
            rc = hiremate.main(args)
    raw = buf.getvalue().strip()
    return rc, json.loads(raw) if raw else {}


class TestOpenPluginAndE2E(unittest.TestCase):
    def setUp(self) -> None:
        from unittest import mock as _mock

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "hm_home"
        self.ws = self.root / "ws"
        self.server_data = self.root / "server_data"
        self.srv = None
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("STRIPE_", "XUNHUPAY_", "EPAY_"))}
        clean_env["HIREMATE_DISABLE_DESKTOP_NOTIFY"] = "1"
        clean_env["HIREMATE_DISABLE_UPDATE_CHECK"] = "1"
        clean_env["HIREMATE_NO_BROWSER"] = "1"
        clean_env["HIREMATE_HOME"] = str(self.home)
        clean_env.pop("HIREMATE_ENV", None)
        self._env_patcher = _mock.patch.dict(os.environ, clean_env, clear=True)
        self._env_patcher.start()

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
        self._env_patcher.stop()
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
        self.assertFalse(gate_pro["allow_background_discover"])
        self.assertEqual(gate_pro["discover_action"], "semi_review_only")

        # 切换为 favorite 模式后，patrol-gate 放行后台静默收藏且明确标记 favorite_only
        _run_cli(self._base_flags() + ["config", "set", "--mode-discover", "favorite"])
        _, gate_fav = _run_cli(
            self._base_flags() + ["pro", "patrol-gate", "--now", "2026-11-16T10:30:00+08:00"]
        )
        self.assertTrue(gate_fav["allow_background_discover"])
        self.assertEqual(gate_fav["discover_action"], "favorite_only")
        _run_cli(self._base_flags() + ["config", "set", "--mode-discover", "semi"])

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
            ["hiremate-status", "hiremate-login", "hiremate-dashboard", "hiremate-update"],
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
        self.assertEqual(
            parsed_dedup["commands"],
            ["hiremate-status", "hiremate-login", "hiremate-dashboard", "hiremate-update"],
        )

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
        self.assertEqual(order["amount_cents"], 6900)
        self.srv.store.complete_payment_order(order["order_no"], paid_amount_cents=6900)

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

        # 6. 验证自带 Browser Use「有头+持久化」两分支判定与用户二选一持久化
        _run_cli(flags + ["config", "set", "--execution-mode", "browser", "--browser-provider", "auto"])

        # 分支 A-1：自带 Browser Use 不支持有头+持久化，且本机未装 ego lite -> 直接跳过并指引安装 ego lite + 告知用户
        _, chk_skip_install = _run_cli(
            flags + ["checklist", "--builtin-headed-persistent", "false", "--ego-installed", "false"]
        )
        self.assertEqual(chk_skip_install["items"][0]["status"], "install_ego_required")
        self.assertIn("不支持有头+持久化", chk_skip_install["items"][0]["summary"])
        self.assertIn("ego lite", chk_skip_install["items"][0]["action"])

        # 分支 A-2：自带 Browser Use 不支持有头+持久化，本机已装 ego lite -> 直接跳过并切换本机 ego lite
        _, chk_skip_ready = _run_cli(
            flags + ["checklist", "--builtin-headed-persistent", "false", "--ego-installed", "true"]
        )
        self.assertEqual(chk_skip_ready["items"][0]["status"], "auto_ego_ready")
        self.assertTrue(chk_skip_ready["ego_lite_installed"])

        # 分支 B：自带 Browser Use 支持有头+持久化 -> 让用户按 Token 偏好二选一（ego lite vs 自带 Browser Use）
        _, chk_choice = _run_cli(
            flags + ["checklist", "--builtin-headed-persistent", "true", "--ego-installed", "false"]
        )
        self.assertEqual(chk_choice["items"][0]["status"], "choice_required")
        self.assertIn("省 Token", chk_choice["items"][0]["action"])
        self.assertIn("Token 多随便用", chk_choice["items"][0]["action"])

        # 用户选定「Token 多随便用 -> 自带 Browser Use」后持久化生效并同步到看板
        _run_cli(flags + ["config", "set", "--browser-provider", "builtin_browser_use"])
        _, chk_chosen_builtin = _run_cli(flags + ["checklist", "--builtin-headed-persistent", "true"])
        self.assertEqual(chk_chosen_builtin["browser_provider"], "builtin_browser_use")
        self.assertEqual(chk_chosen_builtin["items"][0]["status"], "ready")
        self.assertIn("宿主自带 Browser Use", chk_chosen_builtin["items"][0]["summary"])

        dash_builtin = self.root / "builtin-browser-dash.html"
        _run_cli(flags + ["build-dashboard", "--out", str(dash_builtin)])
        self.assertIn("宿主自带 Browser Use", dash_builtin.read_text(encoding="utf-8"))

        # 用户改选「省 Token -> ego lite」后同样持久化并同步到看板
        _run_cli(flags + ["config", "set", "--browser-provider", "ego_lite"])
        _, chk_chosen_ego = _run_cli(
            flags + ["checklist", "--builtin-headed-persistent", "true", "--ego-installed", "true"]
        )
        self.assertEqual(chk_chosen_ego["browser_provider"], "ego_lite")
        self.assertEqual(chk_chosen_ego["items"][0]["status"], "ready")
        self.assertIn("post_onboarding_guide", chk_chosen_ego)

        # 退出登录验证纯免费版（Free）建档收尾引导菜单 + 免费版空看板的下一步启动卡与 Pro 购买跳转链接
        _run_cli(flags + ["auth", "logout"])
        _, chk_free_guide = _run_cli(
            flags + ["checklist", "--builtin-headed-persistent", "true", "--ego-installed", "true"]
        )
        self.assertEqual(chk_free_guide["tier"], "free")
        self.assertIn("跑第一轮搜岗深筛预览", chk_free_guide["post_onboarding_guide"]["user_reply_menu_markdown"])

        dash_ego = self.root / "ego-lite-dash.html"
        _, dash_empty_res = _run_cli(flags + ["build-dashboard", "--open", "--out", str(dash_ego)])
        self.assertEqual(dash_empty_res["tier"], "free")
        self.assertTrue(dash_empty_res.get("first_run_empty"))
        self.assertIn("file_url", dash_empty_res)
        self.assertTrue(str(dash_empty_res["file_url"]).startswith("file://"))
        # 测试环境下 HIREMATE_NO_BROWSER=1，验证 --open 正常走完不报错且返回 dashboard_spotlight 独立推介卡片
        self.assertIs(dash_empty_res.get("browser_opened"), False)
        self.assertIn("dashboard_spotlight", dash_empty_res)
        self.assertIn("本地可视化求职看板", dash_empty_res["dashboard_spotlight"]["markdown"])
        self.assertIn("7 格实时投递转化漏斗", dash_empty_res["dashboard_spotlight"]["markdown"])
        self.assertIn("四大交互视图", dash_empty_res["dashboard_spotlight"]["markdown"])
        self.assertIn("post_onboarding_guide", dash_empty_res)
        self.assertIn("本地可视化求职看板", dash_empty_res["post_onboarding_guide"]["user_reply_menu_markdown"])
        ego_html = dash_ego.read_text(encoding="utf-8")
        self.assertIn("ego lite", ego_html)
        # 验证免费版看板包含直接可点击的 Pro 购买跳转链接与功能对比链接
        self.assertIn("/activate?plan=pro_buyout#subscribe-box", ego_html)
        self.assertIn("/#pricing", ego_html)
        self.assertIn("¥69 立即购买解锁 Pro", ego_html)
        # 验证 0 岗位冷启动空看板包含下一步开跑引导卡与一键复制口令
        self.assertIn("本地求职建档已完成，选一种方式跑通第一轮", ego_html)
        self.assertIn("复制口令开跑", ego_html)

        # 7. 验证 8 项职位筛选策略全透明确认表与全参数可修改（拒绝暗箱默认）
        self.assertIn("strategy_confirmation", chk_free_guide)
        self.assertEqual(len(chk_free_guide["strategy_confirmation"]["items"]), 8)
        self.assertIn("异地知名大厂可先聊", chk_free_guide["strategy_confirmation"]["markdown"])
        self.assertIn("容忍面板标薪略低", chk_free_guide["strategy_confirmation"]["markdown"])

        _, mod_strat = _run_cli(
            flags
            + [
                "config",
                "set",
                "--remote-bigtech-flex",
                "false",
                "--salary-panel-tolerance",
                "false",
                "--blocked-companies",
                "某竞对科技",
                "--exclude-keywords",
                "电销,保险",
                "--headhunter-policy",
                "block_all",
                "--inbound-resume-policy",
                "strict",
            ]
        )
        self.assertTrue(mod_strat["ok"])
        self.assertFalse(mod_strat["config"]["remote_bigtech_flex"])
        self.assertFalse(mod_strat["config"]["salary_panel_tolerance"])
        self.assertIn("前东家示例公司", mod_strat["config"]["blocked_companies"])
        self.assertIn("某竞对科技", mod_strat["config"]["blocked_companies"])
        self.assertEqual(mod_strat["config"]["exclude_keywords"], ["电销", "保险"])
        self.assertIn("严格仅看所选城市", mod_strat["strategy_confirmation"]["markdown"])
        self.assertIn("严格卡死底薪", mod_strat["strategy_confirmation"]["markdown"])

        # 8. 验证「先以免费号登录 -> 网页付款升 Pro -> 绑定/直接切全自动」完整权益欢迎单与全自动切换引导
        if HAS_PRO_SERVER and self.srv is not None:
            # 8a. 先登录尚未付费的免费账号（本地缓存 tier="free"，找岗与答疑均为 semi）
            _, f_start = _run_cli(flags + ["auth", "login", "--no-wait"])
            self.srv.store.approve_device_auth(
                user_code=f_start["user_code"], email="upgrade_journey@example.com"
            )
            _, f_bound = _run_cli(flags + ["auth", "login", "--wait-seconds", "5"])
            self.assertEqual(f_bound["tier"], "free")
            self.assertNotIn("pro_activation_guide", f_bound)

            # 8b. 模拟用户在网页扫码完成 ¥69 买断（此时尚未手动跑 auth status）
            self.srv.store.grant_pro(email="upgrade_journey@example.com", days=0)

            # 8c. 用户跑 auth status（或重跑 auth login）：自动将本地看板刷新为 Pro 视图，输出 5 大权益并提醒当前仍为半自动可切全自动
            _, st_pro = _run_cli(flags + ["auth", "status"])
            self.assertEqual(st_pro["tier"], "pro")
            self.assertTrue(st_pro.get("dashboard_refreshed"))
            self.assertIn("pro_activation_guide", st_pro)
            guide = st_pro["pro_activation_guide"]
            self.assertEqual(len(guide["unlocked_benefits"]), 5)
            self.assertTrue(guide["still_in_semi_mode"])
            self.assertIn("全自动模式", guide["user_reply_menu_markdown"])
            self.assertIn("仍处于免费版默认的半自动状态", guide["user_reply_menu_markdown"])

            # 8d. 即使本地凭证曾被标为 free，用户刚买完直接请求开启全自动时也会实时向云端刷新放行（仅需风险确认，绝不误报未开通 Pro）
            cred_file = Path(f_bound["credentials_path"])
            stale_cred = json.loads(cred_file.read_text(encoding="utf-8"))
            stale_cred["tier"] = "free"
            cred_file.write_text(json.dumps(stale_cred, ensure_ascii=False), encoding="utf-8")
            rc_auto, auto_confirm = _run_cli(
                flags + ["config", "set", "--mode-discover", "auto", "--mode-reply", "auto"]
            )
            self.assertEqual(rc_auto, 1)
            self.assertEqual(auto_confirm["code"], "AUTO_RISK_CONFIRMATION_REQUIRED")
            _, auto_ok = _run_cli(
                flags + ["config", "set", "--mode-discover", "auto", "--mode-reply", "auto", "--ack-auto-risk"]
            )
            self.assertTrue(auto_ok["ok"])
            self.assertEqual(auto_ok["config"]["mode_discover"], "auto")
            self.assertEqual(auto_ok["config"]["mode_reply"], "auto")
            self.assertTrue(auto_ok.get("dashboard_refreshed"))
            ws_dash_html = Path(auto_ok["dashboard_path"]).read_text(encoding="utf-8")
            self.assertIn("找岗：全自动 / 答疑：全自动", ws_dash_html)
            self.assertIn("全自动找岗已开启 · 冷却/超额暂存候选", ws_dash_html)
            self.assertIn("全自动定额破冰（已开启）", ws_dash_html)
            self.assertIn("全自动守卫答疑（已开启）", ws_dash_html)
            self.assertNotIn("半自动待确认破冰候选", ws_dash_html)

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

    def test_product_backlog_closures_docx_dedup_privacy_ledger_and_hosts(self) -> None:
        import sqlite3
        import subprocess
        import zipfile
        from hiremate_lib import __version__, client as hm_client, ledger as hm_ledger, notifier as hm_notifier
        from hiremate_lib.normalize import companies_match, employer_shield_match, title_similarity

        # 1. --version 版本号一致性校验
        ver_proc = subprocess.run(
            [sys.executable, str(OPEN_ROOT / "hiremate.py"), "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn(__version__, ver_proc.stdout.strip())

        # 2. 括号内细分方向/语言栈保留 & check-dup 对历史 screened_skip 的精准放行
        self.assertLess(title_similarity("高级产品经理（AI方向）", "高级产品经理（电商交易）"), 0.55)
        self.assertLess(title_similarity("后端开发工程师（Go）", "后端开发工程师（Java）"), 0.55)
        self.assertGreater(title_similarity("高级AI产品经理（急聘/双休）", "AI产品经理"), 0.85)

        ws3 = self.root / "ws_backlog"
        home3 = self.root / "home_backlog"
        flags = ["--home", str(home3), "--workspace", str(ws3), "--api-base", "http://127.0.0.1:9"]
        _run_cli(flags + ["init", "--account-name", "林远", "--employers", "北京字跳网络技术有限公司", "--roles", "AI产品经理"])

        # 曾跳过“同公司但方向不符”的岗位后，后续遇到同公司真正对口的细分方向绝不误判重复
        _run_cli(
            flags
            + [
                "ledger",
                "upsert-job",
                "--job-json",
                json.dumps(
                    {
                        "company": "快手",
                        "title": "高级产品经理（电商交易）",
                        "recruiter": "王女士",
                        "status": "screened_skip",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        _, dup_diff_track = _run_cli(
            flags + ["ledger", "check-dup", "--company", "快手", "--title", "高级产品经理（AI方向）", "--recruiter", "李先生"]
        )
        self.assertFalse(dup_diff_track["duplicate"])
        self.assertFalse(dup_diff_track["same_company_active"])

        # 但同一细分岗位换个 HR 重发时仍准确拦截重复
        _, dup_same_track = _run_cli(
            flags + ["ledger", "check-dup", "--company", "快手", "--title", "高级产品经理（电商交易）", "--recruiter", "赵先生"]
        )
        self.assertTrue(dup_same_track["duplicate"])

        # 曾跳过宽泛“产品经理”后，遇到“AI产品经理”不误杀
        _run_cli(
            flags
            + [
                "ledger",
                "upsert-job",
                "--job-json",
                json.dumps({"company": "小红书", "title": "产品经理", "status": "screened_skip"}, ensure_ascii=False),
            ]
        )
        _, dup_broad_vs_ai = _run_cli(
            flags + ["ledger", "check-dup", "--company", "小红书", "--title", "AI产品经理"]
        )
        self.assertFalse(dup_broad_vs_ai["duplicate"])

        # 3. 集团工商别名扩充、2字核心词+英文后缀前东家拦截、companies_match 跨主体同公司识别
        self.assertTrue(employer_shield_match("字节跳动", "北京字跳网络技术有限公司"))
        self.assertTrue(employer_shield_match("哔哩哔哩", "上海宽娱数码科技有限公司"))
        self.assertTrue(employer_shield_match("BOSS直聘", "北京华品博睿网络技术有限公司"))
        self.assertTrue(employer_shield_match("元象XVERSE", "深圳元象信息科技有限公司"))
        self.assertTrue(employer_shield_match("平安科技", "中国平安保险（集团）股份有限公司"))
        self.assertTrue(companies_match("北京三快在线科技有限公司", "美团"))
        self.assertTrue(companies_match("上海宽娱数码科技有限公司", "哔哩哔哩"))

        # 4. Word .docx 原生解析 + 含括号地域主体提取 + 叙事前缀剥离 + 压缩 PDF 部分漏提预警
        docx_path = self.root / "resume_sample.docx"
        docx_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            "<w:p><w:r><w:t>2021年3月就职于腾讯科技（深圳）有限公司</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>曾任杭州远山数字科技有限公司高级产品经理</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
        with zipfile.ZipFile(docx_path, "w") as zf:
            zf.writestr("word/document.xml", docx_xml)
        _, docx_ext = _run_cli(flags + ["config", "extract-employers", "--from-file", str(docx_path)])
        self.assertIn("腾讯科技（深圳）有限公司", docx_ext["added_employers"])
        self.assertIn("杭州远山数字科技有限公司", docx_ext["added_employers"])
        _, scr_yuanshan = _run_cli(flags + ["screen-basic", "--company", "远山数字", "--title", "AI产品经理"])
        self.assertFalse(scr_yuanshan["pass_basic"])
        _, scr_tencent = _run_cli(flags + ["screen-basic", "--company", "腾讯音乐", "--title", "AI产品经理"])
        self.assertFalse(scr_tencent["pass_basic"])

        partial_pdf = self.root / "partial_compressed.pdf"
        partial_pdf.write_bytes(
            b"%PDF-1.4\n1 0 obj << /Filter /FlateDecode >> stream\x00\x9c\xffendstream\n"
            + "深圳星辰智造科技有限公司\n".encode("utf-8")
        )
        from unittest import mock

        with mock.patch("hiremate_lib.normalize.shutil.which", return_value=None):
            _, pdf_ext = _run_cli(flags + ["config", "extract-employers", "--from-file", str(partial_pdf)])
        self.assertIn("深圳星辰智造科技有限公司", pdf_ext["added_employers"])
        self.assertIn("pdf_partial_warning", pdf_ext)

        # 5. 旧版 8 状态 SQLite 表平滑迁移至 9 状态（新增 interview_scheduled）+ 跟进消警 + 状态回退时间戳清理 + 直接 upsert handed_off 通知
        legacy_ws = self.root / "ws_legacy_db"
        legacy_ws.mkdir(parents=True, exist_ok=True)
        legacy_db = legacy_ws / "ledger.sqlite3"
        conn = sqlite3.connect(legacy_db)
        old_schema_sql = hm_ledger.SCHEMA_SQL.replace("        'interview_scheduled',\n", "")
        conn.executescript(
            old_schema_sql
            + """
            INSERT INTO jobs (job_id, company, company_clean, company_core, title, title_norm, status, handed_off_at, created_at, updated_at)
            VALUES ('job_leg_1', '深势科技', '深势科技', '深势', 'AI产品经理', 'ai产品经理', 'handed_off', '2026-04-01T10:00:00+08:00', '2026-04-01T09:00:00+08:00', '2026-04-01T10:00:00+08:00');
            """
        )
        conn.commit()
        conn.close()

        migrated_led = hm_ledger.JobLedger(legacy_ws)
        self.assertEqual(migrated_led.summary()["funnel"]["handed_off_pending"], 1)
        tr_iv = migrated_led.transition(job_id="job_leg_1", to_status="interview_scheduled", note="已约周四下午二面")
        self.assertEqual(tr_iv["to_status"], "interview_scheduled")
        sum_after_iv = migrated_led.summary()["funnel"]
        self.assertEqual(sum_after_iv["handed_off_pending"], 0)
        self.assertEqual(sum_after_iv["interview_scheduled"], 1)
        dup_iv = migrated_led.check_duplicate(company="深势科技", title="大模型产品经理")
        self.assertTrue(dup_iv["same_company_active"])

        # 直接 upsert_job(status='handed_off') 也会触发桌面通知字段，且状态误标回退会清理里程碑计数
        with mock.patch.dict(os.environ, {"HIREMATE_DISABLE_DESKTOP_NOTIFY": "1"}):
            up_ho = migrated_led.upsert_job({"company": "智谱华章", "title": "AI产品专家", "status": "handed_off"})
        self.assertIn("desktop_notification", up_ho)

        base_today = migrated_led.summary()["today_opening_sent"]
        up_open = migrated_led.upsert_job({"company": "月之暗面", "title": "AI产品经理", "status": "opening_sent"})
        open_job_id = up_open["job"]["job_id"]
        self.assertEqual(migrated_led.summary()["today_opening_sent"], base_today + 1)
        migrated_led.transition(job_id=open_job_id, to_status="discovered", note="误触修正")
        self.assertEqual(migrated_led.summary()["today_opening_sent"], base_today)

        migrated_led.transition(job_id=open_job_id, to_status="resume_delivered", resume_used="AI主简历.pdf")
        self.assertEqual(migrated_led.summary()["funnel"]["pdf_delivered_total"], 1)
        migrated_led.transition(job_id=open_job_id, to_status="resume_requested", note="仅发请求尚未同意")
        self.assertEqual(migrated_led.summary()["funnel"]["pdf_delivered_total"], 0)

        # 6. 前东家名单零上传隐私保护 + 本地 guard-reply 拦截 + copilot 模式巡检短路 + AppleScript 尾部反斜杠防截断
        from hiremate_lib import workspace as hm_ws

        cfg3 = hm_ws.load_config(ws3)
        pro_prefs = hm_client.config_to_pro_preferences(cfg3)
        self.assertEqual(pro_prefs["blacklist_companies"], [])

        _, local_guard_block = _run_cli(
            flags
            + [
                "pro",
                "guard-reply",
                "--input-json",
                json.dumps(
                    {
                        "job": {"company": "字节跳动", "title": "AI产品经理"},
                        "conversation": {"incoming_message": "在看机会吗", "draft_reply": "在看的"},
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        self.assertEqual(local_guard_block["code"], "LOCAL_EMPLOYER_SHIELD_BLOCKED")
        self.assertTrue(local_guard_block["handoff_required"])

        _run_cli(flags + ["config", "set", "--execution-mode", "copilot"])
        _, copilot_gate = _run_cli(flags + ["pro", "patrol-gate"])
        self.assertFalse(copilot_gate["allow_unread_check"])
        self.assertEqual(copilot_gate["reason"], "COPILOT_MODE_NO_BROWSER")

        escaped_tail = hm_notifier._escape_applescript("A" * 159 + "\\")
        self.assertTrue(escaped_tail.endswith("\\\\"))

        # 7. 多宿主安装器覆盖 antigravity / cursor / windsurf
        multi_canon = self.root / "multi_canon"
        agy_dir = self.root / "mock_agy"
        cur_dir = self.root / "mock_cursor"
        ws_dir = self.root / "mock_windsurf"
        for d in (agy_dir, cur_dir, ws_dir):
            d.mkdir(parents=True, exist_ok=True)
        multi_res = install_plugin.install(
            target="auto",
            canonical_home=str(multi_canon),
            search_roots={"antigravity": agy_dir, "cursor": cur_dir, "windsurf": ws_dir},
        )
        self.assertEqual({h["host"] for h in multi_res["linked_hosts"]}, {"antigravity", "cursor", "windsurf"})

    def test_audit_p0_p1_concurrency_migration_bracket_collision_xss_and_release_guards(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import importlib.util
        import sqlite3
        import subprocess
        from unittest import mock
        from hiremate_lib import client as hm_client, ledger as hm_ledger
        from hiremate_lib.normalize import (
            companies_match,
            employer_shield_match,
            extract_employers_from_text,
            title_similarity,
        )

        # 1. [P0-2] 多进程并发首次迁移旧版 8 状态 SQLite 账本：零报错、零丢表、零丢行，并生成 .pre_mig_bak 快照
        conc_ws = self.root / "ws_conc_mig"
        conc_ws.mkdir(parents=True, exist_ok=True)
        conc_db = conc_ws / "ledger.sqlite3"
        old_sql = hm_ledger.SCHEMA_SQL.replace("        'interview_scheduled',\n", "")
        c_init = sqlite3.connect(conc_db)
        c_init.executescript(old_sql)
        for idx in range(120):
            c_init.execute(
                "INSERT INTO jobs (job_id, company, company_clean, company_core, title, title_norm, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'discovered', '2026-04-01T09:00:00+08:00', '2026-04-01T09:00:00+08:00')",
                (f"job_c_{idx}", f"并发测试企业{idx}", f"并发测试企业{idx}", f"并发测试企业{idx}", "AI产品经理", "ai产品经理"),
            )
        c_init.commit()
        c_init.close()

        worker_code = (
            f"import sys; sys.path.insert(0, {str(OPEN_ROOT)!r})\n"
            "from pathlib import Path\n"
            "from hiremate_lib.ledger import JobLedger\n"
            "led = JobLedger(Path(sys.argv[1]))\n"
            "print(led.summary()['total_evaluated'])\n"
        )
        procs = [
            subprocess.Popen([sys.executable, "-B", "-c", worker_code, str(conc_ws)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(4)
        ]
        for p in procs:
            out_s, err_s = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0, msg=f"Concurrent migration failed: {err_s}")
            self.assertEqual(out_s.strip(), "120")
        self.assertTrue(conc_db.with_suffix(".sqlite3.pre_mig_bak").is_file())

        # 2. [P0-3 & P1-3] 括号地域/属性不互撞、太平洋保险不误杀太平洋汽车、短词不跨行业误杀、行政区括号不漏判
        self.assertFalse(companies_match("甲科技（江西）有限公司", "乙网络（江西）有限公司"))
        self.assertFalse(companies_match("西门子(China)", "SAP(China)"))
        self.assertFalse(employer_shield_match("乙公司（上市公司）", "甲公司（上市公司）"))
        self.assertFalse(employer_shield_match("乙公司（A轮）", "甲公司（A轮）"))
        self.assertFalse(employer_shield_match("太平洋汽车网", "中国太平洋保险（集团）股份有限公司"))
        self.assertFalse(employer_shield_match("阳光科技有限公司", "阳光保险"))
        self.assertFalse(employer_shield_match("博冠光电", "网易"))
        self.assertFalse(employer_shield_match("海思科医药", "华为"))
        self.assertFalse(employer_shield_match("阿里斯顿", "阿里巴巴"))
        self.assertFalse(employer_shield_match("字节星球", "字节跳动"))

        self.assertTrue(employer_shield_match("平安科技", "中国平安保险（集团）股份有限公司"))
        self.assertTrue(employer_shield_match("阿里云计算有限公司", "阿里巴巴"))
        self.assertTrue(employer_shield_match("深圳市海思半导体有限公司", "华为"))
        self.assertTrue(employer_shield_match("上海博冠网络科技有限公司", "网易"))
        self.assertEqual(title_similarity("产品经理", "产品经理（朝阳区）"), 1.0)

        # 3. [P1-2] 简历雇主提取：剥离时间线/第一人称粘连前缀，排除甲乙方合作描述
        resume_snippet = (
            "2019.07-2022.03就职于北京字节跳动科技有限公司；\n"
            "我在腾讯科技（深圳）有限公司担任高级工程师；\n"
            "曾在阿里巴巴（中国）有限公司任职；\n"
            "毕业后入职深圳市腾讯计算机系统有限公司；\n"
            "项目职责：负责与华为技术有限公司的合作对接及交付。"
        )
        extracted_emps = extract_employers_from_text(resume_snippet)
        self.assertIn("北京字节跳动科技有限公司", extracted_emps)
        self.assertIn("腾讯科技（深圳）有限公司", extracted_emps)
        self.assertIn("阿里巴巴（中国）有限公司", extracted_emps)
        self.assertIn("深圳市腾讯计算机系统有限公司", extracted_emps)
        self.assertFalse(any("华为" in x for x in extracted_emps))
        self.assertTrue(any(employer_shield_match("字节跳动", x) for x in extracted_emps))
        self.assertTrue(any(employer_shield_match("腾讯", x) for x in extracted_emps))
        self.assertTrue(any(employer_shield_match("阿里巴巴", x) for x in extracted_emps))

        # 4. [P0-4 & P1-7] 被动来信无历史 job_id 直接 transition 自动建档弹窗 + 已送达岗位标 screened_skip 不扣减当日配额
        ws4 = self.root / "ws_p04"
        home4 = self.root / "home_p04"
        flags4 = ["--home", str(home4), "--workspace", str(ws4), "--api-base", self.api_base]
        _run_cli(flags4 + ["init", "--account-name", "林远", "--roles", "AI产品经理"])

        rc_tr_auto, tr_auto = _run_cli(
            flags4
            + [
                "ledger",
                "transition",
                "--company",
                "新来信智能科技",
                "--title",
                "AI产品专家",
                "--status",
                "handed_off",
                "--note",
                "HR主动来信约周五下午面试",
            ]
        )
        self.assertEqual(rc_tr_auto, 0)
        self.assertTrue(tr_auto.get("auto_created"))
        self.assertEqual(tr_auto["to_status"], "handed_off")
        self.assertIn("desktop_notification", tr_auto)

        led4 = hm_ledger.JobLedger(ws4)
        sent_job = led4.upsert_job({"company": "阶跃星辰", "title": "AI产品经理", "status": "opening_sent"})
        today_before = led4.summary()["today_opening_sent"]
        self.assertGreaterEqual(today_before, 1)
        led4.transition(job_id=sent_job["job"]["job_id"], to_status="screened_skip", note="聊后发现为驻场岗")
        self.assertEqual(led4.summary()["today_opening_sent"], today_before)

        # Pro 老用户首次 auth status 展示激活菜单，后续常规 auth status 不再重复刷屏
        if HAS_PRO_SERVER and self.srv is not None:
            self.srv.store.grant_pro(email="pro_once@example.com", days=0)
            _, st_start = _run_cli(flags4 + ["auth", "login", "--no-wait"])
            self.srv.store.approve_device_auth(user_code=st_start["user_code"], email="pro_once@example.com")
            _, login_first = _run_cli(flags4 + ["auth", "login", "--wait-seconds", "5"])
            self.assertIn("pro_activation_guide", login_first)
            _, status_repeat = _run_cli(flags4 + ["auth", "status"])
            self.assertEqual(status_repeat["tier"], "pro")
            self.assertNotIn("pro_activation_guide", status_repeat)

        # 5. [P1-8] 看板 XSS 载荷转义回归（公司名/岗位/HR/备注/恶意 javascript: URL）
        xss_payload = '"><img src=x onerror=alert(1)></script>'
        led4.upsert_job(
            {
                "company": f"测试企业{xss_payload}",
                "title": f"产品经理{xss_payload}",
                "recruiter_name": f"张HR{xss_payload}",
                "note": f"备注{xss_payload}",
                "job_url": "javascript:alert(1)",
                "status": "discovered",
            }
        )
        _, dash_xss = _run_cli(flags4 + ["build-dashboard"])
        raw_html = Path(dash_xss["out_path"]).read_text(encoding="utf-8")
        self.assertNotIn("<img src=x onerror=alert(1)>", raw_html)
        self.assertNotIn("javascript:alert(1)", raw_html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", raw_html)

        # 6. [P1-6] Windows Junction 安全移除分支模拟验证
        junc_dir = self.root / "fake_junction"
        junc_dir.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(install_plugin, "_is_windows_reparse_point", return_value=True):
            with mock.patch("os.rmdir") as mock_rmdir:
                self.assertTrue(install_plugin._remove_link_or_dir(junc_dir))
                mock_rmdir.assert_called_once_with(junc_dir)

        # 7. [P1-4 & P1-8] 发布扫描器负向埋点检测 & 导出器危险目标目录拦截
        scan_spec = importlib.util.spec_from_file_location(
            "scan_before_release", OPEN_ROOT / "scripts" / "scan_before_release.py"
        )
        assert scan_spec and scan_spec.loader
        scan_mod = importlib.util.module_from_spec(scan_spec)
        scan_spec.loader.exec_module(scan_mod)

        dirty_dir = self.root / "dirty_open"
        dirty_dir.mkdir(parents=True, exist_ok=True)
        (dirty_dir / "leak_resume.pdf").write_bytes(b"%PDF-1.4 secret resume")
        (dirty_dir / "leak_tok.txt").write_text("token = hm_live_" + "f" * 32, encoding="utf-8")
        scan_bad = scan_mod.scan(dirty_dir)
        self.assertFalse(scan_bad["ok"])
        self.assertGreaterEqual(len(scan_bad["issues"]), 2)

        export_script = OPEN_ROOT.parent / "scripts" / "export_open_release.py"
        if export_script.is_file():
            exp_spec = importlib.util.spec_from_file_location("export_open_release", export_script)
            assert exp_spec and exp_spec.loader
            exp_mod = importlib.util.module_from_spec(exp_spec)
            exp_spec.loader.exec_module(exp_mod)
            with self.assertRaises(ValueError):
                exp_mod._validate_safe_out_dir(OPEN_ROOT.parent)
            with self.assertRaises(ValueError):
                exp_mod._validate_safe_out_dir(OPEN_ROOT.parent.parent)
            with self.assertRaises(ValueError):
                exp_mod._validate_safe_out_dir(Path.home())

        # 8. [P2] 异源 302 重定向自动剥离 Authorization 头防泄露
        captured_auth_headers: list[str | None] = []

        class _TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                captured_auth_headers.append(self.headers.get("Authorization"))
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                pass

        target_srv = HTTPServer(("127.0.0.1", 0), _TargetHandler)
        t_tgt = threading.Thread(target=target_srv.serve_forever, daemon=True)
        t_tgt.start()
        target_port = target_srv.server_address[1]

        class _RedirHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{target_port}/landed")
                self.end_headers()

            def log_message(self, fmt: str, *args) -> None:
                pass

        redir_srv = HTTPServer(("127.0.0.1", 0), _RedirHandler)
        t_red = threading.Thread(target=redir_srv.serve_forever, daemon=True)
        t_red.start()
        try:
            st_r, _ = hm_client._http_json(
                "GET",
                f"http://127.0.0.1:{redir_srv.server_address[1]}/start",
                token="hm_live_secret_should_not_leak",
            )
            self.assertEqual(st_r, 200)
            self.assertEqual(captured_auth_headers, [None])
        finally:
            redir_srv.shutdown()
            redir_srv.server_close()
            target_srv.shutdown()
            target_srv.server_close()

    def test_dual_source_update_check_self_update_rollback_and_tarball_fallback(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import hashlib
        from io import BytesIO
        import shutil
        import tarfile

        from hiremate_lib import updater

        # 0. 准备本地用户工作区数据与初始多宿主软链安装，验证升级全程零损用户数据
        canon_home = self.root / "upd_home"
        ws_dir = self.root / "upd_ws"
        mock_wb = self.root / "upd_wb"
        mock_dsh = self.root / "upd_dsh"
        mock_wb.mkdir()
        mock_dsh.mkdir()
        search_roots = {"workbuddy": mock_wb, "dsh": mock_dsh}

        inst_init = install_plugin.install(
            target="auto",
            canonical_home=str(canon_home),
            search_roots=search_roots,
        )
        self.assertTrue(inst_init["ok"])

        flags = ["--home", str(canon_home), "--workspace", str(ws_dir), "--api-base", self.api_base]
        rc, _ = _run_cli(flags + ["init", "--account-name", "陈默", "--employers", "旧城网络", "--roles", "后端开发"])
        self.assertEqual(rc, 0)
        rc, job_res = _run_cli(
            flags
            + [
                "ledger",
                "upsert-job",
                "--job-json",
                json.dumps({"company": "星火云创", "title": "高级Go工程师", "status": "connected"}, ensure_ascii=False),
            ]
        )
        self.assertEqual(rc, 0)
        saved_job_id = job_res["job"]["job_id"]

        # 1. 启动本地双源 Mock HTTP 服务：模拟 Gitee 主源 502 故障 -> 自动毫秒级切换 GitHub 备源
        manifest_state: dict[str, object] = {
            "latest_version": "9.8.0",
            "min_supported_version": "0.5.0",
            "published_at": "2026-11-18",
            "highlights": [
                "新增双源自动容灾更新 <script>alert(1)</script>",
                "优化 BOSS 详情面板深筛速度",
            ],
            "tarball_bytes": b"",
            "tarball_sha256": "",
        }
        hit_paths: list[str] = []

        class _ReleaseMockHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                hit_paths.append(self.path)
                if self.path == "/gitee/release.json":
                    self.send_response(502)
                    self.end_headers()
                    self.wfile.write(b"Bad Gateway")
                    return
                if self.path == "/github/release.json":
                    payload = {
                        "schema_version": 1,
                        "latest_version": manifest_state["latest_version"],
                        "min_supported_version": manifest_state["min_supported_version"],
                        "published_at": manifest_state["published_at"],
                        "highlights": manifest_state["highlights"],
                    }
                    if manifest_state["tarball_sha256"]:
                        payload["tarball_sha256"] = manifest_state["tarball_sha256"]
                    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.path == "/github/main.tar.gz":
                    data = bytes(manifest_state["tarball_bytes"])  # type: ignore[arg-type]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, fmt: str, *args) -> None:
                pass

        rel_srv = HTTPServer(("127.0.0.1", 0), _ReleaseMockHandler)
        rel_thread = threading.Thread(target=rel_srv.serve_forever, daemon=True)
        rel_thread.start()
        base_http = f"http://127.0.0.1:{rel_srv.server_address[1]}"

        def _prepare_mock_release_tree(dest_dir: Path, ver: str, *, break_cli: bool = False) -> Path:
            shutil.copytree(
                OPEN_ROOT,
                dest_dir,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "dist", ".workspace"),
            )
            init_py = dest_dir / "hiremate_lib" / "__init__.py"
            init_py.write_text(f'"""mock release"""\n\n__version__ = "{ver}"\n', encoding="utf-8")
            pkg_json = dest_dir / "dsh" / "package.json"
            pkg_obj = json.loads(pkg_json.read_text(encoding="utf-8"))
            pkg_obj["version"] = ver
            pkg_json.write_text(json.dumps(pkg_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            rel_json = dest_dir / "release.json"
            rel_obj = json.loads(rel_json.read_text(encoding="utf-8"))
            rel_obj["latest_version"] = ver
            rel_obj["highlights"] = [f"升级至 v{ver} 核心特性"]
            rel_json.write_text(json.dumps(rel_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if break_cli:
                (dest_dir / "hiremate.py").write_text("import sys\nsys.exit(99)\n", encoding="utf-8")
            return dest_dir

        def _init_git_repo_from_dir(repo_dir: Path) -> None:
            subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "chore(release): mock release"], cwd=repo_dir, check=True, capture_output=True)

        try:
            os.environ["HIREMATE_DISABLE_UPDATE_CHECK"] = "0"
            os.environ["HIREMATE_RELEASE_MANIFEST_URLS"] = f"{base_http}/gitee/release.json,{base_http}/github/release.json"

            # 验证被动感知：主源 502 后自动切备源拿到 v9.8.0，并在 config show 与 build-dashboard 双通道输出提醒
            rc_cfg, cfg_out = _run_cli(flags + ["config", "show"])
            self.assertEqual(rc_cfg, 0)
            self.assertIn("/gitee/release.json", hit_paths)
            self.assertIn("/github/release.json", hit_paths)
            self.assertTrue(cfg_out["update_notice"]["update_available"])
            self.assertEqual(cfg_out["update_notice"]["latest_version"], "9.8.0")
            self.assertIn("升级求职助手", cfg_out["update_notice"]["agent_prompt_hint"])

            # 验证 6 小时本地冷却缓存生效：再次调用不再重复发起 HTTP 请求
            hits_before = len(hit_paths)
            rc_dash, dash_out = _run_cli(flags + ["build-dashboard"])
            self.assertEqual(rc_dash, 0)
            self.assertEqual(len(hit_paths), hits_before)
            self.assertEqual(dash_out["update_notice"]["latest_version"], "9.8.0")
            dash_html = Path(dash_out["out_path"]).read_text(encoding="utf-8")
            self.assertIn('id="release-update-banner"', dash_html)
            self.assertIn("求职助手 v9.8.0 已发布", dash_html)
            self.assertIn("请帮我升级求职助手到最新版本", dash_html)
            # 验证 highlights 中的恶意标签被完整转义，杜绝 XSS
            self.assertNotIn("<script>alert(1)</script>", dash_html)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", dash_html)

            # 验证 CLI `update --check` 实时刷新检查
            rc_chk, chk_out = _run_cli(flags + ["update", "--check"])
            self.assertEqual(rc_chk, 0)
            self.assertTrue(chk_out["check_only"])
            self.assertTrue(chk_out["update_available"])
            self.assertEqual(chk_out["latest_version"], "9.8.0")

            # 2. 验证损坏更新包安装失败时 100% 自动无损回滚原主副本
            broken_repo = _prepare_mock_release_tree(self.root / "broken_v980", "9.8.0", break_cli=True)
            _init_git_repo_from_dir(broken_repo)
            os.environ["HIREMATE_UPDATE_GIT_URLS"] = f"http://127.0.0.1:9/unreachable.git,{broken_repo.as_uri()}"

            bad_res = updater.perform_self_update(
                home=canon_home,
                workspace=ws_dir,
                search_roots=search_roots,
            )
            self.assertFalse(bad_res["ok"])
            self.assertEqual(bad_res["code"], "UPDATE_INSTALL_FAILED")
            self.assertTrue(bad_res["rolled_back"])
            # 确认回滚后原安装目录 hiremate.py 与 dsh-plugin/skills/hiremate 软链依然完好可用
            ver_after_rb = subprocess.run(
                [sys.executable, str(canon_home / "skill" / "hiremate" / "hiremate.py"), "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn(updater.__version__, ver_after_rb.stdout)
            self.assertTrue((canon_home / "dsh-plugin" / "skills" / "hiremate" / "SKILL.md").is_file())

            # 验证源地址安全守卫：拦截参数注入、非可信域名与生产模式下的明文 HTTP / file://
            for unsafe_url in (
                "--upload-pack=evil",
                "http://example.com/release.json",
                "file:///etc/passwd",
            ):
                with self.assertRaises(ValueError):
                    updater._validate_source_url(unsafe_url, allow_test_local=False)
            with self.assertRaises(ValueError):
                updater._validate_source_url(
                    "https://evil-mirror.example.com/main.tar.gz",
                    allow_test_local=False,
                    restrict_trusted_hosts=True,
                )

            # 3. 验证正常 Git 双源容灾升级（主源不可达 -> 自动切备源 v9.8.0）+ 全宿主软链同步 + 本地账本零丢失
            good_repo = _prepare_mock_release_tree(self.root / "good_v980", "9.8.0", break_cli=False)
            _init_git_repo_from_dir(good_repo)
            os.environ["HIREMATE_UPDATE_GIT_URLS"] = f"http://127.0.0.1:9/dead-gitee.git,{good_repo.as_uri()}"

            ok_res = updater.perform_self_update(
                home=canon_home,
                workspace=ws_dir,
                search_roots=search_roots,
            )
            self.assertTrue(ok_res["ok"], ok_res)
            self.assertTrue(ok_res["updated"])
            self.assertEqual(ok_res["current_version"], "9.8.0")
            self.assertTrue(ok_res["dashboard_refreshed"])

            # 主副本及 WorkBuddy / DSH 软链均已同步为 9.8.0
            for entry_py in (
                canon_home / "skill" / "hiremate" / "hiremate.py",
                mock_wb / "skills" / "hiremate" / "hiremate.py",
                mock_dsh / "skills" / "hiremate" / "hiremate.py",
            ):
                ver_proc = subprocess.run([sys.executable, str(entry_py), "--version"], capture_output=True, text=True, check=True)
                self.assertIn("9.8.0", ver_proc.stdout)

            # 升级后刷新的看板已升至 v9.8.0 且不再残留旧版升级横幅
            refreshed_html = (ws_dir / "dashboard.html").read_text(encoding="utf-8")
            self.assertIn("v9.8.0", refreshed_html)
            self.assertNotIn('id="release-update-banner"', refreshed_html)

            # 验证用户的本地求职账本记录与配置 100% 原样保留
            rc_ls, ls_out = _run_cli(flags + ["ledger", "list"])
            self.assertEqual(rc_ls, 0)
            self.assertEqual(ls_out["count"], 1)
            self.assertEqual(ls_out["items"][0]["job_id"], saved_job_id)
            self.assertEqual(ls_out["items"][0]["company"], "星火云创")

            # 4. 验证 Tar-Slip 路径穿越攻击拦截 + 无 Git 环境下的 HTTPS Tarball (含 SHA-256 校验) 兜底升级
            evil_buf = BytesIO()
            with tarfile.open(fileobj=evil_buf, mode="w:gz") as tf:
                info = tarfile.TarInfo(name="../../escaped_evil.txt")
                payload_b = b"pwned"
                info.size = len(payload_b)
                tf.addfile(info, BytesIO(payload_b))
            with self.assertRaises(RuntimeError):
                updater._safe_extract_tarball(evil_buf.getvalue(), self.root / "evil_extract")

            tar_src = _prepare_mock_release_tree(self.root / "tar_v990", "9.9.0", break_cli=False)
            good_tar_buf = BytesIO()
            with tarfile.open(fileobj=good_tar_buf, mode="w:gz") as tf:
                tf.add(tar_src, arcname="hiremate-main")
            tar_bytes = good_tar_buf.getvalue()
            manifest_state["latest_version"] = "9.9.0"
            manifest_state["tarball_bytes"] = tar_bytes
            manifest_state["tarball_sha256"] = hashlib.sha256(tar_bytes).hexdigest()

            os.environ["HIREMATE_FORCE_TARBALL_UPDATE"] = "1"
            os.environ["HIREMATE_UPDATE_TARBALL_URL"] = f"{base_http}/github/main.tar.gz"

            tar_res = updater.perform_self_update(
                home=canon_home,
                workspace=ws_dir,
                search_roots=search_roots,
            )
            self.assertTrue(tar_res["ok"], tar_res)
            self.assertEqual(tar_res["current_version"], "9.9.0")
            self.assertEqual(tar_res["source_used"], "tarball:github")
            ver_tar = subprocess.run(
                [sys.executable, str(canon_home / "skill" / "hiremate" / "hiremate.py"), "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("9.9.0", ver_tar.stdout)
        finally:
            rel_srv.shutdown()
            rel_srv.server_close()

    def test_device_login_sandbox_open_failure_and_resume_reopen_browser(self) -> None:
        """回归验证：
        1. macOS 沙盒（如 Codex Seatbelt）下 `open` 返回非 0 退出码时，`open_browser_url` 必须如实返回 False（严禁 Popen 假阳性）；
        2. 首次 `auth login` 弹窗失败时绝不死等 45 秒，立即（<1.5s）返回 `browser_opened: False` 与 `agent_action_required`（防无链接问题框）；
        3. 二次 `auth login`（`resumed=True`）若首次 poll 发现仍为 `authorization_pending`，必须自动补弹确认页；
        4. 二次 `auth login`（`resumed=True`）若用户已在网页点过确认授权，0 延迟直接返回 `authorized`，绝不重复弹出多余浏览器标签页。
        """
        from datetime import datetime, timedelta, timezone
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import time
        from unittest import mock
        from hiremate_lib import client

        # 1. 验证 darwin 下 open 退出码非 0 时 open_browser_url 返回 False，退出码 0 时返回 True
        prev_no_b = os.environ.pop("HIREMATE_NO_BROWSER", None)
        try:
            with mock.patch.object(client.sys, "platform", "darwin"):
                with mock.patch.object(
                    client.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(["open", "https://example.com"], 1),
                ) as m_fail:
                    self.assertFalse(client.open_browser_url("https://example.com/activate?code=HM-XGFX"))
                    m_fail.assert_called_once()
                with mock.patch.object(
                    client.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(["open", "https://example.com"], 0),
                ) as m_ok:
                    self.assertTrue(client.open_browser_url("https://example.com/activate?code=HM-XGFX"))
                    m_ok.assert_called_once()
        finally:
            if prev_no_b is not None:
                os.environ["HIREMATE_NO_BROWSER"] = prev_no_b

        # 搭建轻量本地配对模拟服务（不依赖闭源 pro-server，确保开源独立包亦可完整回归）
        pair_state = {"approved": False}
        exp_iso = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()

        class _MockDeviceHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length:
                    self.rfile.read(length)
                base_u = f"http://127.0.0.1:{self.server.server_address[1]}"
                if self.path == "/v1/auth/device/start":
                    payload = {
                        "ok": True,
                        "device_code": "dcode_sandbox_test",
                        "user_code": "HM-XGFX",
                        "verification_uri": f"{base_u}/activate",
                        "verification_uri_complete": f"{base_u}/activate?code=HM-XGFX",
                        "expires_at": exp_iso,
                        "interval": 1,
                    }
                    raw = json.dumps(payload).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.path == "/v1/auth/device/poll":
                    if pair_state["approved"]:
                        payload = {
                            "ok": True,
                            "status": "authorized",
                            "email": "second_device@example.com",
                            "token": "tok_second_device_ok",
                            "tier": "pro",
                            "plan": "pro_buyout",
                            "active": True,
                            "lifetime": True,
                        }
                        code = 200
                    else:
                        payload = {"ok": False, "code": "AUTHORIZATION_PENDING", "message": "等待浏览器确认"}
                        code = 428
                    raw = json.dumps(payload).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_response(404)
                self.end_headers()

        mock_srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockDeviceHandler)
        mock_thread = threading.Thread(target=mock_srv.serve_forever, daemon=True)
        mock_thread.start()
        mock_base = f"http://127.0.0.1:{mock_srv.server_address[1]}"
        h_dir = self.root / "sandbox_home"
        w_dir = self.root / "sandbox_ws"
        flags = ["--home", str(h_dir), "--workspace", str(w_dir), "--api-base", mock_base]
        try:
            # 2. 首次运行 auth login（默认 wait_seconds=45），模拟沙盒拦截导致 open_browser_url 返回 False：
            #    必须秒级返回（严禁阻塞 45 秒），且返回 agent_action_required 严禁弹无链接问题框
            with mock.patch.object(client, "open_browser_url", return_value=False) as m_open1:
                t0 = time.monotonic()
                rc1, res1 = _run_cli(flags + ["auth", "login", "--wait-seconds", "45"])
                elapsed1 = time.monotonic() - t0
                self.assertEqual(rc1, 0)
                self.assertLess(elapsed1, 2.0, "浏览器未成功弹出时严禁空等 45 秒")
                self.assertEqual(res1["status"], "authorization_pending")
                self.assertFalse(res1["browser_opened"])
                self.assertFalse(res1["resumed"])
                self.assertEqual(res1["user_code"], "HM-XGFX")
                self.assertIn("agent_action_required", res1)
                self.assertIn("request_user_input", res1["agent_action_required"])
                m_open1.assert_called_once_with(f"{mock_base}/activate?code=HM-XGFX")

            # 3. 二次运行 auth login（resumed=True），用户尚未授权且宿主已放开权限（open_browser_url 返回 True）：
            #    首次 poll 探查未授权后，必须立即补弹确认授权页面！
            with mock.patch.object(client, "open_browser_url", return_value=True) as m_open2:
                rc2, res2 = _run_cli(flags + ["auth", "login", "--wait-seconds", "0"])
                self.assertEqual(rc2, 0)
                self.assertEqual(res2["status"], "authorization_pending")
                self.assertTrue(res2["resumed"])
                self.assertTrue(res2["browser_opened"])
                self.assertNotIn("agent_action_required", res2)
                m_open2.assert_called_once_with(f"{mock_base}/activate?code=HM-XGFX")

            # 4. 用户在刚刚弹出的确认页点完「确认授权此设备」后，三次运行 auth login（resumed=True）：
            #    首轮 0 延迟 poll 直接命中 authorized，绝不再重复弹出多余标签页！
            pair_state["approved"] = True
            with mock.patch.object(client, "open_browser_url", return_value=True) as m_open3:
                rc3, res3 = _run_cli(flags + ["auth", "login", "--wait-seconds", "5"])
                self.assertEqual(rc3, 0)
                self.assertEqual(res3["status"], "authorized")
                self.assertEqual(res3["tier"], "pro")
                m_open3.assert_not_called()
        finally:
            mock_srv.shutdown()
            mock_srv.server_close()

    def test_inbound_screen_basic_unfetched_jd_warning_and_field_research_exemption(self) -> None:
        _run_cli(
            self._base_flags()
            + [
                "init",
                "--roles",
                "AI产品经理,产品经理",
                "--cities",
                "广州,深圳",
                "--min-salary-k",
                "20",
                "--resume-source",
                "attachment_pdf",
            ]
        )
        # 1. 被动来信传了绑定职位标题但未点开抓 JD：返回 ui_jd_unfetched=True 提醒先点开「查看职位」
        rc1, unfetched = _run_cli(
            self._base_flags()
            + [
                "screen-basic",
                "--company",
                "ToDesk",
                "--title",
                "产品经理",
                "--source",
                "inbound",
            ]
        )
        self.assertEqual(rc1, 0)
        self.assertTrue(unfetched["pass_basic"])
        self.assertTrue(unfetched["ui_jd_unfetched"])
        self.assertTrue(any("查看职位" in w for w in unfetched["warnings"]))

        # 2. “现场调研 / 深入业务现场”正常业务词不误杀为驻场岗
        rc2, field_ok = _run_cli(
            self._base_flags()
            + [
                "screen-basic",
                "--company",
                "广汽能源",
                "--title",
                "AI 产品经理（产品定义方向）",
                "--jd",
                "深入业务现场开展现场调研，负责产品规划与项目落地",
                "--source",
                "inbound",
            ]
        )
        self.assertEqual(rc2, 0)
        self.assertTrue(field_ok["pass_basic"])
        self.assertFalse(field_ok["is_onsite"])
        self.assertFalse(field_ok["ui_jd_unfetched"])

    def test_screen_basic_four_source_location_conflict_and_jd_truncation(self) -> None:
        flags = ["--workspace", str(self.ws), "--home", str(self.home)]
        _run_cli(
            flags
            + [
                "init",
                "--roles",
                "AI产品经理,产品经理",
                "--cities",
                "深圳",
                "--min-salary-k",
                "20",
            ]
        )

        # 1. 真实翻车案例：鸣鸣很忙集团——地点标签和工作地址都写深圳，但标题写了 base 长沙 -> 必须判定地点矛盾并拦截
        rc1, out_conflict = _run_cli(
            flags
            + [
                "screen-basic",
                "--company",
                "鸣鸣很忙集团",
                "--title",
                "AI产品经理（base长沙）",
                "--salary",
                "25-35K",
                "--city",
                "深圳·南山区",
                "--work-address",
                "深圳市南山区科技园",
                "--jd",
                "负责零售AI产品规划与落地。",
            ]
        )
        self.assertEqual(rc1, 0)
        self.assertFalse(out_conflict["pass_basic"])
        self.assertTrue(out_conflict["location_mismatch"])
        self.assertTrue(out_conflict["location_conflict"])
        self.assertTrue(any("长沙" in r and "矛盾" in r for r in out_conflict["blocked_reasons"]))

        # 2. JD 正文显式写明工作地/常驻在非优先城市 -> 拦截
        rc2, out_jd_loc = _run_cli(
            flags
            + [
                "screen-basic",
                "--company",
                "某零售科技公司",
                "--title",
                "高级产品经理",
                "--salary",
                "25-35K",
                "--city",
                "深圳",
                "--jd",
                "负责核心中台建设。\n工作地点：长沙市岳麓区\n要求5年以上经验。",
            ]
        )
        self.assertEqual(rc2, 0)
        self.assertFalse(out_jd_loc["pass_basic"])
        self.assertTrue(out_jd_loc["location_mismatch"])
        self.assertTrue(out_jd_loc["location_conflict"])

        # 3. 多城可选（含优先城市）与正常业务区域/偶尔出差描述 -> 不误杀
        rc3, out_multi_ok = _run_cli(
            flags
            + [
                "screen-basic",
                "--company",
                "某自研科技公司",
                "--title",
                "AI产品经理（base 深圳/长沙）",
                "--salary",
                "25-35K",
                "--city",
                "深圳",
                "--jd",
                "负责北京、上海大客户数字化解决方案，需偶尔出差长沙调研客户需求。",
            ]
        )
        self.assertEqual(rc3, 0)
        self.assertTrue(out_multi_ok["pass_basic"])
        self.assertFalse(out_multi_ok["location_mismatch"])
        self.assertFalse(out_multi_ok["jd_truncated_suspected"])

        # 4. 长 JD 末尾疑似折叠收起（展开全部 / 省略号截断）-> 给出 jd_truncated_suspected 预警
        rc4, out_trunc = _run_cli(
            flags
            + [
                "screen-basic",
                "--company",
                "某自研科技公司",
                "--title",
                "AI产品经理",
                "--salary",
                "25-35K",
                "--city",
                "深圳",
                "--jd",
                "1. 负责大模型产品架构设计与核心指标优化；2. 协同算法团队推进落...展开全部",
            ]
        )
        self.assertEqual(rc4, 0)
        self.assertTrue(out_trunc["jd_truncated_suspected"])
        self.assertTrue(any("折叠" in w or "展开" in w for w in out_trunc["warnings"]))

        # 5. 省级优先城市（广东省）放行广州、拦截长沙
        _run_cli(flags + ["config", "set", "--cities", "广东省"])
        _, out_gd_ok = _run_cli(
            flags
            + [
                "screen-basic",
                "--company",
                "某广州科技公司",
                "--title",
                "产品经理（base广州）",
                "--salary",
                "25-35K",
                "--city",
                "广州",
            ]
        )
        self.assertTrue(out_gd_ok["pass_basic"])
        _, out_gd_block = _run_cli(
            flags
            + [
                "screen-basic",
                "--company",
                "某湖南科技公司",
                "--title",
                "产品经理（base长沙）",
                "--salary",
                "25-35K",
                "--city",
                "深圳",
            ]
        )
        self.assertFalse(out_gd_block["pass_basic"])

    def test_interview_tracking_migration_rounds_dashboard_and_export(self) -> None:
        """端到端验证：旧 9 状态库平滑迁移 -> 约面同步 -> 多轮面试/面经 -> Offer -> 看板面试Tab与日程 -> CSV/XLSX导出。"""
        import sqlite3
        from hiremate_lib import ledger as hm_ledger

        ws = self.root / "ws_interviews"
        home = self.root / "home_interviews"
        ws.mkdir(parents=True, exist_ok=True)
        flags = ["--workspace", str(ws), "--home", str(home)]

        # 1. 构造旧版 9 状态表（无 interviewing/offer/offer_accepted 及 interview_scheduled_at/offer_at）并验证平滑迁移
        legacy_db = ws / "ledger.sqlite3"
        conn = sqlite3.connect(legacy_db)
        old_9_sql = (
            hm_ledger.SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS interview_rounds")[0]
            .replace("        'interviewing',\n", "")
            .replace("        'offer',\n", "")
            .replace("        'offer_accepted',\n", "")
            .replace("    interview_scheduled_at TEXT,\n", "")
            .replace("    offer_at TEXT,\n", "")
        )
        conn.executescript(
            old_9_sql
            + """
            INSERT INTO jobs (job_id, company, company_clean, company_core, title, title_norm, status, handed_off_at, created_at, updated_at)
            VALUES ('job_iv_1', '星尘智能', '星尘智能', '星尘智能', 'AI Agent 产品经理', 'ai agent 产品经理', 'handed_off', '2026-11-18T10:00:00+08:00', '2026-11-18T09:00:00+08:00', '2026-11-18T10:00:00+08:00');
            """
        )
        conn.commit()
        conn.close()

        # 2. 通过 CLI 一条命令完成：约面状态流转 + 记录一面日程
        rc, tr_res = _run_cli(
            flags
            + [
                "ledger",
                "transition",
                "--job-id",
                "job_iv_1",
                "--status",
                "interview_scheduled",
                "--note",
                "BOSS 聊天已敲定周四下午业务一面",
                "--round",
                "一面",
                "--interview-time",
                "2026-11-20 14:00",
                "--interview-mode",
                "腾讯会议",
                "--interviewer",
                "AI 业务负责人",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(tr_res["to_status"], "interview_scheduled")
        self.assertIn("interview_round", tr_res)
        r1_id = tr_res["interview_round"]["round_id"]
        self.assertEqual(tr_res["interview_round"]["outcome"], "pending")

        # 3. 更新一面结果与面经复盘，再新增二面（验证自动推进到 interviewing）
        rc_upd, upd_res = _run_cli(
            flags
            + [
                "ledger",
                "interview-update",
                "--round-id",
                r1_id,
                "--outcome",
                "passed",
                "--summary",
                "重点聊了 RAG 评测集和 Agent 容错，反馈很不错",
            ]
        )
        self.assertEqual(rc_upd, 0)
        self.assertEqual(upd_res["interview_round"]["outcome"], "passed")

        rc_add, add_res = _run_cli(
            flags
            + [
                "ledger",
                "interview-add",
                "--job-id",
                "job_iv_1",
                "--round",
                "二面",
                "--time",
                "2026-11-22 15:30",
                "--mode",
                "现场面试 · 南山科技园",
                "--interviewer",
                "产研 VP",
                "--summary",
                "准备商业化 ROI 与跨部门推进案例",
            ]
        )
        self.assertEqual(rc_add, 0)
        self.assertEqual(add_res["job"]["status"], "interviewing")
        self.assertEqual(add_res["status_transition"]["to_status"], "interviewing")

        # 4. 另建一个 Offer 岗位并核验漏斗汇总与同公司防撞车保护
        rc_off, off_res = _run_cli(
            flags
            + [
                "ledger",
                "transition",
                "--company",
                "青禾数科",
                "--title",
                "AI 平台产品专家",
                "--status",
                "offer",
                "--note",
                "口头 Offer 42K*15",
                "--round",
                "终面",
                "--interview-time",
                "2026-11-17 16:00",
                "--interview-outcome",
                "passed",
                "--interview-summary",
                "CTO 终面通过",
            ]
        )
        self.assertEqual(rc_off, 0)
        self.assertEqual(off_res["to_status"], "offer")

        rc_sum, sum_res = _run_cli(flags + ["ledger", "summary"])
        self.assertEqual(rc_sum, 0)
        funnel = sum_res["summary"]["funnel"]
        self.assertEqual(funnel["interview_total"], 2)
        self.assertEqual(funnel["interview_active_total"], 1)
        self.assertEqual(funnel["upcoming_interviews_count"], 1)
        self.assertEqual(funnel["offer_count"], 1)

        rc_dup, dup_res = _run_cli(
            flags + ["ledger", "check-dup", "--company", "青禾数科", "--title", "大模型产品经理"]
        )
        self.assertEqual(rc_dup, 0)
        self.assertTrue(dup_res["duplicate"])
        self.assertTrue(dup_res["same_company_active"])

        # 5. 生成看板并验证：7 格漏斗「面试与 Offer」、独立 tab-interviews、待面日程与多轮时间线
        dash_out = ws / "dashboard.html"
        rc_d, _ = _run_cli(flags + ["build-dashboard", "--out", str(dash_out)])
        self.assertEqual(rc_d, 0)
        html_text = dash_out.read_text(encoding="utf-8")
        self.assertIn("面试与 Offer", html_text)
        self.assertIn('id="tab-interviews"', html_text)
        self.assertIn("即将到来的面试日程（1）", html_text)
        self.assertIn("2026-11-22 15:30", html_text)
        self.assertIn("重点聊了 RAG 评测集和 Agent 容错", html_text)
        self.assertIn("Offer 与录用跟进（1）", html_text)
        self.assertIn("青禾数科", html_text)
        # 验证卡片视图 / 可交互漏斗图切换及点击联动结构
        self.assertIn('id="kpi-cards-panel"', html_text)
        self.assertIn('id="funnel-viz-panel"', html_text)
        self.assertIn('data-funnel-view="cards"', html_text)
        self.assertIn('data-funnel-view="funnel"', html_text)
        self.assertIn('id="fv-inspector"', html_text)
        self.assertIn('data-jump-filter="screened_skip"', html_text)

        # 6. 验证 CSV 导出包含「面试轮次记录」列
        csv_out = ws / "export.csv"
        rc_csv, _ = _run_cli(flags + ["ledger", "export-csv", "--out", str(csv_out)])
        self.assertEqual(rc_csv, 0)
        csv_text = csv_out.read_text(encoding="utf-8-sig")
        self.assertIn("面试轮次记录", csv_text)
        self.assertIn("一面(已通过", csv_text)
        self.assertIn("二面(待面试", csv_text)

    def test_favorite_only_discover_mode_onboarding_ledger_and_dashboard(self) -> None:
        """端到端验证：前期建档可选 favorite（仅自动收藏不投递）、旧12状态库平滑迁移、查重边界、看板队列与 Pro 巡检门控。"""
        import sqlite3
        from hiremate_lib import ledger as hm_ledger

        ws = self.root / "ws_favorite"
        home = self.root / "home_favorite"
        ws.mkdir(parents=True, exist_ok=True)
        flags = ["--workspace", str(ws), "--home", str(home)]

        # 1. 新用户 Checklist 交互题包含第 3 组「找岗动作模式」（默认 semi 推荐 + favorite 可选）
        rc_chk, chk_res = _run_cli(flags + ["checklist", "--builtin-headed-persistent", "true"])
        self.assertEqual(rc_chk, 0)
        iq = chk_res["interactive_questions"]
        self.assertEqual(len(iq["question_batches"]), 3)
        self.assertEqual(len(iq["questions"]), 8)
        mode_q = iq["questions"][-1]
        self.assertEqual(mode_q["id"], "discover_action_mode")
        self.assertEqual(mode_q["options"][0]["value"], "semi")
        self.assertIn("(Recommended)", mode_q["options"][0]["label"])
        self.assertEqual(mode_q["options"][1]["value"], "favorite")

        # 2. 构造无 favorited / favorited_at 的旧 12 状态 SQLite 库，验证自动无损迁移
        legacy_db = ws / "ledger.sqlite3"
        conn = sqlite3.connect(legacy_db)
        old_12_sql = (
            hm_ledger.SCHEMA_SQL.replace("        'favorited',\n", "")
            .replace("    favorited_at TEXT,\n", "")
        )
        conn.executescript(
            old_12_sql
            + """
            INSERT INTO jobs (job_id, company, company_clean, company_core, title, title_norm, status, created_at, updated_at)
            VALUES ('job_old_disc', '云起智算', '云起智算', '云起智算', 'AI 产品经理', 'ai 产品经理', 'discovered', '2026-11-19T10:00:00+08:00', '2026-11-19T10:00:00+08:00');
            """
        )
        conn.commit()
        conn.close()

        # 3. 免费版 init 直接指定 --mode-discover favorite 成功生效（无需 Pro 或 --ack-auto-risk）
        rc_init, init_res = _run_cli(
            flags
            + [
                "init",
                "--account-name",
                "林远",
                "--roles",
                "AI产品经理,Agent产品经理",
                "--cities",
                "深圳,杭州",
                "--min-salary-k",
                "28",
                "--employers",
                "前东家智联",
                "--resume-source",
                "attachment_pdf",
                "--mode-discover",
                "favorite",
            ]
        )
        self.assertEqual(rc_init, 0)
        self.assertEqual(init_res["config"]["mode_discover"], "favorite")
        self.assertIn("仅自动收藏", init_res["strategy_confirmation"]["markdown"])
        self.assertEqual(
            init_res["post_onboarding_guide"]["recommended_action"],
            "run_free_first_round_boss_favorite",
        )
        Path(init_res["profile_facts_path"]).write_text("# 个人求职事实与答法档案\n- 5年AI产品经验\n", encoding="utf-8")

        # 4. 写入 favorited 岗位：验证不虚增开场送达数、同公司相似岗查重拦截、同公司不同岗不误拦、网页已收藏标记拦截
        rc_up, up_fav = _run_cli(
            flags
            + [
                "ledger",
                "upsert-job",
                "--job-json",
                json.dumps(
                    {
                        "company": "星辰大模型科技",
                        "title": "高级 AI 产品经理",
                        "salary": "32-45K",
                        "city": "深圳·南山区",
                        "recruiter_name": "周总监",
                        "recruiter_title": "AI产品负责人",
                        "match_reasons": "RAG 与 Agent 评测经验高度契合",
                        "icebreaker": "您好，看咱们在招自研 AI 产品，我近两年主导过企业级 Agent 落地，方便聊聊吗？",
                        "status": "favorited",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        self.assertEqual(rc_up, 0)
        fav_job_id = up_fav["job"]["job_id"]
        self.assertTrue(bool(up_fav["job"]["favorited_at"]))
        self.assertIsNone(up_fav["job"]["opening_sent_at"])

        _, sum1 = _run_cli(flags + ["ledger", "summary"])
        self.assertEqual(sum1["summary"]["funnel"]["favorited_pending"], 1)
        self.assertEqual(sum1["summary"]["funnel"]["favorited_total"], 1)
        self.assertEqual(sum1["summary"]["funnel"]["opening_delivered_total"], 0)
        self.assertEqual(sum1["summary"]["today_favorited"], 1)
        self.assertEqual(sum1["summary"]["today_opening_sent"], 0)

        # 同公司相似岗位 -> 查重拦截（防止重复点收藏变取消收藏）
        _, dup_sim = _run_cli(flags + ["ledger", "check-dup", "--company", "星辰大模型科技", "--title", "AI产品经理"])
        self.assertTrue(dup_sim["duplicate"])
        self.assertTrue(dup_sim["same_company_favorited"])
        self.assertFalse(dup_sim["same_company_active"])

        # 同公司完全不同方向岗位 -> 允许独立评估（因为 favorited 未发起聊天，不触发连坐封杀）
        _, dup_diff = _run_cli(flags + ["ledger", "check-dup", "--company", "星辰大模型科技", "--title", "iOS客户端研发工程师"])
        self.assertFalse(dup_diff["duplicate"])

        # 网页按钮已显示「取消感兴趣」-> --already-favorited 拦截
        _, dup_ui = _run_cli(
            flags + ["ledger", "check-dup", "--company", "另一家自研公司", "--title", "AI产品经理", "--already-favorited"]
        )
        self.assertTrue(dup_ui["duplicate"])
        self.assertTrue(dup_ui["already_favorited"])

        # 5. 构建看板验证：免费版徽标、待收藏审阅队列、筛选胶囊与参考破冰话术高亮
        dash_file = ws / "dashboard.html"
        rc_dash, _ = _run_cli(flags + ["build-dashboard", "--out", str(dash_file)])
        self.assertEqual(rc_dash, 0)
        dash_html = dash_file.read_text(encoding="utf-8")
        self.assertIn("当前可用：单轮自动收藏（不主动投递）", dash_html)
        self.assertIn("已自动收藏 · 待本人审阅投递（1）", dash_html)
        self.assertIn('data-filter="favorited"', dash_html)
        self.assertIn("已收藏·待本人投递", dash_html)
        self.assertIn("仅自动收藏·不主动投递（单轮收藏", dash_html)

        # 6. 用户看完觉得合适后发起沟通：favorited -> opening_sent，保留历史 favorited_total 计数且当日配额不重复双算
        rc_tr, _ = _run_cli(flags + ["ledger", "transition", "--job-id", fav_job_id, "--status", "opening_sent"])
        self.assertEqual(rc_tr, 0)
        _, sum2 = _run_cli(flags + ["ledger", "summary"])
        self.assertEqual(sum2["summary"]["funnel"]["favorited_pending"], 0)
        self.assertEqual(sum2["summary"]["funnel"]["favorited_total"], 1)
        self.assertEqual(sum2["summary"]["funnel"]["opening_delivered_total"], 1)
        self.assertEqual(sum2["summary"]["today_favorited"], 1)
        self.assertEqual(sum2["summary"]["today_opening_sent"], 1)
        self.assertEqual(sum2["summary"]["today_outbound_actions"], 1)

        # 7. copilot 无浏览器权限互斥、同传显式报错与 checklist 隐藏网页点击选项
        rc_both, both_err = _run_cli(flags + ["config", "set", "--execution-mode", "copilot", "--mode-discover", "favorite"])
        self.assertEqual(rc_both, 1)
        self.assertEqual(both_err["code"], "FAVORITE_REQUIRES_BROWSER_MODE")

        rc_cop, cop_res = _run_cli(flags + ["config", "set", "--execution-mode", "copilot"])
        self.assertEqual(rc_cop, 0)
        self.assertEqual(cop_res["config"]["mode_discover"], "semi")
        self.assertIn("notice", cop_res)

        _, chk_cop = _run_cli(flags + ["checklist", "--browser-capable", "false"])
        self.assertEqual(len(chk_cop["interactive_questions"]["question_batches"]), 2)
        self.assertEqual(len(chk_cop["interactive_questions"]["questions"]), 7)

        rc_cop_fav, cop_fav_err = _run_cli(flags + ["config", "set", "--mode-discover", "favorite"])
        self.assertEqual(rc_cop_fav, 1)
        self.assertEqual(cop_fav_err["code"], "FAVORITE_REQUIRES_BROWSER_MODE")

    def test_voice_profile_and_multi_host_sidebar_integration(self) -> None:
        """端到端验证：跨宿主历史对话语气档案（去噪抽样/建档/改稿沉淀/看板联动）与 DSH/Codex/Antigravity 侧边栏挂载。"""
        import time
        from hiremate_lib import voice

        ws = self.root / "voice_ws"
        hm = self.root / "voice_home"
        flags = ["--home", str(hm), "--workspace", str(ws)]

        rc_init, _ = _run_cli(
            flags
            + [
                "init",
                "--account-name",
                "林远",
                "--roles",
                "AI产品经理",
                "--cities",
                "深圳",
                "--min-salary-k",
                "28",
                "--employers",
                "示例科技",
                "--resume-source",
                "attachment_pdf",
            ]
        )
        self.assertEqual(rc_init, 0)
        (ws / "profile_facts.md").write_text("# 个人求职事实与答法档案\n- 5年自研 AI 应用产品经验\n", encoding="utf-8")
        _run_cli(flags + ["build-dashboard"])

        # 1. 空语气档案静默降级，不报错
        rc_vs0, vs0 = _run_cli(flags + ["voice", "show"])
        self.assertEqual(rc_vs0, 0)
        self.assertFalse(vs0["ready"])
        self.assertIn("尚未建立个人求职语气档案", vs0["text"])

        _, cfg0 = _run_cli(flags + ["config", "show"])
        self.assertFalse(cfg0["voice_profile_ready"])
        self.assertEqual(cfg0["voice_profile_text"], "")

        # 2. 构造多宿主模拟历史会话，验证求职语域去噪（保留自然短句/成段表达，剔除代码块/粘贴JD/短指令）
        mock_hosts_dir = self.root / "mock_voice_hosts"
        dsh_sdir = mock_hosts_dir / "dsh" / "sessions" / "ws1" / "session-01"
        dsh_sdir.mkdir(parents=True)
        now_ms = int(time.time() * 1000)
        dsh_events = [
            # 合法短句样本（含斜杠命令前缀应被自动剥离）
            {
                "type": "user/message",
                "time": now_ms - 3000,
                "data": {
                    "source": {"kind": "user"},
                    "content": [
                        {
                            "type": "text",
                            "text": "/hiremate 其实我平时跟业务方沟通比较直接，一般两句话把核心场景和上线指标讲清楚，不太爱加波浪号",
                        }
                    ],
                },
            },
            # skill 注入消息（source.kind != user，必须排除）
            {
                "type": "user/message",
                "time": now_ms - 2500,
                "data": {
                    "source": {"kind": "skill-invocation"},
                    "content": [{"type": "text", "text": "这是一段系统自动注入的技能长指令文本，绝不能当成用户本人语气"}],
                },
            },
            # 用户粘贴的外部招聘 JD（必须被去噪器拦截排除）
            {
                "type": "user/message",
                "time": now_ms - 2000,
                "data": {
                    "source": {"kind": "user"},
                    "content": [
                        {
                            "type": "text",
                            "text": "岗位职责：1. 负责大模型平台规划；2. 协同算法团队。任职要求：本科及以上学历，3年以上产品经验。",
                        }
                    ],
                },
            },
            # 纯短指令（必须排除）
            {
                "type": "user/message",
                "time": now_ms - 1000,
                "data": {"source": {"kind": "user"}, "content": [{"type": "text", "text": "继续"}]},
            },
        ]
        (dsh_sdir / "session.v2.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in dsh_events) + "\n",
            encoding="utf-8",
        )

        claude_pdir = mock_hosts_dir / "claude" / "projects" / "proj1"
        claude_pdir.mkdir(parents=True)
        claude_ev = {
            "type": "user",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": (
                    "这块我之前带 6 个研发从 0 到 1 做过企业知识库 RAG 评测体系，把问答准确率从 62% 提到了 89%，"
                    "但我平时从不说赋能和抓手这种词\n```python\nprint('code block should be stripped')\n```"
                ),
            },
        }
        (claude_pdir / "conv1.jsonl").write_text(json.dumps(claude_ev, ensure_ascii=False) + "\n", encoding="utf-8")

        codex_sdir = mock_hosts_dir / "codex" / "sessions" / "2026" / "11" / "20"
        codex_sdir.mkdir(parents=True)
        codex_ev = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "给对方发附件简历的时候我就习惯简单说一句：好的，附件简历刚发您了，方便时请过目",
                    }
                ],
            },
        }
        (codex_sdir / "rollout-1.jsonl").write_text(json.dumps(codex_ev, ensure_ascii=False) + "\n", encoding="utf-8")

        col_res = voice.collect_voice_corpus(
            ws,
            host_roots_override={
                "dsh": mock_hosts_dir / "dsh",
                "claude": mock_hosts_dir / "claude",
                "codex": mock_hosts_dir / "codex",
            },
        )
        self.assertTrue(col_res["ok"])
        self.assertTrue(col_res["corpus_ready"])
        self.assertEqual(col_res["picked_messages"], 3)
        corpus_text = Path(col_res["corpus_path"]).read_text(encoding="utf-8")
        self.assertIn("不太爱加波浪号", corpus_text)
        self.assertIn("RAG 评测体系", corpus_text)
        self.assertIn("方便时请过目", corpus_text)
        self.assertNotIn("code block should be stripped", corpus_text)
        self.assertNotIn("任职要求", corpus_text)
        self.assertNotIn("系统自动注入的技能长指令", corpus_text)

        # 3. 保存语气档案 + 自动同步刷新本地看板 + note 增量沉淀
        sample_profile = (
            "# 个人求职语气档案\n"
            "更新：2026-11-20 · 样本来源：3 条本机对话语料\n\n"
            "## 1. 句长节奏与标点指纹\n"
            "- 偏好两句内讲清核心事实，句末常省句号，不用波浪号「～」\n\n"
            "## 4. 分场景求职声线\n"
            "- 递简历附言：「好的，附件简历刚发您了，方便时请过目」\n"
        )
        rc_sv, sv_res = _run_cli(flags + ["voice", "save", "--text", sample_profile])
        self.assertEqual(rc_sv, 0)
        self.assertTrue(sv_res.get("dashboard_refreshed"))

        rc_nt, nt_res = _run_cli(flags + ["voice", "note", "把『贵团队』统一改为『咱们团队』"])
        self.assertEqual(rc_nt, 0)
        self.assertEqual(nt_res["voice_status"]["learned_notes_count"], 1)

        _, cfg1 = _run_cli(flags + ["config", "show"])
        self.assertTrue(cfg1["voice_profile_ready"])
        self.assertIn("方便时请过目", cfg1["voice_profile_text"])
        self.assertIn("咱们团队", cfg1["voice_profile_text"])

        dash_html = (ws / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("个人求职语气档案（本人行文风格指纹 · 仅存本机）", dash_html)
        self.assertIn("已启用", dash_html)
        self.assertIn("方便时请过目", dash_html)

        rc_clr, _ = _run_cli(flags + ["voice", "clear", "--corpus-only"])
        self.assertEqual(rc_clr, 0)
        self.assertFalse((ws / "voice_corpus.md").exists())
        self.assertTrue((ws / "voice_profile.md").is_file())

        # 4. 验证 DSH / Codex / Antigravity 三端侧边栏安装、Node 语法与客户端槽位协议
        for js_file in (
            OPEN_ROOT / "dsh" / "index.mjs",
            OPEN_ROOT / "dsh" / "client.js",
            OPEN_ROOT / "antigravity" / "sidecar" / "main.mjs",
        ):
            chk_js = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
            self.assertEqual(chk_js.returncode, 0, f"{js_file} syntax error: {chk_js.stderr}")

        # 验证 DSH client.js 的 __ModuleLoader__.load ID 严格等于 package.json name ("hiremate-dsh") 并成对注册 sidebar.panellist 与 main
        client_check_script = f"""
import fs from 'node:fs';
import vm from 'node:vm';
let loaded = null;
const slots = [];
let startedSession = null;
const sandbox = {{
  window: {{
    __ModuleLoader__: {{ load: (spec) => {{ loaded = spec; }} }},
  }},
  location: {{ origin: 'http://127.0.0.1:4321' }},
  navigator: {{ language: 'zh-CN' }},
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
  Date,
  Symbol,
  Object,
  String,
  console,
}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(OPEN_ROOT / 'dsh' / 'client.js'))}, 'utf-8'), sandbox);
const fakeReact = {{
  createElement: (tag, props, ...children) => ({{ tag, props, children }}),
  useRef: (v) => ({{ current: v }}),
  useState: (init) => [typeof init === 'function' ? init() : init, () => {{}}],
  useEffect: () => {{}},
}};
const mod = loaded.factory((id) => {{
  if (id === 'react') return fakeReact;
  throw new Error('unexpected require: ' + id);
}});
mod.apply({{
  inject: () => {{}},
  slots: {{
    inject: (_slot, fn) => fn(),
    register: (meta) => {{ slots.push({{ name: meta.name, id: meta.id || null, key: meta.key || null, label: meta.label ? meta.label() : null }}); }},
  }},
  uiWorkspace: {{ startSession: (_id, opts) => {{ startedSession = opts; }} }},
}});
console.log(JSON.stringify({{ id: loaded.id, isPlugin: mod.isPlugin, slots }}));
"""
        c_proc = subprocess.run(["node", "--input-type=module", "-e", client_check_script], capture_output=True, text=True)
        self.assertEqual(c_proc.returncode, 0, c_proc.stderr)
        c_info = json.loads(c_proc.stdout.strip())
        self.assertEqual(c_info["id"], "hiremate-dsh")
        self.assertTrue(c_info["isPlugin"])
        self.assertEqual(
            c_info["slots"],
            [
                {"name": "sidebar.panellist", "id": "hiremate", "key": None, "label": "求职助手"},
                {"name": "main", "id": None, "key": "hiremate", "label": None},
            ],
        )

        # 验证多宿主安装器在沙盒中挂载 DSH（含 - insert: 升级）、Codex（含 MCP 侧栏）与 Antigravity（含 SIDEBAR sidecar）
        sb_canon = self.root / "sb_canon"
        sb_dsh = self.root / "sb_dsh"
        (sb_dsh / "profiles" / "desktop" / "node_modules").mkdir(parents=True)
        # 模拟旧版缺少 - insert: 的遗留补丁，验证自动修复为合法的 - insert: + 包名
        (sb_dsh / "profiles" / "desktop" / "cordis.patch.yml").write_text(
            '# hiremate-dsh:begin\n- id: hiremate-dsh\n  name: "/old/index.mjs"\n# hiremate-dsh:end\n',
            encoding="utf-8",
        )
        sb_codex = self.root / "sb_codex"
        sb_codex.mkdir()
        sb_agy = self.root / "sb_agy"
        sb_agy.mkdir()

        inst_all = install_plugin.install(
            target="auto",
            canonical_home=str(sb_canon),
            search_roots={"dsh": sb_dsh, "codex": sb_codex, "antigravity": sb_agy},
        )
        self.assertTrue(inst_all["ok"])
        patched_dsh_yml = (sb_dsh / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertIn("- insert:\n    - id: hiremate-dsh\n      name: \"hiremate-dsh\"", patched_dsh_yml)
        self.assertTrue((sb_canon / "dsh-plugin" / "client.js").is_file())
        self.assertTrue((sb_canon / "dsh-plugin" / "icon.svg").is_file())

        codex_reg = inst_all["sidebar_registrations"]["codex"]
        self.assertTrue(codex_reg["registered"])
        self.assertTrue(Path(codex_reg["cache_dir"]).joinpath(".codex-plugin", "plugin.json").is_file())
        self.assertTrue(Path(codex_reg["cache_dir"]).joinpath(".mcp.json").is_file())

        agy_sidecar_json = json.loads(
            (sb_agy / "sidecars" / "hiremate_board" / "sidecar.json").read_text(encoding="utf-8")
        )
        entrypoints = [v["entrypoint"] for v in agy_sidecar_json["ui_config"]["views"]]
        self.assertIn("SIDECAR_UI_ENTRYPOINT_SIDEBAR", entrypoints)
        self.assertIn("SIDECAR_UI_ENTRYPOINT_AUX_PANE", entrypoints)

        # 5. 端到端验证 Codex MCP App stdio 协议（initialize -> tools/list -> resources/read -> tools/call）
        mcp_proc = subprocess.Popen(
            [sys.executable, str(OPEN_ROOT / "hiremate.py"), "--home", str(hm), "--workspace", str(ws), "mcp-serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert mcp_proc.stdin is not None and mcp_proc.stdout is not None

            def rpc(req_id: int, method: str, params: dict | None = None) -> dict:
                line = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}) + "\n"
                mcp_proc.stdin.write(line)
                mcp_proc.stdin.flush()
                resp_line = mcp_proc.stdout.readline()
                self.assertTrue(resp_line, "MCP server returned EOF")
                return json.loads(resp_line)

            r_init_mcp = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "codex"}})
            self.assertEqual(r_init_mcp["result"]["serverInfo"]["name"], "hiremate")

            r_tools = rpc(2, "tools/list")
            tools_by_name = {t["name"]: t for t in r_tools["result"]["tools"]}
            self.assertIn("hiremate_home", tools_by_name)
            self.assertEqual(
                tools_by_name["hiremate_home"]["_meta"]["openai/ui"]["entrypoints"],
                [{"type": "global"}],
            )
            page_uri = tools_by_name["hiremate_home"]["_meta"]["ui"]["resourceUri"]
            self.assertTrue(page_uri.startswith("ui://hiremate/"))

            r_read = rpc(3, "resources/read", {"uri": page_uri})
            contents = r_read["result"]["contents"][0]
            self.assertEqual(contents["mimeType"], "text/html;profile=mcp-app")
            self.assertIn("ui/initialize", contents["text"])

            r_call = rpc(4, "tools/call", {"name": "hiremate_home", "arguments": {}})
            snap = r_call["result"]["structuredContent"]
            self.assertTrue(snap["ok"])
            self.assertTrue(snap["voice_ready"])
            self.assertIn("方便时请过目", snap["dashboard_html"])
            self.assertIn('id="hm-strategy-form"', snap["dashboard_html"])
            self.assertIn('id="hm-cfg-mode-discover"', snap["dashboard_html"])

            # 验证在侧栏看板页面直接修改求职策略选项并保存后，立即落盘且后续 Agent 调用自动按新配置执行（且过滤非法字段）
            r_save = rpc(
                5,
                "tools/call",
                {
                    "name": "hiremate_ui_save_config",
                    "arguments": {
                        "patch": {
                            "mode_discover": "favorite",
                            "min_salary_k": 35,
                            "salary_match_mode": "floor_strict",
                            "outsourcing_policy": "inbound_only",
                            "past_employers": ["旧东家A", "新增前东家B"],
                            "blocked_companies": ["额外屏蔽竞对C"],
                            "schedule": {"max_per_round": 4, "max_per_day": 15, "weekdays_only": True},
                            "api_base": "https://evil-attacker.example.com",
                        }
                    },
                },
            )
            saved_sc = r_save["result"]["structuredContent"]
            self.assertTrue(saved_sc["ok"])
            self.assertEqual(saved_sc["config"]["mode_discover"], "favorite")
            self.assertEqual(saved_sc["config"]["min_salary_k"], 35)
            self.assertEqual(saved_sc["config"]["salary_match_mode"], "floor_strict")
            self.assertEqual(saved_sc["config"]["outsourcing_policy"], "inbound_only")
            self.assertIn("新增前东家B", saved_sc["config"]["blocked_companies"])
            self.assertIn("额外屏蔽竞对C", saved_sc["config"]["blocked_companies"])
            self.assertNotEqual(saved_sc["config"]["api_base"], "https://evil-attacker.example.com")
            self.assertIn("单轮自动收藏（不主动投递）", saved_sc["snapshot"]["dashboard_html"])

            # Agent 紧接着调用 CLI 时零延迟读取最新保存的策略（例如 floor_strict 35K 拦截 30-42K 岗位，且新增前东家B立即防撞生效）
            _, after_show = _run_cli(flags + ["config", "show"])
            self.assertEqual(after_show["config"]["mode_discover"], "favorite")
            self.assertEqual(after_show["config"]["min_salary_k"], 35)
            _, scr_low_floor = _run_cli(
                flags + ["screen-basic", "--company", "星海自研软件", "--title", "AI产品经理", "--salary", "30-42K"]
            )
            self.assertFalse(scr_low_floor["pass_basic"])
            _, dup_new_emp = _run_cli(
                flags + ["ledger", "check-dup", "--company", "新增前东家B科技有限公司", "--title", "AI产品经理"]
            )
            self.assertTrue(dup_new_emp["duplicate"])
            self.assertTrue(dup_new_emp["blocked_employer"])
        finally:
            mcp_proc.terminate()
            mcp_proc.wait(timeout=5)
            for pipe in (mcp_proc.stdin, mcp_proc.stdout, mcp_proc.stderr):
                if pipe:
                    pipe.close()

        # 6. 验证 DSH client.js 在第三方扩展（如 dsh-web-all）覆写 startSession 丢弃 options 时仍能写草稿并自动发送
        import shutil
        node_bin = shutil.which("node")
        if node_bin:
            client_js_path = str(OPEN_ROOT / "dsh" / "client.js")
            node_test_script = r"""
const fs = require('node:fs');
const clientPath = process.argv[1];
let registeredPlugin = null;
let messageHandler = null;
global.location = { origin: 'http://127.0.0.1:3082' };
global.navigator = { language: 'zh-CN' };
global.requestAnimationFrame = (cb) => setImmediate(cb);
global.addEventListener = (ev, fn) => { if (ev === 'message') messageHandler = fn; };
global.removeEventListener = () => {};
global.window = global;
global.window.__ModuleLoader__ = {
  load(spec) { registeredPlugin = spec; }
};
const fakeFrameWin = {};
const fakeReact = {
  createElement: (tag, props) => ({ tag, props }),
  useRef: () => ({ current: { contentWindow: fakeFrameWin } }),
  useState: (init) => [typeof init === 'function' ? init() : init, () => {}],
  useEffect: (fn) => { fn(); },
};
eval(fs.readFileSync(clientPath, 'utf8'));
const mod = registeredPlugin.factory((id) => {
  if (id === 'react') return fakeReact;
  throw new Error('unexpected require: ' + id);
});
const log = [];
class FakeUiWorkspace {
  startSession(wsId, opts) { log.push({ call: 'protoStart', wsId, opts }); }
  async openWorkspace(wsId, beforeOpen) {
    log.push({ call: 'openWorkspace', wsId });
    if (beforeOpen) beforeOpen('sess-blank-1');
  }
}
const uiWorkspace = new FakeUiWorkspace();
// 模拟 dsh-web-all auto-isolation 覆写实例方法并吞掉第二个参数 options
uiWorkspace.startSession = (wsId) => { log.push({ call: 'monkeyStartDroppedOpts', wsId }); };

let currentDraft = '';
const shell = {
  state: { getSnapshot: () => ({ draft: currentDraft, phase: 'plain' }) },
  actions: {
    setDraft: (t) => { currentDraft = t; log.push({ call: 'setDraft', text: t }); },
    submit: () => { log.push({ call: 'submit', draft: currentDraft }); },
  },
  focus: () => { log.push({ call: 'focus', draft: currentDraft }); },
};
const binding = {
  sessionId: 'sess-blank-1',
  ctx: {},
  session: {
    open: async () => { log.push({ call: 'sessionOpen' }); },
    getSnapshot: () => ({ openState: 'open' }),
  },
};
const services = {
  workspaces: { list: { getSnapshot: () => ({ items: [{ workspaceId: 'ws-main', sessionIds: ['sess-blank-1'] }] }) } },
  sessions: {
    list: { getSnapshot: () => ({ byId: { 'sess-blank-1': { id: 'sess-blank-1', updatedAt: 100 } } }) },
    binding: (id) => (id === 'sess-blank-1' ? binding : undefined),
  },
  conversation: {
    input: {
      requestDraftInitialization: (_b, opts) => {
        currentDraft = opts.prompt || '';
        log.push({ call: 'requestDraftInit', prompt: currentDraft });
        return 'applied';
      },
      for: () => shell,
    },
  },
};
let mainComp = null;
mod.apply({
  uiWorkspace,
  inject: (deps, cb) => {
    const bag = {};
    for (const d of deps) if (services[d]) bag[d] = services[d];
    cb(bag);
  },
  get: (k) => services[k],
  slots: {
    inject: (_slot, fn) => fn(),
    register: (meta, comp) => { if (meta.name === 'main') mainComp = comp; },
  },
});
mainComp();
(async () => {
  messageHandler({
    source: fakeFrameWin,
    origin: 'http://127.0.0.1:3082',
    data: { type: 'hiremate:start', prompt: '/hiremate 帮我初始化求职助手', autoSend: true },
  });
  await new Promise((r) => setTimeout(r, 40));
  messageHandler({
    source: fakeFrameWin,
    origin: 'http://127.0.0.1:3082',
    data: { type: 'hiremate:start', prompt: '/hiremate 请补充粘贴样本：\n\n', autoSend: false },
  });
  await new Promise((r) => setTimeout(r, 40));
  process.stdout.write(JSON.stringify(log));
})();
"""
            node_res = subprocess.run(
                [node_bin, "-e", node_test_script, client_js_path],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            dsh_calls = json.loads(node_res.stdout)
            call_names = [c["call"] for c in dsh_calls]
            self.assertNotIn("monkeyStartDroppedOpts", call_names)
            self.assertIn("openWorkspace", call_names)
            self.assertIn("requestDraftInit", call_names)
            self.assertIn("submit", call_names)
            self.assertIn("focus", call_names)
            submitted = [c for c in dsh_calls if c["call"] == "submit"]
            self.assertEqual(len(submitted), 1)
            self.assertEqual(submitted[0]["draft"], "/hiremate 帮我初始化求职助手")

        # 7. 验证「仅在支持侧边栏看板的 Agent 中主动提示侧边栏入口，不支持侧边栏的宿主绝不误导」
        from hiremate_lib import workspace as hm_ws

        dsh_host_info = hm_ws.detect_runtime_agent_host(
            argv0="/Users/demo/.dsh/skills/hiremate/hiremate.py", environ={}
        )
        self.assertTrue(dsh_host_info["sidebar_supported"])
        self.assertEqual(dsh_host_info["host"], "dsh")

        wb_host_info = hm_ws.detect_runtime_agent_host(
            argv0="/Users/demo/.workbuddy/skills/hiremate/hiremate.py", environ={}
        )
        self.assertFalse(wb_host_info["sidebar_supported"])
        self.assertEqual(wb_host_info["host"], "workbuddy")

        _, chk_dsh = _run_cli(flags + ["checklist", "--builtin-headed-persistent", "true"], env={"HIREMATE_HOST": "dsh"})
        self.assertTrue(chk_dsh["host_sidebar_guide"]["sidebar_supported"])
        self.assertIn("左侧边栏", chk_dsh["markdown"])
        self.assertIn("侧边栏随时直达", chk_dsh["post_onboarding_guide"]["dashboard_spotlight_markdown"])

        _, chk_codex = _run_cli(flags + ["checklist", "--builtin-headed-persistent", "true"], env={"HIREMATE_HOST": "codex"})
        self.assertTrue(chk_codex["host_sidebar_guide"]["sidebar_supported"])
        self.assertIn("左侧边栏", chk_codex["post_onboarding_guide"]["dashboard_spotlight_markdown"])

        _, chk_wb = _run_cli(flags + ["checklist", "--builtin-headed-persistent", "true"], env={"HIREMATE_HOST": "workbuddy"})
        self.assertFalse(chk_wb["host_sidebar_guide"]["sidebar_supported"])
        self.assertNotIn("侧边栏", chk_wb["markdown"])
        self.assertNotIn("侧边栏", chk_wb["post_onboarding_guide"]["dashboard_spotlight_markdown"])

        sb_guide_dsh = install_plugin._build_sidebar_install_guide("dsh", inst_all["sidebar_registrations"])
        self.assertTrue(sb_guide_dsh["current_host_supports_sidebar"])
        self.assertIn("左侧边栏", sb_guide_dsh["next_step"])
        sb_guide_wb = install_plugin._build_sidebar_install_guide("workbuddy", inst_all["sidebar_registrations"])
        self.assertFalse(sb_guide_wb["current_host_supports_sidebar"])
        self.assertNotIn("侧边栏", sb_guide_wb["next_step"])

        # 验证卸载干净移除三端侧边栏构件且保留用户工作区数据
        un_all = install_plugin.uninstall(
            canonical_home=str(sb_canon),
            search_roots={"dsh": sb_dsh, "codex": sb_codex, "antigravity": sb_agy},
            purge_data=False,
        )
        self.assertTrue(un_all["ok"])
        self.assertFalse((sb_agy / "sidecars" / "hiremate_board").exists())
        self.assertFalse((sb_codex / "plugins" / "cache" / "hiremate").exists())
        self.assertEqual((sb_dsh / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8").strip(), "")

    def test_code_review_hardening_regressions(self) -> None:
        """覆盖 Code Review 修复项：敏感凭证脱敏、config set --employers、Antigravity /preload.js 注入、DSH 无注释补丁升级卸载、MCP LSP Content-Length 帧。"""
        from hiremate_lib import mcp_server as hm_mcp
        from hiremate_lib import voice as hm_voice
        import install_plugin

        # 1. voice.py 跨会话敏感密钥/Token/口令/身份证号自动脱敏，且 XML 同名闭合标签不误吞用户正文
        cleaned_sec = hm_voice._clean_user_utterance(
            "<system-reminder>internal note</system-reminder>"
            "我平时写接口说明其实习惯先列清楚边界，测试 key 是 sk-proj-AbCdEf1234567890GhIjKlMnOp，密码：MyPass9988，身份证 44030519950816123X，还有</system-reminder>后面的正文也保留"
        )
        self.assertIsNotNone(cleaned_sec)
        assert cleaned_sec is not None
        self.assertNotIn("sk-proj-", cleaned_sec)
        self.assertNotIn("MyPass9988", cleaned_sec)
        self.assertNotIn("44030519950816123X", cleaned_sec)
        self.assertIn("[密钥]", cleaned_sec)
        self.assertIn("[敏感凭证]", cleaned_sec)
        self.assertIn("[身份证号]", cleaned_sec)
        self.assertIn("后面的正文也保留", cleaned_sec)

        # 2. config set --employers 正常支持且自动同步进 blocked_companies；voice note 兼容带括号补充的标题与 Updated 行
        hm = self.root / "hm-rev"
        ws = hm / "default"
        flags = ["--home", str(hm), "--workspace", str(ws)]
        _run_cli(flags + ["init", "--roles", "AI产品经理", "--employers", "初始前东家A"])
        code_emp, res_emp = _run_cli(flags + ["config", "set", "--employers", "初始前东家A,新增前东家X"])
        self.assertEqual(code_emp, 0)
        self.assertIn("新增前东家X", res_emp["config"]["past_employers"])
        self.assertIn("新增前东家X", res_emp["config"]["blocked_companies"])

        hm_voice.save_voice_profile(
            ws,
            "# 个人求职语气档案\nUpdated: 2026-11-01\n\n## 5. 审阅与改稿中学到的（持续沉淀）\n- 2026-11-01：不用感叹号\n",
        )
        note_res = hm_voice.add_voice_note(ws, "递简历附言改为简短自然")
        self.assertEqual(note_res["voice_status"]["learned_notes_count"], 2)

        # 3. DSH 空新配置目录（尚无 node_modules）也能自动挂上插件包，且 DSH 重写去掉注释后的 cordis.patch.yml 可干净升级与卸载
        rev_canon = self.root / "rev-canon"
        rev_dsh = self.root / "rev-dsh"
        rev_codex = self.root / "rev-codex"
        rev_agy = self.root / "rev-agy"
        (rev_dsh / "profiles" / "desktop").mkdir(parents=True)
        rev_codex.mkdir(parents=True)
        (rev_dsh / "profiles" / "desktop" / "cordis.patch.yml").write_text(
            "plugins:\n  hiremate-dsh:\n    homeDir: /old/path\n  other-plugin: {}\n",
            encoding="utf-8",
        )
        (rev_codex / "config.toml").write_text(
            '# 用户保留注释\nmodel = "gpt-5"\n\n[marketplaces.hiremate]\nsource_type = "local"\nsource = "/old"\n',
            encoding="utf-8",
        )
        (rev_agy / "sidecars").mkdir(parents=True)

        inst = install_plugin.install(
            canonical_home=str(rev_canon),
            search_roots={"dsh": rev_dsh, "codex": rev_codex, "antigravity": rev_agy},
        )
        self.assertTrue(inst["ok"])
        self.assertTrue((rev_dsh / "profiles" / "desktop" / "node_modules" / "hiremate-dsh").exists())
        agy_sc_cfg = json.loads((rev_agy / "sidecars" / "hiremate_board" / "sidecar.json").read_text(encoding="utf-8"))
        self.assertEqual(agy_sc_cfg["restart_policy"], "always")
        self.assertIn("HIREMATE_CLI_PATH", agy_sc_cfg["env"])
        agy_main_src = (rev_agy / "sidecars" / "hiremate_board" / "main.mjs").read_text(encoding="utf-8")
        self.assertIn('<script src="/preload.js"></script>', agy_main_src)
        codex_toml = (rev_codex / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(codex_toml.count("[marketplaces.hiremate]"), 1)
        self.assertEqual(codex_toml.count('[plugins."hiremate@hiremate"]'), 1)

        # 模拟 DSH 运行时重序列化去掉 # hiremate-dsh 注释块后执行 uninstall
        (rev_dsh / "profiles" / "desktop" / "cordis.patch.yml").write_text(
            "- insert:\n    - id: other-plugin\n      name: other-plugin\n- insert:\n    - id: hiremate-dsh\n      name: hiremate-dsh\n",
            encoding="utf-8",
        )
        install_plugin.uninstall(
            canonical_home=str(rev_canon),
            search_roots={"dsh": rev_dsh, "codex": rev_codex, "antigravity": rev_agy},
        )
        after_patch = (rev_dsh / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertNotIn("hiremate-dsh", after_patch)
        self.assertIn("other-plugin", after_patch)
        after_toml = (rev_codex / "config.toml").read_text(encoding="utf-8")
        self.assertIn("# 用户保留注释", after_toml)
        self.assertNotIn("[marketplaces.hiremate]", after_toml)

        # 4. MCP Server 同时支持 LSP Content-Length 帧，且 Shell HTML 将 bridgePatch 注入在 <head> 首位
        import io
        req_bytes = json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping", "params": {}}).encode("utf-8")
        lsp_stream = io.BytesIO(f"Content-Length: {len(req_bytes)}\r\n\r\n".encode("ascii") + req_bytes)
        frame_body, frame_mode = hm_mcp._read_next_rpc_frame(lsp_stream)
        self.assertEqual(frame_mode, "lsp")
        self.assertEqual(json.loads(frame_body.decode("utf-8"))["id"], 99)
        shell_html = hm_mcp._build_shell_html()
        self.assertIn('rawHtml.indexOf("<head>")', shell_html)


if __name__ == "__main__":
    unittest.main()
