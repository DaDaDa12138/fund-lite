"""Phase 2 质量增强模块的契约测试（只读，不依赖生产库）。"""
import math
import sqlite3
import unittest

from fundana.audit import quality


def _cache(values):
    return {code: {days: {"annual_pct": values[code]} for days in (365, 1095, 1825)}
            for code in quality.INDEX_META}


class ContractBenchmarkTest(unittest.TestCase):
    def test_empty(self):
        out = quality.parse_contract_benchmark(None)
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(out["quality"], "unmapped")
        self.assertEqual(out["primary_class"], "unknown")

    def test_exact_composite(self):
        out = quality.parse_contract_benchmark("中债综合财富(总值)指数收益率")
        self.assertEqual(out["status"], "mapped")
        self.assertEqual(out["quality"], "exact")
        self.assertEqual(out["primary_class"], "composite")
        self.assertEqual(out["maturity"], "broad")
        self.assertAlmostEqual(out["mapped_weight"], 1.0)

    def test_composite_plus_cash_is_proxy(self):
        out = quality.parse_contract_benchmark(
            "中债-新综合财富(总值)指数收益率×90%+银行活期存款利率(税后)×10%")
        self.assertEqual(out["status"], "mapped_cash_zero")
        self.assertEqual(out["quality"], "proxy")
        self.assertAlmostEqual(out["cash_weight"], 0.10)
        self.assertAlmostEqual(out["mapped_weight"], 0.90)

    def test_credit_maturity(self):
        out = quality.parse_contract_benchmark("中债-高信用等级中期票据全价(1-3年)指数收益率")
        self.assertEqual(out["primary_class"], "credit")
        self.assertEqual(out["maturity"], "medium")
        self.assertEqual(out["quality"], "proxy")

    def test_cash_only(self):
        out = quality.parse_contract_benchmark("一年期定期存款利率(税后)+1.2%")
        self.assertEqual(out["status"], "cash_only")
        self.assertEqual(out["quality"], "cash_only")
        self.assertEqual(out["primary_class"], "cash")

    def test_partial_equity(self):
        out = quality.parse_contract_benchmark("中债综合指数收益率×80%+沪深300指数收益率×20%")
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["quality"], "partial")
        self.assertAlmostEqual(out["unknown_weight"], 0.20)

    def test_weights_normalized(self):
        out = quality.parse_contract_benchmark("中债综合指数×80%+中证转债指数×40%")
        self.assertAlmostEqual(sum(c["weight"] for c in out["components"]), 1.0)
        self.assertEqual(out["maturity"], "broad")


class ContractMetricsTest(unittest.TestCase):
    def test_exact_composite(self):
        cache = _cache({code: 3.0 for code in quality.INDEX_META})
        out = quality.contract_metrics(None, "中债综合财富(总值)指数收益率", "2026-11-09", cache)
        self.assertEqual(out["annual_1y_pct"], 3.0)
        self.assertEqual(out["annual_3y_pct"], 3.0)
        self.assertEqual(out["annual_5y_pct"], 3.0)
        self.assertEqual(out["price_index_weight"], 0.0)

    def test_weighted_composite_cash(self):
        cache = _cache({code: 4.0 for code in quality.INDEX_META})
        out = quality.contract_metrics(
            None, "中债综合财富指数×80%+银行活期存款利率(税后)×20%", "2026-11-09", cache)
        self.assertAlmostEqual(out["annual_1y_pct"], 3.2)
        self.assertEqual(out["quality"], "proxy")

    def test_price_index_weight(self):
        cache = _cache({code: 2.0 for code in quality.INDEX_META})
        out = quality.contract_metrics(None, "中债-国债总全价指数收益率", "2026-11-09", cache)
        self.assertAlmostEqual(out["annual_3y_pct"], 2.0)
        self.assertAlmostEqual(out["price_index_weight"], 1.0)

    def test_cash_only_has_no_excess(self):
        cache = _cache({code: 2.0 for code in quality.INDEX_META})
        out = quality.contract_metrics(None, "活期存款利率(税后)", "2026-11-09", cache)
        self.assertIsNone(out["annual_1y_pct"])
        self.assertEqual(out["quality"], "cash_only")


class AlphaMetricsTest(unittest.TestCase):
    def test_counts_and_median(self):
        items = [
            {"alpha_annual_pct": 2.0, "alpha_t": 2.5, "factor_r2": 0.30, "alpha_reliable": True},
            {"alpha_annual_pct": 0.5, "alpha_t": 1.0, "factor_r2": 0.20, "alpha_reliable": True},
            {"alpha_annual_pct": 3.0, "alpha_t": 3.0, "factor_r2": 0.10, "alpha_reliable": True},
            {"alpha_annual_pct": None, "alpha_t": None, "factor_r2": None, "alpha_reliable": False},
        ]
        out = quality.alpha_metrics(items)
        self.assertEqual(out["alpha_covered"], 3)
        self.assertEqual(out["alpha_reliable"], 3)
        self.assertAlmostEqual(out["alpha_median_pct"], 2.0)
        self.assertAlmostEqual(out["alpha_positive_share"], 1.0)
        self.assertAlmostEqual(out["alpha_significant_share"], 0.6667)
        self.assertAlmostEqual(out["alpha_r2_median"], 0.20)
        self.assertEqual(out["alpha_reference_pass"], 1)

    def test_empty(self):
        out = quality.alpha_metrics([])
        self.assertIsNone(out["alpha_median_pct"])
        self.assertIsNone(out["alpha_positive_share"])
        self.assertEqual(out["alpha_reference_pass"], 0)


class BoardMetricsTest(unittest.TestCase):
    def test_benchmark_counts(self):
        items = [
            {"benchmark_contract": {"quality": "exact", "maturity": "broad"}},
            {"benchmark_contract": {"quality": "proxy", "maturity": "short"}},
            {"benchmark_contract": {"quality": "partial", "maturity": "broad"}},
            {"benchmark_contract": {"quality": "cash_only", "maturity": "broad"}},
            {"benchmark_contract": {}},
        ]
        out = quality.benchmark_metrics(items)
        self.assertEqual(out["benchmark_mapped"], 2)
        self.assertEqual(out["benchmark_exact"], 1)
        self.assertEqual(out["benchmark_proxy"], 1)
        self.assertEqual(out["benchmark_partial"], 1)
        self.assertEqual(out["benchmark_cash_only"], 1)
        self.assertEqual(out["benchmark_unmapped"], 1)
        self.assertEqual(out["benchmark_maturity"]["broad"], 4)

    def test_exposure_and_risk(self):
        items = [
            {"fcode": "A", "beta_duration": 0.2, "beta_credit": 0.1,
             "beta_convertible": 0.0, "factor_r2": 0.4, "leverage": True,
             "mdd_3y": 1.0, "vol_3y": 0.5},
            {"fcode": "B", "beta_duration": 0.8, "beta_credit": 0.6,
             "beta_convertible": 0.5, "factor_r2": 0.6, "leverage": False,
             "mdd_3y": 3.0, "vol_3y": 4.0},
        ]
        out = quality.exposure_metrics(items)
        self.assertAlmostEqual(out["duration_beta_p10"], 0.26)
        self.assertAlmostEqual(out["duration_beta_p90"], 0.74)
        self.assertAlmostEqual(out["max_convertible_beta"], 0.5)
        self.assertEqual(out["high_convertible"], ["B"])
        self.assertAlmostEqual(out["leveraged_share"], 0.5)
        risk = quality.enhanced_risk_metrics(items)
        self.assertEqual(risk["enhanced_3y_covered"], 2)
        self.assertEqual(risk["mdd_3y_over_2pct"], ["B"])
        self.assertEqual(risk["vol_3y_over_3pct"], ["B"])

    def test_flags(self):
        item = {"alpha_reliable": True, "alpha_t": 1.5, "alpha_annual_pct": 0.8,
                "factor_r2": 0.10, "benchmark_contract": {"quality": "partial"}}
        self.assertEqual(quality.quality_flags(item),
                         ["alpha_not_significant", "alpha_below_1pct",
                          "factor_r2_low", "benchmark_unmapped"])


class Core10SelectionTest(unittest.TestCase):
    def _series(self, pattern):
        out = []
        for i, value in enumerate(pattern):
            out.append(("2026-01-%02d" % (i + 1), value / 10000.0))
        return out

    def test_best_effort_returns_selection(self):
        patterns = [
            [1, -1] * 30,
            [1, 1, -1, -1] * 15,
            [1, -1, -1, 1] * 15,
        ]
        items = []
        for i, pattern in enumerate(patterns):
            items.append({
                "fcode": "00000%d" % (i + 1),
                "company_name": "公司%d" % i,
                "family_id": "F%d" % i,
                "style_code": ("bond_short", "bond_credit", "bond_rate")[i],
                "rank_score": 70 - i * 5,
                "series": self._series(pattern),
            })
        out = quality.best_effort_core10(items, "2026-11-09", None)
        self.assertIn(out["status"], ("above_target", "below_target"))
        self.assertGreaterEqual(out["n"], 2)
        self.assertLessEqual(out["n"], 3)
        self.assertIsNotNone(out["effective_bets"])
        self.assertIsNotNone(out["max_pair"])


class QualityTierTest(unittest.TestCase):
    def test_classify_exact_a_b_c(self):
        exact = {"annual_3y": 4.0, "contract_excess_3y_pct": 1.0,
                 "mdd_3y": 1.0, "benchmark_contract": {"quality": "exact"}}
        a = quality.classify_quality_tier(
            exact, {"n_windows": 12, "win_rate": 0.70}, board="bond_stable")
        self.assertEqual(a["tier"], "A")
        self.assertEqual(a["reasons"], [])
        b = quality.classify_quality_tier(
            {**exact, "contract_excess_3y_pct": -0.2, "mdd_3y": 4.0},
            {"n_windows": 12, "win_rate": 0.55}, board="bond_stable")
        self.assertEqual(b["tier"], "B")
        c = quality.classify_quality_tier(
            {**exact, "contract_excess_3y_pct": -0.8},
            {"n_windows": 12, "win_rate": 0.55}, board="bond_stable")
        self.assertEqual(c["tier"], "C")

    def test_enhanced_mdd_threshold_is_wider(self):
        item = {"annual_3y": 3.0, "contract_excess_3y_pct": 0.2,
                "mdd_3y": 4.5, "benchmark_contract": {"quality": "exact"}}
        rolling = {"n_windows": 12, "win_rate": 0.70}
        self.assertEqual(quality.classify_quality_tier(
            item, rolling, board="bond_stable")["tier"], "B")
        self.assertEqual(quality.classify_quality_tier(
            item, rolling, board="bond_enhanced")["tier"], "A")

    def test_proxy_only_b_or_c_and_unrated(self):
        item = {"annual_3y": 4.0, "proxy_excess_3y_pct": 0.8,
                "mdd_3y": 2.0, "benchmark_contract": {"quality": "proxy"}}
        rolling = {"n_windows": 12, "win_rate": 0.60}
        self.assertEqual(quality.classify_quality_tier(
            item, rolling, board="bond_stable")["tier"], "B")
        self.assertEqual(quality.classify_quality_tier(
            {**item, "proxy_excess_3y_pct": 0.4}, rolling, board="bond_stable")["tier"], "C")
        self.assertEqual(quality.classify_quality_tier(
            {"annual_3y": None, "benchmark_contract": {"quality": "exact"}},
            rolling, board="bond_stable")["tier"], "unrated")
        self.assertEqual(quality.classify_quality_tier(
            {"annual_3y": 3.0, "benchmark_contract": {"quality": "unmapped"}},
            rolling, board="bond_stable")["tier"], "unrated")

    def test_nav_jump_is_unrated(self):
        item = {"annual_3y": 13.0, "contract_excess_3y_pct": 9.0,
                "mdd_3y": 1.0, "benchmark_contract": {"quality": "exact"}}
        out = quality.classify_quality_tier(
            item, {"n_windows": 12, "win_rate": 0.90,
                   "max_daily_move_pct": 32.38, "max_daily_move_date": "2024-06-24"},
            board="bond_stable")
        self.assertEqual(out["tier"], "unrated")
        self.assertIn("nav_jump_ge_10pct", out["reasons"])

    def test_rolling_excess_stats_positive_series(self):
        from datetime import date, timedelta
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE fund_nav_daily (fcode TEXT, nav_date TEXT, unit_nav REAL,"
                     " acc_nav REAL, growth_pct REAL, PRIMARY KEY(fcode, nav_date))")
        conn.execute("CREATE TABLE market_index_daily (index_code TEXT, trade_date TEXT,"
                     " close REAL, PRIMARY KEY(index_code, trade_date))")
        start = date(2022, 12, 1)
        end = date(2025, 12, 31)
        d = start
        nav = 1.0
        idx = 1000.0
        conn.execute("INSERT INTO fund_nav_daily VALUES (?,?,?,?,?)",
                     ("F1", "2022-11-30", 1.0, 1.0, None))
        conn.execute("INSERT INTO market_index_daily VALUES (?,?,?)",
                     ("IDX", "2020-12-31", 1000.0))
        while d <= end:
            nav *= 1.0002
            idx *= 1.0001
            conn.execute("INSERT INTO fund_nav_daily VALUES (?,?,?,?,?)",
                         ("F1", d.isoformat(), nav, nav, None))
            conn.execute("INSERT INTO market_index_daily VALUES (?,?,?)",
                         ("IDX", d.isoformat(), idx))
            d += timedelta(days=1)
        conn.commit()
        out = quality.rolling_excess_stats(conn, "F1", "IDX", "2025-12-31")
        self.assertEqual(out["status"], "ok")
        self.assertGreaterEqual(out["n_windows"], 8)
        self.assertGreater(out["win_rate"], 0.95)
        self.assertGreater(out["median_excess_pct"], 0)
        conn.close()

    def test_absolute_tiers_dedup_and_board_counts(self):
        items = [
            {"fcode": "F1", "name": "一", "board": "bond_stable",
             "annual_3y": None, "benchmark_contract": {"quality": "unmapped"}},
            {"fcode": "F1", "name": "一", "board": "bond_enhanced",
             "annual_3y": None, "benchmark_contract": {"quality": "unmapped"}},
            {"fcode": "F2", "name": "二", "board": "bond_stable",
             "annual_3y": None, "benchmark_contract": {"quality": "unmapped"}},
        ]
        out = quality.absolute_quality_tiers(None, items, "2026-11-09")
        self.assertEqual(out["summary"]["n"], 2)
        self.assertEqual(out["summary"]["unrated"], 2)
        self.assertEqual(out["by_board"]["bond_stable"]["n"], 2)
        self.assertEqual(out["by_board"]["bond_enhanced"]["n"], 1)
        self.assertEqual(out["items"][0]["boards"], ["bond_enhanced", "bond_stable"])

    def test_family_counts_dedupe_share_classes(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE fund_share_class (fcode TEXT PRIMARY KEY, family_id TEXT)")
        conn.executemany("INSERT INTO fund_share_class VALUES (?, ?)",
                         [("F1", "fam1"), ("F1C", "fam1"), ("F2", "fam2")])
        items = [
            {"fcode": "F1", "name": "一A", "board": "bond_stable",
             "annual_3y": None, "benchmark_contract": {"quality": "unmapped"}},
            {"fcode": "F1C", "name": "一C", "board": "bond_stable",
             "annual_3y": None, "benchmark_contract": {"quality": "unmapped"}},
            {"fcode": "F2", "name": "二", "board": "bond_stable",
             "annual_3y": None, "benchmark_contract": {"quality": "unmapped"}},
        ]
        out = quality.absolute_quality_tiers(conn, items, "2026-11-09")
        self.assertEqual(out["summary"]["n"], 3)
        self.assertEqual(out["summary"]["family_n"], 2)
        self.assertEqual(out["summary"]["family_unrated"], 2)
        self.assertEqual({x["family_id"] for x in out["items"]}, {"fam1", "fam2"})
        conn.close()


if __name__ == "__main__":
    unittest.main()


class Core10ShadowFrontierTest(unittest.TestCase):
    def _items(self, n=12):
        from datetime import date, timedelta
        start = date(2026, 1, 1)
        days = [start + timedelta(days=i) for i in range(90)]
        items = []
        for i in range(n):
            series = []
            for k, d in enumerate(days):
                val = 0.0008 * math.sin((k + i * 11) / 3.7) + 0.0004 * math.cos((k + i * 5) / 9.1)
                series.append((d.isoformat(), val))
            items.append({
                "fcode": "f%02d" % i,
                "name": "基金%02d" % i,
                "style_code": "bond_pure",
                "family_id": "fam%02d" % i,
                "company_name": "公司%02d" % i,
                "rank_score": 100.0 - i,
                "scores": {"rank_score": 100.0 - i},
                "leverage": i in (0, 1, 2, 3, 4, 5, 6),
                "series": series,
            })
        return items

    def test_frontier_rows_respect_constraints(self):
        items = self._items()
        prod = ["f%02d" % i for i in range(10)]
        out = quality.core10_shadow_frontier(items, prod)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["pool_size"], 12)
        for row in [out["production"]] + out["frontier"]:
            self.assertEqual(len(row["codes"]), 10)
            self.assertEqual(len(set(row["codes"])), 10)
            self.assertLessEqual(row["leveraged_share"], 0.60 + 1e-9)
        self.assertIn(out["recommended"], (None, "pair", "eff", "score"))

    def test_short_pool_unavailable(self):
        out = quality.core10_shadow_frontier(self._items(5), ["f00"])
        self.assertEqual(out["status"], "unavailable")

