"""筛选引擎门禁与打分契约测试。"""
import sqlite3
import unittest

from fundana.fuse.profile import infer_holding_days, map_buyable
from fundana.fuse.identity import split_class
from fundana.fuse.style import classify, classify_bond_name
from fundana.screen import scoring, thresholds as T
from fundana.screen.engine import (apply_dynamic, base_gate, coverage_meta, dynamic_gate,
                                   _core10_local_search, _core10_risk_budget,
                                   dynamic_reasons, dynamic_thresholds,
                                   enhanced_checks, enhanced_peer_stats,
                                   enhanced_relative_checks, enhanced_strict_overlay,
                                   fixed_checks, freeze_thresholds, load_frozen, quality_of,
                                   scale_pit_check, score_candidate, _rank, _resolve_baseline,
                                   _load_candidates, _select_concentrated)


def make_cand(**kw):
    c = {
        "fcode": "000001", "name": "测试债A", "type_text": "债券型-长债",
        "type_group": "bond", "estab_date": "2015-01-01", "identity_status": "ok",
        "asset_scale_billion": 5.0,
        "share_scale_billion": 5.0, "manager_tenure_min_days": 1000,
        "recurring_fee_pct": 0.35,
        "mgr_fee_pct": 0.30, "trust_fee_pct": 0.05, "sales_fee_pct": 0.0,
        "max_sub_fee_pct": None, "max_red_fee_pct": None,
        "effective_days": None, "lockup_type": "none",
        "style_code": "bond_pure", "style_confidence": "text_heuristic",
        "buyable_state": "confirmed_yes", "purchase_status": "开放申购",
        "nav_points": 2500, "nav_first_date": "2015-01-05", "nav_last_date": "2026-10-29",
        "freshness_days": 2, "gap_max_days": 5, "metrics": {},
    }
    c.update(kw)
    c["metrics"] = {365: {"annual_dec": 0.03, "total_return_pct": 3.0, "sharpe": 1.2,
                          "sortino": 1.5, "calmar_dec": 3.0, "mdd_dec": 0.010,
                          "vol_dec": 0.020, "positive_ratio_pct": 60.0,
                          "flat_ratio_pct": 10.0, "non_negative_ratio_pct": 70.0}}
    c["metrics"].update(kw.get("metrics", {}))
    return c


class TestBaseGate(unittest.TestCase):
    def test_pass(self):
        layer, reasons = base_gate(make_cand(), "2026-10-31", 10)
        self.assertEqual(reasons, [])
        self.assertEqual(layer, "L5.5")

    def test_identity_quarantine(self):
        c = make_cand(identity_status="quarantine")
        self.assertEqual(base_gate(c, "2026-10-31", 10), ("L1", ["identity_quarantined"]))

    def test_stale_nav(self):
        c = make_cand(freshness_days=30)
        layer, reasons = base_gate(c, "2026-10-31", 10)
        self.assertEqual(layer, "L1")
        self.assertIn("nav_stale", reasons)

    def test_metric_end_within_tolerance_passes(self):
        # P0-4：周更/定开 3 天滞后可接受，但必须记录 stale 天数。
        c = make_cand(metric_end_date="2026-10-28")
        layer, reasons = base_gate(c, "2026-10-31", 10)
        self.assertEqual((layer, reasons), ("L5.5", []))
        self.assertEqual(c["metric_stale_days"], 3)

    def test_metric_end_beyond_tolerance_blocks(self):
        c = make_cand(metric_end_date="2026-10-23")
        layer, reasons = base_gate(c, "2026-10-31", 10)
        self.assertEqual(layer, "L1")
        self.assertEqual(reasons, ["metric_stale_gt_7d"])
        self.assertEqual(c["metric_stale_days"], 8)

    def test_metric_lookahead_blocks(self):
        c = make_cand(metric_end_date="2026-11-02")
        layer, reasons = base_gate(c, "2026-10-31", 10)
        self.assertEqual(layer, "L1")
        self.assertEqual(reasons, ["metric_lookahead"])
        self.assertEqual(c["metric_stale_days"], -2)

    def test_buyable_unknown_blocks(self):
        c = make_cand(buyable_state="unknown")
        layer, reasons = base_gate(c, "2026-10-31", 10)
        self.assertEqual(layer, "L2")
        self.assertEqual(reasons, ["buyable_unknown"])

    def test_exchange_traded_is_not_unknown(self):
        c = make_cand(buyable_state="unknown", purchase_status="场内交易")
        self.assertEqual(base_gate(c, "2026-10-31", 10), ("L2", ["exchange_traded"]))
        c2 = make_cand(buyable_state="confirmed_no", purchase_status="场内交易")
        self.assertEqual(base_gate(c2, "2026-10-31", 10), ("L2", ["exchange_traded"]))

    def test_long_lock_excluded_at_L3(self):
        c = make_cand(effective_days=365)
        layer, reasons = base_gate(c, "2026-10-31", 10)
        self.assertEqual(layer, "L3")
        self.assertEqual(reasons, ["locked_365d"])

    def test_scale_tier_for_2y_fund(self):
        c = make_cand(estab_date="2025-01-01", asset_scale_billion=2.2)   # 1.8 年 → 需 2.0
        self.assertEqual(base_gate(c, "2026-10-31", 10)[1], [])
        c2 = make_cand(estab_date="2021-06-01", asset_scale_billion=2.2)  # 4 年 → 需 3.0
        layer, reasons = base_gate(c2, "2026-10-31", 10)
        self.assertEqual(layer, "L5")
        self.assertEqual(reasons, ["scale_below_3.00"])

    def test_fee_cap(self):
        # v3：L5.5 比的是"持有 1 年总成本"，粗筛上限 1.30%（ADR-059）
        c = make_cand(recurring_fee_pct=1.45)
        layer, reasons = base_gate(c, "2026-10-31", 10)
        self.assertEqual(layer, "L5.5")
        self.assertEqual(reasons, ["holding_cost_above_1.30"])

    def test_fee_cap_uses_purchase_fee(self):
        # 经常性费率只有 0.30%，但 1.20% 申购费不打折、持有 1 年 → 总成本 1.50%
        c = make_cand(recurring_fee_pct=0.30, max_sub_fee_pct=1.20)
        self.assertEqual(base_gate(c, "2026-10-31", 10)[1], ["holding_cost_above_1.30"])
        # 同一只基金在第三方 1 折平台：0.30 + 0.12 = 0.42%，可以过
        c2 = make_cand(recurring_fee_pct=0.30, max_sub_fee_pct=1.20, has_discount=1)
        self.assertEqual(base_gate(c2, "2026-10-31", 10), ("L5.5", []))


class TestScoring(unittest.TestCase):
    def test_percentile_ties_average_rank(self):
        p = scoring.percentile_ranks({"a": 1.0, "b": 1.0, "c": 2.0, "d": 3.0})
        self.assertAlmostEqual(p["a"], 100 * (1.5 - 0.5) / 4)   # 并列平均名次 1.5
        self.assertAlmostEqual(p["d"], 100 * (3.5) / 4)

    def test_negative_direction(self):
        p = scoring.percentile_ranks({"a": 0.01, "b": 0.02}, higher_is_better=False)
        self.assertGreater(p["a"], p["b"])

    def test_missing_not_renormalized(self):
        got = scoring.weighted({"a": 100.0}, {"a": 0.5, "b": 0.5})
        self.assertEqual(got, 50.0)

    def test_partial_5y_mdd_not_counted_as_5y(self):
        cand = make_cand(metrics={
            1095: {"years": 3.0},
            1825: {"years": 3.1},
        })
        keys = set(T.SHORT_CORE_WEIGHTS) | set(T.SHORT_FORMAL_WEIGHTS) |             set(T.LONG_CORE_WEIGHTS) | set(T.LONG_FORMAL_WEIGHTS)
        pcts = {k: 100.0 for k in keys}
        got = score_candidate(cand, pcts)
        self.assertFalse(got["coverage"]["is_full_5y"])
        self.assertAlmostEqual(got["coverage"]["long_core_weight"], 0.88)

    def test_5y_span_without_annual_is_not_5y_evidence(self):
        # 5 年窗口跨了 3.98 年，但点数不足导致 annual_dec 为 NULL；
        # coverage_ratio_5y 仍可能 >0.60，不能当作 5 年收益证据。
        cand = make_cand(metrics={
            1095: {"years": 3.0, "annual_dec": 0.03},
            1825: {"years": 3.98, "annual_dec": None},
        })
        cov = coverage_meta(cand)
        # 分母按 1825/365.25=4.9966 年，而不是整数 5：3.98/4.9966=0.797。
        self.assertAlmostEqual(cov["coverage_ratio_5y"], 0.797)
        self.assertFalse(cov["has_5y_annual"])

    def test_grade_bands(self):
        self.assertEqual(scoring.grade(75, T.GRADE_BANDS), "A")
        self.assertEqual(scoring.grade(74.9, T.GRADE_BANDS), "B+")
        self.assertEqual(scoring.grade(51, T.GRADE_BANDS), "D")


class TestSemantics(unittest.TestCase):
    def test_map_buyable(self):
        self.assertEqual(map_buyable("开放申购"), "confirmed_yes")
        self.assertEqual(map_buyable("限大额"), "confirmed_yes")
        self.assertEqual(map_buyable("暂停申购"), "confirmed_no")
        self.assertEqual(map_buyable(""), "unknown")

    def test_holding_days(self):
        self.assertEqual(infer_holding_days("某某债A", "XX债券型证券投资基金（每份份额最短持有180天）"),
                         (180, "holding"))
        self.assertEqual(infer_holding_days("某某一年定开债"), (365, "periodic_open"))
        self.assertEqual(infer_holding_days("某某纯债A"), (None, None))
        # 名称前缀式（东财主流写法）：180天持有 / 6个月持有期 / 一年持有
        self.assertEqual(infer_holding_days("易方达兴利180天持有债券A"), (180, "holding"))
        self.assertEqual(infer_holding_days("浦银6个月持有期债券A"), (180, "holding"))
        self.assertEqual(infer_holding_days("国富恒丰一年持有期债券A"), (365, "holding"))
        self.assertEqual(infer_holding_days("南方稳利1年持有期债券A"), (365, "holding"))
        # 中文数字月份（2026-11 实测 75 只曾被漏判）
        self.assertEqual(infer_holding_days("大摩安盈稳固六个月持有债券A"), (180, "holding"))
        self.assertEqual(infer_holding_days("泰信添安增利九个月持有期债券A"), (270, "holding"))
        self.assertEqual(infer_holding_days("汇添富稳安三个月持有债券A"), (90, "holding"))
        self.assertEqual(infer_holding_days("格林聚利增强一个月持有期债券A"), (30, "holding"))
        # 滚动持有（2026-11 实测 372 只曾被漏判，其中 69 只 >90 天）
        self.assertEqual(infer_holding_days("广发添财180天滚动持有债券A"), (180, "holding"))
        self.assertEqual(infer_holding_days("平安元鑫120天滚动持有中短债A"), (120, "holding"))
        self.assertEqual(infer_holding_days("嘉实方舟6个月滚动持有债券发起A"), (180, "holding"))
        self.assertEqual(infer_holding_days("华夏六个月滚动持有债券A"), (180, "holding"))
        self.assertEqual(infer_holding_days("长城鑫利30天滚动持有中短债A"), (30, "holding"))

    def test_split_class(self):
        self.assertEqual(split_class("易方达信用债债券A"), ("易方达信用债债券", "A"))
        self.assertEqual(split_class("易方达信用债债券"), ("易方达信用债债券", None))

    def test_style_convertible_priority(self):
        self.assertEqual(classify_bond_name("XX可转债债券A", "000003"), "convertible")
        # 东财把可转债基金归入混合二级，名称证据必须优先于类型标签
        out = classify("XX可转债债券A", "债券型-混合二级", [])
        self.assertEqual(out["style_code"], "bond_convertible")

    def test_style_holding_evidence(self):
        holdings = [{"bond_code": "113001", "bond_name": "转债1", "pct_nav": 15.0,
                     "bond_class": "convertible"},
                    {"bond_code": "019001", "bond_name": "国债1", "pct_nav": 45.0,
                     "bond_class": "rate"}]
        out = classify("XX增强债券A", "债券型-混合二级", holdings)
        self.assertEqual(out["style_code"], "bond_convertible")
        self.assertGreaterEqual(out["evidence"]["convertible_ratio"], 0.20)

    def test_style_benchmark_short_beats_mixed_primary(self):
        # 平安元泓30天滚动持有短债A：法律类型是混合一级，但合同基准是 1 年以下指数
        out = classify("平安元泓30天滚动持有短债A", "债券型-混合一级", [],
                       "中债综合财富(1年以下)指数收益率*90%+一年期定期存款基准利率(税后)*10%")
        self.assertEqual(out["style_code"], "bond_short")
        self.assertEqual(out["evidence"]["rule"], "benchmark_short")

    def test_style_benchmark_credit_beats_weak_convertible_holding(self):
        # 国联益泓90天滚动持有债券A：转债占债券持仓 24.65%，但合同基准 80% 是高等级信用债
        holdings = [{"bond_code": "113001", "bond_name": "转债1", "pct_nav": 9.72,
                     "bond_class": "convertible"},
                    {"bond_code": "019001", "bond_name": "信用债1", "pct_nav": 29.72,
                     "bond_class": "credit"}]
        out = classify("国联益泓90天滚动持有债券A", "债券型-混合一级", holdings,
                       "中债-高等级信用债财富(1-3年)指数收益率*80%+一年期定期存款基准利率*15%"
                       "+中证高等级可转债及可交换债券指数收益率*5%")
        self.assertEqual(out["style_code"], "bond_credit")
        self.assertEqual(out["evidence"]["rule"], "benchmark_credit")

    # ── 动态门槛的应用规则 ──────────────────────────────────
    @staticmethod
    def _checks(composite_ok, others_ok):
        checks = {"composite": {"pass": composite_ok, "reason": None if composite_ok else "below_dynamic"}}
        for name, ok in zip(T.DYNAMIC_DIMENSIONS, others_ok):
            checks[name] = {"pass": ok, "reason": None if ok else "below_dynamic"}
        return checks

    def test_dynamic_gate_all_requires_every_item(self):
        # v2：6 个维度（annual/sharpe/sortino/calmar/non_negative/fee）
        checks = self._checks(True, [True] * 5 + [False])
        ok, fails, _ = dynamic_gate(checks, "all", 4)
        self.assertFalse(ok)
        self.assertEqual(fails, ["fee"])

    def test_dynamic_gate_k_of_n_needs_composite_and_four(self):
        # 综合分过 + 5 个非综合维度中 4 项过 → 通过
        ok, fails, n = dynamic_gate(self._checks(True, [True, True, True, False, False, True]),
                                    "composite_and_k_of_n", 4)
        self.assertTrue(ok)
        self.assertEqual(n, 4)
        self.assertEqual(sorted(fails), ["calmar", "non_negative"])
        # 综合分过但只有 3 项 → 不通过，并标 min_dimensions
        ok2, fails2, n2 = dynamic_gate(self._checks(True, [True] * 3 + [False] * 3),
                                      "composite_and_k_of_n", 4)
        self.assertFalse(ok2)
        self.assertIn("min_dimensions", fails2)
        self.assertEqual(n2, 3)

    def test_dynamic_gate_composite_is_hard(self):
        ok, fails, _ = dynamic_gate(self._checks(False, [True] * 6), "composite_and_k_of_n", 4)
        self.assertFalse(ok)
        self.assertEqual(fails, ["composite"])


class TestDynamicReasons(unittest.TestCase):
    """k-of-n 下未导致淘汰的分项失败不得写进 reason_codes。"""

    def test_soft_fail_not_reported(self):
        checks = {"non_negative": {"reason": "below_dynamic"}}
        self.assertEqual(dynamic_reasons(True, ["non_negative"], checks, 7, 6), [])

    def test_hard_fail_reported(self):
        checks = {"non_negative": {"reason": "below_dynamic"}}
        self.assertEqual(dynamic_reasons(False, ["non_negative"], checks, 5, 6),
                         ["dyn_non_negative:below_dynamic"])

    def test_min_dimensions_reported(self):
        self.assertEqual(dynamic_reasons(False, ["min_dimensions"], {}, 5, 6),
                         ["dyn_min_dimensions:5<6"])


if __name__ == "__main__":
    unittest.main()
class TestEnhancedRelative(unittest.TestCase):
    """ADR-031：增强债从绝对准入改为"同风格分位 + 地板 + 正式分 + 配额"。"""

    def _members(self, style, n=40, lo=0.02, step=0.01):
        return [make_cand(fcode="%06d" % i, style_code=style,
                          metrics={365: {"annual_dec": lo + i * step}}) for i in range(n)]

    def test_peer_stats_use_style_quantiles(self):
        peers = enhanced_peer_stats(self._members("bond_secondary_enhanced"))
        stat = peers["bond_secondary_enhanced"]
        self.assertEqual(stat["n"], 40)
        self.assertFalse(stat["fallback"])
        self.assertGreater(stat["annual_min_pct"], 1.0)
        self.assertLess(stat["annual_min_pct"], 41.0)

    def test_small_style_falls_back_to_floor(self):
        peers = enhanced_peer_stats(self._members("bond_primary_enhanced", n=5))
        stat = peers["bond_primary_enhanced"]
        self.assertTrue(stat["fallback"])
        self.assertGreaterEqual(stat["annual_min_pct"], 1.0)

    def test_relative_check_passes_above_peer_threshold(self):
        peers = enhanced_peer_stats(self._members("bond_secondary_enhanced"))
        stat = peers["bond_secondary_enhanced"]
        good = make_cand(style_code="bond_secondary_enhanced", metrics={365: {
            "annual_dec": (stat["annual_min_pct"] + 1.0) / 100, "sharpe": 1.5,
            "calmar_dec": 3.0, "mdd_dec": 0.02, "positive_ratio_pct": 60.0}})
        checks = enhanced_relative_checks(good, "bond_secondary_enhanced", stat, 70.0)
        self.assertTrue(all(c["pass"] for c in checks.values()),
                        {k: v for k, v in checks.items() if not v["pass"]})

    def test_formal_score_floor_is_a_check(self):
        peers = enhanced_peer_stats(self._members("bond_convertible"))
        cand = make_cand(style_code="bond_convertible", metrics={365: {
            "annual_dec": 0.20, "sharpe": 1.5, "calmar_dec": 3.0, "mdd_dec": 0.02,
            "positive_ratio_pct": 60.0}})
        checks = enhanced_relative_checks(cand, "bond_convertible", peers["bond_convertible"], 50.0)
        self.assertFalse(checks["formal_score"]["pass"])
        self.assertEqual(checks["formal_score"]["reason"], "below_min")

    def test_strict_overlay_counts_passed_checks(self):
        strict = {f"c{i}": {"pass": i < 5} for i in range(7)}
        self.assertIsNone(enhanced_strict_overlay(strict, 0))
        overlay = enhanced_strict_overlay(strict, 5)
        self.assertEqual(overlay["value"], 5)
        self.assertTrue(overlay["pass"])
        self.assertIsNone(overlay["reason"])
        self.assertFalse(enhanced_strict_overlay(strict, 6)["pass"])

    def test_quality_of_applies_strict_overlay(self):
        cand = make_cand(style_code="bond_primary_enhanced")
        ok, ths = quality_of(cand, {"formal_score": 70.0}, {}, {}, "rule", 4,
                             strict_min=7)
        self.assertFalse(ok)
        self.assertIn("strict_overlay", ths["fixed_or_admission"])
        self.assertFalse(ths["fixed_or_admission"]["strict_overlay"]["pass"])
        ok0, ths0 = quality_of(cand, {"formal_score": 70.0}, {}, {}, "rule", 4,
                               strict_min=0)
        self.assertTrue(ok0)
        self.assertNotIn("strict_overlay", ths0["fixed_or_admission"])

    def test_board_quota_per_style(self):
        cands, decisions, scores = [], {}, {}
        for i in range(45):
            code = "1%05d" % i
            cands.append(make_cand(fcode=code, style_code="bond_secondary_enhanced"))
            sc = {"formal_score": 90.0 - i, "composite": 80.0}
            scores[code] = sc
            decisions[code] = {"passed": 1, "board": "bond_enhanced", "scores": sc,
                               "confidence": 8.0, "style_code": "bond_secondary_enhanced",
                               "confidence_detail": {"tier": "high"}, "reasons": [],
                               "layer_reached": "L9"}
        for i in range(5):
            code = "2%05d" % i
            cands.append(make_cand(fcode=code, style_code="bond_primary_enhanced"))
            sc = {"formal_score": 60.0 - i, "composite": 70.0}
            scores[code] = sc
            decisions[code] = {"passed": 1, "board": "bond_enhanced", "scores": sc,
                               "confidence": 7.0, "style_code": "bond_primary_enhanced",
                               "confidence_detail": {"tier": "high"}, "reasons": [],
                               "layer_reached": "L9"}
        boards = _rank(decisions, scores, cands)
        enh = boards["bond_enhanced"]
        self.assertEqual(len([x for x in enh if x["style_code"] == "bond_secondary_enhanced"]),
                         T.ENHANCED_RELATIVE["quota_per_style"])
        self.assertEqual(len([x for x in enh if x["style_code"] == "bond_primary_enhanced"]), 5)
        self.assertEqual([x["rank"] for x in enh], list(range(1, len(enh) + 1)))



class HoldingPolicyTest(unittest.TestCase):
    def test_index_duration_is_not_a_lockup(self):
        """「0-3 年期」「1-3 年」是标的指数久期，不是投资者持有期（用户 2026-11-01 质疑的那只）。"""
        from fundana.fuse.profile import infer_holding_days
        self.assertEqual(infer_holding_days("平安0-3年期政策性金融债债券D"), (None, None))
        self.assertEqual(infer_holding_days("中银中债1-3年期国开行债券指数A"), (None, None))
        self.assertEqual(infer_holding_days("易方达兴利180天持有债券A"), (180, "holding"))


_FREEZE_SCHEMA = """
CREATE TABLE screen_batch(batch_id TEXT PRIMARY KEY, profile TEXT NOT NULL,
  anchor_business_date TEXT NOT NULL, config_hash TEXT NOT NULL,
  formula_contract TEXT NOT NULL, screen_contract TEXT, frozen_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'frozen', note TEXT, baseline_run_id TEXT);
CREATE TABLE screen_threshold(batch_id TEXT NOT NULL, scope TEXT NOT NULL,
  metric TEXT NOT NULL, enter_value REAL, exit_value REAL, n INTEGER,
  quantile REAL, created_at TEXT NOT NULL, PRIMARY KEY(batch_id, scope, metric));
"""


class TestFrozenThresholds(unittest.TestCase):
    """ADR-055 冻结往返：小样本风格的 fallback 标记不能被写成"全 NULL 阈值"。

    回归背景：v3 首轮实测同批次重跑 watch 30→32。原因是 freeze_thresholds 把
    {"n":9,"fallback":"global"} 这种空壳 scope 也写进了 screen_threshold，
    load_frozen 读回来成了"风格阈值存在但全为 NULL"。P0-3 之前
    threshold_unavailable 一律 pass，等于该风格 6 维免检；现在缺失阈值按失败处理。
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_FREEZE_SCHEMA)

    def tearDown(self):
        self.conn.close()

    def _dyn(self):
        return {
            "global": {"n": 500, "composite_min": 70.0, "composite_min_exit": 65.0,
                       "annual_min_pct": 2.5, "annual_min_pct_exit": 2.0,
                       "sharpe_min": 1.5, "sharpe_min_exit": 1.4,
                       "sortino_min": 3.0, "sortino_min_exit": 2.9,
                       "calmar_min": 5.0, "calmar_min_exit": 4.7,
                       "non_negative_min_pct": 60.0, "non_negative_min_pct_exit": 58.0,
                       "fee_max_pct": 0.65, "fee_max_pct_exit": 0.73},
            "by_style": {"bond_pure": {"n": 40, "composite_min": 72.0,
                                       "composite_min_exit": 67.0,
                                       "annual_min_pct": 2.6, "annual_min_pct_exit": 2.1,
                                       "sharpe_min": 1.6, "sharpe_min_exit": 1.5,
                                       "sortino_min": 3.1, "sortino_min_exit": 3.0,
                                       "calmar_min": 5.1, "calmar_min_exit": 4.8,
                                       "non_negative_min_pct": 61.0, "non_negative_min_pct_exit": 59.0,
                                       "fee_max_pct": 0.60, "fee_max_pct_exit": 0.68},
                         "bond_credit": {"n": 9, "fallback": "global"}},
        }

    def test_freeze_skips_fallback_scope(self):
        n = freeze_thresholds(self.conn, "b1", self._dyn(), "2026-11-01", "h", "c")
        self.assertEqual(n, 14)                      # 只有 global + bond_pure 两个 scope
        scopes = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT scope FROM screen_threshold WHERE batch_id='b1'")}
        self.assertEqual(scopes, {"global", "bond_pure"})

    def test_load_frozen_roundtrip_keeps_style_thresholds(self):
        freeze_thresholds(self.conn, "b1", self._dyn(), "2026-11-01", "h", "c")
        dyn = load_frozen(self.conn, "b1")
        self.assertTrue(dyn["frozen"])
        self.assertEqual(dyn["global"]["composite_min"], 70.0)
        self.assertEqual(dyn["by_style"]["bond_pure"]["composite_min"], 72.0)
        self.assertNotIn("bond_credit", dyn["by_style"])   # 空壳 scope 不复活

    def test_load_frozen_ignores_all_null_scope(self):
        """历史脏数据（整列 NULL 的风格 scope）必须被当成不存在。"""
        self.conn.execute("INSERT INTO screen_threshold VALUES"
                          "('b2','bond_credit','composite_min',NULL,NULL,9,0.7,'t')")
        self.conn.execute("INSERT INTO screen_threshold VALUES"
                          "('b2','global','composite_min',70.0,65.0,500,0.7,'t')")
        dyn = load_frozen(self.conn, "b2")
        self.assertNotIn("bond_credit", dyn["by_style"])

    def test_apply_dynamic_falls_back_when_style_scope_empty(self):
        c = make_cand(style_code="bond_credit")
        dyn = {"global": self._dyn()["global"], "by_style": {"bond_credit": {"n": 9}}}
        checks = apply_dynamic(c, {"composite": 75.0}, dyn)
        self.assertEqual(checks["_meta"]["threshold_source"], "global")
        self.assertEqual(checks["composite"]["min"], 70.0)
        self.assertTrue(checks["composite"]["pass"])

    def test_missing_threshold_is_failure_not_free_pass(self):
        """P0-3：threshold_unavailable 不能再当免检。"""
        c = make_cand(style_code="bond_pure")
        dyn = {"global": {"composite_min": 70.0},
               "by_style": {"bond_pure": {"fallback": "global"}}}
        checks = apply_dynamic(c, {"composite": 75.0}, dyn)
        self.assertTrue(checks["composite"]["pass"])
        self.assertFalse(checks["annual_1y"]["pass"])
        self.assertEqual(checks["annual_1y"]["reason"], "threshold_unavailable")


class TestDynamicThresholds(unittest.TestCase):
    """P0-3：composite 阈值必须在完整母体上取无条件 q70，而不是先筛固定线。"""

    def _estimate(self, lo, hi):
        cands = []
        scores = {}
        for i in range(20):
            c = make_cand(fcode="%06d" % i)
            cands.append(c)
            scores[c["fcode"]] = {"composite": float(lo + i)}
        return dynamic_thresholds(cands, scores)["global"]

    def test_unconditional_q70_below_floor_is_inactive(self):
        floor = T.FIXED["composite_min"]
        g = self._estimate(30, 49)
        self.assertLess(g["composite_q70_raw"], floor)
        self.assertEqual(g["composite_min"], floor)
        self.assertTrue(g["dynamic_inactive"])

    def test_unconditional_q70_above_floor_sets_threshold(self):
        floor = T.FIXED["composite_min"]
        g = self._estimate(floor + 5, floor + 24)
        self.assertGreater(g["composite_q70_raw"], floor)
        self.assertEqual(g["composite_min"], g["composite_q70_raw"])
        self.assertFalse(g["dynamic_inactive"])


class TestRookieBoard(unittest.TestCase):
    """新锐榜（P1-1）：历史 1–3 年、只差综合分这一条者单开一榜。"""

    def test_history_days_and_rookie_flag(self):
        from fundana.screen.engine import history_days, is_rookie
        young = make_cand(nav_first_date="2025-01-02", nav_last_date="2026-10-29")
        self.assertEqual(history_days(young), 665)
        self.assertTrue(is_rookie(young))
        self.assertFalse(is_rookie(make_cand()))          # 2015 起，11 年
        self.assertFalse(is_rookie(make_cand(nav_first_date="2026-01-02",
                                            nav_last_date="2026-10-29")))  # <1 年

    def test_history_3y_is_explicit_check(self):
        """P0-2：3 年覆盖不足必须显式失败 history_3y，而不是只靠 composite 上限。"""
        young = make_cand(metrics={1095: {"years": 2.0}})
        checks = fixed_checks(young, score_candidate(young, {}))
        self.assertFalse(checks["history_3y"]["pass"])
        self.assertEqual(checks["history_3y"]["reason"], "below_min")
        self.assertIn("history_3y", [k for k, v in checks.items() if not v["pass"]])

        full = make_cand(metrics={1095: {"years": 3.0}})
        checks_full = fixed_checks(full, score_candidate(full, {}))
        self.assertTrue(checks_full["history_3y"]["pass"])
        self.assertIsNone(checks_full["history_3y"]["reason"])

    def test_scale_pit_check_rejects_profile_fallback(self):
        """P0-5：正式榜只接受 as-of 规模/份额，profile 当前值必须显式失败。"""
        ok = make_cand(scale_source="scale_history_asof",
                       share_scale_source="scale_history_asof")
        self.assertTrue(scale_pit_check(ok)["pass"])
        self.assertIsNone(scale_pit_check(ok)["reason"])

        for kw in ({"scale_source": "profile_current",
                    "share_scale_source": "scale_history_asof"},
                   {"scale_source": "scale_history_asof",
                    "share_scale_source": "profile_current"}):
            c = make_cand(**kw)
            chk = scale_pit_check(c)
            self.assertFalse(chk["pass"])
            self.assertEqual(chk["reason"], "not_pit")

    def test_rookie_score_weights(self):
        from fundana.screen.engine import rookie_score
        flat = {k: 50.0 for k in T.ROOKIE_WEIGHTS}          # 分位是 0–100 量纲
        self.assertAlmostEqual(rookie_score(flat), 50.0, places=4)
        only_annual = {"annual_1y": 100.0}
        self.assertAlmostEqual(rookie_score(only_annual), 35.0, places=4)

    def test_rank_collects_and_folds_rookies(self):
        cands = [make_cand(fcode="00000%d" % i, family_id="F%d" % i) for i in range(3)]
        cands.append(make_cand(fcode="000009", family_id="F0"))   # 与 000000 同家族
        decisions = {}
        for i, c in enumerate(cands):
            decisions[c["fcode"]] = {"layer_reached": "L6C", "passed": 0,
                                     "board": T.BOARD_ROOKIE,
                                     "scores": {"rookie": 90.0 - i, "formal_score": 60.0,
                                                "composite": 55.0}}
        picks = _rank(decisions, {}, cands)
        codes = [it["fcode"] for it in picks[T.BOARD_ROOKIE]]
        self.assertEqual(codes, ["000000", "000001", "000002"])   # 000009 被家族折叠
        self.assertEqual(picks[T.BOARD_ROOKIE][0]["rank"], 1)

    def test_rank_rookie_quota(self):
        cands = [make_cand(fcode="%06d" % i, family_id="F%d" % i) for i in range(T.ROOKIE_QUOTA + 5)]
        decisions = {c["fcode"]: {"layer_reached": "L6C", "passed": 0,
                                  "board": T.BOARD_ROOKIE,
                                  "scores": {"rookie": 100.0 - i, "formal_score": 60.0,
                                             "composite": 55.0}}
                     for i, c in enumerate(cands)}
        picks = _rank(decisions, {}, cands)
        self.assertEqual(len(picks[T.BOARD_ROOKIE]), T.ROOKIE_QUOTA)


class TestBatchBaseline(unittest.TestCase):
    """ADR-081：同 batch 重跑必须复用同一条滞回基线，不能按 created_at 漂移。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_FREEZE_SCHEMA)
        self.conn.execute(
            "CREATE TABLE screen_run(run_id TEXT PRIMARY KEY, status TEXT,"
            " business_date TEXT, batch_id TEXT, created_at TEXT)")

    def tearDown(self):
        self.conn.close()

    def _run(self, run_id, business_date, batch_id, created_at):
        self.conn.execute("INSERT INTO screen_run VALUES(?,?,?,?,?)",
                          (run_id, "done", business_date, batch_id, created_at))

    def test_new_batch_uses_previous_business_day_and_freezes_it(self):
        self._run("r0", "2026-11-01", "2026-11-01", "2026-11-01T08:00:00")
        self._run("r1", "2026-11-02", "2026-11-02", "2026-11-02T08:00:00")
        baseline = _resolve_baseline(self.conn, "r2", "2026-11-02", "new-batch")
        self.assertEqual(baseline, "r0")
        freeze_thresholds(self.conn, "new-batch", {"global": {}, "by_style": {}},
                          "2026-11-02", "h", "c", baseline_run_id=baseline)
        row = self.conn.execute(
            "SELECT baseline_run_id FROM screen_batch WHERE batch_id='new-batch'").fetchone()
        self.assertEqual(row["baseline_run_id"], "r0")
        # 即使之后又插入了同日的 r9，也必须继续复用冻结的 r0。
        self._run("r9", "2026-11-02", "new-batch", "2026-11-02T09:00:00")
        self.assertEqual(_resolve_baseline(self.conn, "r3", "2026-11-02", "new-batch"), "r0")

    def test_legacy_batch_uses_its_earliest_run_as_baseline(self):
        self._run("r1", "2026-11-02", "b1", "2026-11-02T08:00:00")
        self._run("r2", "2026-11-02", "b1", "2026-11-02T09:00:00")
        freeze_thresholds(self.conn, "b1", {"global": {}, "by_style": {}},
                          "2026-11-02", "h", "c")
        self.assertEqual(_resolve_baseline(self.conn, "r3", "2026-11-02", "b1"), "r1")
        self.assertEqual(_resolve_baseline(self.conn, "r4", "2026-11-02", "b1"), "r1")


_CAND_SCHEMA = """
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
"""


class TestBuyableSnapshot(unittest.TestCase):
    """ADR-081：申赎状态只能看当前业务日及以前，历史回测显式开启 fallback。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_CAND_SCHEMA)
        self.conn.execute("INSERT INTO fund VALUES('000001','测试债A','债券型-长债','bond',"
                          "'2015-01-01','ok','测试公司')")
        self.conn.execute("INSERT INTO fund_share_class VALUES('000001','000001')")
        self.conn.execute("INSERT INTO fund_profile VALUES('000001',5.0,0.35,0.30,0.05,0.0,"
                          "0.0,0.0,1,500,900,7.5,'{\"share_class_scale_billion\": 5.0}')")
        self.conn.execute("INSERT INTO fund_fee_meta VALUES('000001',1,NULL)")
        self.conn.execute("INSERT INTO fund_holding_policy VALUES('000001',NULL,'none')")
        self.conn.execute("INSERT INTO fund_style VALUES('000001','bond_pure','text_heuristic')")
        self.conn.execute("INSERT INTO fund_coverage VALUES('000001','r1',2500,'2015-01-05',"
                          "'2026-10-29',2,5)")
        self.conn.execute("INSERT INTO fund_buyable_daily VALUES"
                          "('000001','2026-10-31','confirmed_no','暂停申购')")
        self.conn.execute("INSERT INTO fund_buyable_daily VALUES"
                          "('000001','2026-11-02','confirmed_yes','开放申购')")

    def tearDown(self):
        self.conn.close()

    def test_snapshot_is_asof_not_future(self):
        c = _load_candidates(self.conn, "2026-11-01", "c", types=("bond",))[0]
        self.assertEqual(c["buyable_state"], "confirmed_no")
        c = _load_candidates(self.conn, "2026-11-02", "c", types=("bond",))[0]
        self.assertEqual(c["buyable_state"], "confirmed_yes")

    def test_historical_backtest_can_opt_into_fallback(self):
        c = _load_candidates(self.conn, "2020-06-30", "c", types=("bond",))[0]
        self.assertIsNone(c["buyable_state"])
        c = _load_candidates(self.conn, "2020-06-30", "c", types=("bond",),
                             buyable_fallback=True)[0]
        self.assertEqual(c["buyable_state"], "confirmed_yes")


class TestDeterministicTieBreak(unittest.TestCase):
    """同分并列时用 fcode 兜底，避免依赖 dict/查询返回顺序。"""

    def test_watch_and_rookie_ties_are_stable(self):
        cands = [make_cand(fcode="000002", family_id="F2"),
                 make_cand(fcode="000001", family_id="F1")]
        decisions = {}
        for c in reversed(cands):
            decisions[c["fcode"]] = {
                "layer_reached": "L2", "passed": 0, "board": T.BOARD_WATCH,
                "scores": {"rank_score": 1.0, "formal_score": 60.0, "composite": 55.0}}
        picks = _rank(decisions, {}, cands)
        self.assertEqual([it["fcode"] for it in picks[T.BOARD_WATCH]],
                         ["000001", "000002"])

        decisions = {}
        for c in reversed(cands):
            decisions[c["fcode"]] = {
                "layer_reached": "L6C", "passed": 0, "board": T.BOARD_ROOKIE,
                "scores": {"rookie": 1.0, "formal_score": 60.0, "composite": 55.0}}
        picks = _rank(decisions, {}, cands)
        self.assertEqual([it["fcode"] for it in picks[T.BOARD_ROOKIE]],
                         ["000001", "000002"])


class TestCoreBoard(unittest.TestCase):
    """核心精选榜（v5 / ADR-071）：正式候选按排序分集中到 50 只。"""

    @staticmethod
    def _decisions(cands, styles):
        out = {}
        for i, c in enumerate(cands):
            style = styles.get(c["fcode"], "bond_pure")
            out[c["fcode"]] = {
                "layer_reached": "L9", "passed": 1,
                "board": T.BOARD_ENHANCED if style in T.ENHANCED_STYLES else T.BOARD_STABLE,
                "style_code": style,
                "scores": {"rank_score": 1000.0 - i, "formal_score": 80.0,
                           "composite": 70.0,
                           "coverage": {"is_full_3y": True}},
            }
        return out

    def test_core_quota_and_order(self):
        cands = [make_cand(fcode="%06d" % i, family_id="F%d" % i,
                           company_name="C%03d" % i) for i in range(60)]
        picks = _rank(self._decisions(cands, {}), {}, cands)
        core = picks[T.BOARD_CORE]
        self.assertEqual(len(core), T.CORE_QUOTA)
        self.assertEqual([it["fcode"] for it in core], ["%06d" % i for i in range(50)])
        self.assertEqual([it["rank"] for it in core], list(range(1, 51)))
        self.assertNotIn(T.BOARD_CORE, T.FORMAL_BOARDS)

    def test_core_style_caps(self):
        cands, styles = [], {}
        for i in range(10):                       # 增强：可转债，排序分最高
            code = "2%05d" % i
            cands.append(make_cand(fcode=code, family_id="V%d" % i, company_name="CV%d" % i))
            styles[code] = "bond_convertible"
        for i in range(20):                       # 增强：二级债
            code = "1%05d" % i
            cands.append(make_cand(fcode=code, family_id="E%d" % i, company_name="CE%d" % i))
            styles[code] = "bond_secondary_enhanced"
        for i in range(60):                       # 稳健（v10 核心只收纯债/短债等稳健风格）
            code = "3%05d" % i
            cands.append(make_cand(fcode=code, family_id="S%d" % i, company_name="CS%d" % i))
            styles[code] = "bond_pure"
        picks = _rank(self._decisions(cands, styles), {}, cands)
        core = picks[T.BOARD_CORE]
        n_enh = sum(1 for it in core if it["style_code"] in T.ENHANCED_STYLES)
        n_conv = sum(1 for it in core if it["style_code"] == "bond_convertible")
        self.assertEqual(len(core), T.CORE_QUOTA)
        self.assertEqual(n_enh, T.CORE_ENHANCED_CAP)
        self.assertEqual(n_conv, T.CORE_CONVERTIBLE_CAP)

    def test_core_family_fold_backfills(self):
        cands = [make_cand(fcode="%06d" % i, family_id="F%d" % i,
                           company_name="C%03d" % i) for i in range(51)]
        # 000001 与 000000 同家族，排在第二位；折叠后应由 000050 补位。
        cands[1] = make_cand(fcode="000001", family_id="F0", company_name="C0")
        picks = _rank(self._decisions(cands, {}), {}, cands)
        codes = [it["fcode"] for it in picks[T.BOARD_CORE]]
        self.assertEqual(len(codes), T.CORE_QUOTA)
        self.assertIn("000000", codes)
        self.assertNotIn("000001", codes)
        self.assertIn("000050", codes)


class TestCore10Board(unittest.TestCase):
    """收益精选 10 榜（v6 / ADR-072）：同一排序分、风格限额后集中到 10 只。"""

    @staticmethod
    def _decisions(cands, styles):
        out = {}
        for i, c in enumerate(cands):
            style = styles.get(c["fcode"], "bond_pure")
            out[c["fcode"]] = {
                "layer_reached": "L9", "passed": 1,
                "board": T.BOARD_ENHANCED if style in T.ENHANCED_STYLES else T.BOARD_STABLE,
                "style_code": style,
                "scores": {"rank_score": 1000.0 - i, "formal_score": 80.0,
                           "composite": 70.0,
                           "coverage": {"is_full_3y": True}},
            }
        return out

    def test_core10_quota_and_order(self):
        cands = [make_cand(fcode="%06d" % i, family_id="F%d" % i,
                           company_name="C%03d" % i) for i in range(15)]
        picks = _rank(self._decisions(cands, {}), {}, cands)
        core = picks[T.BOARD_CORE10]
        self.assertEqual(len(core), T.CORE10_QUOTA)
        self.assertEqual([it["fcode"] for it in core], ["%06d" % i for i in range(10)])
        self.assertEqual([it["rank"] for it in core], list(range(1, 11)))
        self.assertIn(T.BOARD_CORE10, T.ALL_BOARDS)
        self.assertNotIn(T.BOARD_CORE10, T.FORMAL_BOARDS)

    def test_core10_style_caps(self):
        cands, styles = [], {}
        for i in range(5):                        # 增强：可转债，排序分最高
            code = "2%05d" % i
            cands.append(make_cand(fcode=code, family_id="V%d" % i, company_name="CV%d" % i))
            styles[code] = "bond_convertible"
        for i in range(10):                       # 增强：二级债
            code = "1%05d" % i
            cands.append(make_cand(fcode=code, family_id="E%d" % i, company_name="CE%d" % i))
            styles[code] = "bond_secondary_enhanced"
        for i in range(30):                       # 稳健
            code = "3%05d" % i
            cands.append(make_cand(fcode=code, family_id="S%d" % i, company_name="CS%d" % i))
            styles[code] = "bond_pure"
        picks = _rank(self._decisions(cands, styles), {}, cands)
        core = picks[T.BOARD_CORE10]
        n_enh = sum(1 for it in core if it["style_code"] in T.ENHANCED_STYLES)
        n_conv = sum(1 for it in core if it["style_code"] == "bond_convertible")
        self.assertEqual(len(core), T.CORE10_QUOTA)
        self.assertEqual(n_enh, T.CORE10_ENHANCED_CAP)
        self.assertEqual(n_conv, T.CORE10_CONVERTIBLE_CAP)

    def test_core10_short_style_cap(self):
        """v10：短债最多 2 只，避免低波动短债占满 10 个核心名额。"""
        cands, styles = [], {}
        for i in range(10):                       # 短债排序分最高
            code = "4%05d" % i
            cands.append(make_cand(fcode=code, family_id="T%d" % i, company_name="CT%d" % i))
            styles[code] = "bond_short"
        for i in range(20):                       # 纯债补位
            code = "5%05d" % i
            cands.append(make_cand(fcode=code, family_id="P%d" % i, company_name="CP%d" % i))
            styles[code] = "bond_pure"
        picks = _rank(self._decisions(cands, styles), {}, cands)
        core = picks[T.BOARD_CORE10]
        n_short = sum(1 for it in core if it["style_code"] == "bond_short")
        self.assertEqual(len(core), T.CORE10_QUOTA)
        self.assertLessEqual(n_short, T.CORE10_STYLE_CAPS["bond_short"])
        self.assertEqual(n_short, T.CORE10_STYLE_CAPS["bond_short"])

    def test_core_requires_share_scale_and_manager_tenure(self):
        """v10：核心榜拒绝目标份额 <2 亿、现任经理 <2 年的高排序分基金。"""
        cands = [make_cand(fcode="%06d" % i, family_id="F%d" % i,
                           company_name="C%03d" % i) for i in range(60)]
        cands[0] = make_cand(fcode="000000", family_id="F0", company_name="C000",
                             share_scale_billion=1.0)
        cands[1] = make_cand(fcode="000001", family_id="F1", company_name="C001",
                             manager_tenure_min_days=365)
        picks = _rank(self._decisions(cands, {}), {}, cands)
        codes = [it["fcode"] for it in picks[T.BOARD_CORE]]
        self.assertEqual(len(codes), T.CORE_QUOTA)
        self.assertNotIn("000000", codes)
        self.assertNotIn("000001", codes)
        self.assertIn("000050", codes)


class TestLongBoard(unittest.TestCase):
    """P1 长期核心观察榜：3y 完整 + 5y 覆盖 + 5y 年化非空 + 经理/份额/风格。"""

    @staticmethod
    def _decisions(cands):
        out = {}
        for i, c in enumerate(cands):
            out[c["fcode"]] = {
                "layer_reached": "L9", "passed": 1, "board": T.BOARD_STABLE,
                "style_code": "bond_pure",
                "scores": {"rank_score": 1000.0 - i, "long_score": 1000.0 - i,
                           "formal_score": 80.0, "composite": 70.0,
                           "coverage": {"is_full_3y": True, "coverage_ratio_5y": 0.99,
                                        "has_5y_annual": True}},
            }
        return out

    def test_long_quota_and_requires_5y_annual(self):
        cands = [make_cand(fcode="%06d" % i, family_id="F%d" % i,
                           company_name="C%03d" % i) for i in range(25)]
        dec = self._decisions(cands)
        dec["000000"]["scores"]["coverage"]["has_5y_annual"] = False
        dec["000001"]["scores"]["coverage"]["coverage_ratio_5y"] = 0.59
        cands[2] = make_cand(fcode="000002", family_id="F2", company_name="C002",
                             manager_tenure_min_days=800)
        picks = _rank(dec, {}, cands)
        long = picks[T.BOARD_LONG]
        self.assertEqual(len(long), T.LONG_QUOTA)
        self.assertNotIn("000000", [it["fcode"] for it in long])
        self.assertNotIn("000001", [it["fcode"] for it in long])
        self.assertNotIn("000002", [it["fcode"] for it in long])
        self.assertIn("000021", [it["fcode"] for it in long])
        self.assertNotIn(T.BOARD_LONG, T.FORMAL_BOARDS)


class TestCore10Diversification(unittest.TestCase):
    """P2-2：core10 不再按分数硬截，相关超过 0.85 的重复暴露被拒绝。"""

    def test_core10_rejects_near_duplicate_high_score(self):
        cands = {}
        passed = []
        series = {}
        base = [0.0012 if i % 2 == 0 else -0.0007 for i in range(60)]
        for i in range(6):
            code = "100%03d" % i
            c = make_cand(fcode=code, family_id="F%d" % i, company_name="CO%d" % i)
            cands[code] = c
            passed.append((code, {"style_code": "bond_pure",
                                  "scores": {"rank_score": 100.0 - i,
                                             "coverage": {"is_full_3y": True}}}))
            if i < 5:
                series[code] = [("2026-01-%02d" % (j + 1), v) for j, v in enumerate(base)]
            else:
                series[code] = [("2026-01-%02d" % (j + 1), -v) for j, v in enumerate(base)]
        kept = _select_concentrated(
            passed, cands, [], board=T.BOARD_CORE10, quota=5,
            enh_cap=T.CORE10_ENHANCED_CAP, conv_cap=T.CORE10_CONVERTIBLE_CAP,
            series_by_code=series)
        codes = [it["fcode"] for it in kept]
        self.assertIn("100000", codes)
        self.assertNotIn("100001", codes)   # 与 100000 完全相关，硬拒绝
        self.assertIn("100005", codes)      # 负相关，作为分散候选被选中
        self.assertLessEqual(len(codes), 5)


class TestCore10LocalSearch(unittest.TestCase):
    """P2-2b：有界单换只在分数损失 <=2 且 effective bets 真提升时发生。"""

    def _series(self, factor: float, idio: float, phase: int):
        import math
        days = [(m, d) for m in (1, 2) for d in range(1, 29)]
        # 共同因子同相位 -> 入选基金彼此高度相关；id 噪声错相位 -> 候选低相关。
        return [("2026-%02d-%02d" % (m, d),
                 factor * math.sin(i * 0.7) + idio * math.sin(i * 1.3 + phase))
                for i, (m, d) in enumerate(days)]

    def _item(self, code, score):
        return {"fcode": code, "style_code": "bond_pure",
                "scores": {"rank_score": score}}

    def test_swap_accepts_small_score_loss_for_diversification(self):
        kept = [self._item("K%d" % i, 100.0 - i) for i in range(4)]
        cand = self._item("CAND", 96.5)   # 换掉 97 分那只，损失 0.5
        series = {}
        for i, item in enumerate(kept):
            series[item["fcode"]] = self._series(1.0, 0.18, i * 1.7)
        series["CAND"] = self._series(0.05, 1.0, 9.0)
        eligible = [(x["fcode"], x, make_cand(fcode=x["fcode"], family_id=x["fcode"],
                                             company_name=x["fcode"]), "bond_pure")
                    for x in kept + [cand]]
        out = _core10_local_search(kept, eligible, {}, 10, series)
        codes = [x["fcode"] for x in out]
        self.assertIn("CAND", codes)
        self.assertEqual(len(codes), 4)
        self.assertEqual(len(set(codes)), 4)
        removed = [c for c in ("K0", "K1", "K2", "K3") if c not in codes]
        self.assertEqual(len(removed), 1)

    def test_swap_rejects_large_score_loss(self):
        kept = [self._item("K%d" % i, 100.0 - i) for i in range(4)]
        cand = self._item("CAND", 90.0)   # 损失 7 分，超过 2 分预算
        series = {}
        for i, item in enumerate(kept):
            series[item["fcode"]] = self._series(1.0, 0.18, i * 1.7)
        series["CAND"] = self._series(0.05, 1.0, 9.0)
        eligible = [(x["fcode"], x, make_cand(fcode=x["fcode"], family_id=x["fcode"],
                                             company_name=x["fcode"]), "bond_pure")
                    for x in kept + [cand]]
        out = _core10_local_search(kept, eligible, {}, 10, series)
        self.assertNotIn("CAND", [x["fcode"] for x in out])


class TestCore10RiskBudget(unittest.TestCase):
    def _days(self, sign):
        out = []
        for month in (1, 2):
            for day in range(1, 29):
                v = 0.001 if (month * 100 + day) % 2 == 0 else -0.001
                out.append(("2026-%02d-%02d" % (month, day), sign * v))
        return out

    def test_correlated_core10_is_downgraded(self):
        days = self._days(1)
        kept = [{"fcode": "a"}, {"fcode": "b"}]
        out = _core10_risk_budget(kept, {"a": days, "b": days})
        self.assertEqual(out["status"], "downgraded")
        self.assertAlmostEqual(out["effective_bets"], 1.0, places=3)
        self.assertIn("不能宣称分散化", out["note"])

    def test_negatively_correlated_core10_meets_target(self):
        kept = [{"fcode": "a"}, {"fcode": "b"}]
        out = _core10_risk_budget(kept, {"a": self._days(1), "b": self._days(-1)})
        self.assertEqual(out["status"], "meets_target")
        self.assertGreaterEqual(out["effective_bets"], T.CORE10_TARGET_EFFECTIVE_BETS)
