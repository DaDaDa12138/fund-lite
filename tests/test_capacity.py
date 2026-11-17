"""P2-11 规模容量曲线契约测试：只给研究区间，不设硬上限。"""
import sqlite3
import unittest

from fundana import capacity, db


class TestCapacity(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)

    def _fund(self, code, style, scale, annual_1y=None, annual_3y=None):
        self.conn.execute(
            "INSERT INTO fund_style(fcode, style_code, confidence, updated_at)"
            " VALUES(?,?,?,?)", (code, style, "structured_confirmed", "2026-11-04"))
        self.conn.execute(
            "INSERT INTO fund_scale_history(fcode, report_period, asset_scale_billion,"
            " share_scale_billion, src) VALUES(?,?,?,?,?)",
            (code, "2026-06-30", scale, scale, "test"))
        for period, annual in ((365, annual_1y), (1095, annual_3y)):
            if annual is None:
                continue
            self.conn.execute(
                "INSERT INTO fund_metric(fcode, period_days, formula_contract,"
                " end_date, annual_dec, sharpe, mdd_dec) VALUES(?,?,?,?,?,?,?)",
                (code, period, "c1", "2026-11-04", annual, 1.5, -0.01))

    def test_style_bucket_medians_and_counts(self):
        for i in range(5):
            self._fund("s%02d" % i, "bond_pure", 1.0, 0.02, 0.03)
        for i in range(5):
            self._fund("l%02d" % i, "bond_pure", 12.0, 0.03, 0.04)
        self.conn.commit()

        out = capacity.summary(self.conn, None, as_of="2026-11-04", contract="c1")
        self.assertEqual(out["funds"], 10)
        pure = [s for s in out["styles"] if s["style_code"] == "bond_pure"][0]
        small = [b for b in pure["buckets"] if b["bucket"] == "<2 亿"][0]
        large = [b for b in pure["buckets"] if b["bucket"] == "10–20 亿"][0]
        self.assertEqual(small["n"], 5)
        self.assertAlmostEqual(small["annual_1y_pct"], 2.0, places=4)
        self.assertAlmostEqual(large["annual_1y_pct"], 3.0, places=4)
        self.assertAlmostEqual(large["annual_3y_pct"], 4.0, places=4)
        # 没有样本的桶直接不出现，不能用空桶或 1–2 只基金编造规模效应
        self.assertNotIn("≥50 亿", [b["bucket"] for b in pure["buckets"]])
        self.assertIn("不是规模上限", out["note"])

    def test_bucket_below_min_count_returns_null(self):
        for i in range(4):
            self._fund("x%02d" % i, "bond_short", 3.0, 0.02, 0.03)
        self.conn.commit()
        out = capacity.summary(self.conn, None, as_of="2026-11-04", contract="c1")
        short = [s for s in out["styles"] if s["style_code"] == "bond_short"][0]
        cell = [b for b in short["buckets"] if b["bucket"] == "2–5 亿"][0]
        self.assertEqual(cell["n"], 4)
        self.assertIsNone(cell["annual_1y_pct"])

    def test_bucket_boundaries(self):
        self.assertEqual(capacity._bucket(2.0), "2–5 亿")
        self.assertEqual(capacity._bucket(5.0), "5–10 亿")
        self.assertEqual(capacity._bucket(50.0), "≥50 亿")


if __name__ == "__main__":
    unittest.main()
