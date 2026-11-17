"""发布门禁规则 17（动态线边界占比）的单测。"""
from __future__ import annotations

import json
import sqlite3
import unittest

from fundana.publish import gate


SCHEMA = """
CREATE TABLE screen_threshold(batch_id TEXT, scope TEXT, metric TEXT, enter_value REAL);
CREATE TABLE screen_pick(run_id TEXT, fcode TEXT, board TEXT);
CREATE TABLE screen_decision(run_id TEXT, fcode TEXT, scores_json TEXT);
"""


class TestBoundaryShare(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO screen_threshold VALUES('b1','global','composite_min',70.0)")

    def _add(self, code: str, composite: float | None, board: str = "bond_stable") -> None:
        self.conn.execute("INSERT INTO screen_pick VALUES('r1',?,'" + board + "')", (code,))
        payload = {} if composite is None else {"composite": composite}
        self.conn.execute("INSERT INTO screen_decision VALUES('r1',?,?)",
                          (code, json.dumps(payload)))

    def test_half_near_threshold(self) -> None:
        self._add("a", 70.4)
        self._add("b", 69.5)
        self._add("c", 80.0)
        self._add("d", 90.0)
        share, near, threshold = gate.boundary_share(self.conn, "r1", "b1")
        self.assertEqual(near, 2)
        self.assertEqual(threshold, 70.0)
        self.assertAlmostEqual(share, 0.5, places=4)

    def test_ignores_non_formal_boards(self) -> None:
        self._add("a", 70.2)
        self._add("b", 70.3, board="bond_watch")
        share, near, _ = gate.boundary_share(self.conn, "r1", "b1")
        self.assertEqual(near, 1)
        self.assertAlmostEqual(share, 1.0, places=4)

    def test_missing_batch_or_threshold(self) -> None:
        self._add("a", 70.2)
        self.assertEqual(gate.boundary_share(self.conn, "r1", None), (None, 0, None))
        self.assertEqual(gate.boundary_share(self.conn, "r1", "nope"), (None, 0, None))

    def test_scores_without_composite_are_skipped(self) -> None:
        self._add("a", None)
        self._add("b", 71.0)
        share, near, _ = gate.boundary_share(self.conn, "r1", "b1")
        self.assertEqual(near, 1)
        self.assertAlmostEqual(share, 1.0, places=4)


if __name__ == "__main__":
    unittest.main()
