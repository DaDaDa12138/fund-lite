import sqlite3
import unittest

from fundana import alerts


SCHEMA = """
CREATE TABLE publication (publication_id TEXT PRIMARY KEY, run_id TEXT, generation INTEGER,
                          status TEXT);
CREATE TABLE screen_run (run_id TEXT PRIMARY KEY, business_date TEXT);
CREATE TABLE screen_pick (run_id TEXT, board TEXT, rank INTEGER, fcode TEXT,
                          formal_score REAL, stability_rank REAL, confidence REAL,
                          rank_score REAL);
CREATE TABLE fund (fcode TEXT PRIMARY KEY, name TEXT);
CREATE TABLE fund_nav_daily (fcode TEXT, nav_date TEXT, unit_nav REAL, acc_nav REAL,
                             growth_pct REAL);
CREATE TABLE fund_manager_tenure (fcode TEXT, mgr_id TEXT, mgr_name TEXT,
                                  start_date TEXT, end_date TEXT);
CREATE TABLE fund_scale_history (fcode TEXT, report_period TEXT,
                                 asset_scale_billion REAL, share_scale_billion REAL,
                                 PRIMARY KEY (fcode, report_period));
CREATE TABLE fund_share_class (fcode TEXT PRIMARY KEY, family_id TEXT);
CREATE TABLE screen_decision (run_id TEXT, fcode TEXT, style_code TEXT);
"""


class TestAlerts(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.executescript("""
        INSERT INTO publication VALUES ('p1','r1',1,'committed');
        INSERT INTO publication VALUES ('p2','r2',2,'committed');
        INSERT INTO screen_run VALUES ('r1','2026-10-30');
        INSERT INTO screen_run VALUES ('r2','2026-11-02');
        INSERT INTO fund VALUES ('000001','测试纯债A');
        INSERT INTO screen_pick VALUES ('r2','bond_core10',1,'000001',80.0,1.0,90.0,80.0);
        INSERT INTO screen_decision VALUES ('r1','000001','bond_short');
        INSERT INTO screen_decision VALUES ('r2','000001','bond_pure');
        INSERT INTO fund_manager_tenure VALUES ('000001','m1','张三','2025-08-01',NULL);
        INSERT INTO fund_scale_history VALUES ('000001','2025-06-30',1.0,0.8);
        INSERT INTO fund_share_class VALUES ('000001','F1');
        """)

    def test_scan_detects_events(self):
        out = alerts.scan(self.conn, as_of="2025-09-30", boards=["bond_core10"])
        codes = {a["code"] for a in out["alerts"]}
        self.assertIn("nav_missing", codes)
        self.assertIn("manager_change", codes)
        self.assertIn("scale_small", codes)
        self.assertIn("style_drift", codes)
        self.assertEqual(out["summary"]["funds"], 1)
        self.assertGreaterEqual(out["summary"]["alerts"], 4)

    def test_render(self):
        out = alerts.scan(self.conn, as_of="2025-09-30", boards=["bond_core10"])
        text = alerts.render(out)
        self.assertIn("产品告警", text)
        self.assertIn("000001", text)

    def test_no_publication(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        out = alerts.scan(conn)
        self.assertIsNone(out["publication_id"])
        self.assertEqual(out["alerts"], [])


if __name__ == "__main__":
    unittest.main()
