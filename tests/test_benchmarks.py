"""基准测试：真实中债指数 + ETF 代理，只读真实表，不生成价格。"""
import sqlite3
import unittest

from fundana import benchmarks
from fundana.publish import report_v2


class TestBenchmarks(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE fund_metric(fcode TEXT, formula_contract TEXT, period_days INT,"
            " annual_dec REAL, mdd_dec REAL, end_date TEXT)")
        self.conn.execute(
            "CREATE TABLE market_index_daily(index_code TEXT, trade_date TEXT, close REAL,"
            " pct_chg REAL, source_id TEXT, raw_sha256 TEXT, observed_at TEXT,"
            " PRIMARY KEY(index_code, trade_date))")

    def test_style_mapping_is_etf_proxy(self):
        b = benchmarks.for_style("bond_short")
        self.assertEqual(b["code"], "511360")
        self.assertEqual(b["type"], "etf_proxy")
        self.assertIsNone(benchmarks.for_style("bond_unknown"))

    def test_metrics_missing_returns_none_not_zero(self):
        self.assertIsNone(benchmarks.metrics(self.conn, "511220", "c1"))

    def test_summary_compares_peer_median_to_etf(self):
        rows = [("511220", "c1", 365, 0.030, 0.01, "2026-10-30"),
                ("511220", "c1", 1095, 0.025, 0.02, "2026-10-30")]
        self.conn.executemany("INSERT INTO fund_metric VALUES(?,?,?,?,?,?)", rows)
        formal = [{"style_code": "bond_pure", "annual_1y_pct": 4.0, "annual_3y_pct": 3.5,
                   "annual_5y_pct": 3.0},
                  {"style_code": "bond_pure", "annual_1y_pct": 5.0, "annual_3y_pct": 4.5,
                   "annual_5y_pct": 4.0}]
        got = benchmarks.summary(self.conn, "c1", formal)
        self.assertEqual(got["type"], "etf_proxy")
        row = got["rows"][0]
        self.assertEqual(row["benchmark"]["code"], "511220")
        self.assertAlmostEqual(row["peer_median_1y_pct"], 4.5)
        self.assertAlmostEqual(row["excess_1y_pct"], 1.5, places=2)
        self.assertAlmostEqual(row["excess_3y_pct"], 1.5)
        self.assertIn("不是中债/中证总回报指数", got["note"])

    def test_real_index_metrics_and_summary_prefers_index(self):
        rows = [("CBC_CFZS_00", "2021-11-04", 80.0, None, "cb_new_composite", None, "x"),
                ("CBC_CFZS_00", "2023-11-04", 90.0, None, "cb_new_composite", None, "x"),
                ("CBC_CFZS_00", "2025-11-04", 100.0, None, "cb_new_composite", None, "x"),
                ("CBC_CFZS_00", "2026-11-04", 103.0, None, "cb_new_composite", None, "x")]
        self.conn.executemany("INSERT INTO market_index_daily VALUES(?,?,?,?,?,?,?)", rows)
        got = benchmarks.index_metrics(self.conn, as_of="2026-11-04")
        self.assertEqual(got["type"], "real_index")
        self.assertAlmostEqual(got["annual_1y"], 3.0, places=2)
        self.assertAlmostEqual(got["annual_3y"], 4.6, places=1)
        self.assertAlmostEqual(got["annual_5y"], 5.2, places=1)
        formal = [{"style_code": "bond_pure", "annual_1y_pct": 4.5,
                   "annual_3y_pct": 4.0, "annual_5y_pct": 3.5}]
        out = benchmarks.summary(self.conn, "c1", formal, as_of="2026-11-04")
        self.assertEqual(out["type"], "real_index+etf_reference")
        row = out["rows"][0]
        self.assertEqual(row["benchmark"]["code"], "CBC_CFZS_00")
        self.assertAlmostEqual(row["excess_1y_pct"], 1.5, places=2)
        self.assertIsNotNone(row["etf"])

    def test_enhanced_style_has_no_applicable_benchmark(self):
        rows = [("CBC_CFZS_00", "2021-11-04", 80.0, None, "cb_new_composite", None, "x"),
                ("CBC_CFZS_00", "2026-11-04", 100.0, None, "cb_new_composite", None, "x"),
                ("511380", "2026-10-30", 1.0, 0.0, "x", None, "x"),
                ("511380", "2026-11-04", 1.1, 0.0, "x", None, "x")]
        self.conn.executemany("INSERT INTO market_index_daily VALUES(?,?,?,?,?,?,?)", rows)
        self.conn.executemany("INSERT INTO fund_metric VALUES(?,?,?,?,?,?)",
                              [("511380", "c1", 365, -0.05, 0.02, "2026-11-04")])
        formal = [{"style_code": "bond_convertible", "annual_1y_pct": 2.0,
                   "annual_3y_pct": 1.0, "annual_5y_pct": None}]
        out = benchmarks.summary(self.conn, "c1", formal, as_of="2026-11-04")
        row = out["rows"][0]
        self.assertIsNone(row["benchmark"])
        self.assertEqual(row["benchmark_scope"], "not_applicable")
        # ETF 只作风格参考，不能拿可转债 ETF 的 -5% 去算一级/二级债基的超额
        self.assertIsNone(row["excess_1y_pct"])
        self.assertEqual(row["etf"]["code"], "511380")

    def test_benchmark_table_labels_peer_not_benchmark(self):
        md = report_v2._benchmark_table({"rows": [{
            "style_code": "bond_pure", "members": 2,
            "benchmark": {"code": "511220", "name": "城投债ETF海富通"},
            "benchmark_metrics": {"annual_1y": 3.0, "annual_3y": 2.5, "as_of": "2026-10-30"},
            "peer_median_1y_pct": 4.5, "peer_median_3y_pct": 4.0,
            "excess_1y_pct": 1.5, "excess_3y_pct": 1.5}]})
        self.assertIn("参考ETF", md)
        self.assertIn("peer中位1年", md)
        self.assertIn("1.50pp", md)
        self.assertIn("2026-10-30", md)


if __name__ == "__main__":
    unittest.main()
