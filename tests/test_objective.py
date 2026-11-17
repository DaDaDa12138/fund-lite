"""P1-1 目标函数契约测试。"""
import unittest

from fundana.screen import objective


def _row(**over):
    base = {
        "is_full_3y": True,
        "coverage_ratio_5y": 0.80,
        "annual_5y_pct": 3.5,
        "risk_status": "ok",
        "buyable_state": "confirmed_yes",
        "share_scale_billion": 3.0,
        "cost_1y_pct": 0.50,
        "manager_tenure_coverage_3y": 0.90,
    }
    base.update(over)
    return base


def _bench(n=9, positive=5):
    rows = []
    for i in range(n):
        rows.append({
            "excess_1y_pct": 1.0 if i < positive else -0.5,
            "excess_3y_pct": 1.0 if i < positive else -0.5,
        })
    return {"type": "etf_proxy", "rows": rows}


def _by_id(result):
    return {c["id"]: c for c in result["criteria"]}


class TestObjective(unittest.TestCase):
    def test_all_dimensions_pass_is_validated(self):
        rows = [_row() for _ in range(4)]
        result = objective.evaluate(rows, _bench(9, 9), {"matured_formal_observations": 5}, 0.96)
        self.assertEqual(result["verdict"], "validated")
        self.assertTrue(all(c["status"] == "pass" for c in result["criteria"]))
        self.assertEqual(result["ratios"]["risk_block"], 0.0)

    def test_watch_only_is_partial_not_fail(self):
        rows = [_row(), _row(risk_status="watch")]
        result = objective.evaluate(rows, _bench(9, 6), {"matured_formal_observations": 1}, 0.96)
        d = _by_id(result)["drawdown"]
        self.assertEqual(d["status"], "partial")
        self.assertIn("block 0", d["evidence"])
        self.assertIn("watch/unknown 1", d["evidence"])

    def test_risk_block_fails(self):
        rows = [_row(), _row(risk_status="block")]
        result = objective.evaluate(rows, _bench(9, 6), {"matured_formal_observations": 1}, 0.96)
        self.assertEqual(_by_id(result)["drawdown"]["status"], "fail")

    def test_benchmark_requires_sixty_percent_positive(self):
        rows = [_row()]
        partial = objective.evaluate(rows, _bench(9, 5), {"matured_formal_observations": 1}, 0.96)
        self.assertEqual(_by_id(partial)["benchmark_excess"]["status"], "partial")
        passed = objective.evaluate(rows, _bench(9, 6), {"matured_formal_observations": 1}, 0.96)
        self.assertEqual(_by_id(passed)["benchmark_excess"]["status"], "pass")

    def test_benchmark_missing_one_horizon_is_not_compared(self):
        rows = [_row()]
        bench = {"type": "etf_proxy", "rows": [
            {"excess_1y_pct": 1.0, "excess_3y_pct": None},
            {"excess_1y_pct": 1.0, "excess_3y_pct": 1.0},
        ]}
        result = objective.evaluate(rows, bench, {"matured_formal_observations": 1}, 0.96)
        b = _by_id(result)["benchmark_excess"]
        self.assertIn("1 个风格", b["evidence"])
        self.assertEqual(b["status"], "pass")

    def test_no_matured_forward_is_candidate_pool(self):
        rows = [_row()]
        result = objective.evaluate(rows, _bench(9, 9), {"matured_formal_observations": 0}, 0.66)
        self.assertEqual(result["verdict"], "candidate_pool")
        self.assertEqual(_by_id(result)["validation"]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
