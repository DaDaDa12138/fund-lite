"""调度器：07:30 触发与重启补跑。"""
import sqlite3
import subprocess
import unittest
from datetime import datetime
from unittest.mock import patch

from fundana import scheduler


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE run(id INTEGER PRIMARY KEY, mode TEXT, status TEXT, started_at TEXT)")

    def tearDown(self):
        self.conn.close()

    def test_next_run_at_skips_weekend(self):
        # 2026-11-06 是周五；07:29 应排到当天 07:30，07:31 排到下周一。
        self.assertEqual(scheduler.next_run_at(datetime(2026, 11, 6, 7, 29)).isoformat(),
                         "2026-11-06T07:30:00")
        self.assertEqual(scheduler.next_run_at(datetime(2026, 11, 6, 7, 31)).isoformat(),
                         "2026-11-09T07:30:00")
        self.assertEqual(scheduler.next_run_at(datetime(2026, 11, 7, 9, 0)).isoformat(),
                         "2026-11-09T07:30:00")

    def test_next_run_at_skips_national_day_holiday(self):
        # 2026-10-01 至 10-07 休市，节前最后一班应排到 10-08（周四）。
        self.assertEqual(scheduler.next_run_at(datetime(2026, 9, 30, 8, 0)).isoformat(),
                         "2026-10-08T07:30:00")
        self.assertFalse(scheduler.needs_catchup(self.conn, datetime(2026, 10, 1, 9, 0)))

    def test_needs_catchup_only_after_time_and_without_success(self):
        self.assertFalse(scheduler.needs_catchup(self.conn, datetime(2026, 11, 6, 7, 29)))
        self.assertTrue(scheduler.needs_catchup(self.conn, datetime(2026, 11, 6, 7, 31)))
        self.conn.execute("INSERT INTO run(mode,status,started_at) VALUES('daily','success','2026-11-06T07:30:01+08:00')")
        self.assertFalse(scheduler.needs_catchup(self.conn, datetime(2026, 11, 6, 7, 31)))

    def test_failed_run_is_retried_and_weekend_is_skipped(self):
        self.conn.execute("INSERT INTO run(mode,status,started_at) VALUES('daily','failed','2026-11-06T07:30:01+08:00')")
        self.assertTrue(scheduler.needs_catchup(self.conn, datetime(2026, 11, 6, 7, 31)))
        self.assertFalse(scheduler.needs_catchup(self.conn, datetime(2026, 11, 7, 9, 0)))

    def test_heartbeat_keeps_history_and_snapshot(self):
        import tempfile
        from pathlib import Path

        class Cfg:
            def get(self, section, key, default=None, **_kw):
                if section == "paths" and key == "state_dir":
                    return str(Path(tmp) / "state")
                return default

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Cfg()
            for i in range(3):
                scheduler.heartbeat(cfg, {"last_status": "ok", "i": i})
            hist = scheduler.read_heartbeat_history(cfg, limit=2)
            self.assertEqual([x["i"] for x in hist], [1, 2])
            self.assertEqual(scheduler.read_heartbeat(cfg)["i"], 2)

    def test_daily_runs_in_subprocess_with_quiet_and_force(self):
        proc = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"status":"ok","run_id":"run-x"}', stderr="")
        with patch.object(scheduler.subprocess, "run", return_value=proc) as run:
            out = scheduler._run_daily_subprocess(force=True)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["run_id"], "run-x")
        cmd = run.call_args.args[0]
        self.assertIn("--quiet", cmd)
        self.assertIn("--force", cmd)
        self.assertEqual(run.call_args.kwargs["cwd"], str(scheduler.PROJECT_ROOT))

    def test_retry_then_success_alerts_only_once(self):
        class Cfg:
            def get(self, section, key, default=None, **_kw):
                return {"max_attempts": 3, "retry_delay_s": 1}.get(key, default)

        calls = []
        def fake_run(force=False, cfg=None):
            calls.append(1)
            if len(calls) == 1:
                return {"status": "failed", "stage": "snapshot"}
            return {"status": "ok"}

        sleeps = []
        with patch.object(scheduler, "_run_daily_subprocess", side_effect=fake_run), \
                patch.object(scheduler, "_alert_failure") as alert:
            out = scheduler._run_with_retries(Cfg(), sleep=sleeps.append)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["attempts"], 2)
        self.assertEqual(sleeps, [1])
        alert.assert_not_called()

    def test_publish_failure_is_not_retried_but_alerts(self):
        class Cfg:
            def get(self, section, key, default=None, **_kw):
                return {"max_attempts": 3, "retry_delay_s": 1}.get(key, default)

        with patch.object(scheduler, "_run_daily_subprocess",
                          return_value={"status": "failed", "stage": "publish"}) as run, \
                patch.object(scheduler, "_alert_failure") as alert:
            out = scheduler._run_with_retries(Cfg(), sleep=lambda _s: None)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(out["retry_stopped"], "publish_stage")
        alert.assert_called_once()

    def test_alert_without_config_does_not_touch_production(self):
        with patch.object(scheduler.notify, "send") as send:
            scheduler._alert_failure(None, {"status": "failed"})
        send.assert_not_called()

    def test_daily_subprocess_failure_is_not_reported_ok(self):
        proc = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom")
        with patch.object(scheduler.subprocess, "run", return_value=proc):
            out = scheduler._run_daily_subprocess()
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["exit_code"], 1)
        self.assertIn("boom", out["stderr_tail"])


if __name__ == "__main__":
    unittest.main()
