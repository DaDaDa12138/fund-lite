"""持有期受限榜、决策快照与家族去重的契约测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from fundana import db
from fundana.api import decision, locked, rankings


class StubCfg:
    def __init__(self, root: Path):
        self.app = {"db": {"busy_timeout_ms": 15000, "wal": False}}
        self.root = root
        self.db_path = root / "t.db"
        self.raw_dir = root / "raw"
        self.freeze_dir = root / "freeze"
        self.report_root = root / "reports"
        self.code_version = "0.1.0"

    def get(self, *keys, default=None):
        return default


BD = "2026-10-31"
RUN = "run-1"
CONTRACT = "fundana-risk-test"

THRESHOLDS = {
    "fixed_or_admission": {
        "composite": {"value": 80.5, "min": 58.0, "pass": True, "reason": None, "unit": "score"},
        "annual_1y": {"value": 1.2, "min": 2.0, "pass": False, "reason": "below_min", "unit": "pct"},
        "mdd": {"value": 0.009, "max": 0.020, "pass": True, "reason": None, "unit": "dec"},
    },
    "dynamic": {
        "_meta": {"threshold_source": "style", "style_n": 120, "rule": "composite_and_k_of_n",
                  "min_dimensions": 6, "dimensions_passed": 5, "gate_pass": False},
        "composite": {"value": 80.5, "min": 71.2, "pass": True, "reason": None, "unit": "score"},
        "annual_1y": {"value": 1.2, "min": 2.85, "pass": False, "reason": "below_min", "unit": "pct"},
    },
    "enhanced_strict": {
        "annual_1y": {"value": 1.2, "min": 4.5, "pass": False, "reason": "below_min", "unit": "pct"},
    },
}



ENH_THRESHOLDS = {
    "fixed_or_admission": {
        "annual_1y": {"value": 3.1, "min": 2.23, "pass": True, "reason": None, "unit": "pct",
                      "window": "1y_3y"},
        "sharpe": {"value": 0.5, "min": 0.2, "pass": True, "reason": None, "unit": "num"},
    },
    "dynamic": {"_meta": {"threshold_source": "enhanced_relative", "rule": "admission",
                          "gate_pass": True, "peers": 283, "fallback": False}},
    "enhanced_relative_meta": {"n": 283, "fallback": False, "annual_window": "1y_3y"},
}

def seed(conn):
    conn.executemany(
        "INSERT INTO fund(fcode, name, type_text, type_group, estab_date, company_name,"
        " identity_status, first_seen_date, last_seen_date)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        [("000001", "测试纯债A", "债券型-长债", "bond", "2016-01-01", "测试基金", "ok", BD, BD),
         ("000002", "测试纯债C", "债券型-长债", "bond", "2016-01-01", "测试基金", "ok", BD, BD),
         ("000003", "另一只纯债A", "债券型-长债", "bond", "2016-01-01", "别家基金", "ok", BD, BD),
         ("000004", "定开债A", "债券型-长债", "bond", "2016-01-01", "测试基金", "ok", BD, BD)])
    conn.executemany(
        "INSERT INTO screen_run_input(run_id, fcode, as_of, family_id, created_at)"
        " VALUES(?,?,?,?,?)",
        [(RUN, "000001", BD, "测试基金::测试纯债", "t"),
         (RUN, "000002", BD, "测试基金::测试纯债", "t"),
         (RUN, "000003", BD, "别家基金::另一只纯债", "t"),
         (RUN, "000004", BD, "测试基金::定开债", "t")])
    conn.executemany(
        "INSERT INTO fund_share_class(fcode, family_id, class_letter, is_primary) VALUES(?,?,?,?)",
        [("000001", "测试基金::测试纯债", "A", 1), ("000002", "测试基金::测试纯债", "C", 0),
         ("000003", "别家基金::另一只纯债", "A", 1), ("000004", "测试基金::定开债", "A", 1)])
    conn.executemany(
        "INSERT INTO fund_holding_policy(fcode, structured_days, name_inferred_days, effective_days,"
        " lockup_type, evidence_json, updated_at) VALUES(?,?,?,?,?,?,?)",
        [("000004", None, 540, 540, "periodic_open", "{}", "t"),
         ("000001", None, 180, 180, "holding", "{}", "t")])
    conn.executemany(
        "INSERT INTO fund_buyable_daily(fcode, business_date, purchase_status, redemption_status,"
        " buyable_state, next_open_date, min_purchase, fee_pct, raw_sha256)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        [("000004", BD, "开放申购", "开放赎回", "confirmed_yes", "2026-10-15", 10.0, 0.08, "s1"),
         ("000001", BD, "暂停申购", "开放赎回", "confirmed_no", None, 10.0, 0.08, "s2")])
    conn.execute("INSERT INTO fund_profile(fcode, asset_scale_billion, recurring_fee_pct)"
                 " VALUES('000004', 2.97, 0.43)")
    conn.execute("INSERT INTO fund_profile(fcode, asset_scale_billion, recurring_fee_pct)"
                 " VALUES('000001', 5.10, 0.40)")
    conn.execute("INSERT INTO fund_metric(fcode, period_days, formula_contract, annual_dec, sharpe,"
                 " mdd_dec, positive_ratio_pct, n_obs) VALUES('000004',365,?,0.035264,1.03,0.009098,53.3,242)",
                 (CONTRACT,))
    conn.execute("INSERT INTO fund_metric(fcode, period_days, formula_contract, annual_dec, sharpe,"
                 " mdd_dec, positive_ratio_pct, n_obs) VALUES('000001',365,?,0.041,1.4,0.0071,56.2,250)",
                 (CONTRACT,))
    conn.execute("INSERT INTO screen_run(run_id, profile, config_hash, code_version,"
                 " formula_contract, universe_count, base_pass_count, formal_count, locked_count,"
                 " status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (RUN, "bond_v1", "h", "0.1.0", CONTRACT, 4, 2, 2, 2, "done", "2026-10-31T21:00:00"))
    conn.executemany(
        "INSERT INTO screen_pick(run_id, board, rank, fcode, formal_score, confidence)"
        " VALUES(?,?,?,?,?,?)",
        [(RUN, "bond_stable", 1, "000001", 87.4, 10.4), (RUN, "bond_stable", 2, "000002", 87.1, 10.4),
         (RUN, "bond_stable", 3, "000003", 86.7, 9.4),
         (RUN, "bond_locked", 1, "000004", None, None), (RUN, "bond_locked", 2, "000001", None, None)])
    conn.executemany(
        "INSERT INTO screen_decision(run_id, fcode, layer_reached, passed, reason_codes_json,"
        " style_code, thresholds_json, scores_json, confidence) VALUES(?,?,?,?,?,?,?,?,?)",
        [(RUN, "000001", "L7", 0, json.dumps(["annual_1y:below_min", "dyn_min_dimensions:5<6"]),
          "bond_pure", json.dumps(THRESHOLDS), json.dumps({"composite": 80.5, "formal_score": 87.4}), 10.4),
         (RUN, "000004", "L3", 0, json.dumps(["locked_540d"]), "bond_pure", None, None, None),
         (RUN, "000003", "L9", 1, json.dumps([]), "bond_secondary_enhanced",
          json.dumps(ENH_THRESHOLDS), json.dumps({"composite": 88.0, "formal_score": 90.5}), 9.0)])
    conn.commit()


class TestDecisionView(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(StubCfg(Path(self.tmp.name)))
        db.migrate(self.conn)
        seed(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_groups_and_margins(self):
        v = decision.view(self.conn, "000001")
        keys = [g["key"] for g in v["groups"]]
        self.assertEqual(keys, ["fixed_or_admission", "dynamic", "enhanced_strict"])
        fixed = v["groups"][0]
        self.assertEqual((fixed["passed_count"], fixed["total_count"]), (2, 3))
        annual = [i for i in fixed["items"] if i["key"] == "annual_1y"][0]
        self.assertFalse(annual["passed"])
        self.assertAlmostEqual(annual["margin"], -0.8)     # 1.2 - 2.0
        self.assertEqual(annual["reason_label"], "1 年年化低于门槛")
        mdd = [i for i in fixed["items"] if i["key"] == "mdd"][0]
        self.assertEqual(mdd["op"], "max")
        self.assertAlmostEqual(mdd["margin"], 0.011)       # 0.020 - 0.009
        self.assertTrue(v["groups"][2]["archive"])
        dyn_meta = v["groups"][1]["meta"]
        self.assertEqual(dyn_meta["threshold_source_label"], "同风格分位")
        self.assertEqual(dyn_meta["dimensions_passed"], 5)
        self.assertEqual([r["code"] for r in v["reason_codes"]],
                         ["annual_1y:below_min", "dyn_min_dimensions:5<6"])
        self.assertEqual(v["board"], "bond_stable")
        self.assertEqual(v["layer_label"], "动态分位门槛")

    def test_no_thresholds_keeps_note(self):
        v = decision.view(self.conn, "000004")
        self.assertEqual(v["groups"], [])
        self.assertFalse(v["has_thresholds"])
        self.assertIn("持有期限制", v["note"])
        self.assertEqual(v["reason_codes"][0]["label"], "最短持有期 540 天（超过 90 天，进持有期受限榜）")

    def test_enhanced_group_is_relabelled(self):
        v = decision.view(self.conn, "000003")
        fixed = v["groups"][0]
        self.assertEqual(fixed["title"], "增强债准入（同风格分位 + 绝对地板）")
        self.assertEqual(fixed["meta"]["threshold_source_label"], "增强债同风格相对口径")
        self.assertEqual(fixed["meta"]["peers"], 283)
        annual = [i for i in fixed["items"] if i["key"] == "annual_1y"][0]
        self.assertEqual(annual["window_label"], "1 年 + 3 年各半")
        self.assertEqual([g["key"] for g in v["groups"]], ["fixed_or_admission"])
        self.assertTrue(v["passed"])

    def test_unknown_fund_returns_none(self):
        self.assertIsNone(decision.view(self.conn, "999999"))


class TestLocked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(StubCfg(Path(self.tmp.name)))
        db.migrate(self.conn)
        seed(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_rows_and_summary(self):
        out = locked.rows(self.conn, CONTRACT, limit=10)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["as_of"], BD)
        by_lockup = {b["code"]: b["count"] for b in out["summary"]["by_lockup"]}
        self.assertEqual(by_lockup, {"periodic_open": 1, "holding": 1})
        buckets = {b["code"]: b["count"] for b in out["summary"]["by_bucket"]}
        self.assertEqual(buckets["91-180"], 1)       # 180 天属于 91–180 档
        self.assertEqual(buckets["366-730"], 1)      # 540 天

    def test_periodic_open_past_date_is_not_a_countdown(self):
        row = [r for r in locked.rows(self.conn, CONTRACT, limit=10)["rows"]
               if r["fcode"] == "000004"][0]
        self.assertEqual(row["open_state"], "observed_past")
        self.assertEqual(row["days_until_open"], -16)
        self.assertIn("开放期已于 2026-10-15 开始", row["hint"])
        self.assertNotIn("距下次开放日", row["hint"])

    def test_holding_hint_uses_effective_days(self):
        row = [r for r in locked.rows(self.conn, CONTRACT, limit=10)["rows"]
               if r["fcode"] == "000001"][0]
        self.assertEqual(row["hint"], "买入后 180 天可赎回")
        self.assertAlmostEqual(row["annual_1y_pct"], 4.1)

    def test_filters(self):
        self.assertEqual(locked.rows(self.conn, CONTRACT, lockup="holding")["total"], 1)
        self.assertEqual(locked.rows(self.conn, CONTRACT, bucket="366-730")["total"], 1)
        self.assertEqual(locked.rows(self.conn, CONTRACT, lockup="periodic_open",
                                     bucket="181-365")["total"], 0)


class TestFamilyDedupe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(StubCfg(Path(self.tmp.name)))
        db.migrate(self.conn)
        seed(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _rows(self, mode):
        clause = rankings.dedupe_clause(mode)
        sql = ("SELECT p.rank, p.fcode FROM screen_pick p"
               " LEFT JOIN fund_share_class s ON s.fcode=p.fcode"
               " LEFT JOIN screen_run_input i ON i.run_id=p.run_id AND i.fcode=p.fcode"
               " WHERE p.run_id=? AND p.board='bond_stable'")
        args = [RUN]
        if clause:
            sql += " AND " + clause
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY p.rank", args)]

    def test_family_keeps_best_rank_only(self):
        self.assertEqual([r["fcode"] for r in self._rows(None)], ["000001", "000002", "000003"])
        self.assertEqual([r["fcode"] for r in self._rows("family")], ["000001", "000003"])

    def test_unknown_mode_is_ignored(self):
        self.assertIsNone(rankings.dedupe_clause("bogus"))
        self.assertIsNone(rankings.dedupe_clause(""))
        self.assertIn("MIN(p2.rank)", rankings.dedupe_clause("family"))

    def test_family_member_count(self):
        counts = rankings.family_member_count(self.conn, RUN, "bond_stable")
        self.assertEqual(counts["测试基金::测试纯债"], 2)
        self.assertEqual(counts["别家基金::另一只纯债"], 1)
