"""P2-15 OOS 研究模型的纯函数契约：不调参、不泄漏。"""
import unittest

from fundana.screen import oos


class TestOOSMath(unittest.TestCase):
    def test_spearman_perfect_and_inverse(self):
        self.assertAlmostEqual(oos._spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0, places=6)
        self.assertAlmostEqual(oos._spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0, places=6)

    def test_ridge_recovers_linear_relation(self):
        x = [[1.0, v] for v in (-2, -1, 0, 1, 2)]
        y = [2.0 + 3.0 * row[1] for row in x]
        beta = oos._ridge(x, y, lam=0.0)
        self.assertAlmostEqual(beta[0], 2.0, places=6)
        self.assertAlmostEqual(beta[1], 3.0, places=6)

    def test_standardize_centers(self):
        z = oos._standardize([1.0, 2.0, 3.0])
        self.assertAlmostEqual(sum(z) / 3, 0.0, places=9)
        self.assertAlmostEqual(oos._standardize([5.0, 5.0])[0], 0.0)

    def test_style_neutral_demeans_only_large_groups(self):
        rows = [{"fcode": str(i), "style": "bond_pure", "target": float(i)}
                for i in range(5)]
        rows += [{"fcode": "x", "style": "bond_short", "target": 9.0}]
        out = oos._style_neutral(rows, "target")
        self.assertAlmostEqual(sum(out[str(i)] for i in range(5)), 0.0, places=9)
        self.assertEqual(out["x"], 9.0)


if __name__ == "__main__":
    unittest.main()
