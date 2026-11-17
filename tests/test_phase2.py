import json
import unittest

from fundana.audit import phase2


class BenchmarkTest(unittest.TestCase):
    def test_credit_benchmark(self):
        p = phase2.classify_benchmark("中债高信用等级债券财富指数")
        self.assertEqual(p["primary"], "credit")
        self.assertIsNone(phase2.benchmark_mismatch("bond_credit", p))

    def test_rate_and_mismatch(self):
        p = phase2.classify_benchmark("中债-国债总全价指数")
        self.assertEqual(p["primary"], "rate")
        self.assertEqual(phase2.benchmark_mismatch("bond_credit", p),
                         "credit_style_but_benchmark_rate")

    def test_convertible(self):
        p = phase2.classify_benchmark("中证可转换债券指数收益率*80%+中债综合指数*20%")
        self.assertEqual(p["primary"], "mixed")
        self.assertIsNone(phase2.benchmark_mismatch("bond_convertible", p))

    def test_composite_and_cash(self):
        self.assertEqual(phase2.classify_benchmark("中债综合指数收益率")["primary"], "composite")
        self.assertEqual(phase2.classify_benchmark("活期存款基准利率")["primary"], "cash")

    def test_empty(self):
        p = phase2.classify_benchmark(None)
        self.assertEqual(p["primary"], "unknown")
        self.assertIsNone(phase2.benchmark_mismatch("bond_credit", p))


class BoardSummaryTest(unittest.TestCase):
    def test_contract_excess_stats(self):
        items = [
            {"fcode": "a", "contract_excess_3y_pct": 1.0},
            {"fcode": "b", "contract_excess_3y_pct": -0.5},
            {"fcode": "c", "contract_excess_3y_pct": None},
        ]
        out = phase2._board_summary(items)
        self.assertEqual(out["contract_excess_3y_n"], 2)
        self.assertAlmostEqual(out["contract_excess_3y_median_pct"], 0.25)
        self.assertAlmostEqual(out["contract_excess_3y_positive_share"], 0.5)

    def test_contract_excess_empty(self):
        out = phase2._board_summary([{"fcode": "a", "contract_excess_3y_pct": None}])
        self.assertEqual(out["contract_excess_3y_n"], 0)
        self.assertIsNone(out["contract_excess_3y_median_pct"])
        self.assertIsNone(out["contract_excess_3y_positive_share"])

    def test_fee_gross_stats(self):
        items = [
            {"fcode": "a", "contract_excess_3y_pct": -0.5, "recurring_fee_pct": 0.4,
             "contract_excess_3y_gross_pct": -0.1},
            {"fcode": "b", "contract_excess_3y_pct": 0.2, "recurring_fee_pct": 0.3,
             "contract_excess_3y_gross_pct": 0.5},
            {"fcode": "c", "contract_excess_3y_pct": None, "recurring_fee_pct": None,
             "contract_excess_3y_gross_pct": None},
        ]
        out = phase2._board_summary(items)
        self.assertAlmostEqual(out["median_recurring_fee_pct"], 0.35)
        self.assertEqual(out["contract_excess_3y_gross_n"], 2)
        self.assertAlmostEqual(out["contract_excess_3y_gross_median_pct"], 0.2)
        self.assertAlmostEqual(out["contract_excess_3y_gross_positive_share"], 0.5)


class AssetAllocTest(unittest.TestCase):
    def _raw(self, bond, cash=0.0, stock=0.0):
        return json.dumps({"series": [
            {"name": "债券占净比", "data": [bond]},
            {"name": "现金占净比", "data": [cash]},
            {"name": "股票占净比", "data": [stock]},
        ], "categories": ["2026-09-30"]}, ensure_ascii=False)

    def test_leveraged(self):
        out = phase2.parse_asset_alloc(self._raw(114.8, 0.22))
        self.assertTrue(out["leveraged"])
        self.assertEqual(out["period"], "2026-09-30")

    def test_not_leveraged(self):
        out = phase2.parse_asset_alloc(self._raw(89.18, 0.03))
        self.assertFalse(out["leveraged"])

    def test_bad_json(self):
        self.assertIsNone(phase2.parse_asset_alloc("{bad"))


class MetricUnitsTest(unittest.TestCase):
    def test_decimal_metric_to_percent(self):
        raw = '{"1095": {"annual_dec": 0.03, "mdd_dec": 0.01, "vol_dec": 0.02}}'
        self.assertAlmostEqual(phase2._pct_metric(raw, "1095", "annual_dec"), 3.0)
        self.assertAlmostEqual(phase2._pct_metric(raw, "1095", "mdd_dec"), 1.0)
        self.assertIsNone(phase2._pct_metric(None, "1095", "mdd_dec"))


class Long5Test(unittest.TestCase):
    def test_score(self):
        pcts = {k: 70.0 for k in phase2.LONG5_WEIGHTS}
        out = phase2.long5_score(pcts, 50.0)
        self.assertAlmostEqual(out["score"], 66.0, places=4)
        self.assertEqual(out["coverage"], 1.0)

    def test_low_coverage_returns_none(self):
        pcts = {"annual_5y": 70.0}
        out = phase2.long5_score(pcts, 50.0)
        self.assertIsNone(out["score"])
        self.assertLess(out["coverage"], 0.6)


if __name__ == "__main__":
    unittest.main()
