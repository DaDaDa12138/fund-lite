"""收藏（自选）模块测试：添加、去重、列表、删除、代码校验。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fundana.api import watchlist as wl


SCHEMA = """
CREATE TABLE fund (fcode TEXT PRIMARY KEY, name TEXT, type_text TEXT, estab_date TEXT);
CREATE TABLE fund_style (fcode TEXT PRIMARY KEY, style_code TEXT, confidence TEXT);
CREATE TABLE fund_profile (fcode TEXT PRIMARY KEY, asset_scale_billion REAL,
                           recurring_fee_pct REAL, max_sub_fee_pct REAL);
CREATE TABLE fund_fee_meta (fcode TEXT PRIMARY KEY, mgr_fee_pct REAL, trust_fee_pct REAL,
                            sales_fee_pct REAL, has_discount INTEGER, source TEXT);
CREATE TABLE fund_buyable_daily (fcode TEXT, business_date TEXT, buyable_state TEXT,
                                 purchase_status TEXT, next_open_date TEXT);
CREATE TABLE fund_metric (fcode TEXT, period_days INTEGER, formula_contract TEXT,
                          annual_dec REAL, sharpe REAL, mdd_dec REAL,
                          positive_ratio_pct REAL);
CREATE TABLE fund_nav_daily (fcode TEXT, nav_date TEXT, unit_nav REAL, acc_nav REAL,
                             growth_pct REAL);
CREATE TABLE fund_watchlist (fcode TEXT PRIMARY KEY, added_at TEXT NOT NULL,
                             source TEXT, note TEXT);
"""


class WatchlistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(Path(self.tmp.name) / "t.db")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO fund VALUES('000032','易方达信用债A','债券型-长债','2013-04-24')")
        self.conn.execute("INSERT INTO fund VALUES('000003','中海可转债A','债券型-混合二级','2013-03-20')")
        self.conn.execute("INSERT INTO fund_style VALUES('000032','bond_pure','text_heuristic')")
        self.conn.execute("INSERT INTO fund_profile VALUES('000032',185.64,0.45,0.80)")
        self.conn.execute("INSERT INTO fund_fee_meta VALUES('000032',0.30,0.10,0.05,1,'t')")
        self.conn.execute("INSERT INTO fund_buyable_daily VALUES"
                          "('000032','2026-10-31','confirmed_yes','开放申购',NULL)")
        self.conn.execute("INSERT INTO fund_metric VALUES"
                          "('000032',365,'fundana-risk-2026.11.3',0.0344,2.61,0.0031,58.0)")
        self.conn.execute("INSERT INTO fund_nav_daily VALUES('000032','2026-10-29',1.1313,1.6591,-0.30)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_add_then_list_and_remove(self):
        item = wl.add(self.conn, "000032", "fundana-risk-2026.11.3", source="rankings")
        self.assertEqual(item["fcode"], "000032")
        self.assertEqual(item["name"], "易方达信用债A")
        rows = wl.list_items(self.conn, "fundana-risk-2026.11.3")
        self.assertEqual([r["fcode"] for r in rows], ["000032"])
        self.assertAlmostEqual(rows[0]["annual_1y_dec"], 0.0344, places=6)
        self.assertEqual(rows[0]["unit_nav"], 1.1313)
        self.assertTrue(wl.remove(self.conn, "000032"))
        self.assertEqual(wl.list_items(self.conn, "fundana-risk-2026.11.3"), [])

    def test_duplicate_and_unknown_code_rejected(self):
        wl.add(self.conn, "000032", "fundana-risk-2026.11.3")
        with self.assertRaises(wl.WatchlistError):
            wl.add(self.conn, "000032", "fundana-risk-2026.11.3")
        with self.assertRaises(wl.WatchlistError):
            wl.add(self.conn, "999999", "fundana-risk-2026.11.3")
        with self.assertRaises(wl.WatchlistError):
            wl.add(self.conn, "   ", "fundana-risk-2026.11.3")

    def test_remove_missing_returns_false(self):
        self.assertFalse(wl.remove(self.conn, "000032"))

    def test_list_orders_by_added_desc(self):
        wl.add(self.conn, "000032", "fundana-risk-2026.11.3")
        wl.add(self.conn, "000003", "fundana-risk-2026.11.3")
        codes = [r["fcode"] for r in wl.list_items(self.conn, "fundana-risk-2026.11.3")]
        self.assertEqual(codes[0], "000003")   # 最后加入的排最前
        self.assertEqual(set(codes), {"000032", "000003"})


if __name__ == "__main__":
    unittest.main()
