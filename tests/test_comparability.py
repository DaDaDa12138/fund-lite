"""P0-2：指标可比性旗标与分位母体隔离。"""
import sqlite3
import unittest
from pathlib import Path

from fundana.metrics import comparability

MIGRATION = Path(__file__).resolve().parents[1] / "fundana" / "migrations" / "0027_metric_comparability.sql"

SCHEMA = """
CREATE TABLE fund(fcode TEXT PRIMARY KEY, name TEXT);
CREATE TABLE fund_holding_policy(fcode TEXT, lockup_type TEXT);
CREATE TABLE fund_coverage(fcode TEXT, run_id TEXT, nav_last_date TEXT);
CREATE TABLE fund_metric(fcode TEXT, period_days INTEGER, formula_contract TEXT,
                         mdd_dec REAL, vol_dec REAL, median_gap_days REAL, p75_gap_days REAL);
"""


class TestClassify(unittest.TestCase):
    def test_amortized_by_name(self):
        flags = comparability.classify(name="某基金87个月定开债", lockup_type="periodic_open",
                                       mdd_dec=0, vol_dec=0.001, median_gap_days=1,
                                       p75_gap_days=1, nav_last_date="2026-10-30",
                                       as_of="2026-10-30")
        self.assertIn("amortized_cost", flags)

    def test_amortized_by_lockup_and_zero_drawdown(self):
        flags = comparability.classify(name="某定开债", lockup_type="periodic_open",
                                       mdd_dec=0, vol_dec=0.002, median_gap_days=1,
                                       p75_gap_days=1, nav_last_date="2026-10-30",
                                       as_of="2026-10-30")
        self.assertIn("amortized_cost", flags)
        self.assertNotIn("zero_risk", flags)

    def test_zero_risk(self):
        flags = comparability.classify(name="某纯债", lockup_type="none",
                                       mdd_dec=0, vol_dec=0, median_gap_days=1,
                                       p75_gap_days=1, nav_last_date="2026-10-30",
                                       as_of="2026-10-30")
        self.assertEqual(flags, ["zero_risk"])

    def test_sparse_and_stale(self):
        flags = comparability.classify(name="某纯债", lockup_type="none",
                                       mdd_dec=1.0, vol_dec=0.5, median_gap_days=4,
                                       p75_gap_days=7, nav_last_date="2026-09-01",
                                       as_of="2026-10-30")
        self.assertIn("sparse_nav", flags)
        self.assertIn("stale_nav", flags)
        self.assertNotIn("zero_risk", flags)

    def test_comparable(self):
        flags = comparability.classify(name="某纯债", lockup_type="none",
                                       mdd_dec=1.0, vol_dec=0.5, median_gap_days=1,
                                       p75_gap_days=1, nav_last_date="2026-10-29",
                                       as_of="2026-10-30")
        self.assertEqual(flags, [])


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.executescript(MIGRATION.read_text(encoding="utf-8"))
        self.conn.execute("INSERT INTO fund VALUES('A','某87个月定开债')")
        self.conn.execute("INSERT INTO fund VALUES('B','某纯债')")
        self.conn.execute("INSERT INTO fund_holding_policy VALUES('A','periodic_open')")
        self.conn.execute("INSERT INTO fund_holding_policy VALUES('B','none')")
        self.conn.execute("INSERT INTO fund_coverage VALUES('A','r1','2026-10-30')")
        self.conn.execute("INSERT INTO fund_coverage VALUES('B','r1','2026-10-30')")
        self.conn.execute("INSERT INTO fund_metric VALUES('A',365,'x',0,0.001,1,1)")
        self.conn.execute("INSERT INTO fund_metric VALUES('B',365,'x',1.0,0.5,1,1)")

    def tearDown(self):
        self.conn.close()

    def test_refresh_and_load_latest(self):
        res = comparability.refresh(self.conn, as_of="2026-10-30", formula_contract="x")
        self.assertEqual(res["rows"], 2)
        self.assertEqual(res["comparable"], 1)
        flags = comparability.load_latest(self.conn, "2026-10-30")
        self.assertIn("amortized_cost", flags["A"])
        self.assertEqual(flags["B"], [])
        # 未来快照不应被 <=as_of 的查询读到。
        flags_old = comparability.load_latest(self.conn, "2026-10-29")
        self.assertEqual(flags_old, {})
