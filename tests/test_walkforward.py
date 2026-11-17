"""walkforward 审计工具的单测：只覆盖纯函数（不依赖真实库）。"""
from __future__ import annotations

import unittest

from fundana.audit import walkforward as wf


class TestCohorts(unittest.TestCase):
    def test_cohort_dates(self) -> None:
        self.assertEqual(wf.cohort_dates([2021, 2025]), ["2021-06-30", "2025-06-30"])

    def test_cohort_dates_custom_month_day(self) -> None:
        self.assertEqual(wf.cohort_dates([2024], "12-31"), ["2024-12-31"])


class TestMetricEndDate(unittest.TestCase):
    def test_uses_dry_window_not_latest_metric(self) -> None:
        self.assertEqual(wf._metric_end_date({365: {"end_date": "2021-06-30"}}),
                         "2021-06-30")
        self.assertEqual(wf._metric_end_date({365: {"end_date": "2021-06-30T00:00:00"}}),
                         "2021-06-30")
        self.assertIsNone(wf._metric_end_date({}))
        self.assertIsNone(wf._metric_end_date({365: {}}))


class TestSpearman(unittest.TestCase):
    def test_perfect_positive(self) -> None:
        xs = {str(i): float(i) for i in range(40)}
        ys = {str(i): float(i * 2) for i in range(40)}
        self.assertAlmostEqual(wf.spearman(xs, ys), 1.0, places=4)

    def test_perfect_negative(self) -> None:
        xs = {str(i): float(i) for i in range(40)}
        ys = {str(i): float(-i) for i in range(40)}
        self.assertAlmostEqual(wf.spearman(xs, ys), -1.0, places=4)

    def test_too_few_points(self) -> None:
        xs = {str(i): float(i) for i in range(5)}
        self.assertIsNone(wf.spearman(xs, xs))

    def test_constant_series_returns_none(self) -> None:
        xs = {str(i): 1.0 for i in range(30)}
        ys = {str(i): float(i) for i in range(30)}
        self.assertIsNone(wf.spearman(xs, ys))

    def test_ranks_handle_ties(self) -> None:
        ranks = wf._ranks({"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0})
        self.assertEqual(ranks["a"], 1.0)
        self.assertEqual(ranks["b"], 2.5)
        self.assertEqual(ranks["c"], 2.5)
        self.assertEqual(ranks["d"], 4.0)


class TestMedian(unittest.TestCase):
    def test_odd_even_and_empty(self) -> None:
        self.assertEqual(wf._median([3.0, 1.0, 2.0]), 2.0)
        self.assertEqual(wf._median([4.0, 1.0, 3.0, 2.0]), 2.5)
        self.assertIsNone(wf._median([]))


class TestBucketStats(unittest.TestCase):
    def test_forward_and_hit_rate(self) -> None:
        fwd = {
            "a": {"annual": 0.10, "sharpe": 1.2},
            "b": {"annual": 0.02, "sharpe": 0.4},
            "c": None,
        }
        stats = wf._bucket_stats(["a", "b", "c"], fwd, 0.05)
        self.assertEqual(stats["n"], 3)
        self.assertEqual(stats["n_forward"], 2)
        self.assertAlmostEqual(stats["annual_mean_pct"], 6.0, places=4)
        self.assertAlmostEqual(stats["annual_median_pct"], 6.0, places=4)
        self.assertAlmostEqual(stats["sharpe_mean"], 0.8, places=4)
        self.assertAlmostEqual(stats["hit_rate"], 0.5, places=4)

    def test_empty_and_no_baseline(self) -> None:
        self.assertIsNone(wf._bucket_stats([], {}, 0.05)["annual_mean_pct"])
        stats = wf._bucket_stats(["a"], {"a": {"annual": 0.1, "sharpe": 1.0}}, None)
        self.assertIsNone(stats["hit_rate"])


class TestCheckFailureCounts(unittest.TestCase):
    def test_counts_pass_fail_and_missing(self) -> None:
        checks = {
            "a": {"annual_1y": {"pass": True}, "sharpe": {"pass": False, "reason": "below_min"}},
            "b": {"annual_1y": {"pass": False, "reason": "missing"},
                  "sharpe": {"pass": True}},
        }
        counts = wf._check_failure_counts(checks)
        self.assertEqual(counts["annual_1y"], {"n": 2, "pass": 1, "fail": 1, "missing": 1})
        self.assertEqual(counts["sharpe"], {"n": 2, "pass": 1, "fail": 1, "missing": 0})


class TestRender(unittest.TestCase):
    def test_render_contains_keys_and_rates(self) -> None:
        result = {
            "top": 118, "horizon_days": 365,
            "cohorts": [{
                "as_of": "2023-06-30", "gate_pass": 900, "fixed_pass": 240, "pool": {"n": 240},
                "boards": {k: {"annual_mean_pct": 4.4, "sharpe_mean": 4.2, "hit_rate": 0.6}
                           for k in wf.RANK_KEYS},
            }],
            "summary": {k: {"annual_mean_pct": 4.4, "sharpe_mean": 4.2, "hit_rate": 0.6,
                            "ic_annual": 0.3, "ic_sharpe": 0.2} for k in wf.RANK_KEYS},
        }
        text = wf.render(result)
        self.assertIn("walkforward top=118", text)
        self.assertIn("rank_score", text)
        self.assertIn("60.00%", text)

    def test_render_enhanced_section(self) -> None:
        result = {
            "top": 118, "horizon_days": 365,
            "cohorts": [{
                "as_of": "2024-06-30", "gate_pass": 900, "fixed_pass": 240, "pool": {"n": 240},
                "boards": {k: {"annual_mean_pct": 4.4, "sharpe_mean": 4.2, "hit_rate": 0.6}
                           for k in wf.RANK_KEYS},
                "enhanced": {
                    "n": 50,
                    "strict": {"pass_n": 8, "pick_n": 7,
                               "pick_stats": {"annual_mean_pct": 5.1, "sharpe_mean": 1.4,
                                              "hit_rate": 0.57}},
                    "relative": {"pass_n": 20, "pick_n": 18,
                                 "pick_stats": {"annual_mean_pct": 4.2, "sharpe_mean": 1.1,
                                                "hit_rate": 0.44}},
                    "near_miss": {"6of7": {"n": 9, "annual_mean_pct": 4.8}},
                },
            }],
            "summary": {k: {"annual_mean_pct": 4.4, "sharpe_mean": 4.2, "hit_rate": 0.6,
                            "ic_annual": 0.3, "ic_sharpe": 0.2} for k in wf.RANK_KEYS},
        }
        text = wf.render(result)
        self.assertIn("增强债准入回测", text)
        self.assertIn("strict", text)
        self.assertIn("5.10", text)
        self.assertIn("4.80", text)


if __name__ == "__main__":
    unittest.main()


class TestStyleAsof(unittest.TestCase):
    def _conn(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE fund_style(fcode TEXT PRIMARY KEY, style_code TEXT);"
            "CREATE TABLE fund_style_history(fcode TEXT, as_of_date TEXT, style_code TEXT,"
            " confidence TEXT, classifier_version TEXT, created_at TEXT,"
            " PRIMARY KEY(fcode, as_of_date));")
        return conn

    def test_history_wins_over_current(self) -> None:
        from fundana.fuse.style import style_asof_map
        conn = self._conn()
        conn.execute("INSERT INTO fund_style VALUES('A','bond_pure')")
        conn.execute("INSERT INTO fund_style_history VALUES('A','2026-06-30','bond_short','x','v','t')")
        conn.execute("INSERT INTO fund_style_history VALUES('A','2026-10-30','bond_pure','x','v','t')")
        m, hits = style_asof_map(conn, "2026-06-30")
        self.assertEqual(m["A"], "bond_short")
        self.assertEqual(hits, 1)

    def test_latest_before_date_and_fallback(self) -> None:
        from fundana.fuse.style import style_asof_map
        conn = self._conn()
        conn.execute("INSERT INTO fund_style VALUES('A','bond_pure')")
        conn.execute("INSERT INTO fund_style VALUES('B','bond_rate')")
        conn.execute("INSERT INTO fund_style_history VALUES('A','2026-05-01','bond_credit','x','v','t')")
        conn.execute("INSERT INTO fund_style_history VALUES('A','2026-06-01','bond_short','x','v','t')")
        m, hits = style_asof_map(conn, "2026-06-30")
        self.assertEqual(m["A"], "bond_short")
        self.assertEqual(m["B"], "bond_rate")   # 无历史 → 回退当前
        self.assertEqual(hits, 1)

