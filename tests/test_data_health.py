"""数据健康快照测试（P2）。"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from fundana import data_health
from fundana.metrics import contract as metric_contract


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE schema_migration(version TEXT, applied_at TEXT, sha256 TEXT);
    CREATE TABLE fund_nav_daily(fcode TEXT, nav_date TEXT);
    CREATE TABLE fund_metric(fcode TEXT, period_days INTEGER, formula_contract TEXT,
                             annual_dec REAL, mdd_dec REAL, end_date TEXT);
    CREATE TABLE fetch_log(source_id TEXT, started_at TEXT, business_ok INTEGER);
    CREATE TABLE publication(publication_id TEXT, generation INTEGER, screen_contract TEXT,
                             formula_contract TEXT, status TEXT, created_at TEXT);
    CREATE TABLE publication_gate(publication_id TEXT, status TEXT, warnings_json TEXT,
                                  blocks_json TEXT);
    CREATE TABLE run(mode TEXT, status TEXT, started_at TEXT);
    """)
    return conn


class TestDataHealth(unittest.TestCase):
    def test_schema_drift(self):
        out = data_health.schema_drift(["001.sql"], ["001.sql", "002.sql"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["missing"], ["002.sql"])
        self.assertEqual(out["unknown"], [])
        out = data_health.schema_drift(["001.sql", "999.sql"], ["001.sql"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["unknown"], ["999.sql"])

    def test_build_healthy_and_fetch_failure(self):
        conn = _conn()
        today = date.today().isoformat()
        old = (date.today() - timedelta(days=20)).isoformat()
        for name in data_health.migration_files():
            conn.execute("INSERT INTO schema_migration VALUES (?, 'x', 'x')", (name,))
        for i in range(9):
            code = "A%d" % i
            conn.execute("INSERT INTO fund_nav_daily VALUES (?, ?)", (code, today))
            conn.execute("INSERT INTO fund_metric VALUES (?, 365, ?, 3.0, 0.5, ?)",
                         (code, metric_contract.FORMULA_CONTRACT, today))
        conn.execute("INSERT INTO fund_nav_daily VALUES ('B', ?)", (old,))
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("INSERT INTO fetch_log VALUES ('s1', ?, 1)", (now,))
        conn.execute("INSERT INTO fetch_log VALUES ('s1', ?, 1)", (now,))
        conn.execute("INSERT INTO publication VALUES ('p1', 7, 'screen-x', 'risk-x', 'current', ?)",
                     (now,))
        conn.execute("INSERT INTO publication_gate VALUES ('p1', 'warning', '[]', '[]')")
        out = data_health.build(conn, days=7, business_date=today, integrity=False)
        self.assertTrue(out["ok"], out["failed_checks"])
        self.assertEqual(out["nav"]["age_days"], 0)
        self.assertEqual(out["nav"]["stale_funds"], 1)
        self.assertEqual(out["metrics"]["periods"][0]["n"], 9)
        self.assertEqual(out["fetch"]["success_rate_pct"], 100.0)
        self.assertEqual(out["gate"]["status"], "warning")

        conn.execute("INSERT INTO fetch_log VALUES ('s1', ?, 0)", (now,))
        out = data_health.build(conn, days=7, business_date=today, integrity=False)
        self.assertIn("fetch", out["failed_checks"])
        self.assertFalse(out["ok"])

    def test_provenance_audit_traces_raw_and_aliases(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        good = "a" * 64
        conn.executescript("""
        CREATE TABLE raw_object(sha256 TEXT PRIMARY KEY);
        CREATE TABLE fund_nav_daily(fcode TEXT, nav_date TEXT, nav_source TEXT, raw_sha256 TEXT);
        CREATE TABLE src_directory(business_date TEXT, raw_sha256 TEXT);
        CREATE TABLE src_snapshot_status(business_date TEXT, raw_sha256 TEXT);
        CREATE TABLE src_pzd_profile(business_date TEXT, raw_sha256 TEXT);
        CREATE TABLE src_jbgk_profile(business_date TEXT, raw_sha256 TEXT);
        CREATE TABLE src_zqcc_holding(business_date TEXT, raw_sha256 TEXT);
        CREATE TABLE src_manager_history(business_date TEXT, raw_sha256 TEXT);
        CREATE TABLE src_rank_row(business_date TEXT, raw_sha256 TEXT);
        CREATE TABLE market_index_daily(trade_date TEXT, source_id TEXT, raw_sha256 TEXT);
        CREATE TABLE fund_fee_meta(observed_at TEXT, source TEXT, raw_sha256 TEXT);
        CREATE TABLE fund_holding_bond(report_period TEXT, raw_sha256 TEXT);
        CREATE TABLE fund_scale_history(report_period TEXT, src TEXT, raw_sha256 TEXT);
        """)
        conn.execute("INSERT INTO raw_object VALUES (?)", (good,))
        conn.execute("INSERT INTO fund_nav_daily VALUES ('000001','2026-11-04','em_b1',?)", (good,))
        for table in ("src_directory", "src_snapshot_status", "src_pzd_profile",
                      "src_jbgk_profile", "src_zqcc_holding", "src_manager_history",
                      "src_rank_row"):
            conn.execute("INSERT INTO %s VALUES ('2026-11-04',?)" % table, (good,))
        conn.execute("INSERT INTO market_index_daily VALUES ('2026-11-04','em_index_kline',?)", (good,))
        conn.execute("INSERT INTO fund_fee_meta VALUES ('2026-11-04','em_jjfl',?)", (good,))
        conn.execute("INSERT INTO fund_holding_bond VALUES ('2026Q3',?)", (good,))
        conn.execute("INSERT INTO fund_scale_history VALUES ('2026Q3','em_gmbd',?)", (good,))
        allowed = {"em_nav_snapshot", "em_index_kline", "em_jjfl", "em_gmbd"}
        out = data_health.provenance_audit(conn, nav_since="2026-08-01",
                                           allowed_sources=allowed)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["missing_total"], 0)
        # em_b1 是 em_nav_snapshot 的落库别名，不应被当成未注册来源。
        self.assertEqual(out["unregistered_sources"], [])

        # 指向不存在的 raw_object：必须报 missing_raw，而不是“有 sha 就算过”。
        bad = "b" * 64
        conn.execute("INSERT INTO fund_nav_daily VALUES ('000002','2026-11-04','em_b1',?)", (bad,))
        out = data_health.provenance_audit(conn, nav_since="2026-08-01",
                                           allowed_sources=allowed)
        self.assertFalse(out["ok"])
        self.assertEqual(out["missing_total"], 1)
        conn.execute("INSERT INTO raw_object VALUES (?)", (bad,))
        out = data_health.provenance_audit(conn, nav_since="2026-08-01",
                                           allowed_sources=allowed)
        self.assertTrue(out["ok"], out)

        # 来源不在注册表：必须报 unregistered_sources。
        conn.execute("INSERT INTO fund_fee_meta VALUES ('2026-11-04','unknown_feed',?)", (good,))
        out = data_health.provenance_audit(conn, nav_since="2026-08-01",
                                           allowed_sources=allowed)
        self.assertFalse(out["ok"])
        self.assertEqual(len(out["unregistered_sources"]), 1)
        self.assertEqual(out["unregistered_sources"][0]["table"], "fund_fee_meta")

    def test_source_health_redundancy(self):
        from types import SimpleNamespace
        conn = _conn()
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("INSERT INTO fetch_log VALUES ('nav_a', ?, 1)", (now,))
        conn.execute("INSERT INTO fetch_log VALUES ('nav_a', ?, 0)", (now,))
        conn.execute("INSERT INTO fetch_log VALUES ('nav_b', ?, 1)", (now,))
        out = data_health.source_health(
            conn, {"nav_a": SimpleNamespace(domain="nav"),
                   "nav_b": SimpleNamespace(domain="nav")}, days=7)
        self.assertEqual(len(out["domains"]["nav"]), 2)
        self.assertNotIn("nav", out["single_source_domains"])
        self.assertIn("directory", out["single_source_domains"])
        self.assertIn("single_source:directory", out["warnings"])
        nav_a = next(x for x in out["sources"] if x["source_id"] == "nav_a")
        self.assertEqual(nav_a["success_rate_pct"], 50.0)


    def test_scheduler_health_missing_overdue_and_ok(self):
        with tempfile.TemporaryDirectory() as td:
            out = data_health.scheduler_health(td, now=datetime.fromisoformat("2026-11-03T10:00:00+08:00"))
            self.assertEqual(out["status"], "missing")
            self.assertTrue(out["warn"])
            path = Path(td) / "scheduler_heartbeat.json"
            path.write_text(json.dumps({"next_run_at": "2026-11-03T07:30:00+08:00",
                                        "last_status": "success"}), encoding="utf-8")
            out = data_health.scheduler_health(td, now=datetime.fromisoformat("2026-11-03T10:00:00+08:00"))
            self.assertEqual(out["status"], "overdue")
            self.assertTrue(out["warn"])
            path.write_text(json.dumps({"next_run_at": "2026-11-04T07:30:00+08:00",
                                        "last_status": "success"}), encoding="utf-8")
            out = data_health.scheduler_health(td, now=datetime.fromisoformat("2026-11-03T10:00:00+08:00"))
            self.assertEqual(out["status"], "ok")
            self.assertFalse(out["warn"])
            # 心跳里没有 last_status 时，回退到 run 表最近一次 daily。
            path.write_text(json.dumps({"next_run_at": "2026-11-04T07:30:00+08:00"}), encoding="utf-8")
            conn = _conn()
            conn.execute("INSERT INTO run VALUES ('daily', 'success', '2026-11-03T07:30:00+08:00')")
            out = data_health.scheduler_health(
                td, now=datetime.fromisoformat("2026-11-03T10:00:00+08:00"), conn=conn)
            self.assertEqual(out["last_status"], "success")
            self.assertEqual(out["last_run_at"], "2026-11-03T07:30:00+08:00")

    def test_build_empty_nav_is_not_ok(self):
        conn = _conn()
        out = data_health.build(conn, days=7, business_date=date.today().isoformat(),
                                integrity=False)
        self.assertFalse(out["ok"])
        self.assertIn("nav_freshness", out["failed_checks"])
        self.assertIn("metrics", out["failed_checks"])


if __name__ == "__main__":
    unittest.main()
