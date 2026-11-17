"""V2 尾部指标单元测试：只用合成序列，验证口径而不是验证上游数据。"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from fundana.metrics import tail
from fundana.metrics.window import NavRow


def _eff(rows: list[NavRow]) -> list[float]:
    from fundana.metrics.window import effective_returns
    return effective_returns(rows)[0]


def _rows(n: int, start: str = "2023-01-01", growth: float = 0.05,
          overrides: dict[int, float] | None = None) -> list[NavRow]:
    d0 = date.fromisoformat(start)
    out: list[NavRow] = []
    for i in range(n):
        g = growth if not overrides or i not in overrides else overrides[i]
        out.append(NavRow((d0 + timedelta(days=i)).isoformat(), 1.0, 1.0, g))
    return out


class RollingWorstTest(unittest.TestCase):
    def test_worst_window_includes_crash(self) -> None:
        rows = _rows(120, growth=0.1, overrides={60: -10.0})
        res = tail.rolling_worst(rows, _eff(rows), 30, tail_start="2023-01-01")
        self.assertIsNotNone(res)
        self.assertLess(res["pct"], -7.0)
        self.assertGreaterEqual(res["end"], rows[60].nav_date)

    def test_missing_growth_skips_window(self) -> None:
        rows = _rows(120, growth=0.1)
        rows[60].growth_pct = None
        res = tail.rolling_worst(rows, _eff(rows), 30, tail_start="2023-01-01")
        # 崩溃日缺失且净值不动 -> 该步还原为 0（不是把真实跌幅当 0）
        self.assertIsNotNone(res)
        self.assertGreater(res["pct"], -1.0)

    def test_insufficient_rows_returns_none(self) -> None:
        rows = _rows(10)
        self.assertIsNone(tail.rolling_worst(rows, _eff(rows), 90, tail_start="2023-01-01"))


class MonthlyTest(unittest.TestCase):
    def test_half_positive(self) -> None:
        jan = [NavRow(f"2024-01-{d:02d}", 1, 1, 0.1) for d in range(1, 21)]
        feb = [NavRow(f"2024-02-{d:02d}", 1, 1, -0.1) for d in range(1, 21)]
        rows = jan + feb
        ratio, total, positive = tail.monthly_positive(rows, _eff(rows), tail_start="2023-12-31")
        self.assertEqual((total, positive), (2, 1))
        self.assertAlmostEqual(ratio, 50.0)

    def test_partial_month_excluded(self) -> None:
        rows = [NavRow(f"2024-01-{d:02d}", 1, 1, 0.1) for d in range(1, 6)]
        ratio, total, _ = tail.monthly_positive(rows, _eff(rows), tail_start="2023-12-31")
        self.assertIsNone(ratio)
        self.assertEqual(total, 0)


class UnderwaterTest(unittest.TestCase):
    def test_ongoing(self) -> None:
        longest, ongoing, current = tail.underwater_stats([0.1, -0.1, -0.1, 0.05])
        self.assertEqual(longest, 3)
        self.assertTrue(ongoing)
        self.assertEqual(current, 3)

    def test_recovered(self) -> None:
        longest, ongoing, current = tail.underwater_stats([0.1, -0.1, 0.2])
        self.assertEqual(longest, 1)
        self.assertFalse(ongoing)
        self.assertEqual(current, 0)


class ComputeTest(unittest.TestCase):
    def test_short_history_is_all_null(self) -> None:
        res = tail.compute(_rows(400), "2024-02-04")
        self.assertIsNone(res["mdd_dec"])
        self.assertIsNone(res["worst_rolling90d_pct"])
        self.assertEqual(res["completeness"], 0.0)
        self.assertTrue(any(r.startswith("insufficient_points") for r in res["undefined_reasons"]))

    def test_full_window_metrics(self) -> None:
        rows = _rows(1200, growth=0.05, overrides={600: -20.0})
        res = tail.compute(rows, rows[-1].nav_date)
        self.assertAlmostEqual(res["mdd_dec"], 0.20, places=6)
        self.assertAlmostEqual(res["worst_day_pct"], -20.0, places=6)
        self.assertIsNotNone(res["worst_week_pct"])
        self.assertLessEqual(res["worst_week_pct"], 0.0)
        self.assertEqual(res["worst_day_date"], rows[600].nav_date)
        self.assertIsNotNone(res["cvar5_dec"])
        self.assertLess(res["cvar5_dec"], 0)
        self.assertIsNotNone(res["worst_rolling90d_pct"])
        self.assertIsNotNone(res["worst_rolling180d_pct"])
        self.assertIsNotNone(res["positive_month_ratio_pct"])
        self.assertGreaterEqual(res["completeness"], 0.99)
        self.assertFalse(any("nan" in str(v).lower() for v in res.values()))

    def test_missing_returns_reports_completeness(self) -> None:
        rows = _rows(1200, growth=0.05)
        rows[10].growth_pct = None
        res = tail.compute(rows, rows[-1].nav_date)
        # daily_growth 覆盖率仍 >=90%，按契约接受并标注 proxy_rows=0；completeness 不应虚高为 0
        self.assertGreater(res["completeness"], 0.5)
        self.assertIsNotNone(res["mdd_dec"])


if __name__ == "__main__":
    unittest.main()