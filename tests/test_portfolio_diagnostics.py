"""组合相关性诊断的纯函数测试（P1-1）。"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from fundana import portfolio_diagnostics as d


def _series(values, start="2024-01-01"):
    day = date.fromisoformat(start)
    return [((day + timedelta(days=i)).isoformat(), float(v)) for i, v in enumerate(values)]


class TestPortfolioDiagnostics(unittest.TestCase):
    def test_correlation_and_effective_bets(self):
        base = [0.001 if i % 2 == 0 else -0.0005 for i in range(60)]
        # C 与 A/B 的样本协方差为 0，用来构造 rho_bar = 1/3 的解析解
        c = [0.001, 0.001, -0.001, -0.001] * 15
        series = {"A": _series(base), "B": _series(base), "C": _series(c)}
        meta = {"A": {"company": "甲", "family": "F1", "style": "pure"},
                "B": {"company": "甲", "family": "F1", "style": "pure"},
                "C": {"company": "乙", "family": "F2", "style": "short"}}
        out = d.diagnose(series, meta)
        self.assertEqual(out["n"], 3)
        self.assertEqual(out["n_pairs"], 3)
        self.assertAlmostEqual(out["avg_pairwise_corr"], 0.3333, places=4)
        self.assertAlmostEqual(out["effective_bets"], 1.8, places=3)
        self.assertAlmostEqual(out["max_pairwise_corr"]["corr"], 1.0)
        self.assertIn("high_correlation", out["flags"])
        self.assertIn("near_duplicate_pair", out["flags"])
        self.assertIn("family_duplicate", out["flags"])
        self.assertEqual(out["top_company"]["count"], 2)

    def test_effective_bets_formula(self):
        self.assertAlmostEqual(d.effective_bets(10, 0.676), 1.412, places=3)
        self.assertEqual(d.effective_bets(1, 0.9), 1.0)
        self.assertIsNone(d.effective_bets(0, 0.5))
        self.assertIsNone(d.effective_bets(3, None))
        self.assertEqual(d.effective_bets(3, -0.5), 3.0)

    def test_short_overlap_is_skipped(self):
        a = _series([0.001] * 20)
        b = _series([0.001] * 20)
        out = d.diagnose({"A": a, "B": b}, min_pair_obs=40)
        self.assertEqual(out["n_pairs"], 0)
        self.assertEqual(out["n_pairs_skipped"], 1)
        self.assertIsNone(out["avg_pairwise_corr"])
        self.assertIsNone(out["effective_bets"])

    def test_constant_series_has_no_correlation(self):
        a = _series([0.0] * 60)
        b = _series([0.001 if i % 2 == 0 else -0.001 for i in range(60)])
        out = d.diagnose({"A": a, "B": b})
        self.assertEqual(out["n_pairs"], 0)
        self.assertEqual(out["n_pairs_skipped"], 1)

    def test_company_concentration(self):
        base = [0.001 if i % 2 == 0 else -0.0005 for i in range(60)]
        series = {c: _series([v * (1 + i * 0.01) for v in base]) for i, c in enumerate("ABCD")}
        meta = {c: {"company": "甲" if c != "D" else "乙", "family": c, "style": "pure"}
                for c in "ABCD"}
        out = d.diagnose(series, meta)
        self.assertIn("company_concentration", out["flags"])
        self.assertEqual(out["top_company"]["count"], 3)


if __name__ == "__main__":
    unittest.main()
