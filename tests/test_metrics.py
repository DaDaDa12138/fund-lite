"""指标引擎数学契约：链式复利、年化、波动、零下行/零回撤、封顶。"""
import math
import unittest
from datetime import date, timedelta

from fundana.metrics import risk
from fundana.metrics.window import NavRow, build_window, max_gap, min_points, resolve_returns


def make_series(days: int, daily: float, start: str = "2025-01-01") -> list[NavRow]:
    d0 = date.fromisoformat(start)
    rows, wealth = [], 1.0
    for i in range(days):
        wealth *= (1 + daily)
        rows.append(NavRow(nav_date=(d0 + timedelta(days=i)).isoformat(),
                           unit_nav=round(wealth, 6), acc_nav=None, growth_pct=daily * 100))
    return rows


class TestWindow(unittest.TestCase):
    def test_min_points_and_gap(self):
        self.assertEqual(min_points(7), 4)
        self.assertEqual(min_points(30), 17)
        self.assertEqual(min_points(90), 50)
        self.assertEqual(min_points(365), 202)
        self.assertEqual(max_gap(7), 7)
        self.assertEqual(max_gap(365), 45)

    def test_window_excludes_as_of_and_keeps_anchor(self):
        series = make_series(60, 0.0001)
        w = build_window(series, 30, series[-1].nav_date)
        self.assertTrue(all(r.nav_date <= w.end_date for r in w.rows))
        self.assertEqual(w.rows[-1].nav_date, w.end_date)

    def test_weekly_cadence_uses_own_min_points(self):
        # 每周披露（间隔 7 天）但历史完整：不应被判点数不足
        rows = make_series(380, 0.0005)
        weekly = [r for i, r in enumerate(rows) if i % 7 == 0]
        w = build_window(weekly, 365, weekly[-1].nav_date)
        self.assertEqual(w.median_gap_days, 7.0)
        self.assertEqual(w.min_points_used, 42)
        self.assertFalse([r for r in w.reasons if r.startswith("insufficient_points")])

    def test_daily_cadence_keeps_strict_min_points(self):
        rows = make_series(400, 0.0001)
        w = build_window(rows, 365, rows[-1].nav_date)
        self.assertEqual(w.min_points_used, 202)
        self.assertEqual(w.median_gap_days, 1.0)

    def test_mixed_cadence_uses_p75_not_median(self):
        # 开放期日更 60 天 + 封闭期周更：中位数落在 1 天，p75 才是真实节奏（ADR-027）
        rows = make_series(400, 0.0002)
        mixed = rows[:100] + [r for i, r in enumerate(rows[100:]) if i % 7 == 0]
        w = build_window(mixed, 365, mixed[-1].nav_date)
        self.assertEqual(w.median_gap_days, 1.0)
        self.assertGreater(w.p75_gap_days, 3)
        self.assertLess(w.min_points_used, 202)
        self.assertFalse([r for r in w.reasons if r.startswith("insufficient_points")])

    def test_short_weekly_history_still_insufficient(self):
        rows = make_series(120, 0.0005)
        weekly = [r for i, r in enumerate(rows) if i % 7 == 0]   # 18 点，约 119 天
        w = build_window(weekly, 365, weekly[-1].nav_date)
        self.assertTrue([r for r in w.reasons if r.startswith("insufficient_points")])

    def test_growth_basis_preferred(self):
        series = make_series(60, 0.0001)
        w = build_window(series, 30, series[-1].nav_date)
        returns, basis, proxy = resolve_returns(w)
        self.assertEqual(basis, "daily_growth_pct")
        self.assertEqual(proxy, 0)


class TestGrowthReconstruction(unittest.TestCase):
    """ADR-051：上游 equityReturn 在周频披露基金上返回 0，必须用净值环比还原。"""

    @staticmethod
    def _weekly(n: int = 14, step: float = 0.005, growth: float | None = 0.0,
                acc_offset: float | None = None) -> list[NavRow]:
        d0 = date.fromisoformat("2025-06-06")
        rows, nav = [], 1.0
        for i in range(n):
            nav = round(nav * (1 + step), 6)
            rows.append(NavRow(nav_date=(d0 + timedelta(days=7 * i)).isoformat(),
                               unit_nav=nav,
                               acc_nav=(round(nav + acc_offset, 6) if acc_offset is not None else None),
                               growth_pct=growth))
        return rows

    def test_pseudo_zero_is_reconstructed_from_nav(self):
        rows = self._weekly()
        w = build_window(rows, 90, rows[-1].nav_date)
        returns, basis, _ = resolve_returns(w, w.anchor_row)
        self.assertEqual(basis, "daily_growth_pct")
        self.assertGreater(w.reconstructed_rows, 5)
        wealth = 1.0
        for r in returns:
            wealth *= (1 + r)
        # 链式起点是窗口第一条的"上一观测"（anchor），与 growth 口径一致
        base_row = w.anchor_row or w.rows[0]
        endpoint = w.rows[-1].unit_nav / base_row.unit_nav - 1
        self.assertAlmostEqual(wealth - 1, endpoint, places=9)

    def test_true_zero_growth_is_kept(self):
        # 净值不动、增长率为 0：这是真实零收益，不得被"还原"成噪声
        rows = self._weekly(n=14, step=0.0, growth=0.0)
        w = build_window(rows, 90, rows[-1].nav_date)
        returns, _, _ = resolve_returns(w, w.anchor_row)
        self.assertEqual(w.reconstructed_rows, 0)
        self.assertTrue(all(abs(r) < 1e-12 for r in returns))

    def test_dividend_detected_by_acc_difference(self):
        # 除息日：单位净值多跌 0.10，累计净值不变 -> 该步真实收益 = 0（不是 -8%）
        prev = NavRow("2026-01-02", 1.20, 1.50, 0.0)
        cur = NavRow("2026-01-09", 1.10, 1.50, 0.0)
        from fundana.metrics.window import reconstruct_return
        self.assertAlmostEqual(reconstruct_return(cur, prev), 0.0, places=12)
        # 正常下跌日：Δacc == Δnav，不得误判为分红（首版用比率比较，把所有下跌日都判成分红）
        prev2 = NavRow("2026-01-02", 1.20, 1.50, 0.0)
        cur2 = NavRow("2026-01-09", 1.10, 1.40, 0.0)
        self.assertAlmostEqual(reconstruct_return(cur2, prev2), -0.1 / 1.2, places=12)

    def test_nonzero_growth_is_authoritative(self):
        rows = self._weekly(step=0.002, growth=0.35)
        w = build_window(rows, 90, rows[-1].nav_date)
        returns, _, _ = resolve_returns(w, w.anchor_row)
        self.assertEqual(w.reconstructed_rows, 0)
        self.assertTrue(all(abs(r - 0.0035) < 1e-12 for r in returns))


class TestRisk(unittest.TestCase):
    def test_chain_total_return_matches_product(self):
        returns = [0.001] * 251
        m = risk.compute(returns, span_days=365, risk_free_rate=0.0, mar=0.0,
                         period_days=365, trading_days=252)
        self.assertAlmostEqual(m["total_return_pct"], ((1.001 ** 251) - 1) * 100, places=6)
        self.assertAlmostEqual(m["annual_dec"], (1.001 ** 251) ** (365.25 / 365) - 1, places=9)

    def test_volatility_sample_std(self):
        returns = [0.01, -0.01, 0.01, -0.01, 0.01]
        m = risk.compute(returns, span_days=5, risk_free_rate=0.0, mar=0.0, period_days=7)
        std = math.sqrt(sum((r - 0.002) ** 2 for r in returns) / 4)
        freq = max(1.0, min(252.0, 5 / (5 / 365.25)))
        self.assertAlmostEqual(m["vol_dec"], std * math.sqrt(freq), places=12)

    def test_zero_downside_gives_null_sortino(self):
        m = risk.compute([0.001, 0.002, 0.001], span_days=3, risk_free_rate=0.0, mar=0.0,
                         period_days=7)
        self.assertIsNone(m["sortino"])
        self.assertIn("zero_downside", m["undefined_reasons"])

    def test_zero_drawdown_gives_null_calmar(self):
        m = risk.compute([0.001, 0.002, 0.003], span_days=3, risk_free_rate=0.0, mar=0.0,
                         period_days=7)
        self.assertEqual(m["mdd_dec"], 0.0)
        self.assertIsNone(m["calmar_dec"])
        self.assertIn("zero_drawdown", m["undefined_reasons"])

    def test_flat_days_split_from_positive(self):
        # P0-1：0.00% 的平坦日不是亏损日；positive 只算 >0，non_negative 算 >=0。
        returns = [0.001, 0.0, -0.001, 0.0, 0.002]
        m = risk.compute(returns, span_days=5, risk_free_rate=0.0, mar=0.0, period_days=7)
        self.assertAlmostEqual(m["positive_ratio_pct"], 40.0)
        self.assertAlmostEqual(m["flat_ratio_pct"], 40.0)
        self.assertAlmostEqual(m["non_negative_ratio_pct"], 80.0)

    def test_caps_keep_raw(self):
        m = risk.compute([0.05, 0.06, 0.05, 0.06], span_days=4, risk_free_rate=0.0, mar=0.0,
                         period_days=7)
        self.assertLessEqual(m["sharpe"], 5.0)
        if m["sharpe_cap_hit"]:
            self.assertGreater(m["raw_sharpe"], 5.0)

    def test_drawdown_duration_counts_observations(self):
        mdd, obs = risk.drawdown_stats([0.01, -0.02, -0.01, 0.005, 0.02])
        self.assertAlmostEqual(mdd, 1 - 1.01 * 0.98 * 0.99 / 1.01, places=12)
        self.assertGreaterEqual(obs, 3)


if __name__ == "__main__":
    unittest.main()