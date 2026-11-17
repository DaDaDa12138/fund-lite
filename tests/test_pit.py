"""P0-1：PIT 快照、重放与生命周期测试。"""
import json
import sqlite3
import unittest
from pathlib import Path

from fundana.screen import pit
from fundana.screen.engine import _load_candidates, base_gate, score_candidate
from fundana.lifecycle import import_csv, status_as_of, universe_as_of

MIGRATIONS = [Path(__file__).resolve().parents[1] / "fundana" / "migrations" / name
              for name in ("0026_pit_lifecycle.sql", "0028_pit_metric_flags.sql",
                           "0039_pit_redemption.sql")]

SCHEMA = """
CREATE TABLE fund(fcode TEXT PRIMARY KEY, name TEXT, type_text TEXT, type_group TEXT,
                  estab_date TEXT, identity_status TEXT, company_name TEXT);
CREATE TABLE fund_share_class(fcode TEXT, family_id TEXT);
CREATE TABLE fund_profile(fcode TEXT, asset_scale_billion REAL, recurring_fee_pct REAL,
                          mgr_fee_pct REAL, trust_fee_pct REAL, sales_fee_pct REAL,
                          max_sub_fee_pct REAL, max_red_fee_pct REAL, manager_count INTEGER,
                          manager_career_min_days INTEGER, manager_career_max_days INTEGER,
                          manager_power_score REAL, field_src_json TEXT);
CREATE TABLE fund_fee_meta(fcode TEXT, has_discount INTEGER, redemption_1y_pct REAL);
CREATE TABLE fund_holding_policy(fcode TEXT, effective_days INTEGER, lockup_type TEXT);
CREATE TABLE fund_style(fcode TEXT, style_code TEXT, confidence TEXT);
CREATE TABLE fund_coverage(fcode TEXT, run_id TEXT, nav_points INTEGER, nav_first_date TEXT,
                           nav_last_date TEXT, freshness_days INTEGER, gap_max_days INTEGER);
CREATE TABLE fund_buyable_daily(fcode TEXT, business_date TEXT, buyable_state TEXT,
                                purchase_status TEXT);
CREATE TABLE fund_metric(fcode TEXT, period_days INTEGER, formula_contract TEXT);
CREATE TABLE fund_scale_history(fcode TEXT, report_period TEXT, asset_scale_billion REAL,
                                share_scale_billion REAL);
CREATE TABLE fund_manager_tenure(fcode TEXT, mgr_id TEXT, start_date TEXT, end_date TEXT,
                                 days INTEGER, is_current INTEGER);
"""


def _cand(code="000001"):
    return {
        "fcode": code, "identity_status": "ok", "estab_date": "2015-01-01",
        "company_name": "测试公司", "family_id": "F1", "asset_scale_billion": 5.0,
        "share_scale_billion": 2.0, "recurring_fee_pct": 0.30, "max_sub_fee_pct": 0.10,
        "max_red_fee_pct": 0.0, "has_discount": 0, "manager_tenure_min_days": 1000,
        "effective_days": None, "lockup_type": "none", "buyable_state": "confirmed_yes",
        "purchase_status": "开放申购", "nav_points": 1000, "freshness_days": 1,
        "metrics": {365: {"annual_dec": 0.03, "total_return_pct": 0.03, "sharpe": 1.2,
                          "sortino": 1.5, "calmar_dec": 3.0, "mdd_dec": 0.01,
                          "vol_dec": 0.02, "positive_ratio_pct": 60.0,
                          "p75_gap_days": 1.0}},
    }


class TestPitSnapshot(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for migration in MIGRATIONS:
            self.conn.executescript(migration.read_text(encoding="utf-8"))

    def tearDown(self):
        self.conn.close()

    def test_record_and_replay_are_exact(self):
        cand = _cand()
        pcts = {"annual_1y": 0.8, "sharpe": 0.7, "sortino": 0.7, "calmar": 0.7,
                "mdd": 0.7, "vol": 0.7, "positive": 0.7, "fee": 0.7,
                "return_6m": 0.6, "return_1m": 0.6, "return_3m": 0.6}
        scores = score_candidate(cand, pcts)
        d = {"layer_reached": "L10", "passed": 1, "reasons": [], "style_code": "bond_pure",
             "scores": scores}
        n = pit.record_inputs(self.conn, "r1", "2026-10-30", [cand], {"000001": d},
                              pcts={"000001": pcts})
        self.assertEqual(n, 1)
        out = pit.replay(self.conn, "r1")
        self.assertEqual(out["checked"], 1)
        self.assertEqual(out["gate_mismatches"], 0)
        self.assertEqual(out["score_mismatches"], 0)
        row = self.conn.execute("SELECT percentile_pool, pcts_json FROM screen_run_input").fetchone()
        self.assertEqual(row["percentile_pool"], "investable")
        self.assertEqual(json.loads(row["pcts_json"])["annual_1y"], 0.8)

    def test_replay_ignores_post_gate_risk_reason(self):
        cand = _cand()
        cand["buyable_state"] = "confirmed_no"
        pcts = {"annual_1y": 0.8}
        scores = score_candidate(cand, pcts)
        d = {"layer_reached": "L2", "passed": 0,
             "reasons": ["buyable_confirmed_no", "risk:holdings_required"],
             "style_code": "bond_pure", "scores": scores}
        pit.record_inputs(self.conn, "r1", "2026-10-30", [cand], {"000001": d},
                          pcts={"000001": pcts})
        out = pit.replay(self.conn, "r1")
        self.assertEqual(out["gate_mismatches"], 0)
        self.assertEqual(out["post_gate_reasons_ignored"], 1)
        self.assertIn("不重跑 L6-L10", out["scope"])

    def test_replay_restores_metric_end_date(self):
        cand = _cand()
        cand["metrics"][365]["end_date"] = "2026-10-20"
        scores = score_candidate(cand, {"annual_1y": 0.8})
        d = {"layer_reached": "L1", "passed": 0,
             "reasons": ["metric_stale_gt_7d"], "style_code": "bond_pure",
             "scores": scores}
        pit.record_inputs(self.conn, "r1", "2026-11-02", [cand], {"000001": d},
                          pcts={"000001": {"annual_1y": 0.8}})
        out = pit.replay(self.conn, "r1")
        self.assertEqual(out["gate_mismatches"], 0)
        self.assertEqual(out["details"], [])

    def test_replay_detects_tampering(self):
        cand = _cand()
        pcts = {"annual_1y": 0.8}
        scores = score_candidate(cand, pcts)
        d = {"layer_reached": "L10", "passed": 1, "reasons": [], "style_code": "bond_pure",
             "scores": scores}
        pit.record_inputs(self.conn, "r1", "2026-10-30", [cand], {"000001": d},
                          pcts={"000001": pcts})
        self.conn.execute("UPDATE screen_run_input SET share_scale_billion=0.01 WHERE fcode='000001'")
        out = pit.replay(self.conn, "r1")
        self.assertEqual(out["gate_mismatches"], 1)


class TestLifecycleAndScale(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for migration in MIGRATIONS:
            self.conn.executescript(migration.read_text(encoding="utf-8"))
        for code, fam in (("000001", "F1"), ("000002", "F1")):
            self.conn.execute("INSERT INTO fund VALUES(?,?,?,?,?,?,?)",
                              (code, code, "债券型", "bond", "2015-01-01", "ok", "测试公司"))
            self.conn.execute("INSERT INTO fund_share_class VALUES(?,?)", (code, fam))
            self.conn.execute("INSERT INTO fund_profile VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (code, 1.0, 0.30, 0.0, 0.0, 0.0, 0.10, 0.0, 1, 500, 900, 7.5,
                               '{"share_class_scale_billion": 0.5}'))
            self.conn.execute("INSERT INTO fund_fee_meta VALUES(?,1,NULL)", (code,))
            self.conn.execute("INSERT INTO fund_holding_policy VALUES(?,?,?)", (code, None, "none"))
            self.conn.execute("INSERT INTO fund_style VALUES(?,?,?)", (code, "bond_pure", "x"))
            self.conn.execute("INSERT INTO fund_coverage VALUES(?,?,?,?,?,?,?)",
                              (code, "r1", 2500, "2015-01-05", "2026-10-29", 2, 5))
            self.conn.execute("INSERT INTO fund_buyable_daily VALUES(?,?,?,?)",
                              (code, "2026-10-30", "confirmed_yes", "开放申购"))
        self.conn.execute("INSERT INTO fund_scale_history VALUES('000001','2025-09-30',1.0,0.4)")
        self.conn.execute("INSERT INTO fund_scale_history VALUES('000002','2025-09-30',1.0,0.4)")
        self.conn.execute("INSERT INTO fund_scale_history VALUES('000001','2025-12-31',1.0,0.4)")
        self.conn.execute("INSERT INTO fund_scale_history VALUES('000002','2025-12-31',1.0,0.4)")
        self.conn.execute("INSERT INTO fund_scale_history VALUES('000001','2026-06-30',3.0,1.0)")
        self.conn.execute("INSERT INTO fund_scale_history VALUES('000002','2026-06-30',4.0,2.0)")
        self.conn.execute("INSERT INTO fund_manager_tenure VALUES('000001','m1','2026-10-01',NULL,29,1)")
        self.conn.execute("INSERT INTO fund_manager_tenure VALUES('000002','m2','2020-01-01',NULL,2500,1)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_scale_and_tenure_are_asof(self):
        cands = _load_candidates(self.conn, "2026-10-30", "c", as_of="2026-10-30")
        by = {c["fcode"]: c for c in cands}
        self.assertAlmostEqual(by["000001"]["asset_scale_billion"], 7.0)
        self.assertAlmostEqual(by["000001"]["share_scale_billion"], 1.0)
        self.assertEqual(by["000001"]["scale_source"], "scale_history_asof")
        self.assertEqual(by["000001"]["scale_asof"], "2026-06-30")
        self.assertEqual(by["000001"]["share_scale_source"], "scale_history_asof")
        self.assertEqual(by["000001"]["share_scale_asof"], "2026-06-30")
        self.assertEqual(by["000001"]["scale_lag_days"], 45)
        self.assertLess(by["000001"]["manager_tenure_min_days"], 60)
        self.assertEqual(by["000001"]["tenure_source"], "tenure_asof")
        # P0-5：as-of 2026-01-31 的 cutoff=2025-12-17，只能看到 2025-09-30 的
        # 2.0 亿 family 规模；2025-12-31 季报尚未满 45 天，不能提前使用。
        old = _load_candidates(self.conn, "2026-01-31", "c", as_of="2026-01-31")
        self.assertAlmostEqual({c["fcode"]: c for c in old}["000001"]["asset_scale_billion"], 2.0)
        self.assertEqual({c["fcode"]: c for c in old}["000001"]["scale_asof"], "2025-09-30")

    def test_lifecycle_excludes_liquidated_after_effective_date(self):
        csv_path = Path("/tmp/lifecycle_test.csv")
        csv_path.write_text("fcode,status,effective_date,reason\n"
                            "000002,liquidated,2026-10-01,合同终止\n", encoding="utf-8")
        out = import_csv(self.conn, csv_path)
        self.assertEqual(out["imported"], 1)
        self.assertEqual(status_as_of(self.conn, "000002", "2026-10-30"), "liquidated")
        self.assertIsNone(status_as_of(self.conn, "000002", "2026-09-30"))
        codes = [c["fcode"] for c in _load_candidates(self.conn, "2026-10-30", "c", as_of="2026-10-30")]
        self.assertNotIn("000002", codes)
        codes = [c["fcode"] for c in _load_candidates(self.conn, "2026-09-01", "c", as_of="2026-09-01")]
        self.assertIn("000002", codes)
        self.assertNotIn("000002", universe_as_of(self.conn, "2026-10-30"))
        self.assertIn("000002", universe_as_of(self.conn, "2026-09-30"))

    def test_lifecycle_universe_excludes_non_bond_liquidated(self):
        # EID 公告没有基金类型；混合型清盘样本不能被当成债券基金纳入回测宇宙。
        self.conn.execute(
            "INSERT INTO fund_lifecycle(fcode,status,effective_date,last_nav_date,reason,source,"
            " raw_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("999999", "liquidated", "2026-10-01", "2026-09-30",
             "测试灵活配置混合型证券投资基金清算报告", "csrc_eid:FB060010:test", "{}",
             "2026-11-05T00:00:00+08:00"))
        self.conn.commit()
        self.assertNotIn("999999", universe_as_of(self.conn, "2026-10-30"))


if __name__ == "__main__":
    unittest.main()
