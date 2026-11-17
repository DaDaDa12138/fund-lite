"""日更指标计算范围：已清盘/合并基金不参与当日指标。"""
from __future__ import annotations

import sqlite3
import unittest

from fundana import daily, db


class DailyCodesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE fund(fcode TEXT PRIMARY KEY, type_group TEXT, fund_status TEXT);
        """)

    def tearDown(self):
        self.conn.close()

    def test_liquidated_and_merged_excluded_from_metrics(self):
        self.conn.executemany("INSERT INTO fund VALUES (?,?,?)", [
            ("A", "bond", "active"),
            ("B", "bond", "liquidated"),
            ("C", "bond", "merged"),
            ("D", "bond", None),
        ])
        self.assertEqual(daily._codes(self.conn, "bond"), ["A", "D"])

    def test_missing_status_column_is_backward_compatible(self):
        self.conn.execute("ALTER TABLE fund RENAME TO fund_old")
        self.conn.execute("CREATE TABLE fund(fcode TEXT PRIMARY KEY, type_group TEXT)")
        self.conn.executemany("INSERT INTO fund VALUES (?,?)", [("A", "bond"), ("B", "bond")])
        self.assertEqual(daily._codes(self.conn, "bond"), ["A", "B"])



class RunStagesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE run(run_id TEXT PRIMARY KEY);
            CREATE TABLE run_stage(run_id TEXT, seq INTEGER, stage TEXT, status TEXT,
                                   recorded_at TEXT, detail_json TEXT,
                                   PRIMARY KEY(run_id, seq));
        """)
        self.conn.execute("INSERT INTO run VALUES ('r1')")

    def tearDown(self):
        self.conn.close()

    def test_append_persists_sequence_and_status(self):
        stages = db.RunStages(self.conn, "r1")
        stages.append({"stage": "directory", "ok": True, "rows": 3})
        stages.append({"stage": "snapshot", "ok": False, "error": "boom"})
        stages.append({"stage": "note", "msg": "no verdict"})
        rows = self.conn.execute(
            "SELECT seq, stage, status, detail_json FROM run_stage ORDER BY seq").fetchall()
        self.assertEqual([(r["seq"], r["stage"], r["status"]) for r in rows],
                         [(1, "directory", "ok"), (2, "snapshot", "failed"), (3, "note", "info")])
        self.assertIn('"rows":3', rows[0]["detail_json"])
        self.assertEqual(len(stages), 3)


if __name__ == "__main__":
    unittest.main()
