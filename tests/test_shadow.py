"""增强债影子榜契约测试：换窗口复算、research_only、产物落盘。"""
import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fundana import db
from fundana.screen import shadow


class StubCfg:
    def __init__(self, root):
        self.report_root = str(root)


class TestShadow(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = StubCfg(self.tmp.name)
        self.contract = "test-contract"
        self.conn.execute(
            "INSERT INTO fund(fcode, name, type_group, identity_status) VALUES(?,?,?,?)",
            ("000001", "测试转债A", "bond", "ok"))
        self.conn.execute("INSERT INTO fund_style(fcode, style_code, confidence) VALUES(?,?,?)",
                          ("000001", "bond_convertible", "text_heuristic"))
        self.conn.execute("INSERT INTO fund_profile(fcode, recurring_fee_pct, asset_scale_billion)"
                          " VALUES(?,?,?)", ("000001", 0.40, 5.0))
        self.conn.execute(
            "INSERT INTO fund_metric(fcode, period_days, formula_contract, annual_dec, sharpe,"
            " calmar_dec, mdd_dec, positive_ratio_pct) VALUES(?,?,?,?,?,?,?,?)",
            ("000001", 1095, self.contract, 0.05, 1.5, 4.0, 0.02, 60.0))
        self.conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_must_be_supported(self):
        with self.assertRaises(ValueError):
            shadow.run_shadow(self.conn, self.cfg, self.contract, window_days=1000)

    def test_passing_fund_is_full_pass_and_csv_written(self):
        out = shadow.run_shadow(self.conn, self.cfg, self.contract, window_days=1095, limit=10)
        self.assertTrue(out["meta"]["research_only"])
        self.assertEqual(out["meta"]["entered"], 1)
        self.assertEqual(out["meta"]["full_pass"], 1)
        self.assertEqual(out["rows"][0]["fcode"], "000001")
        self.assertEqual(out["rows"][0]["checks_passed"], 7)
        path = Path(self.cfg.report_root) / "analysis" / "enhanced_shadow_1095d.csv"
        self.assertTrue(path.exists())
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["fcode"], "000001")
        self.assertEqual(rows[0]["failed"], "")

    def test_missing_metrics_counts_as_failed_not_zero(self):
        self.conn.execute("DELETE FROM fund_metric")
        self.conn.commit()
        out = shadow.run_shadow(self.conn, self.cfg, self.contract, window_days=1095)
        self.assertEqual(out["meta"]["full_pass"], 0)
        self.assertIsNone(out["rows"][0]["annual_pct"])
        self.assertIn("annual_1y", out["rows"][0]["failed"])
