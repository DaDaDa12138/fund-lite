"""报告 v2 关键渲染与回撤贡献测试。"""
import unittest

from fundana.publish import report_v2


class TestReportV2(unittest.TestCase):
    def test_evidence_match_requires_same_nav_date(self):
        run = {"nav_as_of": "2026-11-04"}
        old = {"as_of": "2026-11-02", "dsr": 0.66}
        self.assertFalse(report_v2._evidence_match(run, old))
        self.assertIn("不匹配", report_v2._evidence_label(run, old))
        fresh = {"as_of": "2026-11-04", "dsr": 0.66}
        self.assertTrue(report_v2._evidence_match(run, fresh))
        self.assertIn("本次", report_v2._evidence_label(run, fresh))

    def test_survivorship_note_states_partial_correction(self):
        text = report_v2._survivorship_note({"survivorship": {
            "cohorts": 6, "liquidated_funds": 445, "lifecycle_liquidated_total": 1819,
            "lifecycle_with_nav": 458, "rank_ic_annual_ex_survivors": 0.378,
            "rank_ic_annual_with_liquidated": 0.3712, "rank_ic_annual_delta": -0.0068,
            "composite_ic_ex_survivors": 0.1933, "composite_ic_with_liquidated": 0.1936,
        }})
        self.assertIn("6 个半年队列", text)
        self.assertIn("445", text)
        self.assertIn("0.3780→0.3712", text)
        self.assertIn("部分校正", text)

    def test_corr_text_renders_pair(self):
        text = report_v2._corr_text({"a": "006596", "b": "016799", "corr": 0.9052, "common_obs": 242})
        self.assertIn("006596 vs 016799", text)
        self.assertIn("0.905", text)
        self.assertIn("242", text)

    def test_long_table_has_5y_annual(self):
        md = report_v2._long_table([{
            "rank": 1, "fcode": "006980", "name": "国寿安保泰恒纯债债券",
            "style_code": "bond_pure", "long_score": 74.57,
            "annual_3y_pct": 5.39, "mdd_3y_pct": 0.91, "annual_5y_pct": 4.72,
            "coverage_ratio_5y": 0.999, "manager_tenure_min_days": 2288,
            "share_scale_billion": 10.85, "cost_3y_pct": 0.43,
            "why_selected": "固定线通过", "caveat": "5 年覆盖完整",
        }])
        self.assertIn("4.72%", md)
        self.assertIn("99.9%", md)
        self.assertIn("为什么选它", md)

    def test_clip_text_does_not_cut_numbers(self):
        text = "通过固定线：综合分 70.81（门槛 62）、1 年非负日占比 80.99%（门槛 70%）、1 年持有成本 0.46%（上限 1.30%）。"
        clipped = report_v2._clip_text(text, 60)
        self.assertTrue(clipped.endswith("…"))
        self.assertNotIn("0.…", clipped)
        self.assertNotIn("0.46%…", clipped)
        self.assertIn("1 年持有成本", text)

    def test_metrics_table_has_3y_5y_sharpe_calmar_cost(self):
        md = report_v2._metrics_table([{
            "rank": 1, "fcode": "006980", "name": "国寿安保泰恒纯债债券",
            "annual_3y_pct": 5.39, "mdd_3y_pct": 0.91, "sharpe_3y": 2.5,
            "calmar_3y": 5.9, "annual_5y_pct": 4.72, "mdd_5y_pct": 1.2,
            "sharpe_5y": 2.1, "calmar_5y": 3.9, "coverage_ratio_5y": 0.999,
            "cost_3y_pct": 0.42, "cost_5y_pct": 0.412,
        }])
        for token in ("3年Sharpe", "3年Calmar", "5年MDD", "5年Sharpe", "5年Calmar", "5年成本"):
            self.assertIn(token, md)
        self.assertIn("0.41%", md)

    def test_share_table_has_1y_3y_5y_costs(self):
        md = report_v2._share_table([{
            "rank": 1, "fcode": "006596", "name": "国泰聚禾纯债债券A",
            "share_class_compare": {
                "class_letter": "A",
                "members": [{"fcode": "006596", "class_letter": "A",
                             "cost_1y_pct": 0.46, "cost_3y_pct": 0.42, "cost_5y_pct": 0.412}],
                "recommended": {"class_letter": "C", "cost_3y_pct": 0.41},
                "advantage_3y_pct": 0.01, "break_even_years": 6.0,
            },
        }])
        for token in ("当前1年", "当前3年", "当前5年", "推荐3年"):
            self.assertIn(token, md)
        self.assertIn("0.41%", md)

    def test_core10_section_has_company_bucket_and_overlap(self):
        core = [{
            "rank": 1, "fcode": "006980", "name": "国寿安保泰恒纯债债券",
            "company_name": "国寿安保基金", "family_id": "F1",
            "style_code": "bond_pure", "rank_score": 80.1, "formal_score": 75.2,
            "annual_1y_pct": 4.1, "annual_3y_pct": 5.39, "mdd_3y_pct": 0.91,
            "annual_5y_pct": 4.72, "coverage_ratio_5y": 0.999, "cost_3y_pct": 0.43,
            "why_selected": "固定线通过",
        }]
        md, data = report_v2._core10_section(
            core, core, {"006980": {"duration": "mid", "credit": "high"}},
            {"effective_bets": 1.72})
        self.assertIn("国寿安保基金", md)
        self.assertIn("久期桶", md)
        self.assertIn("中", md)
        self.assertIn("与稳健榜重叠 1/1", md)
        self.assertIn("1.720", md)
        self.assertEqual(data["overlap_stable"], ["006980"])

    def test_core10_section_empty_is_explicit(self):
        md, data = report_v2._core10_section([], [], {}, None)
        self.assertIn("core10 为空", md)
        self.assertEqual(data["rows"], [])

    def test_cross_board_counts_enhanced(self):
        boards = {
            "bond_stable": [{"fcode": "A", "family_id": "f1"}],
            "bond_enhanced": [{"fcode": "B", "family_id": "f2"}],
            "bond_core": [], "bond_core10": [], "bond_long": [],
        }
        got = report_v2._cross_board(boards)
        self.assertEqual(got["rows"], 2)
        self.assertEqual(got["unique_families"], 2)

    def test_style_benchmark_is_percentage_points(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE fund_metric(fcode TEXT, period_days INT,"
                     " formula_contract TEXT, annual_dec REAL, end_date TEXT)")
        conn.execute("CREATE TABLE fund_style(fcode TEXT, style_code TEXT)")
        for code, val in (("A", 0.03), ("B", 0.02), ("C", 0.04)):
            conn.execute("INSERT INTO fund_metric VALUES(?,?,?,?,?)",
                         (code, 365, "x", val, "2026-11-01"))
            conn.execute("INSERT INTO fund_style VALUES(?,?)", (code, "bond_pure"))
        got = report_v2._style_benchmarks(
            conn, {"formula_contract": "x", "nav_as_of": "2026-11-02"},
            [{"style_code": "bond_pure"}])
        self.assertEqual(got["bond_pure"]["365"], 3.0)

    def test_provenance_reuses_gate_evidence(self):
        gate = {"checks": [{"code": "provenance_integrity", "evidence": {
            "status": "ok", "tables": [{"rows": 10}, {"rows": 5}],
            "missing_total": 0, "bad_format_total": 0, "unregistered_sources": [],
        }}]}
        got = report_v2._provenance(None, None, gate)
        self.assertEqual(got["tables"], 2)
        self.assertEqual(got["checked_rows"], 15)
        self.assertEqual(got["status"], "ok")

    def test_provenance_text_distinguishes_total_and_checked(self):
        text = report_v2._provenance_text({
            "tables": 12, "total_rows": 14824337, "checked_rows": 3159203,
            "nav_since": "2026-08-07", "missing_raw": 0, "bad_format": 0,
            "unregistered": 0,
        })
        self.assertIn("14,824,337", text)
        self.assertIn("本次门禁核对 3,159,203 行", text)
        self.assertIn("NAV 取 2026-08-07 起近 90 天", text)

    def test_formal_counts_explain_family_fold(self):
        l9, published, folded = report_v2._formal_counts(
            {"formal_count": 159}, [{"fcode": "A"}, {"fcode": "B"}])
        self.assertEqual((l9, published, folded), (159, 2, 157))

    def test_mdd_contribution_compounds_returns(self):
        series = {
            "A": [("2026-01-01", 0.0), ("2026-01-02", 0.01), ("2026-01-03", -0.02)],
            "B": [("2026-01-01", 0.0), ("2026-01-02", -0.01), ("2026-01-03", 0.01)],
        }
        got = report_v2._mdd_contribution(series)
        self.assertTrue(got)
        self.assertTrue(all(x["contribution_pct"] <= 0 for x in got))
