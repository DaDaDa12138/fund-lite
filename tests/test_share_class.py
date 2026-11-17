"""A/C 份额持有期成本比较测试。"""
import json
import sqlite3
import unittest

from fundana.fees.share_class import compare


def _conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
    CREATE TABLE fund_share_class(fcode TEXT PRIMARY KEY, family_id TEXT,
                                  class_letter TEXT, is_primary INTEGER);
    CREATE TABLE fund_profile(fcode TEXT PRIMARY KEY, recurring_fee_pct REAL,
                              max_sub_fee_pct REAL, field_src_json TEXT);
    CREATE TABLE fund_fee_meta(fcode TEXT PRIMARY KEY, has_discount INTEGER,
                              redemption_1y_pct REAL, redemption_3y_pct REAL);
    """)
    rows = [
        ("006596", "F1", "A", 1, 0.40, 0.60, 1),
        ("028497", "F1", "C", 0, 0.41, 0.0, 0),
        ("007408", "F2", "A", 1, 0.40, 0.60, 1),
        ("007409", "F2", "C", 0, 0.80, 0.0, 0),
    ]
    for code, fam, letter, primary, rec, sub, disc in rows:
        c.execute("INSERT INTO fund_share_class VALUES(?,?,?,?)", (code, fam, letter, primary))
        c.execute("INSERT INTO fund_profile VALUES(?,?,?,?)",
                  (code, rec, sub, json.dumps({"share_class_scale_billion": 10.0})))
        c.execute("INSERT INTO fund_fee_meta VALUES(?,?,?,?)", (code, disc, 0.0, 0.0))
    c.commit()
    return c


class TestShareClass(unittest.TestCase):
    def test_break_even_and_recommendation(self):
        c = _conn()
        got = compare(c, "006596")
        self.assertEqual(got["recommended"]["fcode"], "028497")
        self.assertAlmostEqual(got["current_cost_3y_pct"], 0.42)
        self.assertAlmostEqual(got["recommended_cost_3y_pct"], 0.41)
        self.assertAlmostEqual(got["break_even_years"], 6.0)
        self.assertAlmostEqual(got["advantage_3y_pct"], 0.01)

    def test_a_class_dominates_high_fee_c(self):
        c = _conn()
        got = compare(c, "007409")
        self.assertEqual(got["recommended"]["fcode"], "007408")
        self.assertAlmostEqual(got["current_cost_3y_pct"], 0.80)
        self.assertAlmostEqual(got["recommended_cost_3y_pct"], 0.42)
        self.assertAlmostEqual(got["break_even_years"], 0.15)
