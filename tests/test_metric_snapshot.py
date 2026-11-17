"""P2-3 季度指标快照测试。"""
import sqlite3
import unittest
from unittest import mock

from fundana.audit import metric_snapshot
from fundana.config import load_config


class TestMetricSnapshot(unittest.TestCase):
    def test_quarter_ends(self):
        self.assertEqual(metric_snapshot.quarter_ends("2026-09-30", 3),
                         ["2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"][-3:])

    def test_snapshot_writes_only_snapshot_table(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE fund_metric_snapshot(
            fcode TEXT, as_of TEXT, period_days INTEGER, formula_contract TEXT,
            start_date TEXT, end_date TEXT, n_obs INTEGER, years REAL, annual_dec REAL,
            vol_dec REAL, mdd_dec REAL, sharpe REAL, sortino REAL, calmar_dec REAL,
            positive_ratio_pct REAL, non_negative_ratio_pct REAL, flat_ratio_pct REAL,
            coverage_ratio REAL, nav_sig TEXT, computed_at TEXT,
            PRIMARY KEY(fcode,as_of,period_days,formula_contract))""")
        cfg = load_config()
        fake = {"ok": True, "nav_sig": "sig", "dry": {365: {
            "start_date": "2025-09-30", "end_date": "2026-09-30", "n_obs": 242,
            "years": 1.0, "annual_dec": 0.03, "vol_dec": 0.01, "mdd_dec": 0.005,
            "sharpe": 1.2, "sortino": 1.5, "calmar_dec": 3.0,
            "positive_ratio_pct": 60.0, "non_negative_ratio_pct": 72.0,
            "flat_ratio_pct": 12.0}}}
        with mock.patch.object(metric_snapshot.metrics_engine, "compute_fund", return_value=fake):
            out = metric_snapshot.snapshot_as_of(conn, cfg, "2026-09-30",
                                                 fcodes=["A"], periods=(365,))
        self.assertEqual(out["rows"], 1)
        row = conn.execute("SELECT * FROM fund_metric_snapshot").fetchone()
        self.assertEqual(row["fcode"], "A")
        self.assertEqual(row["annual_dec"], 0.03)
        self.assertAlmostEqual(row["coverage_ratio"], 242 / 252, places=4)


if __name__ == "__main__":
    unittest.main()
