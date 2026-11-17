"""B1 跨多日停机：缺口识别与候选池回补的单元测试。"""
import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock

from fundana import collect


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE fund(fcode TEXT PRIMARY KEY, type_group TEXT);
        CREATE TABLE src_directory(code TEXT, business_date TEXT, type_text TEXT);
        CREATE TABLE fund_nav_daily(fcode TEXT, nav_date TEXT, unit_nav REAL);
        CREATE TABLE screen_run(run_id TEXT PRIMARY KEY, status TEXT, created_at TEXT);
        CREATE TABLE screen_run_input(run_id TEXT, fcode TEXT, passed INTEGER,
                                      asset_scale_billion REAL);
        CREATE TABLE screen_pick(run_id TEXT, fcode TEXT);
    """)
    conn.executemany("INSERT INTO fund VALUES(?, 'bond')", [("000001",), ("000002",)])
    conn.executemany("INSERT INTO src_directory VALUES(?, '2026-11-06', '债券型')",
                     [("000001",), ("000002",)])
    conn.executemany("INSERT INTO fund_nav_daily VALUES(?,?,1.0)",
                     [("000001", "2026-11-03"), ("000002", "2026-11-03"),
                      ("000001", "2026-11-04")])
    conn.execute("INSERT INTO screen_run VALUES('r1','success','2026-11-06T07:00:00')")
    conn.executemany("INSERT INTO screen_run_input VALUES('r1',?,1,?)",
                     [("000001", 10.0), ("000002", 5.0)])
    return conn


class MissingNavDatesTest(unittest.TestCase):
    def test_flags_low_coverage_trading_days_only(self):
        gaps = collect.missing_nav_dates(_conn(), "2026-11-02", "2026-11-06",
                                         min_ratio=0.8)
        self.assertEqual([g["date"] for g in gaps], ["2026-11-04", "2026-11-05"])
        self.assertEqual(gaps[0]["covered"], 1)
        self.assertEqual(gaps[1]["ratio"], 0.0)

    def test_weekend_is_not_a_gap(self):
        gaps = collect.missing_nav_dates(_conn(), "2026-11-06", "2026-11-09",
                                         min_ratio=0.8)
        self.assertEqual(gaps, [])

    def test_reversed_range_is_empty(self):
        self.assertEqual(collect.missing_nav_dates(_conn(), "2026-11-09",
                                                   "2026-11-06"), [])


class BackfillNavGapsTest(unittest.TestCase):
    def test_backfills_candidates_when_gap_exists(self):
        conn = _conn()
        calls = []

        def fake(fetcher, code, business_date, force=False):
            calls.append((code, business_date, force))
            return {"ok": True, "nav_rows": 120}

        with mock.patch.object(collect, "collect_pzd_one", side_effect=fake):
            out = collect.backfill_nav_gaps(SimpleNamespace(conn=conn), "2026-11-10",
                                            "2026-11-02", "2026-11-06",
                                            max_funds=5, min_ratio=0.8)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["attempted"], 2)
        self.assertEqual(out["nav_rows"], 240)
        self.assertEqual(calls, [("000001", "2026-11-10", True),
                                 ("000002", "2026-11-10", True)])

    def test_no_gap_does_not_fetch(self):
        conn = _conn()
        conn.executemany("INSERT INTO fund_nav_daily VALUES(?,?,1.0)",
                         [("000002", "2026-11-04"), ("000001", "2026-11-05"),
                          ("000002", "2026-11-05")])
        with mock.patch.object(collect, "collect_pzd_one") as fake:
            out = collect.backfill_nav_gaps(SimpleNamespace(conn=conn), "2026-11-10",
                                            "2026-11-02", "2026-11-06", min_ratio=0.8)
        self.assertEqual(out["status"], "not_needed")
        fake.assert_not_called()

    def test_no_candidates_is_reported_not_silent(self):
        conn = _conn()
        conn.execute("DELETE FROM screen_run_input")
        out = collect.backfill_nav_gaps(SimpleNamespace(conn=conn), "2026-11-10",
                                        "2026-11-02", "2026-11-06", min_ratio=0.8)
        self.assertEqual(out["status"], "no_candidates")
        self.assertFalse(out["ok"])
        self.assertEqual(len(out["missing"]), 2)


if __name__ == "__main__":
    unittest.main()
