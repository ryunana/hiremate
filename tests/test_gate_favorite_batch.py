"""本地确定性门控与「收藏批次统一投递」状态机的回归测试。

覆盖用户报告的两类真实缺陷：

1. 收藏动作占用主动投递额度，导致同一批岗位隔天投递时被重复计费；
2. 「到 apply_at 才统一投递」只写在提示词里，没有任何程序性保证，模型漏读即提前投递。

对应实现：`hiremate_lib/channel.py` 与 `hiremate_lib/gate.py`。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

OPEN_ROOT = Path(__file__).resolve().parents[1]

import sys

if str(OPEN_ROOT) not in sys.path:
    sys.path.insert(0, str(OPEN_ROOT))

import hiremate  # noqa: E402
from hiremate_lib import channel as hm_channel  # noqa: E402
from hiremate_lib import gate as hm_gate  # noqa: E402
from hiremate_lib.ledger import JobLedger  # noqa: E402


def _run_cli(args: list[str]) -> tuple[int, dict]:
    from contextlib import redirect_stdout
    from io import StringIO

    buf = StringIO()
    with redirect_stdout(buf):
        rc = hiremate.main(args)
    raw = buf.getvalue().strip()
    return rc, json.loads(raw) if raw else {}


class TestGateAndFavoriteBatch(unittest.TestCase):
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

    # -- 辅助 ---------------------------------------------------------------

    def _flags(self) -> list[str]:
        return ["--workspace", str(self.ws), "--home", str(self.home)]

    def _init(self) -> None:
        rc, _ = _run_cli(
            self._flags()
            + [
                "init",
                "--account-name",
                "测试用户",
                "--roles",
                "AI产品经理",
                "--cities",
                "深圳",
                "--resume-source",
                "online_only",
            ]
        )
        self.assertEqual(rc, 0)

    def _upsert(self, **payload) -> str:
        rc, res = _run_cli(
            self._flags()
            + ["ledger", "upsert-job", "--job-json", json.dumps(payload, ensure_ascii=False)]
        )
        self.assertEqual(rc, 0)
        return res["job"]["job_id"]

    # -- channel 语义 -------------------------------------------------------

    def test_channel_enum_and_quota_exemption(self) -> None:
        self.assertEqual(
            set(hm_channel.VALID_CHANNELS),
            {"outbound", "inbound", "favorite_queue"},
        )
        # 未知值降级而非抛错，保证历史账本可读
        self.assertEqual(hm_channel.normalize_channel("legacy_weird"), "outbound")
        self.assertEqual(hm_channel.normalize_channel(""), "outbound")
        self.assertEqual(hm_channel.normalize_channel("favorite_queue"), "favorite_queue")

        self.assertTrue(hm_channel.counts_toward_quota("outbound"))
        self.assertTrue(hm_channel.counts_toward_quota("inbound"))
        # 批次补投是延迟动作的收尾，不重复占用投递额度
        self.assertFalse(hm_channel.counts_toward_quota("favorite_queue"))

    # -- 额度分桶（缺陷 1 回归） --------------------------------------------

    def test_favorite_queue_does_not_consume_daily_quota(self) -> None:
        self._init()
        # 1 条常规主动投递 + 3 条批次补投
        self._upsert(company="常规公司", title="常规投递岗", status="opening_sent", channel="outbound")
        for idx in range(3):
            self._upsert(
                company=f"收藏公司{idx}",
                title=f"收藏岗{idx}",
                status="opening_sent",
                channel="favorite_queue",
            )

        led = JobLedger(self.ws)
        summary = led.summary()

        # 总投递数如实统计
        self.assertEqual(summary["today_opening_sent"], 4)
        # 但只有 1 条计入每日额度
        self.assertEqual(summary["today_counted_opening_sent"], 1)
        self.assertEqual(summary["today_by_channel"]["outbound"]["counted_opening_sent"], 1)
        self.assertEqual(
            summary["today_by_channel"]["favorite_queue"]["counted_opening_sent"], 0
        )
        # 明细里仍能看到批次补投的真实投递条数
        self.assertEqual(summary["today_by_channel"]["favorite_queue"]["opening_sent"], 3)
        self.assertEqual(summary["quota_exempt_channels"], ["favorite_queue"])

        quota = hm_gate.quota_state(self.ws)
        self.assertEqual(quota["counted_openings_today"], 1)

    def test_favoriting_alone_does_not_consume_opening_quota(self) -> None:
        """收藏 5 个不等于投递 5 个：这是缺陷 1 的最直观复现。"""
        self._init()
        for idx in range(5):
            self._upsert(
                company=f"候选公司{idx}",
                title=f"候选岗{idx}",
                status="favorited",
                channel="outbound",
            )
        summary = JobLedger(self.ws).summary()
        self.assertEqual(summary["today_favorited"], 5)
        # 收藏不产生 opening_sent，因此不计入投递额度
        self.assertEqual(summary["today_counted_opening_sent"], 0)
        self.assertEqual(hm_gate.quota_state(self.ws)["counted_openings_today"], 0)

    # -- apply_at 硬约束（缺陷 2 回归） ------------------------------------

    def test_batch_rejects_dispatch_before_apply_at(self) -> None:
        self._init()
        apply_at = "2026-11-27T09:30:00+08:00"
        rc, res = _run_cli(
            self._flags()
            + ["favorite-batch", "create", "--apply-at", apply_at, "--batch-id", "b1"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(res["batch"]["status"], hm_gate.BATCH_PENDING)

        rc, _ = _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "add",
                "--batch-id",
                "b1",
                "--item-json",
                json.dumps({"company": "甲", "title": "AI产品经理"}, ensure_ascii=False),
            ]
        )
        self.assertEqual(rc, 0)

        # 提前 20 分钟抢跑：必须在代码层被拒绝，而不是靠提示词
        rc, err = _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "claim-next",
                "--batch-id",
                "b1",
                "--now",
                "2026-11-27T09:10:00+08:00",
            ]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(err["code"], "BATCH_NOT_DUE")

        # 独立的断言入口同样拒绝
        rc, err2 = _run_cli(
            self._flags()
            + ["gate", "--assert-dispatch", "b1", "--now", "2026-11-27T09:10:00+08:00"]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(err2["code"], "BATCH_NOT_DUE")

        # 到点后放行
        rc, ok = _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "claim-next",
                "--batch-id",
                "b1",
                "--now",
                "2026-11-27T09:31:00+08:00",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(ok["next_item"]["company"], "甲")
        self.assertEqual(ok["batch"]["status"], hm_gate.BATCH_APPLYING)

    def test_batch_lifecycle_progress_and_terminal_guard(self) -> None:
        self._init()
        apply_at = "2026-11-27T09:30:00+08:00"
        _run_cli(self._flags() + ["favorite-batch", "create", "--apply-at", apply_at, "--batch-id", "b2"])
        for company in ("甲", "乙"):
            _run_cli(
                self._flags()
                + [
                    "favorite-batch",
                    "add",
                    "--batch-id",
                    "b2",
                    "--item-json",
                    json.dumps({"company": company, "title": "AI产品经理"}, ensure_ascii=False),
                ]
            )

        at = "2026-11-27T09:35:00+08:00"
        rc, res = _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "mark",
                "--batch-id",
                "b2",
                "--company",
                "甲",
                "--title",
                "AI产品经理",
                "--now",
                at,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(res["batch"]["applied"], 1)
        self.assertEqual(res["batch"]["pending"], 1)
        self.assertEqual(res["batch"]["status"], hm_gate.BATCH_APPLYING)

        # 跳过（岗位下线）后队列清空 -> 自动 completed
        rc, res = _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "mark",
                "--batch-id",
                "b2",
                "--company",
                "乙",
                "--title",
                "AI产品经理",
                "--outcome",
                "skipped",
                "--reason",
                "岗位已下线",
                "--now",
                at,
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(res["batch"]["status"], hm_gate.BATCH_COMPLETED)
        self.assertEqual(res["batch"]["skipped"], 1)

        # 结束后不可再 claim / 追加
        rc, err = _run_cli(
            self._flags() + ["favorite-batch", "claim-next", "--batch-id", "b2", "--now", at]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(err["code"], "BATCH_TERMINAL")

        rc, err_add = _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "add",
                "--batch-id",
                "b2",
                "--item-json",
                json.dumps({"company": "丙", "title": "AI产品经理"}, ensure_ascii=False),
            ]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(err_add["code"], "BATCH_TERMINAL")

    def test_batch_abort_keeps_resumable_checkpoint(self) -> None:
        self._init()
        apply_at = "2026-11-27T09:30:00+08:00"
        _run_cli(self._flags() + ["favorite-batch", "create", "--apply-at", apply_at, "--batch-id", "b3"])
        for company in ("甲", "乙", "丙"):
            _run_cli(
                self._flags()
                + [
                    "favorite-batch",
                    "add",
                    "--batch-id",
                    "b3",
                    "--item-json",
                    json.dumps({"company": company, "title": "AI产品经理"}, ensure_ascii=False),
                ]
            )

        at = "2026-11-27T09:35:00+08:00"
        # 已成功投递 1 个后遇验证码中断
        _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "mark",
                "--batch-id",
                "b3",
                "--company",
                "甲",
                "--title",
                "AI产品经理",
                "--now",
                at,
            ]
        )
        rc, res = _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "abort",
                "--batch-id",
                "b3",
                "--reason",
                "触发滑块验证，交本人处理",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(res["batch"]["status"], hm_gate.BATCH_ABORTED)
        # 断点保留：applied=1，pending=2，不会重复投已成功项
        self.assertEqual(res["batch"]["applied"], 1)
        self.assertEqual(res["batch"]["pending"], 2)

    def test_batch_item_validation(self) -> None:
        self._init()
        _run_cli(
            self._flags()
            + ["favorite-batch", "create", "--apply-at", "2026-11-27T09:30:00+08:00", "--batch-id", "b4"]
        )
        # 缺字段拒绝
        rc, err = _run_cli(
            self._flags()
            + ["favorite-batch", "add", "--batch-id", "b4", "--item-json", json.dumps({"company": "只有公司"})]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(err["code"], "INVALID_ITEM")

        # 同公司同岗位重复追加拒绝
        item = json.dumps({"company": "甲", "title": "AI产品经理"}, ensure_ascii=False)
        _run_cli(self._flags() + ["favorite-batch", "add", "--batch-id", "b4", "--item-json", item])
        rc, err_dup = _run_cli(
            self._flags() + ["favorite-batch", "add", "--batch-id", "b4", "--item-json", item]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(err_dup["code"], "ITEM_DUPLICATE")

    def test_create_rejects_naive_timestamp(self) -> None:
        self._init()
        rc, err = _run_cli(
            self._flags()
            + ["favorite-batch", "create", "--apply-at", "2026-11-27T09:30:00", "--batch-id", "b5"]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(err["code"], "NAIVE_TIMESTAMP")

    # -- 时间窗与门控 -------------------------------------------------------

    def test_gate_window_semantics_are_half_open(self) -> None:
        windows = hm_gate.parse_windows(["09:00-12:00", "14:00-18:00"])

        def at(hh: int, mm: int) -> "object":
            from datetime import datetime, timedelta, timezone

            return datetime(2026, 11, 27, hh, mm, tzinfo=timezone(timedelta(hours=8)))

        # 起点含、终点不含
        self.assertTrue(hm_gate.in_windows(at(9, 0), windows))
        self.assertTrue(hm_gate.in_windows(at(11, 59), windows))
        self.assertFalse(hm_gate.in_windows(at(12, 0), windows))
        self.assertFalse(hm_gate.in_windows(at(13, 30), windows))
        self.assertTrue(hm_gate.in_windows(at(14, 0), windows))
        self.assertFalse(hm_gate.in_windows(at(18, 0), windows))

    def test_gate_workday_and_batch_priority(self) -> None:
        self._init()
        # 周六：不在工作时间窗，但批次到点仍允许投递（批次不受工作窗限制）
        _run_cli(
            self._flags()
            + ["favorite-batch", "create", "--apply-at", "2026-11-28T09:00:00+08:00", "--batch-id", "b6"]
        )
        _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "add",
                "--batch-id",
                "b6",
                "--item-json",
                json.dumps({"company": "甲", "title": "AI产品经理"}, ensure_ascii=False),
            ]
        )

        rc, res = _run_cli(self._flags() + ["gate", "--now", "2026-11-28T10:00:00+08:00"])
        self.assertEqual(rc, 0)
        self.assertFalse(res["workday"])  # 2026-11-28 是周六
        self.assertFalse(res["in_active_window"])
        self.assertFalse(res["may_search_discover"])
        self.assertTrue(res["favorite_batch_due"])
        self.assertTrue(res["may_favorite_batch"])
        self.assertTrue(res["has_due_work"])

    def test_gate_quota_remaining_reflects_counted_only(self) -> None:
        self._init()
        self._upsert(company="常规公司", title="常规岗", status="opening_sent", channel="outbound")
        for idx in range(4):
            self._upsert(
                company=f"收藏{idx}",
                title=f"收藏岗{idx}",
                status="opening_sent",
                channel="favorite_queue",
            )
        rc, res = _run_cli(self._flags() + ["gate", "--now", "2026-11-27T10:00:00+08:00"])
        self.assertEqual(rc, 0)
        self.assertEqual(res["quota"]["counted_openings_today"], 1)
        self.assertEqual(res["quota"]["remaining_today"], 19)
        self.assertIn("favorite_queue", res["quota"]["exempt_channels"])

    def test_inbound_channel_counts_toward_quota(self) -> None:
        """被动来信不占「主动找岗」许可，但其外发仍属真实投递，计入额度口径。"""
        self._init()
        self._upsert(company="来信公司", title="被动岗", status="opening_sent", channel="inbound")
        summary = JobLedger(self.ws).summary()
        self.assertEqual(summary["today_by_channel"]["inbound"]["counted_opening_sent"], 1)
        self.assertEqual(summary["today_counted_opening_sent"], 1)


if __name__ == "__main__":
    unittest.main()
