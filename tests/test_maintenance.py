"""P1-5 维护采集：到期判定、部分样本重跑、失败不中断。"""
import json
import sqlite3
import unittest
from unittest import mock

from fundana import maintenance


class _Cfg:
    root = None
    config_hash = "cfg"
    code_version = "test"

    def __init__(self, intervals=None):
        self.intervals = intervals or {}

    def get(self, section, *keys, default=None, **_kw):
        if section == "maintenance" and len(keys) == 3 and keys[0] == "tasks":
            return self.intervals.get(keys[1], default)
        return default


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE run(run_id TEXT PRIMARY KEY, mode TEXT, started_at TEXT,"
                 " finished_at TEXT, status TEXT, config_hash TEXT, code_version TEXT, notes TEXT)")
    conn.execute("CREATE TABLE maintenance_state(task TEXT PRIMARY KEY, last_run_at TEXT,"
                 " last_status TEXT, last_run_id TEXT, detail_json TEXT)")
    conn.execute("CREATE TABLE screen_run(run_id TEXT PRIMARY KEY, status TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE screen_pick(run_id TEXT, fcode TEXT)")
    conn.execute("CREATE TABLE screen_research_pool(run_id TEXT, fcode TEXT)")
    return conn


class TestMaintenance(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self.cfg = _Cfg()
        self.conn.execute("INSERT INTO screen_run VALUES('r1','done','2026-11-05')")
        self.conn.executemany("INSERT INTO screen_pick VALUES('r1',?)", [("A",), ("B",)])

    def test_due_tasks_reruns_when_last_limit_was_partial(self):
        self.conn.execute(
            "INSERT INTO maintenance_state VALUES('fee','2026-11-05T09:00:00+08:00','success','x',?)",
            (json.dumps({"codes": 1}),))
        due = maintenance.due_tasks(self.conn, self.cfg, today="2026-11-05", target_count=2)
        self.assertIn("fee", due)
        self.conn.execute("UPDATE maintenance_state SET detail_json=? WHERE task='fee'",
                          (json.dumps({"codes": 2}),))
        due = maintenance.due_tasks(self.conn, self.cfg, today="2026-11-05", target_count=2)
        self.assertNotIn("fee", due)

    def test_run_records_partial_sample_and_failure(self):
        calls = []

        def fake_call(fetcher, task, code, business_date):
            calls.append((task, code))
            if code == "B":
                return {"ok": False, "status": "parse_failed"}
            return {"ok": True, "rows": 3}

        with mock.patch.object(maintenance, "Fetcher", lambda *a, **k: object()), \
             mock.patch.object(maintenance, "_call", fake_call):
            out = maintenance.run(self.cfg, self.conn, tasks=["fee"], force=True, verbose=False)
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["tasks"]["fee"]["ok"], 1)
        self.assertEqual(out["tasks"]["fee"]["fail"], 1)
        self.assertEqual(out["tasks"]["fee"]["codes"], 2)
        state = self.conn.execute("SELECT detail_json FROM maintenance_state WHERE task='fee'").fetchone()
        self.assertEqual(json.loads(state["detail_json"])["codes"], 2)
        self.assertEqual(len(calls), 2)

    def test_target_codes_includes_research_pool(self):
        self.conn.execute("INSERT INTO screen_research_pool VALUES('r1','C')")
        self.assertEqual(maintenance.target_codes(self.conn), ["A", "B", "C"])


if __name__ == "__main__":
    unittest.main()
