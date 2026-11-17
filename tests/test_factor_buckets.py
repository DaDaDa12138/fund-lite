"""P2-1 因子桶的纯函数测试。"""
import unittest

from fundana import factor_buckets as fb


class TestFactorBuckets(unittest.TestCase):
    def test_classify_requires_coverage_and_r2(self):
        self.assertEqual(fb.classify(0.1, 0.2, 0.40, 242)["duration"], "short")
        self.assertEqual(fb.classify(0.5, 0.2, 0.40, 242)["duration"], "mid")
        self.assertEqual(fb.classify(1.2, 0.2, 0.40, 242)["duration"], "long")
        self.assertEqual(fb.classify(1.2, 0.2, 0.01, 242)["duration"], "unknown")
        self.assertEqual(fb.classify(1.2, 0.2, 0.40, 100)["duration"], "unknown")

    def test_style_matrix_keeps_unknown(self):
        rows = [{"fcode": "a", "style_code": "bond_pure"},
                {"fcode": "b", "style_code": "bond_pure"},
                {"fcode": "c", "style_code": "bond_short"}]
        buckets = {"a": {"duration": "mid", "reliable": True, "beta_duration": 0.5},
                   "b": {"duration": "unknown", "reliable": False, "beta_duration": None},
                   "c": {"duration": "short", "reliable": True, "beta_duration": 0.1}}
        out = fb.style_duration_matrix(rows, buckets)
        self.assertEqual(out["matrix"]["bond_pure"]["mid"], 1)
        self.assertEqual(out["matrix"]["bond_pure"]["unknown"], 1)
        self.assertEqual(out["matrix"]["bond_short"]["short"], 1)
        self.assertEqual(out["unknown"], 1)
        self.assertEqual(out["dispersion"]["bond_pure"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
