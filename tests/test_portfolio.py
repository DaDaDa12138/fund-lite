"""组合层回测的纯函数与序列读取测试。"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import date, timedelta
from unittest import mock

from fundana.audit import portfolio as p


class TestPortfolioMath(unittest.TestCase):
    def test_curve_compounds(self):
        ds, cs = p._curve([("2024-01-02", 0.01), ("2024-01-03", -0.005)],
                          "2024-01-01")
        self.assertEqual(ds, ["2024-01-01", "2024-01-02", "2024-01-03"])
        self.assertAlmostEqual(cs[1], 1.01)
        self.assertAlmostEqual(cs[2], 1.00495)

    def test_equal_weight_portfolio(self):
        series = {
            "A": [("2024-01-02", 0.01), ("2024-01-03", 0.0)],
            "B": [("2024-01-02", 0.0), ("2024-01-03", 0.01)],
        }
        out = p._portfolio(series, "2024-01-01", "2024-12-31")
        self.assertIsNotNone(out)
        self.assertEqual(out["n"], 2)
        self.assertEqual(out["curve"]["values"][0], 1.0)
        self.assertAlmostEqual(out["curve"]["values"][-1], 1.01)
        self.assertAlmostEqual(out["annual_pct"], round(100 * (1.01 ** (365 / 2) - 1), 2))

    def test_weighted_portfolio(self):
        series = {
            "A": [("2024-01-02", 0.10)],
            "B": [("2024-01-02", 0.0)],
        }
        out = p._portfolio(series, "2024-01-01", "2024-12-31",
                           weights={"A": 0.9, "B": 0.1})
        self.assertAlmostEqual(out["curve"]["values"][-1], 1.09)
        out2 = p._portfolio(series, "2024-01-01", "2024-12-31",
                            weights={"A": 0.0, "B": 0.0})
        self.assertAlmostEqual(out2["curve"]["values"][-1], 1.05)  # 全零回退等权

    def test_portfolio_drawdown(self):
        series = {"A": [("2024-01-02", 0.10), ("2024-01-03", -0.20)]}
        out = p._portfolio(series, "2024-01-01", "2024-12-31")
        self.assertGreater(out["mdd_pct"], 0.0)
        self.assertIsNotNone(out["sharpe"])

    def test_summarize(self):
        cohorts = [
            {"portfolio": {"boards": {"core10": {"annual_pct": 4.0, "sharpe": 3.0,
                                                  "mdd_pct": 1.0, "excess_pct": 1.0,
                                                  "beat": True, "n_used": 10}},
                           "benchmark": {"annual_pct": 3.0, "sharpe": 2.0,
                                         "mdd_pct": 0.5, "n_used": 100}}},
            {"portfolio": {"boards": {"core10": {"annual_pct": 2.0, "sharpe": 1.0,
                                                  "mdd_pct": 3.0, "excess_pct": -1.0,
                                                  "beat": False, "n_used": 10}},
                           "benchmark": {"annual_pct": 3.0, "sharpe": 2.0,
                                         "mdd_pct": 0.5, "n_used": 100}}},
        ]
        out = p.summarize(cohorts)
        self.assertEqual(out["core10"]["annual_pct"], 3.0)
        self.assertEqual(out["core10"]["beat_rate"], 0.5)
        self.assertEqual(out["benchmark"]["annual_pct"], 3.0)


class TestPortfolioSeries(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE fund_nav_daily (fcode TEXT, nav_date TEXT, unit_nav REAL,"
            " acc_nav REAL, growth_pct REAL, PRIMARY KEY(fcode,nav_date))")
        self.conn.execute(
            "CREATE TABLE market_index_daily (index_code TEXT, trade_date TEXT,"
            " close REAL, pct_chg REAL, PRIMARY KEY(index_code,trade_date))")

    def _fill(self, code, start, n, daily=0.0001):
        d = date.fromisoformat(start)
        for i in range(n):
            self.conn.execute(
                "INSERT INTO fund_nav_daily VALUES (?,?,?,?,?)",
                (code, (d + timedelta(days=i)).isoformat(), 1.0 + i * 0.001,
                 1.0 + i * 0.001, daily * 100))

    def test_load_series_requires_anchor_and_min_obs(self):
        self._fill("A", "2023-12-29", 1)
        self._fill("A", "2024-01-02", 45)
        out = p._load_series(self.conn, "A", "2024-01-01", "2024-02-20")
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 45)
        self.assertAlmostEqual(out[0][1], 0.0001, places=6)

    def test_load_series_short_history_returns_none(self):
        self._fill("B", "2024-01-02", 10)
        self.assertIsNone(p._load_series(self.conn, "B", "2024-01-01", "2024-03-31"))

    def test_board_codes_passes_series_to_core10(self):
        self._fill("A", "2025-11-06", 365)
        calls = []

        def fake_select(passed, by_code, dropped, **kw):
            calls.append(kw)
            return []

        with mock.patch.object(p, "screen_engine") as se, \
                mock.patch.object(p.wf, "_enhanced_picks", return_value=[]):
            se._select_concentrated.side_effect = fake_select
            p._board_codes([{"fcode": "A"}],
                           [{"fcode": "A", "style_code": "bond_pure"}],
                           {"A": {"rank_score": 70.0}}, {}, conn=self.conn,
                           as_of="2026-11-05")
        self.assertEqual(len(calls), 3)
        self.assertIn("A", calls[0]["series_by_code"])

    def test_index_series_prefers_pct_chg(self):
        self.conn.execute("INSERT INTO market_index_daily VALUES ('000012','2024-01-01',100,0)")
        for i in range(25):
            self.conn.execute("INSERT INTO market_index_daily VALUES ('000012',?,?,?)",
                              ((date(2024, 1, 2) + timedelta(days=i)).isoformat(),
                               100 + i, 0.1))
        out = p._index_series(self.conn, "000012", "2024-01-01", "2024-12-31")
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out[0][1], 0.001)


if __name__ == "__main__":
    unittest.main()
