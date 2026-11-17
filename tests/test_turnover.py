"""榜单换手与规则 18 的单测（ADR-067）。"""
from __future__ import annotations

import json
import sqlite3
import unittest

from fundana.audit import turnover
from fundana.publish import gate


SCHEMA = """
CREATE TABLE screen_run(run_id TEXT, formula_contract TEXT, formal_count INT,
                        business_date TEXT, batch_id TEXT, created_at TEXT,
                        screen_contract TEXT);
CREATE TABLE screen_pick(run_id TEXT, fcode TEXT, board TEXT, rank INT,
                         formal_score REAL, rank_score REAL);
CREATE TABLE screen_decision(run_id TEXT, fcode TEXT, layer_reached TEXT,
                             passed INT, reason_codes_json TEXT);
CREATE TABLE publication(publication_id TEXT, run_id TEXT, generation INT, status TEXT);
"""


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def run_(self, run_id: str, contract: str = "v4", bd: str = "2026-11-01",
             batch: str | None = None) -> None:
        self.conn.execute("INSERT INTO screen_run VALUES(?,?,?,?,?,?,?)",
                          (run_id, "fundana-risk-2026.11.4", 10, bd, batch or bd,
                           bd + "T10:00:00", contract))

    def pick(self, run_id: str, codes, board: str = "bond_stable") -> None:
        for i, code in enumerate(codes, 1):
            self.conn.execute("INSERT INTO screen_pick VALUES(?,?,?,?,?,?)",
                              (run_id, code, board, i, 80.0, 70.0))

    def decide(self, run_id: str, code: str, passed: int, reasons=()) -> None:
        self.conn.execute("INSERT INTO screen_decision VALUES(?,?,?,?,?)",
                          (run_id, code, "L9" if passed else "L6C", passed,
                           json.dumps(list(reasons))))


class TestCompare(Base):
    def test_turnover_and_rank_move(self) -> None:
        self.run_("r1")
        self.run_("r2")
        self.pick("r1", ["a", "b", "c", "d"])
        self.pick("r2", ["b", "c", "d", "e"])
        self.decide("r2", "e", 1)
        self.decide("r2", "a", 0, ["composite:below_min"])
        out = turnover.compare(self.conn, "r2", "r1")
        b = out["boards"]["bond_stable"]
        self.assertEqual((b["prev"], b["cur"], b["entered"], b["left"]), (4, 4, 1, 1))
        self.assertAlmostEqual(b["turnover"], 0.4, places=4)  # 2/5
        self.assertTrue(out["same_contract"])

    def test_left_reasons(self) -> None:
        self.run_("r1")
        self.run_("r2")
        self.pick("r1", ["a", "b", "c"])
        self.pick("r2", ["c"])
        self.decide("r2", "a", 1)                       # 过了门槛但被配额挤掉
        self.decide("r2", "b", 0, ["sharpe:below_min", "x"])
        out = turnover.compare(self.conn, "r2", "r1")
        reasons = out["boards"]["bond_stable"]["left_reasons"]
        self.assertEqual(reasons["a"], ["rank_out"])
        self.assertEqual(reasons["b"], ["sharpe:below_min", "x"])

    def test_contract_change_flagged(self) -> None:
        self.run_("r1", contract="v3", bd="2026-10-01")
        self.run_("r2", contract="v4", bd="2026-11-01")
        self.pick("r1", ["a"])
        self.pick("r2", ["b"])
        out = turnover.compare(self.conn, "r2", "r1")
        self.assertFalse(out["same_contract"])
        self.assertFalse(out["comparable"])

    def test_same_batch_is_comparable_without_contract(self) -> None:
        self.run_("r1", contract=None, bd="2026-11-01")
        self.run_("r2", contract="v4", bd="2026-11-01")
        self.pick("r1", ["a"])
        self.pick("r2", ["a"])
        out = turnover.compare(self.conn, "r2", "r1")
        self.assertFalse(out["same_contract"])
        self.assertTrue(out["same_batch"])
        self.assertTrue(out["comparable"])
        self.assertAlmostEqual(out["boards"]["bond_stable"]["turnover"], 0.0)

    def test_history_skips_contract_change(self) -> None:
        self.run_("r1", contract="v3", bd="2026-09-01")
        self.run_("r2", contract="v4", bd="2026-10-01")
        self.run_("r3", contract="v4", bd="2026-11-01")
        self.pick("r1", ["a"])
        self.pick("r2", ["a"])
        self.pick("r3", ["b"])
        out = turnover.history(self.conn)
        self.assertEqual(len(out["pairs"]), 1)
        self.assertEqual(out["pairs"][0]["run_id"], "r3")
        self.assertAlmostEqual(out["pairs"][0]["stable"], 1.0, places=4)  # 1 进 1 出 / 并集 2


class TestGateRule(Base):
    def _pub(self, run_id: str, gen: int) -> None:
        self.conn.execute("INSERT INTO publication VALUES(?,?,?,'committed')",
                          ("p%d" % gen, run_id, gen))

    def test_not_applicable_without_previous(self) -> None:
        self.run_("r1")
        self.pick("r1", ["a"])
        chk = gate.board_turnover(self.conn, "r1")
        self.assertEqual(chk["status"], "not_applicable")

    def test_not_applicable_on_contract_change(self) -> None:
        self.run_("r1", contract="v3", bd="2026-10-01")
        self.run_("r2", contract="v4", bd="2026-11-01")
        self.pick("r1", ["a"])
        self.pick("r2", ["b"])
        self._pub("r1", 1)
        chk = gate.board_turnover(self.conn, "r2")
        self.assertEqual(chk["status"], "not_applicable")
        self.assertIn("契约", chk["detail"])

    def test_warn_when_turnover_high(self) -> None:
        self.run_("r1")
        self.run_("r2")
        self.pick("r1", ["a", "b", "c", "d"])
        self.pick("r2", ["c", "d", "e", "f"])
        self._pub("r1", 1)
        chk = gate.board_turnover(self.conn, "r2")
        self.assertEqual(chk["status"], "warn")
        self.assertAlmostEqual(chk["evidence"]["boards"]["bond_stable"]["turnover"], 0.6667, places=4)

    def test_pass_when_stable(self) -> None:
        self.run_("r1")
        self.run_("r2")
        self.pick("r1", ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"])
        self.pick("r2", ["a", "b", "c", "d", "e", "f", "g", "h", "i", "k"])  # 1 进 1 出 → 2/11
        self._pub("r1", 1)
        chk = gate.board_turnover(self.conn, "r2")
        self.assertEqual(chk["status"], "pass")


if __name__ == "__main__":
    unittest.main()
