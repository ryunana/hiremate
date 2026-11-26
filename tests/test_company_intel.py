"""公司底细核查与正交二维判定引擎回归测试（覆盖 Issue #3 全部 6 项缺陷与反例）。"""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

OPEN_ROOT = Path(__file__).resolve().parents[1]
if str(OPEN_ROOT) not in sys.path:
    sys.path.insert(0, str(OPEN_ROOT))

import hiremate  # noqa: E402


def _run_cli(args: list[str]) -> tuple[int, dict]:
    buf = StringIO()
    with redirect_stdout(buf):
        rc = hiremate.main(args)
    raw = buf.getvalue().strip()
    return rc, json.loads(raw) if raw else {}


class TestCompanyIntel(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.home = self.root / "hm_home"
        self.ws.mkdir(parents=True, exist_ok=True)
        os.environ["HIREMATE_QUIET_LOG"] = "1"
        os.environ["HIREMATE_DISABLE_UPDATE_CHECK"] = "1"
        os.environ["HIREMATE_DISABLE_DESKTOP_NOTIFY"] = "1"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _flags(self) -> list[str]:
        return ["--workspace", str(self.ws), "--home", str(self.home)]

    def _init(self, cities: str = "深圳") -> None:
        rc, _ = _run_cli(
            self._flags()
            + [
                "init",
                "--account-name",
                "测试求职者",
                "--roles",
                "AI产品经理,后端开发",
                "--cities",
                cities,
                "--resume-source",
                "online_only",
            ]
        )
        self.assertEqual(rc, 0)

    # 缺陷 1：金融科技自研公司（标题含"银行"）不被误判为 not_feasible
    def test_defect1_fintech_self_dev_not_false_positive(self) -> None:
        self._init("深圳")
        rc, res = _run_cli(
            self._flags()
            + [
                "company",
                "probe",
                "--company",
                "某银行科技自研有限公司",
                "--profile-json",
                json.dumps(
                    {
                        "scale": "500-999人",
                        "established": "2016-05-01",
                        "business_scope": "银行核心系统软件研发、金融科技产品",
                        "office_city": "深圳",
                        "intro": "专注银行核心系统自研产品",
                    },
                    ensure_ascii=False,
                ),
                "--jobs-json",
                json.dumps(
                    [
                        {"title": "银行核心系统产品经理", "city": "深圳"},
                        {"title": "银行支付后端开发", "city": "深圳"},
                    ],
                    ensure_ascii=False,
                ),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(res["verdict"], "explorable")
        self.assertNotIn("client_site_hint", res["risk_flags"])

    # 缺陷 2：零售科技公司 12 岗含 2 门店岗不被误判；案例 A 伪装 AI 劳务派遣判 not_feasible
    def test_defect2_retail_saas_safe_and_case_a_labor_dispatch_blocked(self) -> None:
        self._init("深圳")
        retail_jobs = [
            {"title": "门店营业员", "city": "深圳"},
            {"title": "理货员", "city": "深圳"},
        ] + [{"title": f"AI产品经理-{i}", "city": "深圳"} for i in range(10)]
        rc, res_retail = _run_cli(
            self._flags()
            + [
                "company",
                "probe",
                "--company",
                "智汇零售科技有限公司",
                "--profile-json",
                json.dumps({"scale": "500-999人", "intro": "零售SaaS与样板店自研"}, ensure_ascii=False),
                "--jobs-json",
                json.dumps(retail_jobs, ensure_ascii=False),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertNotEqual(res_retail["verdict"], "not_feasible")
        self.assertNotIn("mixed_labor_dispatch", res_retail["risk_flags"])

        # 案例 A：13 岗含 2 航空配餐店员 + 经营范围含劳务服务/人力资源服务 -> not_feasible
        case_a_jobs = [
            {"title": "某航空配餐门店店员", "city": "广州"},
            {"title": "某航空配餐门店店员2", "city": "深圳"},
        ] + [{"title": f"AI研发-{i}", "city": "深圳"} for i in range(11)]
        rc, res_a = _run_cli(
            self._flags()
            + [
                "company",
                "probe",
                "--company",
                "某智算人力有限公司",
                "--profile-json",
                json.dumps(
                    {
                        "scale": "100-499人",
                        "business_scope": "人力资源服务、软件外包服务、劳务服务",
                        "office_city": "深圳",
                    },
                    ensure_ascii=False,
                ),
                "--jobs-json",
                json.dumps(case_a_jobs, ensure_ascii=False),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(res_a["verdict"], "not_feasible")
        self.assertIn("mixed_labor_dispatch", res_a["risk_flags"])

    # 缺陷 3：二次补抓缺 profile 或带空/占位符时 COALESCE 绝不清空已有工商信息；支持 --clear-field
    def test_defect3_coalesce_preserves_profile_and_supports_clear_field(self) -> None:
        self._init("深圳")
        t1 = "2026-10-01T10:00:00+08:00"
        t2 = "2026-11-26T10:00:00+08:00"
        rc, _ = _run_cli(
            self._flags()
            + [
                "company",
                "probe",
                "--company",
                "星图智造科技有限公司",
                "--profile-json",
                json.dumps(
                    {
                        "scale": "50-99人",
                        "established": "2015-06-01",
                        "former_name": "旧名字科技有限公司",
                        "office_city": "深圳",
                    },
                    ensure_ascii=False,
                ),
                "--now",
                t1,
            ]
        )
        self.assertEqual(rc, 0)

        # 第二次只补抓 jobs 且传空字符串/占位词 -> 已有工商信息完好无损，且 profile 独立标 stale
        rc, res2 = _run_cli(
            self._flags()
            + [
                "company",
                "probe",
                "--company",
                "星图智造科技",
                "--profile-json",
                json.dumps({"scale": "", "former_name": "暂无", "established": "   "}, ensure_ascii=False),
                "--jobs-json",
                json.dumps([{"job_id": "j1", "title": "AI产品经理", "city": "深圳"}], ensure_ascii=False),
                "--now",
                t2,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(res2["facts"]["established"], "2015-06-01")
        self.assertEqual(res2["facts"]["former_name"], "旧名字科技有限公司")
        self.assertEqual(res2["facts"]["scale"], "50-99人")
        self.assertTrue(res2["freshness"]["profile_stale"])
        self.assertFalse(res2["freshness"]["jobs_stale"])

        # 显式 --clear-field 清空 former_name
        rc, res3 = _run_cli(
            self._flags()
            + [
                "company",
                "probe",
                "--company",
                "星图智造科技",
                "--clear-field",
                "former_name",
                "--now",
                t2,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(res3["facts"]["former_name"])

    # 缺陷 4 & 5：本地岗不掩盖高风险；0-20 人跨簇职能跨度判 explorable_high_risk，同簇 AI 初创不误杀
    def test_defect4_and_5_orthogonal_risk_and_cluster_span(self) -> None:
        self._init("深圳")
        # 0-20 人同时招算法 + 芯片 + 激光雷达 + 产品（跨 software + hardware_chip 2 簇且 ≥3 细分）-> explorable_high_risk
        rc, res_shell = _run_cli(
            self._flags()
            + [
                "company",
                "probe",
                "--company",
                "空壳前沿科技有限公司",
                "--profile-json",
                json.dumps(
                    {
                        "scale": "0-20人",
                        "established": "2025-01-01",
                        "former_name": "某建筑工程有限公司",
                        "reg_city": "武汉",
                        "office_city": "深圳",
                    },
                    ensure_ascii=False,
                ),
                "--jobs-json",
                json.dumps(
                    [
                        {"title": "AI产品经理", "city": "深圳"},
                        {"title": "NLP算法专家", "city": "深圳"},
                        {"title": "芯片测试工程师", "city": "深圳"},
                        {"title": "激光雷达系统工程师", "city": "深圳"},
                    ],
                    ensure_ascii=False,
                ),
                "--now",
                "2026-11-26T10:00:00+08:00",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(res_shell["feasibility"], "local_match")
        self.assertEqual(res_shell["verdict"], "explorable_high_risk")
        self.assertIn("abnormal_role_span", res_shell["risk_flags"])
        self.assertIn("cross_industry_former_name", res_shell["risk_flags"])
        self.assertIn("cross_province_reg_office", res_shell["risk_flags"])

        # 正常 15 人 AI 初创招 产品 + 算法 + 后端（全部同属 software 单簇）-> 不触发 abnormal_role_span
        rc, res_startup = _run_cli(
            self._flags()
            + [
                "company",
                "probe",
                "--company",
                "正常初创科技有限公司",
                "--profile-json",
                json.dumps({"scale": "0-20人", "intro": "专注企业级 AI Agent 自研产品", "office_city": "深圳"}, ensure_ascii=False),
                "--jobs-json",
                json.dumps(
                    [
                        {"title": "AI产品经理", "city": "深圳"},
                        {"title": "大模型算法工程师", "city": "深圳"},
                        {"title": "Python后端开发", "city": "深圳"},
                    ],
                    ensure_ascii=False,
                ),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(res_startup["verdict"], "explorable")
        self.assertNotIn("abnormal_role_span", res_startup["risk_flags"])

    # 缺陷 6 & B3：同名异地主体冲突保护 + 动态 preferred_cities 重算
    def test_defect6_identity_conflict_and_dynamic_cities(self) -> None:
        self._init("深圳")
        rc, _ = _run_cli(
            self._flags()
            + [
                "company",
                "probe",
                "--company",
                "深圳星辰科技有限公司",
                "--profile-json",
                json.dumps({"scale": "100-499人", "office_city": "深圳"}, ensure_ascii=False),
            ]
        )
        self.assertEqual(rc, 0)

        # 同名异地（杭州星辰）触发 IDENTITY_CONFLICT 拒绝覆盖
        rc, err = _run_cli(
            self._flags()
            + [
                "company",
                "probe",
                "--company",
                "杭州星辰科技有限公司",
                "--profile-json",
                json.dumps({"scale": "0-20人", "office_city": "杭州"}, ensure_ascii=False),
            ]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(err["code"], "IDENTITY_CONFLICT")

        # 动态修改优先城市为杭州后 show 实时重算为 remote_mismatch -> not_feasible
        _run_cli(self._flags() + ["config", "set", "--cities", "杭州"])
        rc, res_show = _run_cli(self._flags() + ["company", "show", "--company", "深圳星辰科技有限公司"])
        self.assertEqual(rc, 0)
        self.assertEqual(res_show["feasibility"], "remote_mismatch")
        self.assertEqual(res_show["verdict"], "not_feasible")


if __name__ == "__main__":
    unittest.main()
