"""why/caveat 解释生成测试。"""
import unittest

from fundana import explain


def _row(**kw):
    row = {
        "board": "bond_stable", "formal_score": 75.0, "composite": 70.0,
        "non_negative_1y_pct": 80.0, "cost_1y_pct": 0.45,
        "is_full_3y": True, "effective_years_3y": 3.0, "coverage_ratio_3y": 0.999,
        "coverage_ratio_5y": 0.999, "effective_years_5y": 5.0, "annual_5y_pct": 3.5,
        "manager_tenure_min_days": 1200, "share_scale_billion": 10.0,
        "risk_status": "ok",
    }
    row.update(kw)
    return row


class TestExplain(unittest.TestCase):
    def test_why_uses_actual_values(self):
        got = explain.build(_row())
        codes = {x["code"] for x in got["why"]}
        self.assertIn("fixed_lines", codes)
        self.assertIn("long_evidence", codes)
        self.assertIn("five_year_evidence", codes)
        text = explain.as_text(got["why"])
        self.assertIn("综合分 70.00", text)
        self.assertIn("5 年年化 3.50%", text)

    def test_5y_span_without_annual_is_caveat(self):
        got = explain.build(_row(annual_5y_pct=None))
        codes = {x["code"] for x in got["caveat"]}
        self.assertIn("five_year_annual_missing", codes)
        self.assertNotIn("five_year_evidence", {x["code"] for x in got["why"]})

    def test_near_floor_caveat(self):
        got = explain.build(_row(non_negative_1y_pct=70.5))
        self.assertIn("non_negative_near_floor", {x["code"] for x in got["caveat"]})

    def test_enhanced_board_does_not_claim_fixed_lines(self):
        got = explain.build(_row(board="bond_enhanced", style_code="bond_secondary_enhanced",
                                 non_negative_1y_pct=64.46, cost_1y_pct=1.05))
        codes = {x["code"] for x in got["why"]}
        self.assertIn("enhanced_admission", codes)
        self.assertNotIn("fixed_lines", codes)
        caveats = {x["code"]: x["text"] for x in got["caveat"]}
        self.assertIn("non_negative_below_floor", caveats)
        self.assertIn("低于 70%", caveats["non_negative_below_floor"])
        self.assertIn("1.30%", caveats["fee_near_cap"])

    def test_stable_fee_cap_uses_l6c_not_l55(self):
        got = explain.build(_row(cost_1y_pct=1.05))
        text = next(x["text"] for x in got["caveat"] if x["code"] == "fee_near_cap")
        self.assertIn("0.90%", text)
        self.assertIn("L6C", text)

    def test_long_board_flags_low_manager_coverage(self):
        got = explain.build(_row(board="bond_long", manager_tenure_min_days=745))
        text = next(x["text"] for x in got["caveat"] if x["code"] == "manager_coverage_low")
        self.assertIn("68.0%", text)
        self.assertIn("低于长期榜 80% 门槛", text)

    def test_long_board_partial_coverage_is_not_misreported(self):
        got = explain.build(_row(board="bond_long", manager_tenure_min_days=1039))
        text = next(x["text"] for x in got["caveat"] if x["code"] == "manager_partial_coverage")
        self.assertIn("94.9%", text)
        self.assertIn("满足长期榜 80% 门槛", text)
        self.assertNotIn("长期榜要求覆盖≥80%", text)

    def test_ongoing_drawdown_flag_is_not_rendered_as_percent(self):
        got = explain.build(_row(ongoing_drawdown=1))
        item = next(x for x in got["caveat"] if x["code"] == "ongoing_drawdown")
        self.assertIn("未修复回撤", item["text"])
        self.assertNotIn("1.00%", item["text"])
        self.assertNotIn("1.0%", item["text"])
