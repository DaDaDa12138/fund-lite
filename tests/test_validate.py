"""跨源核对：容差、日期证据、阶段收益去重（Batch A）。"""
import json
import tempfile
import unittest
from pathlib import Path

from fundana import db
from fundana.validate import cross


class StubCfg:
    def __init__(self, root: Path):
        self.app = {"db": {"busy_timeout_ms": 15000, "wal": False}}
        self.db_path = root / "t.db"
        self.raw_dir = root / "raw"
        self.report_root = root / "reports"
        self.freeze_dir = root / "freeze"
        self.code_version = "0.1.0"

    def get(self, *keys, default=None):
        return default


class CompareTest(unittest.TestCase):
    def test_missing_is_not_zero(self):
        self.assertEqual(cross.compare(None, 1.0, 0.001, 0.01), ("missing_a", None, None))
        self.assertEqual(cross.compare(1.0, None, 0.001, 0.01), ("missing_b", None, None))
        self.assertEqual(cross.compare(None, None, 0.001, 0.01), ("missing", None, None))

    def test_abs_or_rel_tolerance(self):
        status, delta, _ = cross.compare(1.1440, 1.1444, 0.001, 0.01)
        self.assertEqual(status, "pass")
        self.assertAlmostEqual(delta, 0.0004, places=6)
        # 相对容差命中（差 0.02 但相对 1% 内）
        self.assertEqual(cross.compare(1.0, 1.005, 0.001, 0.01)[0], "pass")
        self.assertEqual(cross.compare(1.0, 1.05, 0.001, 0.01)[0], "mismatch")


class CheckDailyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = StubCfg(Path(self.tmp.name))
        self.conn = db.connect(self.cfg)
        db.migrate(self.conn)
        self.conn.execute("INSERT INTO fund(fcode, name, type_group, identity_status)"
                          " VALUES(?,?,?,?)", ("000003", "样本债基", "bond", "ok"))
        # 上游 E3 状态快照：只给 MM-DD
        self.conn.execute(
            "INSERT INTO src_snapshot_status(business_date, observed_at, code, name, type_text,"
            " unit_nav, nav_date_md, purchase_status, redemption_status, raw_sha256)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("2026-11-01", "2026-11-01T08:00:00+08:00", "000003", "样本债基", "债券型-混合二级",
             "1.1440", "10-30", "开放申购", "开放赎回", "0" * 64))
        self.conn.execute(
            "INSERT INTO fund_nav_daily(fcode, nav_date, unit_nav, acc_nav, growth_pct, nav_source)"
            " VALUES(?,?,?,?,?,?)",
            ("000003", "2026-10-30", 1.1440, 1.30, 0.07, "em_b1"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _rows(self):
        return {r["field"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM nav_cross_check WHERE fcode=?", ("000003",))}

    def test_nav_and_date_rows(self):
        res = cross.check_daily(self.conn, "2026-11-01", self.cfg)
        self.assertGreaterEqual(res.get("rows", res.get("written", 0)), 1)
        rows = self._rows()
        self.assertEqual(rows["unit_nav"]["status"], "pass")
        self.assertAlmostEqual(rows["unit_nav"]["delta_abs"], 0.0, places=9)
        # 日期行没有数值，两侧原始日期必须落 detail，前端才能显示证据
        det = json.loads(rows["nav_date"]["detail_json"])
        self.assertEqual(det["status_snapshot_date"], "10-30")
        self.assertEqual(det["nav_daily_date"], "2026-10-30")
        self.assertEqual(rows["nav_date"]["status"], "pass")

    def test_mismatch_keeps_both_values(self):
        self.conn.execute("UPDATE fund_nav_daily SET unit_nav=1.2000 WHERE fcode=? AND nav_date=?",
                          ("000003", "2026-10-30"))
        self.conn.commit()
        cross.check_daily(self.conn, "2026-11-01", self.cfg)
        row = self._rows()["unit_nav"]
        self.assertEqual(row["status"], "mismatch")
        self.assertAlmostEqual(row["value_a"], 1.1440, places=6)
        self.assertAlmostEqual(row["value_b"], 1.2000, places=6)

    def test_rerun_is_idempotent(self):
        cross.check_daily(self.conn, "2026-11-01", self.cfg)
        first = self.conn.execute("SELECT COUNT(*) c FROM nav_cross_check").fetchone()["c"]
        cross.check_daily(self.conn, "2026-11-01", self.cfg)
        second = self.conn.execute("SELECT COUNT(*) c FROM nav_cross_check").fetchone()["c"]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
