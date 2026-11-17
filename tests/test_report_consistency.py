"""P2-13 报告数字与数据库对账：不一致必须阻断。"""
import sqlite3
import unittest

from fundana import db
from fundana.publish import gate


class TestReportConsistency(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO screen_run(run_id, profile, config_hash, code_version,"
            " formula_contract, formal_count, status) VALUES(?,?,?,?,?,?,?)",
            ("r1", "current", "cfg", "code", "c1", 3, "done"))
        self.conn.execute(
            "INSERT INTO screen_pick(run_id, board, rank, fcode) VALUES(?,?,?,?)",
            ("r1", "bond_stable", 1, "000001"))
        self.conn.execute(
            "INSERT INTO screen_pick(run_id, board, rank, fcode) VALUES(?,?,?,?)",
            ("r1", "bond_enhanced", 1, "000002"))
        self.conn.commit()
        self.boards = {"bond_stable": [{"fcode": "000001"}],
                       "bond_enhanced": [{"fcode": "000002"}]}
        self.report = {"facts": {
            "l9_pass": 3, "published": 2, "folded": 1,
            "formal_by_board": {"bond_stable": 1, "bond_enhanced": 1},
            "board_counts": {"bond_stable": 1, "bond_enhanced": 1},
            "cohorts": 0, "matured_formal_observations": 0,
        }}
        self.cfg = {"gate": {"mode": "strict"}}

    def test_consistent_report_passes(self):
        result = {"publication_id": "p1", "run_id": "r1", "checks": [],
                  "blocks": [], "warnings": [], "status": "pass"}
        out = gate.attach_report_consistency(self.conn, self.cfg, "r1", result,
                                             self.report, self.boards)
        check = [c for c in out["checks"] if c["code"] == "report_consistency"][0]
        self.assertEqual(check["status"], "pass")
        self.assertEqual(out["status"], "pass")

    def test_mismatched_published_blocks(self):
        self.report["facts"]["published"] = 1
        result = {"publication_id": "p1", "run_id": "r1", "checks": [],
                  "blocks": [], "warnings": [], "status": "pass"}
        out = gate.attach_report_consistency(self.conn, self.cfg, "r1", result,
                                             self.report, self.boards)
        check = [c for c in out["checks"] if c["code"] == "report_consistency"][0]
        self.assertEqual(check["status"], "block")
        self.assertIn("report_consistency", out["blocks"])


if __name__ == "__main__":
    unittest.main()
