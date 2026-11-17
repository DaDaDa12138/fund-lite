"""v9 影子排序的纯函数契约测试。"""
import unittest

from fundana.screen.shadow_rank import v9_long_score, v9_scores


class TestV9ShadowScore(unittest.TestCase):
    def setUp(self):
        self.pcts = {"return_1m": 60.0, "return_3m": 60.0, "sharpe": 60.0,
                     "sortino": 50.0, "vol": 60.0,
                     "annual_3y": 80.0, "consistency": 70.0, "mdd": 60.0,
                     "calmar": 40.0, "return_6m": 90.0}

    def test_long_score_weights(self):
        # 0.30*80 + 0.25*70 + 0.20*60 + 0.15*50 + 0.10*40
        self.assertAlmostEqual(v9_long_score(self.pcts), 65.0)

    def test_rank_score(self):
        out = v9_scores(self.pcts)
        self.assertAlmostEqual(out["v9_composite"], 62.4)
        self.assertAlmostEqual(out["v9_rank_score"], 66.54)
        self.assertEqual(out["v9_missing_dims"], [])

    def test_missing_long_dimension_is_zero_not_renormalized(self):
        p = dict(self.pcts)
        p.pop("annual_3y")
        out = v9_scores(p)
        self.assertAlmostEqual(out["v9_long"], 41.0)
        self.assertAlmostEqual(out["v9_composite"], 48.0)
        self.assertEqual(out["v9_missing_dims"], ["annual_3y"])
        self.assertAlmostEqual(out["v9_rank_score"], 54.3)

    def test_missing_short_dimensions_are_zero(self):
        p = {k: v for k, v in self.pcts.items()
             if k in ("annual_3y", "consistency", "mdd", "sortino", "calmar", "return_6m")}
        out = v9_scores(p)
        self.assertAlmostEqual(out["v9_composite"], 42.0)
        self.assertAlmostEqual(out["v9_rank_score"], 49.2)
        self.assertEqual(out["v9_missing_dims"],
                         ["return_1m", "return_3m", "sharpe", "vol"])


if __name__ == "__main__":
    unittest.main()
