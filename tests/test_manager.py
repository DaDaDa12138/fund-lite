"""经理维度与 L8 后过滤契约测试。"""
import json
import sqlite3
import unittest

from fundana import db
from fundana.fuse import manager
from fundana.screen.engine import manager_gate

MGR_JSON = json.dumps([
    {"id": "30201885", "name": "张三", "star": 5, "workTime": "13年又50天",
     "fundSize": "291.81亿(8只基金)",
     "power": {"avr": "91.05", "categories": ["经验值"], "data": [96.7]},
     "profit": {"categories": ["任期收益", "同类平均"],
                "series": [{"data": [{"y": 79.7604}, {"y": 85.47}]}]}},
    {"id": "30199999", "name": "李四", "star": 2, "workTime": "200天",
     "power": {"avr": "60"},
     "profit": {"series": [{"data": [{"y": -3.5}]}]}},
], ensure_ascii=False)


class TestParseManagers(unittest.TestCase):
    def test_career_days(self):
        self.assertEqual(manager.parse_career_days("13年又50天"), 13 * 365 + 50)
        self.assertEqual(manager.parse_career_days("3年"), 1095)
        self.assertEqual(manager.parse_career_days("240天"), 240)
        self.assertIsNone(manager.parse_career_days(""))
        self.assertIsNone(manager.parse_career_days("abc"))
        self.assertIsNone(manager.parse_career_days(None))

    def test_parse_fields(self):
        mgrs = manager.parse_managers(MGR_JSON)
        self.assertEqual(len(mgrs), 2)
        self.assertEqual(mgrs[0]["mgr_id"], "30201885")
        self.assertEqual(mgrs[0]["is_lead"], 1)
        self.assertEqual(mgrs[1]["is_lead"], 0)
        self.assertAlmostEqual(mgrs[0]["power_avr"], 91.05)
        self.assertAlmostEqual(mgrs[0]["tenure_return_pct"], 79.7604)
        self.assertEqual(mgrs[0]["star"], 5)

    def test_bad_json_is_empty(self):
        self.assertEqual(manager.parse_managers("{not json"), [])
        self.assertEqual(manager.parse_managers(""), [])

    def test_out_of_range_power_is_null(self):
        mgrs = manager.parse_managers(json.dumps([{"id": "1", "power": {"avr": "188"}}]))
        self.assertIsNone(mgrs[0]["power_avr"])


class TestRunManagers(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        for code, payload in (("000001", MGR_JSON), ("000002", "[]")):
            self.conn.execute(
                "INSERT INTO fund(fcode, name, type_group, identity_status) VALUES(?,?,?,?)",
                (code, "测试" + code, "bond", "ok"))
            self.conn.execute("INSERT INTO fund_profile(fcode) VALUES(?)", (code,))
            self.conn.execute(
                "INSERT INTO src_pzd_profile(code, business_date, observed_at, manager_json,"
                " raw_sha256) VALUES(?,?,?,?,?)",
                (code, "2026-10-31", "2026-10-31T16:00:00+08:00", payload, "sha-" + code))
        self.conn.commit()

    def test_writes_evidence_and_profile(self):
        stats = manager.run_managers(self.conn, "2026-10-31")
        self.assertEqual(stats["funds"], 2)
        self.assertEqual(stats["with_managers"], 1)
        self.assertEqual(stats["without_managers"], 1)
        self.assertEqual(stats["evidence_rows"], 2)
        row = self.conn.execute("SELECT * FROM fund_profile WHERE fcode='000001'").fetchone()
        self.assertEqual(row["manager_count"], 2)
        self.assertEqual(row["manager_career_min_days"], 200)
        self.assertEqual(row["manager_career_max_days"], 13 * 365 + 50)
        self.assertAlmostEqual(row["manager_power_score"], (91.05 + 60) / 2, places=4)
        empty = self.conn.execute("SELECT * FROM fund_profile WHERE fcode='000002'").fetchone()
        self.assertEqual(empty["manager_count"], 0)
        self.assertIsNone(empty["manager_career_min_days"])
        self.assertIsNone(empty["manager_power_score"])

    def test_rerun_is_idempotent(self):
        manager.run_managers(self.conn, "2026-10-31")
        manager.run_managers(self.conn, "2026-10-31")
        n = self.conn.execute("SELECT COUNT(*) AS c FROM fund_manager_evidence").fetchone()["c"]
        self.assertEqual(n, 2)


class TestManagerGate(unittest.TestCase):
    def test_missing_is_not_blocking_by_default(self):
        self.assertEqual(manager_gate({"manager_career_min_days": None}), (False, []))

    def test_missing_blocks_when_required(self):
        blocked, reasons = manager_gate({"manager_career_min_days": None}, require_known=True)
        self.assertTrue(blocked)
        self.assertEqual(reasons, ["manager_career_unknown"])

    def test_below_365_blocks(self):
        blocked, reasons = manager_gate({"manager_career_min_days": 120})
        self.assertTrue(blocked)
        self.assertEqual(reasons, ["manager_career_below_365d:120d"])

    def test_exactly_365_passes(self):
        self.assertEqual(manager_gate({"manager_career_min_days": 365}), (False, []))
