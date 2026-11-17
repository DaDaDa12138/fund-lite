import sqlite3
import unittest

from fundana.audit import events


SCHEMA = """
CREATE TABLE fund_manager_tenure (
    fcode TEXT NOT NULL, mgr_id TEXT, mgr_name TEXT,
    start_date TEXT, end_date TEXT, days INTEGER, is_current INTEGER,
    src TEXT, updated_at TEXT,
    PRIMARY KEY (fcode, mgr_id, start_date)
);
CREATE TABLE fund_scale_history (
    fcode TEXT NOT NULL, report_period TEXT NOT NULL,
    asset_scale_billion REAL, share_scale_billion REAL,
    PRIMARY KEY (fcode, report_period)
);
"""


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


class TestManagerFeatures(unittest.TestCase):
    def test_current_and_changes(self):
        conn = _conn()
        conn.executemany(
            "INSERT INTO fund_manager_tenure (fcode,mgr_id,mgr_name,start_date,end_date)"
            " VALUES (?,?,?,?,?)",
            [("000001", "1", "甲", "2019-01-01", "2024-12-31"),
             ("000001", "2", "乙", "2025-01-01", None)])
        out = events.manager_features(conn, "000001", "2025-06-30")
        self.assertEqual(out["current_count"], 1)
        self.assertEqual(out["changes_365d"], 1)
        self.assertEqual(out["max_tenure_days"], 180)
        self.assertEqual(out["last_change_days"], 180)

    def test_co_managers_use_longest_tenure(self):
        conn = _conn()
        conn.executemany(
            "INSERT INTO fund_manager_tenure (fcode,mgr_id,mgr_name,start_date,end_date)"
            " VALUES (?,?,?,?,?)",
            [("000001", "1", "甲", "2019-01-01", None),
             ("000001", "2", "乙", "2025-01-01", None)])
        out = events.manager_features(conn, "000001", "2025-06-30")
        self.assertEqual(out["current_count"], 2)
        self.assertEqual(out["max_tenure_days"], 2372)
        self.assertEqual(out["newest_tenure_days"], 180)

    def test_no_history(self):
        out = events.manager_features(_conn(), "000001", "2025-06-30")
        self.assertIsNone(out["changes_365d"])
        self.assertIsNone(out["max_tenure_days"])


class TestScaleFeatures(unittest.TestCase):
    def test_yoy_and_half_year(self):
        conn = _conn()
        conn.executemany(
            "INSERT INTO fund_scale_history (fcode,report_period,asset_scale_billion,"
            " share_scale_billion) VALUES (?,?,?,?)",
            [("000001", "2024-06-30", 10.0, 8.0),
             ("000001", "2024-12-31", 12.0, 9.0),
             ("000001", "2025-06-30", 15.0, 11.0)])
        out = events.scale_features(conn, "000001", "2025-09-30")
        self.assertEqual(out["report_period"], "2025-06-30")
        self.assertEqual(out["aum_billion"], 15.0)
        self.assertEqual(out["aum_yoy_pct"], 50.0)
        self.assertEqual(out["aum_6m_pct"], 25.0)
        self.assertEqual(out["share_yoy_pct"], 37.5)

    def test_lag_excludes_latest_report(self):
        conn = _conn()
        conn.executemany(
            "INSERT INTO fund_scale_history (fcode,report_period,asset_scale_billion,"
            " share_scale_billion) VALUES (?,?,?,?)",
            [("000001", "2025-06-30", 10.0, 8.0),
             ("000001", "2025-09-30", 99.0, 99.0)])
        out = events.scale_features(conn, "000001", "2025-10-15")
        self.assertEqual(out["report_period"], "2025-06-30")

    def test_missing(self):
        out = events.scale_features(_conn(), "000001", "2025-09-30")
        self.assertIsNone(out["aum_billion"])
        self.assertIsNone(out["aum_yoy_pct"])


class TestBuckets(unittest.TestCase):
    def test_manager_change_and_tenure(self):
        mf = {"changes_365d": 2, "max_tenure_days": 400}
        sf = {"aum_yoy_pct": 12.0, "aum_6m_pct": -25.0}
        out = events.event_buckets(mf, sf)
        self.assertEqual(out["manager_changes"], "2+")
        self.assertEqual(out["manager_tenure"], "365_730")
        self.assertEqual(out["aum_yoy"], "0_50")
        self.assertEqual(out["aum_6m"], "lt_-20")

    def test_unknown(self):
        out = events.event_buckets({"changes_365d": None, "max_tenure_days": None},
                                   {"aum_yoy_pct": None, "aum_6m_pct": None})
        self.assertEqual(set(out.values()), {"unknown"})


class TestSummarize(unittest.TestCase):
    def test_weighted_by_bucket_n(self):
        cohorts = [
            {"buckets": {"manager_changes": {"0": {"n": 10, "annual_mean_pct": 3.0,
                                                   "sharpe_mean": 2.0, "hit_rate": 0.5}}},
             "ic": {"manager_changes": 0.1}},
            {"buckets": {"manager_changes": {"0": {"n": 30, "annual_mean_pct": 5.0,
                                                   "sharpe_mean": 4.0, "hit_rate": 0.7}}},
             "ic": {"manager_changes": 0.3}},
        ]
        out = events.summarize(cohorts)
        self.assertEqual(out["manager_changes"]["0"]["n"], 40)
        self.assertEqual(out["manager_changes"]["0"]["annual_mean_pct"], 4.0)
        self.assertEqual(out["ic"]["manager_changes"], 0.2)


class TestRender(unittest.TestCase):
    def test_contains_buckets(self):
        result = {"horizon_days": 365,
                  "summary": {"manager_changes": {"0": {"n": 10, "annual_mean_pct": 3.0,
                                                         "sharpe_mean": 2.0, "hit_rate": 0.5}},
                              "ic": {"manager_changes": 0.1}}}
        text = events.render(result)
        self.assertIn("manager_changes", text)
        self.assertIn("0.1", text)


if __name__ == "__main__":
    unittest.main()
