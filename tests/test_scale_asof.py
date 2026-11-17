"""规模 as-of 视图单测。"""
import sqlite3
import unittest

from fundana.fuse.scale import family_scale_rows, scale_asof_map, share_scale_asof_map


class TestScaleAsof(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE fund_scale_history(fcode TEXT, report_period TEXT,"
            " asset_scale_billion REAL, share_scale_billion REAL,"
            " PRIMARY KEY(fcode, report_period))")
        rows = [("A", "2020-03-31", 5.0, 4.0), ("A", "2020-06-30", 6.0, 5.0),
                ("A", "2020-09-30", 7.0, 6.0), ("B", "2020-06-30", 9.0, 8.0)]
        self.conn.executemany("INSERT INTO fund_scale_history VALUES(?,?,?,?)", rows)

    def test_lag_45_days_picks_previous_quarter(self):
        m = scale_asof_map(self.conn, "2020-06-30")
        self.assertEqual(m["A"]["report_period"], "2020-03-31")
        self.assertEqual(m["A"]["asset_scale_billion"], 5.0)
        self.assertNotIn("B", m)

    def test_after_disclosure_picks_new_quarter(self):
        m = scale_asof_map(self.conn, "2020-08-15")
        self.assertEqual(m["A"]["report_period"], "2020-06-30")
        self.assertEqual(m["B"]["asset_scale_billion"], 9.0)

    def test_missing_history_returns_empty(self):
        self.assertEqual(scale_asof_map(self.conn, "2019-12-31"), {})

    def test_null_asset_scale_skips_to_last_non_null(self):
        self.conn.execute("INSERT INTO fund_scale_history VALUES('C','2020-03-31',3.0,2.0)")
        self.conn.execute("INSERT INTO fund_scale_history VALUES('C','2020-06-30',NULL,9.0)")
        m = scale_asof_map(self.conn, "2020-09-30")
        self.assertEqual(m["C"]["report_period"], "2020-03-31")
        self.assertEqual(m["C"]["asset_scale_billion"], 3.0)

    def test_share_scale_map_is_per_share_and_skips_null(self):
        self.conn.execute("INSERT INTO fund_scale_history VALUES('C','2020-03-31',3.0,2.0)")
        self.conn.execute("INSERT INTO fund_scale_history VALUES('C','2020-06-30',4.0,NULL)")
        m = share_scale_asof_map(self.conn, "2020-09-30")
        # 2020-06-30 份额规模为 NULL，不能拿资产规模 4.0 冒充，退回 2020-03-31 的 2.0
        self.assertEqual(m["C"]["report_period"], "2020-03-31")
        self.assertEqual(m["C"]["share_scale_billion"], 2.0)
        self.assertAlmostEqual(m["A"]["share_scale_billion"], 5.0)


class TestScaleFamilyAggregation(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            "CREATE TABLE fund_scale_history(fcode TEXT, report_period TEXT,"
            " asset_scale_billion REAL, share_scale_billion REAL,"
            " PRIMARY KEY(fcode, report_period));"
            "CREATE TABLE fund_share_class(fcode TEXT PRIMARY KEY, family_id TEXT);")
        self.conn.executemany("INSERT INTO fund_share_class VALUES(?,?)",
                              [("A", "F1"), ("C", "F1"), ("B", "F2")])
        self.conn.executemany("INSERT INTO fund_scale_history VALUES(?,?,?,?)",
                              [("A", "2020-03-31", 6.0, 5.0),
                               ("C", "2020-03-31", 1.0, 0.8),
                               ("A", "2020-06-30", 7.0, 6.0),
                               ("C", "2020-06-30", 2.0, 1.5),
                               ("B", "2020-06-30", 9.0, 8.0)])

    def test_share_classes_share_family_total(self):
        m = scale_asof_map(self.conn, "2020-08-15")
        self.assertEqual(m["A"]["asset_scale_billion"], 9.0)
        self.assertEqual(m["C"]["asset_scale_billion"], 9.0)
        self.assertEqual(m["A"]["report_period"], "2020-06-30")
        self.assertEqual(m["B"]["asset_scale_billion"], 9.0)

    def test_family_scale_rows_sum_per_period(self):
        rows = family_scale_rows(self.conn, "C", "2020-08-15")
        self.assertEqual([r["report_period"] for r in rows],
                         ["2020-03-31", "2020-06-30"])
        self.assertEqual(rows[-1]["asset_scale_billion"], 9.0)
        self.assertEqual(rows[-1]["share_scale_billion"], 7.5)


if __name__ == "__main__":
    unittest.main()
