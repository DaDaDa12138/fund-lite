"""fund_status 存活状态维护测试（幸存者偏差可观测化）。"""
import sqlite3
import unittest

from fundana.fuse.identity import refresh_status


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE fund (fcode TEXT PRIMARY KEY, type_group TEXT,"
                 " fund_status TEXT, last_seen_date TEXT)")
    return conn


class TestRefreshStatus(unittest.TestCase):
    def test_active_within_grace_and_stale_after(self):
        conn = _conn()
        rows = [
            ("A", "bond", "2026-11-01"),   # 当日
            ("B", "bond", "2026-10-20"),   # 12 天前 → stale
            ("C", "bond", "2026-10-28"),   # 4 天前，在 7 天宽限内 → active
        ]
        for code, group, seen in rows:
            conn.execute("INSERT INTO fund VALUES (?,?,NULL,?)", (code, group, seen))
        counts = refresh_status(conn, "2026-11-01", 7, types={"bond"})
        status = {r["fcode"]: r["fund_status"] for r in conn.execute("SELECT * FROM fund")}
        self.assertEqual(status, {"A": "active", "B": "stale", "C": "active"})
        self.assertEqual(counts.get("active"), 2)
        self.assertEqual(counts.get("stale"), 1)

    def test_type_scope_leaves_other_groups_alone(self):
        conn = _conn()
        conn.execute("INSERT INTO fund VALUES ('A','bond',NULL,'2026-10-01')")
        conn.execute("INSERT INTO fund VALUES ('S','stock',NULL,'2026-10-01')")
        refresh_status(conn, "2026-11-01", 7, types={"bond"})
        status = {r["fcode"]: r["fund_status"] for r in conn.execute("SELECT * FROM fund")}
        self.assertEqual(status["A"], "stale")
        self.assertIsNone(status["S"])

    def test_liquidated_status_is_preserved(self):
        conn = _conn()
        conn.execute("INSERT INTO fund VALUES ('L','bond','liquidated','2024-01-01')")
        refresh_status(conn, "2026-11-01", 7, types={"bond"})
        self.assertEqual(
            conn.execute("SELECT fund_status FROM fund WHERE fcode='L'").fetchone()[0],
            "liquidated")

    def test_reappearing_fund_back_to_active(self):
        conn = _conn()
        conn.execute("INSERT INTO fund VALUES ('A','bond','stale','2026-10-20')")
        conn.execute("UPDATE fund SET last_seen_date='2026-11-01' WHERE fcode='A'")
        refresh_status(conn, "2026-11-01", 7, types={"bond"})
        self.assertEqual(conn.execute("SELECT fund_status FROM fund WHERE fcode='A'").fetchone()[0], "active")
