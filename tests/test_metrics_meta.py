"""指标字典、决策轮次对比与受限榜排序的契约测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from fundana import db
from fundana.api import decision, locked, metrics_meta


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
CONTRACT = "fundana-risk-test"
RUN_OLD = "20261031T180000-screen-old001"
RUN_NEW = "20261031T230000-screen-new001"


def _thresholds(composite_min: float, annual_min: float, passed: bool) -> dict:
    return {
        "fixed_or_admission": {
            "composite": {"value": 80.5, "min": 58.0, "pass": True, "reason": None, "unit": "score"},
            "annual_1y": {"value": 3.1, "min": 2.0, "pass": True, "reason": None, "unit": "pct"},
        },
        "dynamic": {
            "_meta": {"threshold_source": "style", "style_n": 120, "rule": "composite_and_k_of_n",
                      "min_dimensions": 6, "dimensions_passed": 9, "gate_pass": passed},
            "composite": {"value": 80.5, "min": composite_min, "pass": True, "reason": None,
                          "unit": "score"},
            "annual_1y": {"value": 3.1, "min": annual_min, "pass": True, "reason": None,
                          "unit": "pct"},
        },
    }


def seed(conn) -> None:
    conn.executemany(
        "INSERT INTO fund(fcode, name, type_text, type_group, estab_date, company_name,"
        " identity_status, first_seen_date, last_seen_date) VALUES(?,?,?,?,?,?,?,?,?)",
        [("000001", "测试纯债A", "债券型-长债", "bond", "2016-01-01", "测试基金", "ok", BD, BD),
         ("000002", "测试纯债B", "债券型-长债", "bond", "2016-01-01", "测试基金", "ok", BD, BD),
         ("000003", "定开债A", "债券型-长债", "bond", "2016-01-01", "测试基金", "ok", BD, BD)])
    conn.executemany(
        "INSERT INTO fund_share_class(fcode, family_id, class_letter, is_primary) VALUES(?,?,?,?)",
        [("000001", "测试基金::测试纯债", "A", 1), ("000002", "测试基金::测试纯债", "B", 0),
         ("000003", "测试基金::定开债", "A", 1)])
    conn.executemany(
        "INSERT INTO fund_holding_policy(fcode, structured_days, name_inferred_days, effective_days,"
        " lockup_type, evidence_json, updated_at) VALUES(?,?,?,?,?,?,?)",
        [("000003", None, 180, 180, "holding", "{}", "t"),
         ("000001", None, None, None, "none", "{}", "t")])
    conn.execute(
        "INSERT INTO fund_buyable_daily(fcode, business_date, purchase_status, redemption_status,"
        " buyable_state, next_open_date, min_purchase, fee_pct, raw_sha256)"
        " VALUES('000003',?,?,?,?,?,?,?,?)",
        (BD, "开放申购", "开放赎回", "confirmed_yes", "2026-11-20", 10.0, 0.08, "s1"))
    conn.execute("INSERT INTO fund_profile(fcode, asset_scale_billion, recurring_fee_pct)"
                 " VALUES('000003', 2.97, 0.43)")
    for fcode, days, total in (("000003", 90, 3.02), ("000003", 7, 0.30)):
        conn.execute(
            "INSERT INTO fund_metric(fcode, period_days, formula_contract, total_return_pct,"
            " annual_dec, sharpe, mdd_dec, positive_ratio_pct, n_obs)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (fcode, days, CONTRACT, total, total / 100 * 365.0 / days, 0.5, 0.004, 52.0, 60))
    conn.executemany(
        "INSERT INTO screen_run(run_id, profile, config_hash, code_version, formula_contract,"
        " universe_count, base_pass_count, formal_count, locked_count, status, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(RUN_OLD, "bond_v1", "h", "0.1.0", CONTRACT, 3, 2, 1, 1, "done", "2026-10-31T18:00:00"),
         (RUN_NEW, "bond_v1", "h", "0.1.0", CONTRACT, 3, 2, 1, 1, "done", "2026-10-31T23:00:00")])
    conn.executemany(
        "INSERT INTO screen_pick(run_id, board, rank, fcode, formal_score, confidence)"
        " VALUES(?,?,?,?,?,?)",
        [(RUN_OLD, "bond_stable", 1, "000001", 87.0, 10.0),
         (RUN_NEW, "bond_stable", 1, "000001", 87.4, 10.0),
         (RUN_NEW, "bond_locked", 1, "000003", None, None)])
    conn.executemany(
        "INSERT INTO screen_decision(run_id, fcode, layer_reached, passed, reason_codes_json,"
        " style_code, thresholds_json, scores_json, confidence) VALUES(?,?,?,?,?,?,?,?,?)",
        [(RUN_OLD, "000001", "L7", 1, json.dumps([]), "bond_pure",
          json.dumps(_thresholds(71.0738, 2.8685, True)),
          json.dumps({"composite": 80.3, "formal_score": 87.0}), 10.0),
         (RUN_NEW, "000001", "L7", 1, json.dumps([]), "bond_pure",
          json.dumps(_thresholds(71.1789, 3.0221, True)),
          json.dumps({"composite": 80.5, "formal_score": 87.4}), 10.0)])
    conn.commit()


class TestMetricsMeta(unittest.TestCase):
    def test_payload_shape(self):
        data = metrics_meta.payload(nav_age_limit_days=10)
        # 规范键（28 条基础 + 5 条尾部/回撤扩展）+ 别名（mdd -> mdd_1y、tail -> tail_completeness）
        self.assertEqual(len(data["metrics"]), len(metrics_meta.METRICS) + len(metrics_meta.ALIASES))
        self.assertGreaterEqual(len(metrics_meta.METRICS), 33)
        keys = [m["key"] for m in data["metrics"]]
        self.assertEqual(len(keys), len(set(keys)))
        aliases = {m["key"]: m for m in data["metrics"] if m.get("alias_of")}
        self.assertEqual(set(aliases), set(metrics_meta.ALIASES))
        for key, item in aliases.items():
            canonical = {m["key"]: m for m in data["metrics"]}[item["alias_of"]]
            self.assertEqual(item["desc"], canonical["desc"], key)
        for m in data["metrics"]:
            self.assertTrue(m["label"] and m["desc"])
            self.assertIn(m["direction"], ("higher", "lower", "none"))
            self.assertTrue(m["direction_label"])
        dirs = {d["code"]: d["label"] for d in data["directions"]}
        self.assertEqual(dirs["higher"], "越大越好")
        self.assertEqual(dirs["lower"], "越小越好")

    def test_every_layer_has_detail(self):
        details = metrics_meta.layer_details(nav_age_limit_days=10)
        self.assertEqual(len(details), 13)
        by_code = {d["code"]: d["detail"] for d in details}
        for code in ("L1", "L2", "L3", "L5", "L5.5", "L6C", "L6E", "L7", "L8", "L8R", "L9", "L10"):
            self.assertIn(code, by_code)
            self.assertTrue(by_code[code].strip())
        # 门槛数值必须出现在说明里，否则"每层写清门槛"就没做到
        self.assertIn("90", by_code["L3"])
        self.assertIn("1.3", by_code["L5.5"])          # 持有 1 年总成本上限 1.30%（ADR-059）
        self.assertIn("62", by_code["L6C"])
        self.assertIn("365", by_code["L8"])

    def test_metric_meta_for_table_headers(self):
        keys = {m["key"] for m in metrics_meta.METRICS}
        for key in ("annual_1y", "sharpe", "sortino", "calmar", "mdd_1y", "vol", "positive",
                    "ret_1w", "mdd_3y", "worst90", "scale", "fee", "formal_score"):
            self.assertIn(key, keys)


class TestDecisionHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(StubCfg(Path(self.tmp.name)))
        db.migrate(self.conn)
        seed(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_history_is_newest_first(self):
        view = decision.view(self.conn, "000001")
        self.assertEqual(view["run_id"], RUN_NEW)
        self.assertEqual([h["run_id"] for h in view["history"]], [RUN_NEW, RUN_OLD])
        self.assertEqual(view["history"][0]["layer_label"], "动态分位门槛")

    def test_previous_picks_run_with_real_changes(self):
        view = decision.view(self.conn, "000001")
        prev = view["previous"]
        self.assertEqual(prev["run_id"], RUN_OLD)
        keys = {(c["group"], c["key"]) for c in prev["changes"]}
        self.assertIn(("dynamic", "composite"), keys)
        self.assertIn(("dynamic", "annual_1y"), keys)
        self.assertNotIn(("fixed_or_admission", "composite"), keys)  # 固定线没变，不该出现
        self.assertAlmostEqual(prev["score_delta"], 0.4, places=4)
        comp = [c for c in prev["changes"] if c["key"] == "composite"][0]
        self.assertAlmostEqual(comp["threshold_delta"], 0.1051, places=4)
        self.assertEqual(comp["unit"], "score")

    def test_previous_explicit_run(self):
        view = decision.view(self.conn, "000001", prev_run=RUN_OLD)
        self.assertEqual(view["previous"]["run_id"], RUN_OLD)

    def test_previous_compares_to_other_run_even_if_newer(self):
        # 查看旧轮次时，"对比轮次"就是更新的那一轮：对比是双向的，不是只往前看
        view = decision.view(self.conn, "000001", run_id=RUN_OLD)
        self.assertEqual(view["previous"]["run_id"], RUN_NEW)
        self.assertAlmostEqual(view["previous"]["score_delta"], -0.4, places=4)


class TestLockedSorting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(StubCfg(Path(self.tmp.name)))
        db.migrate(self.conn)
        seed(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_period_columns_present(self):
        out = locked.rows(self.conn, CONTRACT, limit=10)
        row = out["rows"][0]
        for key in ("ret_1w_pct", "ret_1m_pct", "ret_3m_pct", "ret_6m_pct",
                    "annual_1w_dec", "annual_1m_dec", "annual_3m_dec", "annual_6m_dec"):
            self.assertIn(key, row)
        self.assertAlmostEqual(row["ret_3m_pct"], 3.02, places=4)
        self.assertAlmostEqual(row["ret_1w_pct"], 0.30, places=4)

    def test_sort_by_period_return(self):
        out = locked.rows(self.conn, CONTRACT, sort="ret_3m", order="desc", limit=10)
        self.assertEqual(out["total"], 1)

    def test_unknown_sort_falls_back_to_rank(self):
        out = locked.rows(self.conn, CONTRACT, sort="drop_table", order="desc", limit=10)
        self.assertEqual(out["rows"][0]["rank"], 1)

    def test_open_state_and_hint(self):
        out = locked.rows(self.conn, CONTRACT, limit=10)
        row = out["rows"][0]
        self.assertEqual(row["open_state"], "upcoming")
        self.assertGreater(row["days_until_open"], 0)
        # holding 型是"买入后 N 天可赎回"，只有 periodic_open 才谈"下次开放日"
        self.assertIn("买入后 180 天可赎回", row["hint"])


if __name__ == "__main__":
    unittest.main()
