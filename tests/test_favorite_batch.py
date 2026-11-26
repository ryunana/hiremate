"""收藏批次延迟统一投递状态机、渠道额度分桶与 L0 安全底线回归测试。"""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
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
from hiremate_lib import channel as hm_channel  # noqa: E402
from hiremate_lib import dashboard as hm_dashboard  # noqa: E402
from hiremate_lib import favorite_batch as hm_batch  # noqa: E402
from hiremate_lib.ledger import JobLedger  # noqa: E402
from hiremate_lib.workspace import load_config, load_runtime_state, save_config, save_runtime_state  # noqa: E402


def _run_cli(args: list[str]) -> tuple[int, dict]:
    buf = StringIO()
    with redirect_stdout(buf):
        rc = hiremate.main(args)
    raw = buf.getvalue().strip()
    return rc, json.loads(raw) if raw else {}


class TestChannelBucketsAndFavoriteBatch(unittest.TestCase):
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

    def _init(self) -> None:
        rc, _ = _run_cli(
            self._flags()
            + [
                "init",
                "--account-name",
                "测试求职者",
                "--roles",
                "产品经理",
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

    # 1. channel 枚举与 inbound/favorite_queue 豁免
    def test_channel_enum_and_inbound_exemption(self) -> None:
        self.assertEqual(
            set(hm_channel.VALID_CHANNELS),
            {"outbound", "inbound", "favorite_queue"},
        )
        self.assertEqual(hm_channel.normalize_channel("unknown_legacy"), "outbound")
        self.assertEqual(hm_channel.normalize_channel(""), "outbound")
        self.assertTrue(hm_channel.counts_toward_quota("outbound"))
        self.assertFalse(hm_channel.counts_toward_quota("inbound"))
        self.assertFalse(hm_channel.counts_toward_quota("favorite_queue"))
        self.assertEqual(hm_channel.channel_label("favorite_queue"), "收藏批次补投")

    # 2. inbound 回填 opening_sent_at 不扣主动投递额度 & 禁止外部伪造 favorite_queue
    def test_inbound_backfill_and_anti_spoof_favorite_queue(self) -> None:
        self._init()
        # 被动来信建档并进入 connected（会回填 opening_sent_at）
        self._upsert(company="来信公司", title="产品经理", status="connected", channel="inbound")
        # 常规主动找岗
        self._upsert(company="主动公司", title="产品经理", status="opening_sent", channel="outbound")
        # 试图直接通过 upsert-job 伪造 favorite_queue 逃额度：必须被降级为 outbound
        spoof_id = self._upsert(
            company="逃额度公司",
            title="产品经理",
            status="opening_sent",
            channel="favorite_queue",
        )
        led = JobLedger(self.ws)
        spoof_job = led.get_job(spoof_id)
        self.assertIsNotNone(spoof_job)
        self.assertEqual(spoof_job["channel"], "outbound")

        summary = led.summary()
        # outbound 2 条计入额度；inbound 1 条不计入主动找岗额度
        self.assertEqual(summary["today_counted_opening_sent"], 2)
        self.assertEqual(summary["today_by_channel"]["inbound"]["counted_opening_sent"], 0)
        self.assertEqual(summary["today_by_channel"]["inbound"]["opening_sent"], 1)
        self.assertIn("inbound", summary["quota_exempt_channels"])

    # 3. 端到端：favorited -> favorite-batch -> claim-next -> mark applied -> 同事务更新账本 + 不扣常规额度
    def test_end_to_end_favorited_to_batch_applied_syncs_ledger(self) -> None:
        self._init()
        job_id = self._upsert(
            company="示例科技",
            title="高级产品经理",
            status="favorited",
            channel="outbound",
            timestamp="2026-11-26T22:00:00+08:00",
        )
        apply_at = "2026-11-27T09:30:00+08:00"
        rc, created = _run_cli(
            self._flags()
            + ["favorite-batch", "create", "--apply-at", apply_at, "--batch-id", "b_e2e"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(created["batch"]["status"], hm_batch.BATCH_PENDING)

        rc, added = _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "add",
                "--batch-id",
                "b_e2e",
                "--item-json",
                json.dumps({"job_id": job_id}, ensure_ascii=False),
            ]
        )
        self.assertEqual(rc, 0)
        item_id = added["item_id"]

        # 未到 apply_at 强拒绝
        rc, err = _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "claim-next",
                "--batch-id",
                "b_e2e",
                "--now",
                "2026-11-27T09:10:00+08:00",
            ]
        )
        self.assertEqual(rc, 1)
        self.assertEqual(err["code"], "BATCH_NOT_DUE")

        # 到点后认领
        rc, claimed = _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "claim-next",
                "--batch-id",
                "b_e2e",
                "--now",
                "2026-11-27T09:31:00+08:00",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(claimed["next_item"]["item_id"], item_id)
        self.assertEqual(claimed["next_item"]["job_id"], job_id)

        # mark applied：同事务更新账本为 opening_sent + channel=favorite_queue
        rc, marked = _run_cli(
            self._flags()
            + [
                "favorite-batch",
                "mark",
                "--batch-id",
                "b_e2e",
                "--item-id",
                item_id,
                "--outcome",
                "applied",
                "--now",
                "2026-11-27T09:31:20+08:00",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue(marked["ledger_synced"])
        self.assertEqual(marked["batch"]["status"], hm_batch.BATCH_COMPLETED)

        led = JobLedger(self.ws)
        job = led.get_job(job_id)
        self.assertEqual(job["status"], "opening_sent")
        self.assertEqual(job["channel"], "favorite_queue")

        summary = led.summary(local_date_prefix="2026-11-27")
        self.assertEqual(summary["today_opening_sent"], 1)
        self.assertEqual(summary["today_counted_opening_sent"], 0)
        self.assertEqual(summary["today_by_channel"]["favorite_queue"]["opening_sent"], 1)

        # 看板与导出均展示「收藏批次补投」
        dash_res = hm_dashboard.build_dashboard_file(workspace=self.ws)
        html_text = Path(dash_res["out_path"]).read_text(encoding="utf-8")
        self.assertIn("收藏批次补投", html_text)

    # 4. 并发租约防重复发消息 & 租约过期回收
    def test_claim_lease_prevents_duplicate_and_reclaims_after_expiry(self) -> None:
        self._init()
        job_id = self._upsert(company="并发公司", title="后端开发", status="favorited")
        hm_batch.create_batch(self.ws, batch_id="b_lease")
        hm_batch.add_item(self.ws, "b_lease", {"job_id": job_id})

        t0 = datetime(2026, 11, 27, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        first = hm_batch.claim_next(self.ws, "b_lease", now=t0)
        self.assertIsNotNone(first["next_item"])

        # 1 分钟后第二个宿主并发抢占：拿到 None（in_flight_count=1），绝不重复发放同一条
        second = hm_batch.claim_next(self.ws, "b_lease", now=t0 + timedelta(minutes=1))
        self.assertIsNone(second["next_item"])
        self.assertEqual(second["in_flight_count"], 1)

        # 11 分钟后租约过期：自动回收并重新认领
        third = hm_batch.claim_next(self.ws, "b_lease", now=t0 + timedelta(minutes=11))
        self.assertIsNotNone(third["next_item"])
        self.assertEqual(third["next_item"]["job_id"], job_id)

    # 5. skip 后重新 add 同岗位不死锁 & pause/resume/cancel 终态保护
    def test_skip_readd_no_deadlock_and_pause_resume_cancel(self) -> None:
        self._init()
        j1 = self._upsert(company="大厂A", title="产品经理", status="favorited")
        j2 = self._upsert(company="大厂A", title="产品经理", status="favorited")  # 同公司同标题不同岗位
        hm_batch.create_batch(self.ws, batch_id="b_life")
        r1 = hm_batch.add_item(self.ws, "b_life", {"job_id": j1})
        r2 = hm_batch.add_item(self.ws, "b_life", {"job_id": j2})
        self.assertNotEqual(r1["item_id"], r2["item_id"])

        t0 = datetime(2026, 11, 27, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        # 先跳过 j1
        hm_batch.mark_item(self.ws, "b_life", item_id=r1["item_id"], outcome="skipped", reason="暂时跳过", now=t0)
        # 重新加入 j1，应能被正确标记为 applied 而不会命中旧 skipped 项死锁
        r1_new = hm_batch.add_item(self.ws, "b_life", {"job_id": j1}, now=t0)
        hm_batch.mark_item(self.ws, "b_life", item_id=r1_new["item_id"], outcome="applied", now=t0)

        # pause 保留断点，resume 恢复后可继续 claim j2
        paused = hm_batch.pause_batch(self.ws, "b_life", reason="滑块验证", now=t0 + timedelta(minutes=2))
        self.assertEqual(paused["status"], hm_batch.BATCH_PAUSED)
        with self.assertRaises(hm_batch.BatchGateError) as ctx:
            hm_batch.claim_next(self.ws, "b_life", now=t0 + timedelta(minutes=3))
        self.assertEqual(ctx.exception.code, "BATCH_PAUSED")

        resumed = hm_batch.resume_batch(self.ws, "b_life", now=t0 + timedelta(minutes=4))
        self.assertEqual(resumed["status"], hm_batch.BATCH_APPLYING)
        nxt = hm_batch.claim_next(self.ws, "b_life", now=t0 + timedelta(minutes=5))
        self.assertEqual(nxt["next_item"]["job_id"], j2)

        # cancel 进入终态，之后禁止 mark / pause / add
        cancelled = hm_batch.cancel_batch(self.ws, "b_life", reason="用户取消", now=t0 + timedelta(minutes=6))
        self.assertEqual(cancelled["status"], hm_batch.BATCH_CANCELLED)
        with self.assertRaises(hm_batch.BatchGateError) as ctx2:
            hm_batch.pause_batch(self.ws, "b_life", now=t0 + timedelta(minutes=7))
        self.assertEqual(ctx2.exception.code, "BATCH_TERMINAL")

    # 6. L0 安全底线：宵禁、抖动间隔、每日硬顶与云端预算收敛
    def test_l0_safety_curfew_throttle_and_budget_carry_over(self) -> None:
        self._init()
        j1 = self._upsert(company="安全公司1", title="算法", status="favorited")
        j2 = self._upsert(company="安全公司2", title="算法", status="favorited")
        hm_batch.create_batch(self.ws, batch_id="b_safe")
        hm_batch.add_item(self.ws, "b_safe", {"job_id": j1})
        hm_batch.add_item(self.ws, "b_safe", {"job_id": j2})

        # 夜间 22:00 触发宵禁 BATCH_CURFEW
        night = datetime(2026, 11, 27, 22, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        with self.assertRaises(hm_batch.BatchGateError) as ctx:
            hm_batch.claim_next(self.ws, "b_safe", now=night)
        self.assertEqual(ctx.exception.code, "BATCH_CURFEW")
        self.assertIn("next_allowed_at", ctx.exception.extra)

        # 白天 10:00 认领并投递第 1 条
        day = datetime(2026, 11, 27, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        c1 = hm_batch.claim_next(self.ws, "b_safe", now=day)
        hm_batch.mark_item(
            self.ws,
            "b_safe",
            item_id=c1["next_item"]["item_id"],
            outcome="applied",
            now=day + timedelta(seconds=5),
        )

        # 10 秒后立即抢第 2 条：触发代码层抖动间隔拦截 BATCH_THROTTLED
        with self.assertRaises(hm_batch.BatchGateError) as ctx_th:
            hm_batch.claim_next(self.ws, "b_safe", now=day + timedelta(seconds=15))
        self.assertEqual(ctx_th.exception.code, "BATCH_THROTTLED")
        self.assertGreater(ctx_th.exception.extra["retry_after_seconds"], 0)

        # 模拟云端下发预算为 0 -> 超额顺延 carry_over=1
        rt = load_runtime_state(self.ws)
        rt["favorite_batch_budget"] = 0
        rt["favorite_batch_budget_updated_at"] = (day + timedelta(minutes=2)).isoformat()
        save_runtime_state(self.ws, rt)

        c2 = hm_batch.claim_next(self.ws, "b_safe", now=day + timedelta(minutes=3))
        self.assertIsNone(c2["next_item"])
        self.assertEqual(c2["carry_over"], 1)
        self.assertEqual(c2["reason"], "DAILY_SAFETY_CAP_REACHED")

        # 验证 auto_risk_acknowledged 放宽硬顶 30 -> 40
        cfg = load_config(self.ws)
        self.assertEqual(hm_batch.quota_state(self.ws, now=day)["hard_daily_cap"], 30)
        cfg["auto_risk_acknowledged"] = True
        save_config(self.ws, cfg)
        self.assertEqual(hm_batch.quota_state(self.ws, now=day)["hard_daily_cap"], 40)

    # 7. 周末规则与 naive 时间戳拒绝
    def test_weekend_policy_and_naive_timestamp_rejected(self) -> None:
        self._init()
        with self.assertRaises(hm_batch.BatchGateError) as ctx:
            hm_batch.create_batch(self.ws, apply_at="2026-11-28T10:00:00")
        self.assertEqual(ctx.exception.code, "NAIVE_TIMESTAMP")

        j1 = self._upsert(company="周末公司", title="运营", status="favorited")
        # 显式指定周六 apply_at -> 在 08:30-21:30 宵禁内允许投递
        hm_batch.create_batch(self.ws, apply_at="2026-11-28T09:30:00+08:00", batch_id="b_wknd")
        hm_batch.add_item(self.ws, "b_wknd", {"job_id": j1})
        sat = datetime(2026, 11, 28, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        res = hm_batch.claim_next(self.ws, "b_wknd", now=sat)
        self.assertEqual(res["next_item"]["job_id"], j1)

    # 8. check-dup 重复投递分层：超 7 天未回默认允许跟进 vs 近期跳过 vs 明确拒绝永久跳过
    def test_check_dup_stale_no_reply_allows_followup_and_rejected_stays_blocked(self) -> None:
        self._init()
        led = JobLedger(self.ws)
        # 1) 10 天前投递且未回复 -> 默认允许二次跟进
        led.upsert_job(
            {
                "company": "跟进科技",
                "title": "产品经理",
                "status": "opening_sent",
                "channel": "outbound",
                "timestamp": "2026-11-15T10:00:00+08:00",
            }
        )
        res_old = led.check_duplicate(
            company="跟进科技",
            title="产品经理",
            now_iso_override="2026-11-26T10:00:00+08:00",
        )
        self.assertFalse(res_old["duplicate"])
        self.assertTrue(res_old["allow_followup"])
        self.assertEqual(res_old["reapply_policy"], "allow_followup_after_no_reply")
        self.assertGreaterEqual(res_old["days_since_opening"], 10)

        # 2) 2 天前刚投递未回 -> 防刷屏仍跳过
        led.upsert_job(
            {
                "company": "近期科技",
                "title": "产品经理",
                "status": "opening_sent",
                "channel": "outbound",
                "timestamp": "2026-11-24T10:00:00+08:00",
            }
        )
        res_recent = led.check_duplicate(
            company="近期科技",
            title="产品经理",
            now_iso_override="2026-11-26T10:00:00+08:00",
        )
        self.assertTrue(res_recent["duplicate"])
        self.assertFalse(res_recent["allow_followup"])

        # 3) 对方明确拒绝（即使过了 20 天）-> 永久跳过
        led.upsert_job(
            {
                "company": "拒信科技",
                "title": "产品经理",
                "status": "closed_rejected",
                "channel": "outbound",
                "timestamp": "2026-11-01T10:00:00+08:00",
            }
        )
        res_rej = led.check_duplicate(
            company="拒信科技",
            title="产品经理",
            now_iso_override="2026-11-26T10:00:00+08:00",
        )
        self.assertTrue(res_rej["duplicate"])
        self.assertFalse(res_rej["allow_followup"])
        self.assertEqual(res_rej["reapply_policy"], "permanent_skip_rejected")

    # 9. --rules-file 挂载并在 config show / checklist 输出路径、哈希与正文
    def test_execution_rules_file_mount_in_config_and_checklist(self) -> None:
        self._init()
        rules_path = self.root / "my-rules.md"
        rules_path.write_text("# 我的自定义筛岗规则\n- 优先业务直招\n", encoding="utf-8")

        rc, _ = _run_cli(self._flags() + ["config", "set", "--rules-file", str(rules_path)])
        self.assertEqual(rc, 0)

        rc, show = _run_cli(self._flags() + ["config", "show"])
        self.assertEqual(rc, 0)
        self.assertTrue(show["execution_rules_ready"])
        self.assertEqual(show["execution_rules_file"], str(rules_path.resolve()))
        self.assertEqual(len(show["execution_rules_sha256"]), 12)
        self.assertIn("优先业务直招", show["execution_rules_text"])

        rc, chk = _run_cli(self._flags() + ["checklist"])
        self.assertEqual(rc, 0)
        self.assertTrue(chk["execution_rules_ready"])
        self.assertEqual(chk["execution_rules_sha256"], show["execution_rules_sha256"])


if __name__ == "__main__":
    unittest.main()
