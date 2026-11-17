"""持仓券种暴露归因测试（P2-2 / ADR-069）。"""
import sqlite3
import unittest

from fundana.audit import exposure


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE src_zqcc_holding (
        code TEXT, report_period TEXT, observed_at TEXT, bond_code TEXT,
        bond_name TEXT, pct_nav REAL, market_value_wan REAL, raw_sha256 TEXT)""")
    return conn


class TestClassifyBond(unittest.TestCase):
    def test_convertible_by_name(self):
        self.assertEqual(exposure.classify_bond("兴业转债"), "convertible")
        self.assertEqual(exposure.classify_bond("希望转2"), "convertible")

    def test_convertible_by_code(self):
        self.assertEqual(exposure.classify_bond("", "113052"), "convertible")

    def test_rate_and_cd(self):
        self.assertEqual(exposure.classify_bond("26国债01"), "rate")
        self.assertEqual(exposure.classify_bond("25国开08"), "rate")
        self.assertEqual(exposure.classify_bond("25XX同业存单01"), "cd")

    def test_credit_and_other(self):
        self.assertEqual(exposure.classify_bond("25皖通高速SCP001"), "credit")
        self.assertEqual(exposure.classify_bond("22紫金矿业MTN003"), "credit")
        self.assertEqual(exposure.classify_bond(""), "other")


class TestFundShares(unittest.TestCase):
    def test_shares(self):
        shares = exposure.fund_shares([("兴业转债", 3.0), ("26国债01", 1.0)])
        self.assertEqual(shares["convertible"], 0.75)
        self.assertEqual(shares["rate"], 0.25)
        self.assertEqual(shares["credit"], 0.0)

    def test_empty(self):
        self.assertIsNone(exposure.fund_shares([]))
        self.assertIsNone(exposure.fund_shares([("兴业转债", 0.0)]))


class TestBoardExposure(unittest.TestCase):
    def test_latest_period_only_and_coverage(self):
        conn = _conn()
        rows = [
            ("000001", "2026Q2", "22紫金矿业MTN003", 10.0),
            ("000001", "2026Q3", "兴业转债", 3.0),
            ("000001", "2026Q3", "26国债01", 1.0),
            ("000002", "2026Q3", "25国开08", 2.0),
        ]
        for i, (code, period, name, pct) in enumerate(rows):
            conn.execute("INSERT INTO src_zqcc_holding VALUES (?,?,?,?,?,?,?,?)",
                         (code, period, "x", str(i), name, pct, 1.0, "h"))
        result = exposure.board_exposure(conn, ["000001", "000002", "000003"])
        self.assertEqual(result["n"], 3)
        self.assertEqual(result["covered"], 2)
        self.assertAlmostEqual(result["coverage"], 0.6667, places=3)
        self.assertEqual(result["convertible"], 0.375)  # 000001 0.75、000002 0.0 → 中位 0.375
        self.assertEqual(result["rate"], 0.625)

    def test_fund_history_periods_and_limit(self):
        conn = _conn()
        rows = [
            ("000001", "2025Q4", "22紫金矿业MTN003", 10.0),
            ("000001", "2026Q1", "兴业转债", 3.0),
            ("000001", "2026Q1", "26国债01", 1.0),
            ("000001", "2026Q2", "25国开08", 2.0),
        ]
        for i, (code, period, name, pct) in enumerate(rows):
            conn.execute("INSERT INTO src_zqcc_holding VALUES (?,?,?,?,?,?,?,?)",
                         (code, period, "x", str(i), name, pct, 1.0, "h"))
        hist = exposure.fund_history(conn, "000001", limit=2)
        self.assertEqual([h["period"] for h in hist], ["2026Q2", "2026Q1"])
        self.assertAlmostEqual(hist[0]["shares"]["rate"], 1.0)
        self.assertAlmostEqual(hist[1]["shares"]["convertible"], 0.75)
        self.assertEqual(hist[1]["top"][0]["bond_name"], "兴业转债")
        self.assertEqual(exposure.fund_history(conn, "missing"), [])

    def test_render_contains_boards(self):
        text = exposure.render({"boards": {"bond_stable": {
            "n": 2, "covered": 2, "coverage": 1.0,
            "convertible": 0.0, "rate": 0.5, "cd": 0.0, "credit": 0.5, "other": 0.0}}})
        self.assertIn("稳健", text)
        self.assertIn("可转债", text)
