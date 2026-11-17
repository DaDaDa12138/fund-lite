"""SQLite 维护命令的单元测试（临时文件库，不碰生产库）。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fundana import db_maint


class TestDbMaint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("CREATE TABLE t(x TEXT)")
        self.conn.executemany("INSERT INTO t VALUES(?)", [("a" * 100,) for _ in range(200)])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_stats_reports_pages_and_sizes(self):
        got = db_maint.stats(self.conn, self.path)
        self.assertGreater(got["page_count"], 0)
        self.assertEqual(got["page_size"], 4096)
        self.assertGreater(got["db_bytes"], 0)

    def test_optimize_and_quick_check(self):
        self.assertTrue(db_maint.optimize(self.conn)["ok"])
        self.assertTrue(db_maint.quick_check(self.conn)["ok"])

    def test_checkpoint_reports_tuple(self):
        got = db_maint.checkpoint(self.conn, "TRUNCATE")
        self.assertIn("busy", got)
        self.assertEqual(got["mode"], "TRUNCATE")
        self.assertFalse(got["ok"] and got["busy"])

    def test_vacuum_reclaims_deleted_pages(self):
        before = db_maint.stats(self.conn, self.path)
        self.conn.execute("DELETE FROM t")
        self.conn.commit()
        out = db_maint.run(self.conn, self.path, do_checkpoint=True,
                           do_vacuum=True, do_integrity=True)
        self.assertTrue(out["quick_check"]["ok"])
        self.assertTrue(out["vacuum"]["ok"])
        self.assertLessEqual(out["after"]["db_bytes"], before["db_bytes"])
        self.assertGreaterEqual(out["reclaimed_bytes"], 0)

    def test_rejects_unknown_checkpoint_mode(self):
        with self.assertRaises(ValueError):
            db_maint.checkpoint(self.conn, "NOPE")


if __name__ == "__main__":
    unittest.main()
