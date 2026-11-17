"""P2-4/P2-5 门禁辅助测试。"""
import sqlite3
import unittest

from fundana.publish import gate


class _Cfg:
    def __init__(self, evidence):
        self.evidence = evidence

    def get(self, section, default=None, **_kw):
        if section == "evidence":
            return self.evidence
        return default


class TestGateP2(unittest.TestCase):
    def test_lifecycle_zero_is_warn_evidence(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE fund_lifecycle(fcode TEXT, status TEXT)")
        self.assertEqual(gate.lifecycle_stats(conn)["funds"], 0)
        conn.execute("INSERT INTO fund_lifecycle VALUES('A','liquidated')")
        got = gate.lifecycle_stats(conn)
        self.assertEqual(got["funds"], 1)
        self.assertEqual(got["by_status"]["liquidated"], 1)

    def test_dsr_below_threshold_is_warn(self):
        got = gate.dsr_skill_check(_Cfg({"dsr": 0.660, "dsr_threshold": 0.95}))
        self.assertEqual(got["status"], "warn")
        self.assertIn("不能拒绝无技能", got["detail"])

    def test_dsr_above_threshold_passes(self):
        got = gate.dsr_skill_check(_Cfg({"dsr": 0.97, "dsr_threshold": 0.95}))
        self.assertEqual(got["status"], "pass")


class TestBoardDrift(unittest.TestCase):
    def test_cross_board_move_is_not_double_counted(self):
        data = {"boards": {"bond_stable": {"n": 2, "prev_n": 2, "kept": 1, "added": [], "removed": ["B"]},
                           "bond_enhanced": {"n": 2, "prev_n": 2, "kept": 1, "added": ["B"], "removed": []}}}
        self.assertEqual(gate.formal_board_drift(data), 0.0)

    def test_real_entry_exit_is_symmetric_turnover(self):
        data = {"boards": {"bond_stable": {"n": 2, "prev_n": 2, "kept": 1, "added": ["C"], "removed": ["B"]},
                           "bond_enhanced": {"n": 0, "prev_n": 0, "kept": 0, "added": [], "removed": []}}}
        self.assertEqual(gate.formal_board_drift(data), 0.5)


class TestBoardPolicy(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE fund(fcode TEXT, company_name TEXT)")
        self.conn.execute("CREATE TABLE screen_pick(run_id TEXT, fcode TEXT, board TEXT)")

    def _pick(self, code, board, company):
        self.conn.execute("INSERT INTO fund VALUES(?,?)", (code, company))
        self.conn.execute("INSERT INTO screen_pick VALUES('R',?,?)", (code, board))

    def test_company_cap_flags_third_fund_of_same_company(self):
        for code in ("A1", "A2", "A3"):
            self._pick(code, "bond_stable", "甲公司")
        self._pick("B1", "bond_stable", "乙公司")
        got = gate.company_cap_stats(self.conn, "R", 2)
        self.assertTrue(got["checked"])
        self.assertEqual(got["max_count"], 3)
        self.assertEqual(got["violations"],
                         [{"board": "bond_stable", "company": "甲公司", "n": 3}])

    def test_company_cap_passes_at_limit(self):
        self._pick("A1", "bond_stable", "甲公司")
        self._pick("A2", "bond_enhanced", "甲公司")
        got = gate.company_cap_stats(self.conn, "R", 2)
        self.assertEqual(got["violations"], [])
        self.assertEqual(got["max_count"], 1)

    def test_company_cap_without_fund_table_is_not_checked(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        got = gate.company_cap_stats(conn, "R", 2)
        self.assertFalse(got["checked"])

    def test_board_ratio_requires_quarter(self):
        for i in range(73):
            self._pick("S%d" % i, "bond_stable", "C%d" % i)
        for i in range(27):
            self._pick("E%d" % i, "bond_enhanced", "D%d" % i)
        got = gate.board_ratio_stats(self.conn, "R", 0.25)
        self.assertTrue(got["applicable"])
        self.assertTrue(got["pass"])
        self.assertAlmostEqual(got["ratio"], 0.27)

    def test_board_ratio_blocks_twenty_percent(self):
        for i in range(80):
            self._pick("S%d" % i, "bond_stable", "C%d" % i)
        for i in range(20):
            self._pick("E%d" % i, "bond_enhanced", "D%d" % i)
        got = gate.board_ratio_stats(self.conn, "R", 0.25)
        self.assertFalse(got["pass"])

    def test_board_ratio_not_applicable_when_side_empty(self):
        self._pick("S1", "bond_stable", "甲公司")
        got = gate.board_ratio_stats(self.conn, "R", 0.25)
        self.assertFalse(got["applicable"])


if __name__ == "__main__":
    unittest.main()
