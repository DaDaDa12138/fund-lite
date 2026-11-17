"""前瞻看板契约：最近到期日与进度必须由已落库观察期计算。"""
import sqlite3
import unittest
from datetime import date, timedelta

from fundana import db, forward


class TestForwardMaturity(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.today = date.fromisoformat(db.today_cn())
        self.biz = (self.today - timedelta(days=10)).isoformat()
        self.conn.execute(
            "INSERT INTO forward_cohort(cohort_id, run_id, publication_id, business_date,"
            " source, universe_count, investable_count, created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("c1", "r1", "p1", self.biz, "live", 10, 5, db.now_iso()))
        self.conn.execute(
            "INSERT INTO forward_member(cohort_id, fcode, role) VALUES(?,?,?)",
            ("c1", "000001", "formal"))
        self.conn.execute(
            "INSERT INTO forward_observation(cohort_id, fcode, horizon_days, status,"
            " start_date, end_date, n_obs, computed_at) VALUES(?,?,?,?,?,?,?,?)",
            ("c1", "000001", 30, "not_ready", self.biz, self.biz, 10, db.now_iso()))
        self.conn.commit()

    def test_next_maturity_and_progress(self):
        out = forward.summary(self.conn)
        expected = (date.fromisoformat(self.biz) + timedelta(days=30)).isoformat()
        self.assertEqual(out["next_maturity_date"], expected)
        self.assertEqual(out["next_maturity_days"], 20)
        self.assertEqual(out["next_maturity_horizon_days"], 30)
        row = out["rows"][0]
        self.assertEqual(row["maturity_date"], expected)
        self.assertAlmostEqual(row["progress_pct"], 33.3, places=1)
        self.assertEqual(row["status_counts"]["not_ready"], 1)


if __name__ == "__main__":
    unittest.main()
