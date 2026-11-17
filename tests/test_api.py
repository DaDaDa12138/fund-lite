"""API 层单测：SQL 闸门、只读执行、存储统计。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fundana.api import metrics_meta, query as q, sorting
from fundana.api import series
from fundana.api import storage as st


class QueryGuardTest(unittest.TestCase):
    def test_sanitize_strips_comments_and_trailing_semicolon(self):
        self.assertEqual(q.sanitize("SELECT 1; -- note"), "SELECT 1")
        self.assertEqual(q.sanitize("/* x */ SELECT 1 ;;;"), "SELECT 1")

    def test_rejects_write_statements(self):
        for sql in ("UPDATE fund SET name='x'", "DELETE FROM fund", "DROP TABLE fund",
                    "PRAGMA page_count", "ATTACH DATABASE 'x' AS y", "INSERT INTO fund VALUES(1)"):
            with self.assertRaises(q.QueryRejected, msg=sql):
                q.check(sql)

    def test_rejects_multiple_statements(self):
        with self.assertRaises(q.QueryRejected):
            q.check("SELECT 1; SELECT 2")

    def test_rejects_empty_and_non_select(self):
        with self.assertRaises(q.QueryRejected):
            q.check("   ")
        with self.assertRaises(q.QueryRejected):
            q.check("EXPLAIN SELECT 1")

    def test_allows_select_and_cte(self):
        self.assertEqual(q.check("select * from fund"), "select * from fund")
        self.assertTrue(q.check("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH"))

    def test_clamp_limit(self):
        self.assertEqual(q.clamp_limit(0), 1)
        self.assertEqual(q.clamp_limit(10 ** 9), q.MAX_LIMIT)
        self.assertEqual(q.clamp_limit("abc"), q.DEFAULT_LIMIT)
        self.assertEqual(q.clamp_limit(50), 50)


class QueryRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t (code TEXT, v REAL)")
        conn.executemany("INSERT INTO t VALUES (?,?)", [(f"{i:06d}", i / 10) for i in range(30)])
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_returns_rows_and_truncation(self):
        res = q.run(self.db, "SELECT code, v FROM t ORDER BY code", limit=5)
        self.assertEqual(res["columns"], ["code", "v"])
        self.assertEqual(res["row_count"], 5)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["rows"][0][0], "000000")
        self.assertEqual(res["rows"][0][1], 0.0)

    def test_run_not_truncated_when_fewer_rows(self):
        res = q.run(self.db, "SELECT code FROM t", limit=100)
        self.assertEqual(res["row_count"], 30)
        self.assertFalse(res["truncated"])

    def test_run_is_read_only(self):
        with self.assertRaises(q.QueryRejected):
            q.run(self.db, "DELETE FROM t")
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 30)
        conn.close()

    def test_bad_sql_reports_operational_error(self):
        with self.assertRaises(q.QueryRejected):
            q.run(self.db, "SELECT nope FROM t")

    def test_limit_is_clamped_in_result(self):
        res = q.run(self.db, "SELECT code FROM t", limit=99999)
        self.assertEqual(res["limit"], q.MAX_LIMIT)


class StorageTest(unittest.TestCase):
    def test_storage_reports_db_and_project_footprint(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        dbp = root / "f.db"
        conn = sqlite3.connect(dbp)
        conn.execute("CREATE TABLE a (x INTEGER)")
        conn.executemany("INSERT INTO a VALUES (?)", [(i,) for i in range(10)])
        conn.commit()
        conn.close()
        (root / "raw").mkdir()
        (root / "raw" / "x.gz").write_bytes(b"0" * 100)
        (root / "freeze").mkdir()
        (root / "reports").mkdir()

        class Cfg:
            db_path = str(dbp)
            raw_dir = str(root / "raw")
            freeze_dir = str(root / "freeze")
            report_root = str(root / "reports")

        res = st.storage(Cfg)
        self.assertEqual(res["db"]["bytes"], dbp.stat().st_size)
        self.assertEqual(res["disk"]["raw_bytes"], 100)
        self.assertEqual(res["disk"]["raw_files"], 1)
        # 只给"数据库多大 / 项目占多少硬盘"，不再暴露表级明细
        self.assertNotIn("tables", res)
        self.assertNotIn("dbstat", res)
        self.assertGreaterEqual(res["disk"]["project_bytes"], res["db"]["bytes"])
        self.assertEqual(res["total_bytes"], res["disk"]["project_bytes"])
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()

class DiagnosticsTest(unittest.TestCase):
    """诊断 CSV 只读访问：白名单、BOM、截断。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rev = Path(self.tmp.name)
        d = self.rev / "diagnostics"
        d.mkdir()
        (d / "enhanced_near_miss.csv").write_text(
            "\ufefffcode,name,failed\n000001,A,annual_1y\n000002,B,\n", encoding="utf-8")
        (d / "notes.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_only_csv(self):
        from unittest.mock import patch
        from fundana.api import diagnostics as dg
        with patch.object(dg, "current_revision", return_value=self.rev):
            names = [f["name"] for f in dg.list_files(object())]
        self.assertEqual(names, ["enhanced_near_miss.csv"])

    def test_load_strips_bom_and_caps_rows(self):
        from unittest.mock import patch
        from fundana.api import diagnostics as dg
        with patch.object(dg, "current_revision", return_value=self.rev):
            res = dg.load(object(), "enhanced_near_miss.csv", limit=1)
        self.assertEqual(res["columns"], ["fcode", "name", "failed"])
        self.assertEqual(res["total"], 2)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["rows"][0]["fcode"], "000001")

    def test_rejects_traversal_and_non_csv(self):
        from unittest.mock import patch
        from fundana.api import diagnostics as dg
        with patch.object(dg, "current_revision", return_value=self.rev):
            for bad in ("../enhanced_near_miss.csv", "a/b.csv", "notes.txt", ""):
                with self.assertRaises(dg.DiagnosticError, msg=bad):
                    dg.load(object(), bad)

    def test_missing_file_raises(self):
        from unittest.mock import patch
        from fundana.api import diagnostics as dg
        with patch.object(dg, "current_revision", return_value=self.rev):
            with self.assertRaises(dg.DiagnosticError):
                dg.load(object(), "coverage.csv")


class NavSeriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE fund_nav_daily (fcode TEXT, nav_date TEXT, unit_nav REAL,"
                     " acc_nav REAL, PRIMARY KEY(fcode, nav_date))")
        conn.execute("INSERT INTO fund_nav_daily VALUES ('000001','2024-01-01',1.0,1.0)")
        conn.executemany("INSERT INTO fund_nav_daily VALUES ('000002',?,?,?)",
                         [(f"2023-{m:02d}-{d:02d}", 1.0 + m / 100 + d / 10000, 1.0)
                          for m in range(1, 13) for d in range(1, 29)])
        conn.commit()
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_unknown_fund_returns_empty(self):
        res = series.nav_series(self.conn, "999999")
        self.assertEqual(res["points"], [])
        self.assertEqual(res["n_total"], 0)

    def test_window_filters_by_end_date(self):
        res = series.nav_series(self.conn, "000002", days=40)
        self.assertLess(res["n_total"], 40)
        self.assertEqual(res["end"], "2023-12-28")
        self.assertTrue(all(p["nav_date"] > "2023-11-18" for p in res["points"]))

    def test_downsample_keeps_first_and_last(self):
        res = series.nav_series(self.conn, "000002", days=1095, max_points=50)
        self.assertTrue(res["downsampled"])
        self.assertLessEqual(res["n_points"], 51)
        self.assertEqual(res["points"][0]["nav_date"], res["start"])
        self.assertEqual(res["points"][-1]["nav_date"], res["end"])


class LabelsTest(unittest.TestCase):
    """内部枚举 → 中文标签：前端不再出现 bond_* / em_* / multi_field_consistent。"""

    def test_enum_labels(self):
        from fundana.api import labels as lb
        self.assertEqual(lb.label_style("bond_secondary_enhanced"), "二级债基（增强）")
        self.assertEqual(lb.label_style("bond_index"), "指数债")
        self.assertEqual(lb.label_confidence("multi_field_consistent"), "多字段一致")
        self.assertTrue(lb.label_scale_source("em_jbgk_family_sum").startswith("同家族份额合计"))
        self.assertEqual(lb.label_board("bond_locked"), "持有期受限榜")
        self.assertEqual(lb.label_layer("L6C"), "绝对门槛")
        self.assertEqual(lb.label_layer("L8R"), "独立风险门")

    def test_reason_labels(self):
        from fundana.api import labels as lb
        self.assertEqual(lb.label_reason("composite:below_min"), "综合分低于门槛")
        self.assertEqual(lb.label_reason("scale_below_3.00"), "规模低于该成熟度要求（3.00 亿）")
        self.assertIn("81 天", lb.label_reason("manager_career_below_365d:81d"))
        self.assertIn("365 天", lb.label_reason("lockup_conflict:365d"))
        self.assertEqual(lb.label_reason("nav_return_window_proxy:365d"), "持有窗口不足，改用 365 天收益")
        self.assertIn("7 天", lb.label_reason("sampling_cadence_gt_7d"))
        self.assertEqual(lb.label_reason("something_new"), "其他原因")

    def test_payload_has_no_raw_codes_as_labels(self):
        from fundana.api import labels as lb
        payload = lb.labels_payload()
        self.assertEqual(sorted(payload), ["boards", "bond_classes", "buyable", "check_groups",
                                          "checks", "confidences", "fetch_statuses", "layers",
                                          "lock_buckets", "lockups", "profiles", "return_types",
                                          "run_modes", "run_statuses", "scale_sources",
                                          "sources", "styles", "user_fits"])
        for group in payload.values():
            for item in group:
                self.assertTrue(item["code"] and item["label"])
                self.assertNotIn(item["code"], item["label"])


class _SeriesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(Path(self.tmp.name) / "t.db")
        self.conn.execute(
            "CREATE TABLE fund_nav_daily (fcode TEXT, nav_date TEXT, unit_nav REAL,"
            " acc_nav REAL, growth_pct REAL, nav_source TEXT, PRIMARY KEY(fcode, nav_date))")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _fill(self, fcode, start, days, growth=0.1, drop_on=None, drop_pct=0.0):
        d0 = date.fromisoformat(start)
        rows = []
        for i in range(days):
            day = (d0 + timedelta(days=i)).isoformat()
            g = drop_pct if day == drop_on else growth
            rows.append((fcode, day, 1.0 + i / 10000.0, 1.0, g, "em_pzd"))
        self.conn.executemany(
            "INSERT INTO fund_nav_daily (fcode, nav_date, unit_nav, acc_nav, growth_pct,"
            " nav_source) VALUES (?,?,?,?,?,?)", rows)
        self.conn.commit()


class PeriodsTest(_SeriesBase):
    def test_month_and_year_aggregation(self):
        from fundana.api import periods as pd
        self._fill("000100", "2024-01-01", 91)
        month = pd.period_rows(self.conn, "000100", "month")
        self.assertEqual(month["total_periods"], 3)
        self.assertEqual(month["rows"][0]["period"], "2024-03")
        self.assertEqual(month["rows"][0]["n_obs"], 31)
        self.assertAlmostEqual(month["rows"][0]["chain_return_pct"],
                               round(((1.001 ** 31) - 1) * 100, 4), places=3)
        self.assertTrue(month["rows"][0]["partial"])   # 最新周期未结束
        self.assertTrue(month["rows"][-1]["partial"])  # 首周期从数据起点开始
        year = pd.period_rows(self.conn, "000100", "year")
        self.assertEqual(year["total_periods"], 1)
        self.assertAlmostEqual(year["rows"][0]["chain_return_pct"],
                               round(((1.001 ** 91) - 1) * 100, 4), places=3)
        self.assertEqual(year["bounds"]["n_obs"], 91)

    def test_week_buckets(self):
        from fundana.api import periods as pd
        self._fill("000101", "2024-01-01", 91)
        week = pd.period_rows(self.conn, "000101", "week")
        self.assertEqual(week["total_periods"], 13)
        self.assertEqual(week["rows"][0]["period"], "2024-W13")
        self.assertEqual(week["rows"][0]["period_label"], "2024 年第 13 周")

    def test_drawdown_from_chain_series(self):
        from fundana.api import periods as pd
        self._fill("000200", "2024-01-01", 10, growth=0.0, drop_on="2024-01-05", drop_pct=-10.0)
        jan = pd.period_rows(self.conn, "000200", "month")
        self.assertAlmostEqual(jan["rows"][0]["mdd_pct"], 10.0, places=4)
        self.assertAlmostEqual(jan["rows"][0]["chain_return_pct"], -10.0, places=4)

    def test_invalid_granularity(self):
        from fundana.api import periods as pd
        with self.assertRaises(pd.PeriodError):
            pd.period_rows(self.conn, "000100", "quarter")

    def test_empty_fund(self):
        from fundana.api import periods as pd
        res = pd.period_rows(self.conn, "999999", "month")
        self.assertEqual(res["rows"], [])
        self.assertEqual(res["total_periods"], 0)


class NavRowsTest(_SeriesBase):
    def test_pagination_is_newest_first(self):
        self._fill("000300", "2024-01-01", 10, growth=0.0)
        res = series.nav_rows(self.conn, "000300", page=1, page_size=3)
        self.assertEqual(res["total"], 10)
        self.assertEqual(res["pages"], 4)
        self.assertEqual(len(res["rows"]), 3)
        self.assertEqual(res["rows"][0]["nav_date"], "2024-01-10")
        last = series.nav_rows(self.conn, "000300", page=4, page_size=3)
        self.assertEqual([r["nav_date"] for r in last["rows"]], ["2024-01-01"])
        self.assertEqual(last["bounds"]["n_obs"], 10)

    def test_range_filter(self):
        self._fill("000301", "2024-01-01", 10, growth=0.0)
        res = series.nav_rows(self.conn, "000301", start="2024-01-03", end="2024-01-05")
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["rows"][0]["nav_date"], "2024-01-05")

    def test_bad_range_and_clamp(self):
        self._fill("000302", "2024-01-01", 10, growth=0.0)
        with self.assertRaises(series.RangeError):
            series.nav_rows(self.conn, "000302", start="2024/01/01")
        with self.assertRaises(series.RangeError):
            series.nav_rows(self.conn, "000302", start="2024-02-01", end="2024-01-01")
        res = series.nav_rows(self.conn, "000302", page_size=9999)
        self.assertEqual(res["page_size"], series.MAX_PAGE_SIZE)

class SortingTest(unittest.TestCase):
    ALLOWED = {"rank": "p.rank", "annual_1y": "m.annual_dec",
               "sharpe": "m.sharpe_ratio", "mdd": "m.mdd_dec"}

    def test_single_key(self):
        spec = sorting.parse("annual_1y", "desc", self.ALLOWED)
        self.assertEqual(spec, [("annual_1y", "desc")])
        self.assertEqual(sorting.order_sql(spec, self.ALLOWED), "m.annual_dec DESC NULLS LAST, p.rank ASC")

    def test_multi_key_order_short_repeats_last(self):
        spec = sorting.parse("annual_1y,sharpe,mdd", "desc", self.ALLOWED)
        self.assertEqual([k for k, _ in spec], ["annual_1y", "sharpe", "mdd"])
        self.assertEqual([d for _, d in spec], ["desc", "desc", "desc"])

    def test_unknown_keys_dropped_and_fallback(self):
        spec = sorting.parse("nope,annual_1y", "asc", self.ALLOWED)
        self.assertEqual(spec, [("annual_1y", "asc")])
        self.assertEqual(sorting.parse("nope", "", self.ALLOWED), [("rank", "asc")])
        self.assertEqual(sorting.parse("", "", self.ALLOWED), [("rank", "asc")])

    def test_max_three_keys_and_rank_mapping(self):
        spec = sorting.parse("annual_1y,sharpe,mdd,rank", "desc", self.ALLOWED)
        self.assertEqual(len(spec), sorting.MAX_SORT_KEYS)
        sql = sorting.order_sql(spec, self.ALLOWED)
        self.assertTrue(sql.endswith("p.rank ASC"))
        self.assertIn("m.annual_dec DESC", sql)
        self.assertEqual(sorting.as_query(spec), {"sort": "annual_1y,sharpe,mdd", "order": "desc,desc,desc"})


class _DecisionDbBase(unittest.TestCase):
    def setUp(self):
        from fundana import db as fdb
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)

        class Cfg:
            app = {"db": {"busy_timeout_ms": 15000, "wal": False}}
            db_path = root / "t.db"
            raw_dir = root / "raw"
            report_root = root / "reports"
            freeze_dir = root / "freeze"
            code_version = "0.1.0"

            def get(self, *keys, default=None):
                return default

        self.cfg = Cfg()
        self.conn = fdb.connect(self.cfg)
        fdb.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO screen_run(run_id, profile, config_hash, code_version,"
            " formula_contract, created_at, business_date) VALUES(?,?,?,?,?,?,?)",
            ("r1", "bond_v1", "h", "0.1.0", "f", "2026-11-01T10:00:00+08:00", "2026-11-01"))
        self.conn.execute(
            "INSERT INTO screen_run(run_id, profile, config_hash, code_version,"
            " formula_contract, created_at, business_date) VALUES(?,?,?,?,?,?,?)",
            ("r2", "bond_v1", "h", "0.1.0", "f", "2026-11-01T11:00:00+08:00", "2026-11-01"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class RankHistoryTest(_DecisionDbBase):
    def _decision(self, run_id, scores):
        import json as _json
        self.conn.execute(
            "INSERT INTO screen_decision(run_id, fcode, layer_reached, passed,"
            " reason_codes_json, scores_json) VALUES(?,?,?,?,?,?)",
            (run_id, "000090", "L6C", 0, "[]", _json.dumps(scores)))
        self.conn.commit()

    def test_unpicked_rounds_still_have_score_series(self):
        from fundana.api import decision
        self._decision("r1", {"formal_score": 48.2, "composite": 59.1})
        self._decision("r2", {"formal_score": 51.4, "composite": 61.7})
        h = decision.rank_history(self.conn, "000090")
        self.assertEqual(len(h["items"]), 2)
        self.assertIsNone(h["items"][0]["rank"])
        self.assertEqual(h["items"][0]["score_source"], "decision")
        self.assertAlmostEqual(h["items"][0]["score"], 48.2, places=4)
        self.assertAlmostEqual(h["items"][1]["composite"], 61.7, places=4)
        self.assertAlmostEqual(h["score_delta"], 3.2, places=4)
        self.assertIsNone(h["best_rank"])

    def test_pick_score_wins_over_decision_snapshot(self):
        from fundana.api import decision
        self._decision("r2", {"formal_score": 51.4, "composite": 61.7})
        self.conn.execute(
            "INSERT INTO screen_pick(board, rank, run_id, fcode, formal_score, confidence)"
            " VALUES('bond_stable', 7, 'r2', '000090', 52.0, 9.0)")
        self.conn.commit()
        h = decision.rank_history(self.conn, "000090")
        self.assertEqual(h["items"][0]["score_source"], "pick")
        self.assertEqual(h["items"][0]["rank"], 7)
        self.assertAlmostEqual(h["items"][0]["score"], 52.0, places=4)


class MetricsMetaTest(unittest.TestCase):
    def test_alias_entries_present(self):
        keys = {m["key"] for m in metrics_meta.payload()["metrics"]}
        for key in ("mdd", "mdd_1y", "tail", "tail_completeness", "worst90"):
            self.assertIn(key, keys)

    def test_alias_resolves_to_same_text(self):
        a = metrics_meta.get_metric("mdd")
        b = metrics_meta.get_metric("mdd_1y")
        self.assertIsNotNone(a)
        self.assertEqual(a["desc"], b["desc"])

    def test_every_layer_detail_has_number(self):
        for layer in metrics_meta.layer_details():
            self.assertRegex(layer["detail"], r"\d", layer["code"])

