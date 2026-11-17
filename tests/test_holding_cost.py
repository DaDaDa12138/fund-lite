"""持有期总成本（ADR-059）契约测试。

要点：A/C 两类必须放在同一把尺子上——A 类一次性申购费按持有期摊薄，
C 类销售服务费逐年计入。旧口径只看持续计提费，会系统性偏袒 A 类。
"""
import sqlite3
import unittest

from fundana.fees import holding
from fundana.screen import thresholds as T
from fundana.screen.engine import fixed_checks

from test_screen import make_cand


class TestHoldingCost(unittest.TestCase):
    def test_one_year_no_discount(self):
        row = holding.holding_cost(0.40, 0.80, None, 365)
        self.assertAlmostEqual(row["purchase_effective_pct"], 0.80)
        self.assertAlmostEqual(row["total_pct"], 1.20)

    def test_one_year_with_discount(self):
        row = holding.holding_cost(0.40, 0.80, True, 365)
        self.assertAlmostEqual(row["purchase_effective_pct"], 0.08)
        self.assertAlmostEqual(row["total_pct"], 0.48)
        self.assertTrue(row["discount_applied"])

    def test_three_year_amortization(self):
        row = holding.holding_cost(0.40, 0.80, False, 1095)
        self.assertAlmostEqual(row["amortized_purchase_pct"], 0.80 / 3.0, places=4)
        self.assertAlmostEqual(row["total_pct"], 0.6667, places=4)

    def test_missing_recurring_returns_none(self):
        self.assertIsNone(holding.holding_cost(None, 0.8, False, 365))
        self.assertIsNone(holding.holding_cost(0.4, 0.8, False, 0))

    def test_missing_purchase_fee_treated_as_zero(self):
        row = holding.holding_cost(0.40, None, False, 365)
        self.assertAlmostEqual(row["total_pct"], 0.40)

    def test_a_class_cheaper_than_c_class_at_one_year(self):
        # 002073(A) 0.30+0.10 经常性 + 0.80 申购费 1 折；002074(C) 0.30+0.10+0.40 经常性
        a = holding.holding_cost(0.40, 0.80, True, 365)["total_pct"]
        c = holding.holding_cost(0.80, None, None, 365)["total_pct"]
        self.assertLess(a, c)
        self.assertAlmostEqual(a, 0.48)
        self.assertAlmostEqual(c, 0.80)

    def test_cost_profile_horizons(self):
        prof = holding.cost_profile(0.40, 0.80, True)
        self.assertEqual(sorted(prof), [365, 1095, 1825])
        self.assertLess(prof[1095]["total_pct"], prof[365]["total_pct"])
        self.assertLess(prof[1825]["total_pct"], prof[1095]["total_pct"])

    def test_five_year_amortization(self):
        row = holding.holding_cost(0.40, 0.80, False, 1825)
        self.assertAlmostEqual(row["amortized_purchase_pct"], 0.16, places=4)
        self.assertAlmostEqual(row["total_pct"], 0.56, places=4)

    def test_cost_sql_matches_python(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE fund_profile(fcode TEXT, recurring_fee_pct REAL,"
                     " max_sub_fee_pct REAL)")
        conn.execute("CREATE TABLE fund_fee_meta(fcode TEXT, has_discount INTEGER)")
        conn.executemany("INSERT INTO fund_profile VALUES(?,?,?)",
                         [("A", 0.40, 0.80), ("B", 0.80, None), ("C", 0.30, 1.20)])
        conn.executemany("INSERT INTO fund_fee_meta VALUES(?,?)",
                         [("A", 1), ("B", None), ("C", None)])
        sql = ("SELECT p.fcode, %s AS c1, %s AS c3, %s AS c5 FROM fund_profile p"
               " LEFT JOIN fund_fee_meta fm ON fm.fcode=p.fcode ORDER BY p.fcode"
               % (holding.cost_sql("p.fcode", alias="p"),
                  holding.cost_sql("p.fcode", alias="p", horizon_days=1095),
                  holding.cost_sql("p.fcode", alias="p", horizon_days=1825)))
        got = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(sql)}
        for fcode, rec, sub, disc in (("A", 0.40, 0.80, 1), ("B", 0.80, None, None),
                                      ("C", 0.30, 1.20, None)):
            for i, h in enumerate((365, 1095, 1825)):
                self.assertAlmostEqual(got[fcode][i],
                                       holding.holding_cost(rec, sub, disc, h)["total_pct"],
                                       places=4)


class TestFixedChecksCost(unittest.TestCase):
    def test_fee_check_uses_total_cost(self):
        cand = make_cand(recurring_fee_pct=0.30, max_sub_fee_pct=0.80)
        checks = fixed_checks(cand, {"composite": 70.0})
        self.assertAlmostEqual(checks["fee"]["value"], 1.10)
        self.assertEqual(checks["fee"]["max"], T.FIXED["fee_max_pct"])
        self.assertFalse(checks["fee"]["pass"])          # 1.10 > 0.90

    def test_discounted_purchase_fee_passes(self):
        cand = make_cand(recurring_fee_pct=0.30, max_sub_fee_pct=0.80, has_discount=1)
        checks = fixed_checks(cand, {"composite": 70.0})
        self.assertAlmostEqual(checks["fee"]["value"], 0.38)
        self.assertTrue(checks["fee"]["pass"])


if __name__ == "__main__":
    unittest.main()
