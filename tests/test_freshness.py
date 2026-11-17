"""数据新鲜度：有效净值日、覆盖率与指标覆盖的回归测试。

背景：2026-11-03 只有 85.5% 的基金披露 11-03 净值，MAX(nav_date) 会把
市场数据日误判为 11-03；这些测试锁住“覆盖率 >= 90% 的最新日”这一口径。
"""
from __future__ import annotations

import sqlite3
import unittest

from fundana import daily, freshness


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE fund(fcode TEXT PRIMARY KEY, type_group TEXT);
            CREATE TABLE fund_nav_daily(fcode TEXT, nav_date TEXT, unit_nav REAL);
            CREATE TABLE fund_metric(fcode TEXT, period_days INTEGER, end_date TEXT,
                                     formula_contract TEXT);
        """)
        for i in range(10):
            code = "F%02d" % i
            self.conn.execute("INSERT INTO fund VALUES (?, 'bond')", (code,))
            for d in ("2026-10-30", "2026-11-02"):
                self.conn.execute("INSERT INTO fund_nav_daily VALUES (?,?,1.0)", (code, d))
            self.conn.execute("INSERT INTO fund_metric VALUES (?,365,'2026-11-02','risk')", (code,))
        # 11-03 只有一半基金披露：这是“最新日”，但不是“有效日”。
        for i in range(5):
            self.conn.execute("INSERT INTO fund_nav_daily VALUES (?, '2026-11-03', 1.0)",
                              ("F%02d" % i,))

    def tearDown(self):
        self.conn.close()

    def test_partial_latest_is_not_effective_date(self):
        nav = freshness.nav_coverage(self.conn, as_of="2026-11-03")
        self.assertEqual(nav["effective_date"], "2026-11-02")
        self.assertEqual(nav["max_date"], "2026-11-03")
        self.assertEqual(nav["effective_coverage_pct"], 100.0)
        self.assertEqual(nav["max_coverage_pct"], 50.0)
        self.assertEqual(nav["lag_days"], 1)
        self.assertEqual(nav["status"], "ok")

    def test_low_coverage_every_day_has_no_effective_date(self):
        self.conn.execute("DELETE FROM fund_nav_daily WHERE nav_date='2026-11-02' AND fcode NOT IN ('F00','F01','F02','F03','F04')")
        nav = freshness.nav_coverage(self.conn, as_of="2026-11-03")
        self.assertEqual(nav["effective_date"], "2026-10-30")
        self.assertEqual(nav["effective_coverage_pct"], 100.0)

    def test_liquidated_funds_excluded_from_current_coverage(self):
        self.conn.execute("ALTER TABLE fund ADD COLUMN fund_status TEXT")
        self.conn.execute("INSERT INTO fund VALUES ('L01','bond','liquidated')")
        self.conn.execute("INSERT INTO fund_nav_daily VALUES ('L01','2020-01-02',1.0)")
        nav = freshness.nav_coverage(self.conn, as_of="2026-11-03")
        self.assertEqual(nav["universe"], 10)          # L01 不计入分母
        self.assertEqual(nav["effective_date"], "2026-11-02")
        cov = freshness.metric_coverage(self.conn, "2026-11-02", formula_contract="risk")
        self.assertEqual(cov["denominator"], 10)

    def test_metric_coverage_current_and_stale(self):
        cov = freshness.metric_coverage(self.conn, "2026-11-02", formula_contract="risk")
        self.assertEqual(cov["status"], "ok")
        self.assertEqual(cov["current_pct"], 100.0)
        self.assertEqual(cov["denominator"], 10)

        self.conn.execute("UPDATE fund_metric SET end_date='2026-11-01' WHERE fcode='F08'")
        self.conn.execute("UPDATE fund_metric SET end_date='2026-10-01' WHERE fcode='F09'")
        cov = freshness.metric_coverage(self.conn, "2026-11-02", formula_contract="risk")
        self.assertEqual(cov["status"], "partial")
        self.assertEqual(cov["current_pct"], 80.0)
        self.assertEqual(cov["fresh_pct"], 10.0)
        self.assertEqual(cov["older_pct"], 10.0)

    def test_assess_reports_partial_latest_and_behind_business_date(self):
        out = freshness.assess(self.conn, business_date="2026-11-03",
                               formula_contract="risk")
        self.assertEqual(out["as_of"], "2026-11-02")
        self.assertEqual(out["status"], "ok")
        self.assertIn("nav_partial_latest", out["warnings"])
        self.assertIn("nav_behind_business_date", out["warnings"])
        self.assertEqual(out["nav"]["business_lag_days"], 1)

    def test_assess_blocks_when_metrics_missing(self):
        self.conn.execute("DELETE FROM fund_metric")
        out = freshness.assess(self.conn, business_date="2026-11-03",
                               formula_contract="risk")
        self.assertEqual(out["status"], "blocked")
        self.assertIn("metric_missing", out["warnings"])
        self.assertIn("metric_not_current", out["warnings"])


class DailyBatchTest(unittest.TestCase):
    def test_batch_id_includes_contract_version(self):
        contract = daily.screen_engine.T.SCREEN_CONTRACT
        version = contract.split("-")[2]
        self.assertEqual(daily.batch_id_for("2026-11-02"), "daily-2026-11-02-" + version)
        self.assertNotEqual(daily.batch_id_for("2026-11-02"), "2026-11-02")


if __name__ == "__main__":
    unittest.main()
