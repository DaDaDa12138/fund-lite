"""API 健康页：成功率 / 分位延迟 / 新鲜度 / 注册表覆盖。"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta

from fundana.api import api_health
from fundana.config import SourceSpec, load_config


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class _Cfg:
    def __init__(self, sources, version="test-registry"):
        self.sources = sources
        self.registry_version = version


class ApiHealthTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 2, 12, 0, tzinfo=api_health.CN_TZ)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE fetch_log (source_id TEXT, started_at TEXT, duration_ms INTEGER,"
            " business_ok INTEGER, status TEXT, error_msg TEXT)")
        self.conn.execute(
            "CREATE TABLE source_state (source_id TEXT, key TEXT, consecutive_failures INTEGER,"
            " cooldown_until TEXT)")
        self.sources = {
            "em_pzd": SourceSpec("em_pzd", "nav", "https://example.test/pzd/{code}", ttl_days=1),
            "em_jbgk": SourceSpec("em_jbgk", "profile", "https://example.test/jbgk/{code}", ttl_days=56),
            "em_zqcc": SourceSpec("em_zqcc", "holding", "https://example.test/zqcc/{code}", ttl_days=7),
        }
        self.cfg = _Cfg(self.sources)

    def tearDown(self):
        self.conn.close()

    def _add(self, source_id, when, duration, ok=1, status="ok", error=None):
        self.conn.execute(
            "INSERT INTO fetch_log VALUES (?,?,?,?,?,?)",
            (source_id, _ts(when), duration, ok, status, error))

    def test_build_reports_success_latency_and_freshness(self):
        for i in range(9):
            self._add("em_pzd", self.now - timedelta(hours=1), 100 + i * 100)
        self._add("em_pzd", self.now - timedelta(minutes=30), 1000, ok=0,
                  status="parse_failed", error="boom")
        self._add("em_jbgk", self.now - timedelta(hours=2), 500)
        self._add("legacy_source", self.now - timedelta(hours=3), 300)
        self.conn.execute(
            "INSERT INTO source_state VALUES ('em_pzd','000001',2,NULL)")
        self.conn.commit()

        d = api_health.build(self.conn, self.cfg, days=7, now=self.now)
        by = {s["source_id"]: s for s in d["sources"]}

        self.assertEqual(d["registry"]["version"], "test-registry")
        self.assertEqual(d["registry"]["count"], 3)
        self.assertEqual(d["registry"]["unregistered"], 1)
        self.assertEqual(d["summary"]["sources"], 4)
        self.assertEqual(d["summary"]["calls"], 12)
        self.assertEqual(d["summary"]["business_ok"], 11)
        self.assertEqual(d["summary"]["healthy"], 1)
        self.assertEqual(d["summary"]["degraded"], 1)
        self.assertEqual(d["summary"]["unknown"], 1)
        self.assertEqual(d["summary"]["unregistered"], 1)
        self.assertEqual(d["summary"]["down"], 0)

        pzd = by["em_pzd"]
        self.assertEqual(pzd["calls"], 10)
        self.assertEqual(pzd["business_failed"], 1)
        self.assertEqual(pzd["success_rate_pct"], 90.0)
        self.assertEqual(pzd["p50_ms"], 550.0)
        self.assertEqual(pzd["p95_ms"], 955.0)
        self.assertEqual(pzd["last_error"], "boom")
        self.assertEqual(pzd["last_status"], "parse_failed")
        self.assertEqual(pzd["consecutive_failures"], 2)
        self.assertEqual(pzd["freshness"], "fresh")
        self.assertEqual(pzd["health"], "degraded")
        self.assertIn("成功率低于 99%", pzd["status_note"])
        self.assertEqual(pzd["score_breakdown"]["success"], 90.0)

        self.assertEqual(by["em_jbgk"]["health"], "healthy")
        self.assertEqual(by["em_jbgk"]["role"], "primary")
        self.assertEqual(by["em_zqcc"]["health"], "unknown")
        self.assertIsNone(by["em_zqcc"]["p95_ms"])
        self.assertEqual(by["legacy_source"]["role"], "unknown")
        self.assertEqual(by["legacy_source"]["health"], "unregistered")
        self.assertIsNone(by["legacy_source"]["health_score"])
        self.assertEqual(by["legacy_source"]["url"], "")
        self.assertEqual(by["legacy_source"]["note"], "未在 source_registry.yaml 注册")

    def test_stale_and_down_are_not_healthy(self):
        for i in range(5):
            self._add("em_pzd", self.now - timedelta(days=10), 6000, ok=(i < 2))
        self._add("em_jbgk", self.now - timedelta(days=60), 500)
        self.conn.commit()

        d = api_health.build(self.conn, self.cfg, days=90, now=self.now)
        by = {s["source_id"]: s for s in d["sources"]}
        self.assertEqual(by["em_pzd"]["health"], "down")
        self.assertEqual(by["em_pzd"]["freshness"], "stale")
        self.assertLess(by["em_pzd"]["health_score"], 60)
        self.assertEqual(by["em_jbgk"]["health"], "degraded")
        self.assertIn("超过 TTL", by["em_jbgk"]["status_note"])
        self.assertEqual(by["em_zqcc"]["health"], "unknown")

    def test_empty_window_does_not_crash(self):
        d = api_health.build(self.conn, self.cfg, days=7, now=self.now)
        self.assertEqual(d["summary"]["calls"], 0)
        self.assertEqual(d["summary"]["healthy"], 0)
        self.assertEqual(d["summary"]["unknown"], 3)
        self.assertIsNone(d["summary"]["p95_ms"])
        self.assertTrue(all(s["health_score"] == 0 for s in d["sources"]))

    def test_percentile_is_none_for_empty(self):
        self.assertIsNone(api_health._percentile([], 0.95))
        self.assertEqual(api_health._percentile([10], 0.95), 10.0)
        self.assertEqual(api_health._percentile([0, 100], 0.5), 50.0)

    def test_real_registry_keeps_fractional_ttl(self):
        cfg = load_config()
        self.assertEqual(cfg.sources["em_nav_snapshot"].ttl_days, 0.25)
        self.assertEqual(cfg.registry_version, "source-registry-2026.11.2")


if __name__ == "__main__":
    unittest.main()
