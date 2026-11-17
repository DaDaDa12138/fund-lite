"""P0-3：独立风险门（持仓集中度 + acc_nav 压力测试）。"""
import json
import sqlite3
import unittest

from fundana import risk_gate


SCHEMA = """
CREATE TABLE fund(fcode TEXT PRIMARY KEY, name TEXT, estab_date TEXT);
CREATE TABLE fund_style(fcode TEXT PRIMARY KEY, style_code TEXT);
CREATE TABLE fund_holding_bond(fcode TEXT, report_period TEXT, bond_code TEXT,
                               bond_name TEXT, pct_nav REAL, bond_class TEXT);
CREATE TABLE fund_nav_daily(fcode TEXT, nav_date TEXT, unit_nav REAL, acc_nav REAL);
CREATE TABLE src_pzd_profile(code TEXT, business_date TEXT, asset_alloc_json TEXT);
"""

ALLOC_OK = json.dumps({"series": [
    {"name": "股票占净比", "data": [0.0]},
    {"name": "债券占净比", "data": [100.0]},
    {"name": "现金占净比", "data": [0.0]},
]}, ensure_ascii=False)


class RiskGateTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def tearDown(self):
        self.conn.close()

    def fund(self, fcode="X", name="测试债基", estab="2018-01-01", style="bond_pure"):
        self.conn.execute("INSERT INTO fund VALUES(?,?,?)", (fcode, name, estab))
        self.conn.execute("INSERT INTO fund_style VALUES(?,?)", (fcode, style))
        self.conn.execute("INSERT INTO src_pzd_profile VALUES(?,?,?)",
                          (fcode, "2026-09-30", ALLOC_OK))

    def holdings(self, rows, fcode="X", period="2026Q3"):
        self.conn.executemany(
            "INSERT INTO fund_holding_bond VALUES(?,?,?,?,?,?)",
            [(fcode, period, "B%d" % i, name, pct, cls)
             for i, (name, pct, cls) in enumerate(rows)])

    def test_period_end(self):
        self.assertEqual(risk_gate.period_end("2026Q3").isoformat(), "2026-09-30")
        self.assertEqual(risk_gate.period_end("2026-06-30").isoformat(), "2026-06-30")
        self.assertIsNone(risk_gate.period_end("bad"))

    def test_issuer_key(self):
        self.assertEqual(risk_gate.issuer_key("21中国银行二级04"), "中国银行")
        self.assertEqual(risk_gate.issuer_key("25中行二级资本债01BC"), "中国银行")
        self.assertEqual(risk_gate.issuer_key("23邮储银行二级资本债01"),
                         "中国邮政储蓄银行")
        self.assertEqual(risk_gate.issuer_key("26邮政SCP002"), "中国邮政")
        self.assertEqual(risk_gate.issuer_key("23工行二级资本债02B"), "中国工商银行")

    def test_bond_kind(self):
        self.assertEqual(risk_gate.bond_kind("兴业转债"), "convertible")
        self.assertEqual(risk_gate.bond_kind("21国开03"), "rate")
        self.assertEqual(risk_gate.bond_kind("23工行二级资本债02B"), "credit")
        self.assertEqual(risk_gate.bond_kind("某债", "rate"), "rate")

    def test_holdings_concentration(self):
        self.fund()
        self.holdings([("23工行二级资本债01", 50.0, "credit"),
                       ("21中国银行二级04", 30.0, "credit"),
                       ("26邮政SCP002", 20.0, "credit")])
        h = risk_gate.holdings_risk(self.conn, "X", "2026-11-02")
        self.assertTrue(h["available"])
        self.assertAlmostEqual(h["top10_pct"], 100.0)
        self.assertAlmostEqual(h["top1_pct"], 50.0)
        self.assertAlmostEqual(h["credit_issuer_top_pct"], 50.0)
        self.assertAlmostEqual(h["credit_issuer_top3_pct"], 100.0)
        r = risk_gate.assess(self.conn, "X", "bond_pure", "2026-11-02")
        self.assertTrue(r["summary"]["blocked"])
        self.assertIn("top1", r["summary"]["blocked_flags"])

    def test_rate_like_skips_concentration(self):
        self.fund(estab="2024-01-01", style="bond_rate")
        self.holdings([("21国开01", 35.0, "rate"), ("21国开02", 35.0, "rate"),
                       ("21国开03", 30.0, "rate")])
        r = risk_gate.assess(self.conn, "X", "bond_rate", "2026-11-02")
        self.assertFalse(r["summary"]["blocked"])
        self.assertEqual(r["summary"]["status"], "ok")

    def test_pure_without_holdings_blocks(self):
        self.fund(estab="2024-01-01")
        r = risk_gate.assess(self.conn, "X", "bond_pure", "2026-11-02")
        self.assertTrue(r["summary"]["blocked"])
        self.assertIn("holdings_required", r["summary"]["blocked_flags"])

    def test_rate_without_holdings_is_unknown_not_block(self):
        self.fund(estab="2024-01-01", style="bond_rate")
        r = risk_gate.assess(self.conn, "X", "bond_rate", "2026-11-02")
        self.assertFalse(r["summary"]["blocked"])
        self.assertEqual(r["summary"]["status"], "unknown")

    def test_enhanced_without_holdings_blocks(self):
        self.fund(estab="2024-01-01", style="bond_secondary_enhanced")
        r = risk_gate.assess(self.conn, "X", "bond_secondary_enhanced", "2026-11-02")
        self.assertTrue(r["summary"]["blocked"])
        self.assertIn("holdings_required", r["summary"]["blocked_flags"])

    def test_accumulated_nav_ignores_dividend(self):
        self.fund("006431", estab="2018-01-01")
        self.conn.executemany(
            "INSERT INTO fund_nav_daily VALUES(?,?,?,?)",
            [("006431", "2022-10-31", 1.0782, 1.0778),
             ("006431", "2022-12-30", 1.0278, 1.0778)])
        s = risk_gate.stress_risk(self.conn, "006431", "2026-11-02")
        self.assertAlmostEqual(s["2022_q4"]["pct"], 0.0)

    def test_alloc_falls_back_to_report_period(self):
        self.conn.execute("INSERT INTO fund VALUES(?,?,?)", ("Y", "测试", "2024-01-01"))
        self.conn.execute("INSERT INTO fund_style VALUES(?,?)", ("Y", "bond_pure"))
        raw = json.dumps({"series": [{"name": "股票占净比", "data": [0.0]}],
                          "categories": ["2026-06-30", "2026-09-30"]})
        self.conn.execute("INSERT INTO src_pzd_profile VALUES(?,?,?)",
                          ("Y", "2026-10-31", raw))
        alloc = risk_gate._alloc(self.conn, "Y", "2026-10-30")
        self.assertEqual(alloc["stock_pct"], 0.0)

    def test_stale_holdings_blocks(self):
        self.fund(estab="2020-01-01")
        self.holdings([("债A", 10.0, "credit")], period="2025Q1")
        h = risk_gate.holdings_risk(self.conn, "X", "2026-11-02")
        self.assertFalse(h["available"])
        self.assertEqual(h["reason"], "stale_holdings")
        self.assertEqual(h["report_period"], "2025Q1")
        r = risk_gate.assess(self.conn, "X", "bond_pure", "2026-11-02")
        self.assertTrue(r["summary"]["blocked"])
        self.assertIn("holdings_required", r["summary"]["blocked_flags"])
        item = next(x for x in r["flags"] if x["code"] == "holdings_required")
        self.assertIn("270", item["detail"])

    def test_missing_equity_alloc_blocks(self):
        self.conn.execute("INSERT INTO fund VALUES(?,?,?)", ("Z", "测试", "2020-01-01"))
        self.conn.execute("INSERT INTO fund_style VALUES(?,?)", ("Z", "bond_pure"))
        self.conn.execute("INSERT INTO fund_holding_bond VALUES(?,?,?,?,?,?)",
                          ("Z", "2026Q3", "B1", "债A", 10.0, "credit"))
        r = risk_gate.assess(self.conn, "Z", "bond_pure", "2026-11-02")
        self.assertTrue(r["summary"]["blocked"])
        self.assertIn("equity_required", r["summary"]["blocked_flags"])

    def test_watch_concentration_not_block(self):
        self.fund(estab="2024-01-01")
        self.holdings([("债A", 10.0, "credit"), ("债B", 10.0, "credit"),
                       ("债C", 10.0, "credit"), ("债D", 10.0, "credit"),
                       ("债E", 10.0, "credit"), ("债F", 10.0, "credit"),
                       ("债G", 10.0, "credit"), ("债H", 10.0, "credit")])
        r = risk_gate.assess(self.conn, "X", "bond_pure", "2026-11-02")
        self.assertFalse(r["summary"]["blocked"])
        self.assertEqual(r["summary"]["status"], "watch")
        self.assertIn("top10", r["summary"]["watch_flags"])


if __name__ == "__main__":
    unittest.main()
