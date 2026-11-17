"""费用阶梯解析/落库单测（不联网）。"""
from __future__ import annotations

import sqlite3
import unittest

from fundana.fees import store
from fundana.fetch import parsers


class TestDaysRange(unittest.TestCase):
    def test_not_less_than(self):
        self.assertEqual(parsers._days_range("开放运作期时持有不小于7天"), (7, None))

    def test_less_than_is_exclusive(self):
        self.assertEqual(parsers._days_range("开放运作期时持有小于7天"), (None, 6))

    def test_both_bounds_exclusive_upper(self):
        self.assertEqual(parsers._days_range("大于等于7天，小于30天"), (7, 29))

    def test_both_bounds_inclusive_upper(self):
        self.assertEqual(parsers._days_range("大于等于7天，小于等于29天"), (7, 29))

    def test_years(self):
        self.assertEqual(parsers._days_range("大于等于1年，小于2年"), (365, 729))

    def test_symbol_forms(self):
        self.assertEqual(parsers._days_range("T<7日"), (None, 6))
        self.assertEqual(parsers._days_range("7日≤T<1个封闭期"), (7, None))
        self.assertEqual(parsers._days_range("T≥1个封闭期"), (None, None))

    def test_unparseable_is_none(self):
        self.assertEqual(parsers._days_range("受限开放期"), (None, None))
        self.assertEqual(parsers._days_range("其他情况的赎回费率为0%。"), (None, None))


class TestParseJjfl(unittest.TestCase):
    def test_redemption_labels_and_bounds(self):
        html = """<h4>赎回费率</h4><table>
        <tr><th>适用期限</th><th>赎回费率</th></tr>
        <tr><td>开放运作期时持有不小于7天</td><td>1.50%</td></tr>
        <tr><td>开放运作期时持有小于7天</td><td>0.00%</td></tr>
        </table>"""
        parsed = parsers.parse_jjfl(html)
        rows = parsed["redemption"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["days_lo"], 7)
        self.assertIsNone(rows[0]["days_hi"])
        self.assertEqual(rows[0]["label"], "开放运作期时持有不小于7天")
        self.assertEqual(rows[1]["days_hi"], 6)
        self.assertEqual(rows[1]["label"], "开放运作期时持有小于7天")


class TestFeeStore(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE fund_fee_tier(fcode TEXT, kind TEXT, tier_no INTEGER,
          amount_lo_cny REAL, amount_hi_cny REAL, days_lo INTEGER, days_hi INTEGER,
          rate_pct REAL, discount_pct REAL, fixed_cny REAL, observed_at TEXT,
          raw_sha256 TEXT, raw_label TEXT);
        CREATE TABLE fund_fee_schedule(fcode TEXT, kind TEXT, tier_no INTEGER,
          amount_lo REAL, amount_hi REAL, days_lo INTEGER, days_hi INTEGER,
          rate_pct REAL, src TEXT, raw_label TEXT);
        CREATE TABLE fund_fee_meta(fcode TEXT PRIMARY KEY, mgr_fee_pct REAL,
          trust_fee_pct REAL, sales_fee_pct REAL, has_discount INTEGER, source TEXT,
          observed_at TEXT, raw_sha256 TEXT, redemption_1y_pct REAL,
          redemption_3y_pct REAL);
        """)
        self.parsed = {
            "operating": {"mgr_fee_pct": 0.3, "trust_fee_pct": 0.1, "sales_fee_pct": 0.0},
            "purchase": [{"amount_lo_cny": 0.0, "amount_hi_cny": 1_000_000.0,
                          "rate_pct": 0.8, "discount_pct": 0.08, "fixed_cny": None,
                          "label": "小于100万"}],
            "subscription": [],
            "redemption": [
                {"days_lo": 7, "days_hi": None, "rate_pct": 1.5, "fixed_cny": None,
                 "label": "开放运作期时持有不小于7天"},
                {"days_lo": None, "days_hi": 6, "rate_pct": 0.0, "fixed_cny": None,
                 "label": "开放运作期时持有小于7天"},
            ],
        }

    def test_write_tiers_and_schedule(self):
        written = store.write_fee_tiers(self.conn, "000111", self.parsed, "2026-11-02", "sha")
        self.assertEqual(written["tiers"], 3)
        self.assertEqual(written["schedule"], 3)
        rows = self.conn.execute(
            "SELECT kind, tier_no, rate_pct, raw_label FROM fund_fee_schedule ORDER BY kind, tier_no").fetchall()
        self.assertEqual([r["kind"] for r in rows], ["red", "red", "sub"])
        self.assertEqual(rows[0]["rate_pct"], 1.5)
        self.assertEqual(rows[0]["raw_label"], "开放运作期时持有不小于7天")
        self.assertEqual(rows[2]["rate_pct"], 0.08)

    def test_write_meta_redemption_snapshot(self):
        meta = store.write_fee_meta(self.conn, "000111", self.parsed, "2026-11-02", "sha")
        self.assertEqual(meta["redemption_1y_pct"], 1.5)
        self.assertEqual(meta["redemption_3y_pct"], 1.5)
        row = self.conn.execute("SELECT has_discount, redemption_1y_pct FROM fund_fee_meta").fetchone()
        self.assertEqual(row["has_discount"], 1)
        self.assertEqual(row["redemption_1y_pct"], 1.5)


if __name__ == "__main__":
    unittest.main()
