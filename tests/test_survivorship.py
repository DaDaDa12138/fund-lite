"""P1-7 幸存者偏差诊断：目录消失候选只做披露，不冒充清盘。"""
import sqlite3
import unittest

from fundana import db, survivorship


class TestSurvivorship(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)

    def _snap(self, day, code, name, type_text):
        self.conn.execute(
            "INSERT INTO src_directory(business_date, observed_at, code, name,"
            " type_text, raw_sha256) VALUES(?,?,?,?,?,?)",
            (day, day + "T12:00:00+08:00", code, name, type_text, "sha-" + day + "-" + code))

    def test_missing_bond_candidate_is_disclosed_not_liquidated(self):
        for day in ("2026-11-01", "2026-11-02", "2026-11-03"):
            self._snap(day, "000001", "存续债基", "债券型-长债")
            self._snap(day, "000002", "消失债基", "债券型-长债")
            self._snap(day, "000003", "存续混基", "混合型-灵活")
        # 000002 只在最后一天消失；000003 也消失，但不是债券，不应计入债券候选
        self.conn.execute("DELETE FROM src_directory WHERE code='000002' AND business_date='2026-11-03'")
        self.conn.execute("DELETE FROM src_directory WHERE code='000003' AND business_date='2026-11-03'")
        self.conn.commit()

        out = survivorship.summary(self.conn, lookback_days=30, limit=10)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["snapshots"], 3)
        self.assertEqual(out["coverage_days"], 3)
        self.assertEqual(out["bond_missing_count"], 1)
        cand = out["candidates"][0]
        self.assertEqual(cand["fcode"], "000002")
        self.assertEqual(cand["last_seen"], "2026-11-02")
        self.assertEqual(cand["days_missing"], 1)
        self.assertEqual(cand["status"], "directory_missing_candidate")
        self.assertIn("不等于清盘", out["note"])
        self.assertEqual(out["lifecycle_funds"], 0)

    def test_reappeared_code_is_not_candidate(self):
        for day in ("2026-11-01", "2026-11-02", "2026-11-03"):
            self._snap(day, "000001", "存续债基", "债券型-长债")
        self._snap("2026-11-02", "000002", "短暂缺失", "债券型-长债")
        # 000002 在 11-02 出现、11-03 不在，但 11-01 也不在：仍算消失候选
        out = survivorship.summary(self.conn, lookback_days=30, limit=10)
        self.assertEqual(out["bond_missing_count"], 1)
        # 最新又出现 -> 不是候选
        self._snap("2026-11-03", "000002", "又出现", "债券型-长债")
        out = survivorship.summary(self.conn, lookback_days=30, limit=10)
        self.assertEqual(out["bond_missing_count"], 0)

    def test_insufficient_history(self):
        self._snap("2026-11-01", "000001", "债基", "债券型-长债")
        out = survivorship.summary(self.conn)
        self.assertEqual(out["status"], "insufficient_history")
        self.assertEqual(out["snapshots"], 1)
        self.assertIn("不等于没有幸存者偏差", out["note"])


if __name__ == "__main__":
    unittest.main()
