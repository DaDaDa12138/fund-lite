"""Tracker（长期跟踪榜）单测：幂等、进出、频率分母。"""
import sqlite3
import unittest

from fundana import db, tracker


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def _pick(code, board="bond_stable", rank=1, score=80.0):
    return {"fcode": code, "board": board, "rank": rank, "formal_score": score,
            "confidence": 9.0, "style_code": "bond_pure"}


class TrackerTest(unittest.TestCase):
    def test_same_publication_is_not_counted_twice(self):
        conn = _conn()
        first = tracker.update_after_publish(conn, "pub1", "2026-11-01", "run1", [_pick("000001")])
        again = tracker.update_after_publish(conn, "pub1", "2026-11-01", "run1", [_pick("000001")])
        self.assertFalse(first["skipped"])
        self.assertTrue(again["skipped"])
        self.assertEqual(again["reason"], "snapshot_already_counted")
        self.assertEqual(tracker.snapshots_total(conn), 1)
        row = conn.execute("SELECT appearances, streak FROM screening_tracker WHERE fcode='000001'").fetchone()
        self.assertEqual((row["appearances"], row["streak"]), (1, 1))

    def test_entered_held_exited_returned(self):
        conn = _conn()
        tracker.update_after_publish(conn, "pub1", "2026-11-01", "r1", [_pick("000001"), _pick("000002", rank=2)])
        tracker.update_after_publish(conn, "pub2", "2026-11-02", "r2", [_pick("000001"), _pick("000003")])
        rows = {r["fcode"]: r for r in conn.execute("SELECT * FROM screening_tracker")}
        self.assertEqual(rows["000001"]["appearances"], 2)
        self.assertEqual(rows["000001"]["streak"], 2)
        self.assertEqual(rows["000002"]["status"], "exited")
        self.assertEqual(rows["000002"]["streak"], 0)
        self.assertEqual(rows["000003"]["appearances"], 1)
        ev = conn.execute("SELECT change FROM screening_tracker_event WHERE publication_id='pub2'"
                          " AND fcode='000003'").fetchone()["change"]
        self.assertEqual(ev, "entered")
        # 回流：000002 再进榜
        tracker.update_after_publish(conn, "pub3", "2026-11-03", "r3", [_pick("000002")])
        ev2 = conn.execute("SELECT change FROM screening_tracker_event WHERE publication_id='pub3'"
                           " AND fcode='000002'").fetchone()["change"]
        self.assertEqual(ev2, "returned")
        row2 = conn.execute("SELECT appearances, status, streak FROM screening_tracker WHERE fcode='000002'").fetchone()
        self.assertEqual((row2["appearances"], row2["status"], row2["streak"]), (2, "active", 1))

    def test_locked_board_not_tracked_and_frequency_denominator(self):
        conn = _conn()
        tracker.update_after_publish(conn, "pub1", "2026-11-01", "r1",
                                     [_pick("000001"), _pick("000009", board="bond_locked")])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM screening_tracker").fetchone()[0], 1)
        tracker.update_after_publish(conn, "pub2", "2026-11-02", "r2", [_pick("000001", score=82.0)])
        data = tracker.current(conn)
        row = data["rows"][0]
        self.assertEqual(row["snapshots_total"], 2)
        self.assertEqual(row["frequency_pct"], 100.0)
        self.assertAlmostEqual(row["score_delta"], 2.0)

    def test_metric_contract_duplicates_do_not_duplicate_rows(self):
        """fund_metric 多契约共存时，跟踪榜每只基金只能出一行（锁最新一轮的契约）。"""
        conn = _conn()
        conn.execute("INSERT INTO screen_run(run_id, profile, config_hash, code_version,"
                     " formula_contract, created_at, business_date)"
                     " VALUES('r9','bond_v1','h','0.1.0','fundana-risk-2026.11.4',"
                     " '2026-11-02T09:00:00','2026-11-02')")
        tracker.update_after_publish(conn, "pub1", "2026-11-01", "r1", [_pick("000001")])
        for contract, annual in (("fundana-risk-2026.11.3", 0.01),
                                 ("fundana-risk-2026.11.4", 0.02)):
            conn.execute("INSERT INTO fund_metric(fcode, period_days, formula_contract, annual_dec)"
                         " VALUES('000001', 365, ?, ?)", (contract, annual))
        data = tracker.current(conn)
        self.assertEqual(len(data["rows"]), 1)
        self.assertAlmostEqual(data["rows"][0]["annual_1y"], 0.02)

    def test_same_business_date_republish_supersedes(self):
        """同一交易日重跑 screen + publish 只算一份快照：出现次数不因重发翻倍。"""
        conn = _conn()
        tracker.update_after_publish(conn, "pub1", "2026-11-01", "r1",
                                     [_pick("000001"), _pick("000002", rank=2)])
        out = tracker.update_after_publish(conn, "pub2", "2026-11-01", "r2",
                                           [_pick("000001"), _pick("000003", rank=2)])
        self.assertEqual(out["superseded"], ["pub1"])
        self.assertEqual(tracker.snapshots_total(conn), 1)
        rows = {r["fcode"]: r for r in conn.execute("SELECT * FROM screening_tracker")}
        self.assertEqual(set(rows), {"000001", "000003"})
        self.assertEqual((rows["000001"]["appearances"], rows["000001"]["streak"]), (1, 1))
        self.assertEqual(rows["000003"]["appearances"], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM screening_tracker_event"
                                      " WHERE publication_id='pub1'").fetchone()[0], 0)
        # 重算幂等：再跑一次 rebuild 结果不变
        tracker.rebuild(conn)
        self.assertEqual(tracker.snapshots_total(conn), 1)
        row = conn.execute("SELECT appearances FROM screening_tracker WHERE fcode='000001'").fetchone()
        self.assertEqual(row["appearances"], 1)

    def test_older_business_date_is_not_inserted_before_latest(self):
        """重跑旧净值日不能把跟踪榜倒回去，否则最新一期会算出假换手。"""
        conn = _conn()
        tracker.update_after_publish(conn, "pub2", "2026-11-02", "r2", [_pick("000001")])
        out = tracker.update_after_publish(conn, "pub1", "2026-11-01", "r1", [_pick("000002")])
        self.assertTrue(out["skipped"])
        self.assertEqual(out["reason"], "older_business_date")
        self.assertEqual(tracker.snapshots_total(conn), 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM screening_tracker_event"
                                      " WHERE publication_id='pub1'").fetchone()[0], 0)

    def test_publish_business_date_prefers_run_nav_as_of(self):
        """P2-0：tracker 日期跟 run.nav_as_of，不跟最新 buyable 快照。"""
        from fundana.cli import tracker_business_date

        conn = _conn()
        conn.execute(
            "INSERT INTO screen_run(run_id, profile, config_hash, code_version,"
            " formula_contract, created_at, business_date, nav_as_of)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("r1", "bond_v1", "h", "0.1.0", "fundana-risk-2026.11.5",
             "2026-11-04T09:00:00", "2026-11-02", "2026-11-02"))
        self.assertEqual(tracker_business_date(conn, "r1"), "2026-11-02")

    def test_history_returns_events(self):
        conn = _conn()
        tracker.update_after_publish(conn, "pub1", "2026-11-01", "r1", [_pick("000001")])
        hist = tracker.history(conn, "000001")
        self.assertEqual(len(hist["events"]), 1)
        self.assertEqual(hist["events"][0]["change"], "entered")
