"""统计验证纯函数测试（P1-3）。"""
from __future__ import annotations

import unittest

from fundana.audit import stat_validation as sv


class TestStatValidation(unittest.TestCase):
    def test_bootstrap_constant(self):
        out = sv.block_bootstrap_mean([0.001] * 100, block_size=10, n_boot=200)
        self.assertAlmostEqual(out["mean"], 0.001)
        self.assertAlmostEqual(out["ci95"][0], 0.001)
        self.assertAlmostEqual(out["ci95"][1], 0.001)
        self.assertEqual(out["p_mean_le_zero"], 0.0)

    def test_bootstrap_short_sample(self):
        out = sv.block_bootstrap_mean([0.001])
        self.assertEqual(out["n"], 1)
        self.assertIsNone(out["ci95"][0])
        self.assertEqual(out["n_boot"], 0)

    def test_deflated_sharpe_penalizes_trials(self):
        one = sv.deflated_sharpe(0.08, 1, 1000)
        many = sv.deflated_sharpe(0.08, 256, 1000)
        self.assertGreater(one["dsr"], many["dsr"])
        self.assertGreater(one["dsr"], 0.95)
        self.assertGreater(many["sr0"], one["sr0"])

    def test_deflated_sharpe_edges(self):
        self.assertIsNone(sv.deflated_sharpe(3.0, 1, 1)["dsr"])
        self.assertIsNone(sv.deflated_sharpe(3.0, 0, 100)["dsr"])

    def test_daily_returns_and_alignment(self):
        curve = {"dates": ["2024-01-01", "2024-01-02", "2024-01-03"],
                 "values": [1.0, 1.01, 1.005]}
        rets = sv.daily_returns(curve)
        self.assertEqual(len(rets), 2)
        self.assertAlmostEqual(rets[0][1], 0.01)
        self.assertAlmostEqual(rets[1][1], 1.005 / 1.01 - 1.0)
        excess = sv.aligned_excess(rets, [("2024-01-02", 0.005), ("2024-01-03", 0.0)])
        self.assertEqual(len(excess), 2)
        self.assertAlmostEqual(excess[0], 0.005)


if __name__ == "__main__":
    unittest.main()
