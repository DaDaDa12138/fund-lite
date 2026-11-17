"""债券型基金筛选引擎 L1–L10。

设计要点（与主架构书一致的部分）：
* L1–L5.5 是资格门禁，任一失败即退出，但每只基金都留下 screen_decision 记录与原因码。
* 正式榜百分位母体 = 可投域（L1–L5.5 通过者，PERCENTILE_POOL=investable）；
  观察/受限榜另用 L1 通过者分布评分，避免把买不到/锁定的基金塞进正式分位。
* 动态阈值母体 = 基础门禁（L1–L5.5）通过者，符合"不得先留固定通过者再算动态分位"。
* short_core / composite 仍按缺失=0（证据不足不得装高分）；long_core / long_formal
  在覆盖率达到 MIN_LONG_COVERAGE 时按可用维度重归一化，并把 coverage 写入 scores_json。
"""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import db, freshness
from ..daily_returns import load_daily_returns
from ..portfolio_diagnostics import effective_bets, pair_corr
from ..fees import holding as holding_mod
from ..fuse.scale import scale_asof_map, share_scale_asof_map
from ..metrics import comparability
from ..metrics import engine as metrics_engine
from ..labels import CHECK_UNITS
from . import pool as pool_mod
from . import relief as relief_mod
from .. import fundtypes
from .. import risk_gate
from . import pit, scoring, thresholds as T

PERIOD_KEYS = {30: "m1", 90: "m3", 180: "m6", 365: "y1", 730: "y2", 1095: "y3", 1825: "y5"}


class Candidate(dict):
    pass


def _load_candidates(conn, business_date: str, formula_contract: str,
                      types=None, buyable_fallback: bool = False,
                      as_of: str | None = None) -> list[Candidate]:
    """载入候选。

    buyable_fallback 只给历史回测用：库里最早的申赎快照晚于回测日时，严格按
    business_date <= 回测日 会取不到任何快照，把全部基金误判成不可投。历史
    回测没有当时的申赎快照，因此显式允许退回到库内最新快照；生产筛选不得开启。

    as_of 是 PIT 截止日（默认 business_date）：规模/份额/经理任职按该日回看历史，
    找不到历史时才回退到 profile 当前值，并在 scale_source/tenure_source 标记。
    """
    as_of = (as_of or business_date)[:10]
    clause, params = fundtypes.where(types, column="f.type_group")
    if buyable_fallback:
        buyable_date = ("COALESCE((SELECT MAX(business_date) FROM fund_buyable_daily"
                        " WHERE business_date<=?),"
                        " (SELECT MAX(business_date) FROM fund_buyable_daily))")
    else:
        buyable_date = ("(SELECT MAX(business_date) FROM fund_buyable_daily"
                        " WHERE business_date<=?)")
    tenure_params: list[Any] = []

    def _has_table(name: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone() is not None

    has_tenure_table = _has_table("fund_manager_tenure")
    has_lifecycle = _has_table("fund_lifecycle")
    lifecycle_clause = (" AND NOT EXISTS (SELECT 1 FROM fund_lifecycle l"
                        " WHERE l.fcode=f.fcode AND l.status IN ('liquidated','merged')"
                        " AND l.effective_date<=?)") if has_lifecycle else ""
    has_scale_table = _has_table("fund_scale_history") and _has_table("fund_share_class")
    if has_tenure_table:
        tenure_expr = ("(SELECT MIN(CAST(julianday(?) - julianday(t.start_date) AS INTEGER))"
                       " FROM fund_manager_tenure t WHERE t.fcode=f.fcode"
                       " AND t.start_date<=? AND (t.end_date IS NULL OR t.end_date>?))"
                       " AS manager_tenure_min_days")
        tenure_params = [as_of, as_of, as_of]
    else:
        tenure_expr = "NULL AS manager_tenure_min_days"
    rows = conn.execute(
        "SELECT f.fcode, f.name, f.type_text, f.type_group, f.estab_date, f.identity_status,"
        " f.company_name,"
        " (SELECT family_id FROM fund_share_class WHERE fcode = f.fcode) AS family_id,"
        " p.asset_scale_billion AS asset_scale_profile,"
        " p.recurring_fee_pct, p.mgr_fee_pct, p.trust_fee_pct,"
        " p.sales_fee_pct, p.max_sub_fee_pct, p.max_red_fee_pct, p.field_src_json,"
        " p.manager_count, p.manager_career_min_days, p.manager_career_max_days,"
        " p.manager_power_score,"
        f" {tenure_expr},"
        " h.effective_days, h.lockup_type, s.style_code, s.confidence AS style_confidence,"
        " b.buyable_state, b.purchase_status, c.nav_points, c.nav_first_date, c.nav_last_date,"
        " c.freshness_days, c.gap_max_days, fm.has_discount, fm.redemption_1y_pct"
        " FROM fund f"
        " LEFT JOIN fund_profile p ON p.fcode=f.fcode"
        " LEFT JOIN fund_fee_meta fm ON fm.fcode=f.fcode"
        " LEFT JOIN fund_holding_policy h ON h.fcode=f.fcode"
        " LEFT JOIN fund_style s ON s.fcode=f.fcode"
        " LEFT JOIN fund_coverage c ON c.fcode=f.fcode AND c.run_id=(SELECT MAX(run_id) FROM fund_coverage)"
        f" LEFT JOIN fund_buyable_daily b ON b.fcode=f.fcode AND b.business_date={buyable_date}"
        f" WHERE {clause}{lifecycle_clause}"
        " ORDER BY f.fcode", [*tenure_params, business_date,
                              *params, *([as_of] if has_lifecycle else [])]).fetchall()
    metrics: dict[str, dict[int, dict[str, Any]]] = {}
    for m in conn.execute("SELECT * FROM fund_metric WHERE formula_contract=?"
                          " ORDER BY fcode, period_days", (formula_contract,)):
        metrics.setdefault(m["fcode"], {})[m["period_days"]] = dict(m)
    scale_map: dict[str, dict] = {}
    share_scale_map: dict[str, dict] = {}
    if has_scale_table:
        # P0-5：复用 walkforward 的 45 天披露滞后，避免把季报期末规模当成
        # as_of 已知；旧实现 report_period<=as_of 会把 9/30 规模透进 10 月筛选。
        scale_map = scale_asof_map(conn, as_of, lag_days=T.SCALE_DISCLOSURE_LAG_DAYS)
        share_scale_map = share_scale_asof_map(conn, as_of,
                                               lag_days=T.SCALE_DISCLOSURE_LAG_DAYS)
    metric_flags = comparability.load_latest(conn, as_of)
    out: list[Candidate] = []
    for r in rows:
        cand = Candidate(dict(r))
        cand["metrics"] = metrics.get(r["fcode"], {})
        m365 = cand["metrics"].get(365) or {}
        cand["metric_end_date"] = (str(m365.get("end_date"))[:10]
                                   if m365.get("end_date") else None)
        cand["metric_stale_days"] = None
        cand["style_code"] = cand["style_code"] or "bond_pure"
        # 规模优先用 as-of 历史（family 汇总，report_period<=as_of-45 天）；
        # 没有历史才回退 profile 当前值，并标记 profile_current，正式榜会拒绝。
        profile_asset = cand.pop("asset_scale_profile", None)
        scale_row = scale_map.get(cand["fcode"])
        if scale_row is not None:
            cand["asset_scale_billion"] = scale_row.get("asset_scale_billion")
            cand["scale_source"] = "scale_history_asof"
            cand["scale_asof"] = scale_row.get("report_period")
        else:
            cand["asset_scale_billion"] = profile_asset
            cand["scale_source"] = "profile_current"
            cand["scale_asof"] = None
        cand["scale_lag_days"] = T.SCALE_DISCLOSURE_LAG_DAYS
        # 份额级规模优先用 as-of 历史（按份额取，不能用 family 合计）；
        # 没有历史才解析 profile.field_src_json 的当前值。解析失败按缺失处理，
        # 交给 L4 share_scale_missing，不静默当 0。
        share_row = share_scale_map.get(cand["fcode"])
        share_scale = None
        share_source = None
        share_asof = None
        if share_row is not None and share_row.get("share_scale_billion") is not None:
            share_scale = float(share_row["share_scale_billion"])
            share_source = "scale_history_asof"
            share_asof = share_row.get("report_period")
        else:
            try:
                src = json.loads(cand.get("field_src_json") or "{}")
                raw_share = src.get("share_class_scale_billion") if isinstance(src, dict) else None
                if raw_share is not None:
                    share_scale = float(raw_share)
                    share_source = "profile_current"
            except (TypeError, ValueError, json.JSONDecodeError):
                share_scale = None
        cand["share_scale_billion"] = share_scale
        cand["share_scale_source"] = share_source
        cand["share_scale_asof"] = share_asof
        cand["tenure_source"] = ("tenure_asof"
                                 if has_tenure_table and cand.get("manager_tenure_min_days") is not None
                                 else "missing")
        _attach_cost(cand)
        flags = metric_flags.get(cand["fcode"], [])
        cand["metric_flags"] = flags
        cand["metric_comparable"] = not flags
        out.append(cand)
    return out


def _attach_cost(cand: Candidate) -> None:
    """挂上持有期总成本（ADR-059）：cost_1y_pct 为主口径，cost_3y_pct 仅展示。

    缺经常性费率或申购费时整条成本为 None，交由 L4 fee_missing 处理（不静默按 0）。
    """
    prof = holding_mod.cost_profile(cand.get("recurring_fee_pct"),
                                    cand.get("max_sub_fee_pct"),
                                    cand.get("has_discount"))
    cand["cost"] = prof
    cand["cost_1y_pct"] = (prof.get(T.COST_HORIZON_DAYS) or {}).get("total_pct")
    cand["cost_3y_pct"] = (prof.get(1095) or {}).get("total_pct")


def _cost_1y(cand: Candidate) -> float | None:
    """惰性取 1 年持有期总成本：测试/影子榜直接构造的 Candidate 也能拿到同口径值。"""
    if "cost_1y_pct" not in cand:
        _attach_cost(cand)
    return cand.get("cost_1y_pct")


def _m(cand: Candidate, period: int, field: str) -> float | None:
    row = cand["metrics"].get(period)
    if not row:
        return None
    return row.get(field)


def _years_since(estab_date: str | None, as_of: str) -> float | None:
    if not estab_date:
        return None
    from datetime import date
    try:
        d0 = date.fromisoformat(estab_date[:10])
        d1 = date.fromisoformat(as_of[:10])
    except ValueError:
        return None
    return max(0.0, (d1 - d0).days / 365.25)


def base_gate(cand: Candidate, as_of: str, nav_age_limit: int) -> tuple[str, list[str]]:
    """L1–L5.5。返回 (layer_reached, reasons)；reasons 为空表示通过。"""
    reasons: list[str] = []
    # L1 身份与数据资格
    if cand["identity_status"] != "ok":
        return "L1", ["identity_quarantined"]
    if not cand["estab_date"]:
        reasons.append("estab_date_missing")
    if not cand["nav_points"]:
        return "L1", reasons + ["no_nav"]
    if cand["freshness_days"] is not None and cand["freshness_days"] > nav_age_limit:
        reasons.append("nav_stale")
    # P0-2：净值曲线不可比（摊余成本定开、零风险、稀疏/过期净值）不进分位母体。
    # 旗标由 metrics.comparability 生成；没有该表的旧库默认无旗标，不改变旧行为。
    for flag in cand.get("metric_flags") or []:
        code = "metric_" + str(flag)
        if code not in reasons:
            reasons.append(code)
    if _m(cand, 365, "annual_dec") is None:
        reasons.append("no_1y_metrics")
    # P0-4：指标截止日不要求与 as_of 完全相等。周更/定开产品常见 0–7 天滞后，
    # 旧硬门把 548 只（其中 132 只仅此一条）误杀。允许 0–7 天并记 metric_stale_days；
    # >7 天或指标日 > as_of 仍阻断。容忍只影响资格，不改指标窗口，报告必须披露 stale。
    metric_end = cand.get("metric_end_date")
    cand["metric_stale_days"] = None
    if metric_end and as_of:
        try:
            stale_days = (date.fromisoformat(as_of[:10])
                          - date.fromisoformat(metric_end[:10])).days
        except ValueError:
            stale_days = None
        if stale_days is not None:
            cand["metric_stale_days"] = stale_days
            if stale_days < 0:
                reasons.append("metric_lookahead")
            elif stale_days > T.METRIC_STALE_TOLERANCE_DAYS:
                reasons.append("metric_stale_gt_%dd" % T.METRIC_STALE_TOLERANCE_DAYS)
    gap = _m(cand, 365, "p75_gap_days")
    if gap is None:
        gap = _m(cand, 365, "median_gap_days")
    if gap is not None and gap > T.MAX_SAMPLING_GAP_DAYS:
        reasons.append("sampling_cadence_gt_%dd" % T.MAX_SAMPLING_GAP_DAYS)
    if reasons:
        return "L1", reasons
    # L2 可投性
    if cand["buyable_state"] != "confirmed_yes":
        # 场内 ETF/LOF：东财把申赎状态写成"场内交易"、buyable_state=unknown。
        # 这类基金可在证券账户买卖，但不属于场外申购口径，单列原因码而不是记 unknown。
        if (cand.get("purchase_status") or "") == "场内交易":
            return "L2", ["exchange_traded"]
        return "L2", ["buyable_" + (cand["buyable_state"] or "unknown")]
    # L3 持有期
    days = cand["effective_days"]
    if days is not None and days > T.MAX_LOCK_DAYS:
        return "L3", ["locked_%dd" % days]
    # L3 定开/封闭（ADR-053）：协会口径下定期开放与封闭式不参与同类排名，
    # 一律不进正式榜。v1 里它们靠"暂停申购"偶然被 L2 挡住，已有 3 只漏进正式榜。
    lockup = (cand.get("lockup_type") or "").lower()
    if lockup in ("periodic_open", "closed"):
        return "L3", ["lockup_" + lockup]
    # L4 完整性
    if cand["asset_scale_billion"] is None:
        return "L4", ["scale_missing"]
    if cand.get("share_scale_billion") is None:
        return "L4", ["share_scale_missing"]
    if _cost_1y(cand) is None:
        return "L4", ["fee_missing"]
    # L5 成熟度
    years = _years_since(cand["estab_date"], as_of)
    if years is None or years < 1.0:
        return "L5", ["maturity_lt_1y"]
    # L5 规模
    tier = min(5, max(1, int(years) + 1))
    need = T.SHORT_SCALE_MIN if cand["style_code"] == "bond_short" else T.MATURITY_SCALE_MIN[tier]
    if cand["asset_scale_billion"] < need:
        return "L5", ["scale_below_%.2f" % need]
    # P0-7：family 规模不等于目标份额规模；006432 C 只有 1.58 亿，family 9.56 亿。
    if cand["share_scale_billion"] < T.SHARE_SCALE_MIN:
        return "L5", ["share_scale_below_%.2f" % T.SHARE_SCALE_MIN]
    # L5.5 持有期总成本（ADR-059）：主口径 1 年，粗筛只砍最贵 1%
    if _cost_1y(cand) > T.COST_CAP_PCT_1Y:
        return "L5.5", ["holding_cost_above_%.2f" % T.COST_CAP_PCT_1Y]
    return "L5.5", []


METRIC_SPECS = {
    # key: (period, field, higher_is_better)
    "return_1m": (30, "total_return_pct", True),
    "return_3m": (90, "total_return_pct", True),
    "return_6m": (180, "total_return_pct", True),
    "return_1y": (365, "total_return_pct", True),
    "return_3y": (1095, "total_return_pct", True),
    "annual_1y": (365, "annual_dec", True),
    "annual_3y": (1095, "annual_dec", True),
    "annual_5y": (1825, "annual_dec", True),
    "sharpe": (365, "sharpe", True),
    "sortino": (365, "sortino", True),
    "calmar": (365, "calmar_dec", True),
    "mdd": (365, "mdd_dec", False),
    "vol": (365, "vol_dec", False),
    "positive": (365, "positive_ratio_pct", True),
    "non_negative": (365, "non_negative_ratio_pct", True),
    # P0-1：长期一致性用“非负日占比”，不用“上涨日占比”；后者把 0.00% 平坦日
    # 当成失败，会惩罚低波动、净值四舍五入到 0.01% 的债基。
    "consistency": (1095, "non_negative_ratio_pct", True),
    "mdd_3y": (1095, "mdd_dec", False),
    "vol_3y": (1095, "vol_dec", False),
    "calmar_3y": (1095, "calmar_dec", True),
    "sortino_3y": (1095, "sortino", True),
    "mdd_5y": (1825, "mdd_dec", False),
    "vol_5y": (1825, "vol_dec", False),
    "calmar_5y": (1825, "calmar_dec", True),
}


def _unit(name: str) -> str:
    return CHECK_UNITS.get(name, "num")


def blend_annual(cand: Candidate) -> tuple[float | None, str]:
    """增强债年化混合口径（ADR-035）：1 年与 3 年各半，返回 (小数比率, window)。

    window 取值 1y_3y / 1y_only / 3y_only / none，写进 checks 便于前端与审计区分：
    历史不足 3 年的基金仍然参与，但不会被当成"看过 3 年"。
    """
    w = T.ENHANCED_ANNUAL_BLEND
    a1 = _m(cand, 365, "annual_dec")
    a3 = _m(cand, 1095, "annual_dec")
    if a1 is None and a3 is None:
        return None, "none"
    if a3 is None:
        return a1, "1y_only"
    if a1 is None:
        return a3, "3y_only"
    return w["w1y"] * a1 + w["w3y"] * a3, "1y_3y"


def _raw(cand: Candidate, key: str) -> float | None:
    if key == "fee":
        return _cost_1y(cand)
    if key == "scale":
        return cand["asset_scale_billion"]
    period, field, _ = METRIC_SPECS[key]
    return _m(cand, period, field)


def compute_percentiles(cands: list[Candidate], use_style: bool = True,
                        pool_codes: set[str] | None = None,
                        dist_codes: set[str] | None = None) -> dict[str, dict[str, float | None]]:
    """返回 {fcode: {metric_key: pct}}；缺失值返回 None（不是 0 分）。

    pool_codes 给定时只在该母体内计算分位；正式榜传 investable 母体，
    观察/受限榜传 L1 通过者。风格组样本 <15 时回退到同一母体的全局分布。
    dist_codes 给定时只用这些基金构造分位分布（P0-2 剔除不可比净值曲线），
    但输出仍覆盖 pool_codes 内的所有基金；不在 dist_codes 的基金得到空分位，
    不会被当成“高分”，也不会污染分布。
    """
    pool = [c for c in cands if pool_codes is None or c["fcode"] in pool_codes]
    if not pool:
        return {}
    dist_pool = [c for c in pool if dist_codes is None or c["fcode"] in dist_codes]
    if not dist_pool:
        dist_pool = list(pool)
    keys = list(METRIC_SPECS) + ["fee", "scale"]
    groups: dict[str, list[Candidate]] = {}
    if use_style:
        for c in dist_pool:
            groups.setdefault(c["style_code"], []).append(c)
    else:
        groups[""] = list(dist_pool)
    global_group = list(dist_pool)
    out: dict[str, dict[str, float | None]] = {c["fcode"]: {} for c in pool}
    for key in keys:
        hib = METRIC_SPECS[key][2] if key in METRIC_SPECS else (key == "scale")
        gmap = scoring.percentile_ranks({c["fcode"]: _raw(c, key) for c in global_group}, hib)
        for style, members in groups.items():
            if len(members) >= T.DYNAMIC_MIN_STYLE_N:
                smap = scoring.percentile_ranks({c["fcode"]: _raw(c, key) for c in members}, hib)
            else:
                smap = gmap
            for c in members:
                out[c["fcode"]][key] = smap.get(c["fcode"])  # 缺失保持 None
    return out


def coverage_meta(cand: Candidate) -> dict[str, Any]:
    """3y/5y 有效覆盖：years 来自 fund_metric 的窗口有效年数，不是“成立年限”。"""
    def years(period: int) -> float | None:
        y = _m(cand, period, "years")
        if y is not None:
            return float(y)
        n = _m(cand, period, "n_obs") or _m(cand, period, "min_points_used")
        return (float(n) / 252.0) if n else None

    y3, y5 = years(1095), years(1825)
    # 用 1095/365.25 作为 3 年有效年数，而不是整数 3：窗口 years 本身就是按
    # 365.25 折算的，2.795 年 / 3.0 = 93.2%，会被 90% 阈值误判为“完整 3 年”。
    r3 = min(1.0, max(0.0, y3 / (1095 / 365.25))) if y3 is not None else 0.0
    r5 = min(1.0, max(0.0, y5 / (1825 / 365.25))) if y5 is not None else 0.0
    # 5 年窗口的 years 只说明“窗口跨了多久”，不说明指标有效：5 年窗口点数不足或
    # 起点缺口时 annual_dec 会是 NULL。长期榜必须同时要求 5 年年化非空，否则
    # “5 年覆盖 79.7%”会被误读成“有 5 年收益证据”。
    has_5y_annual = _m(cand, 1825, "annual_dec") is not None
    return {"effective_years_3y": None if y3 is None else round(y3, 3),
            "coverage_ratio_3y": round(r3, 3),
            "is_full_3y": bool(r3 >= T.FULL_COVERAGE_RATIO),
            "effective_years_5y": None if y5 is None else round(y5, 3),
            "coverage_ratio_5y": round(r5, 3),
            "is_full_5y": bool(r5 >= T.FULL_COVERAGE_RATIO),
            "has_5y_annual": bool(has_5y_annual)}


def score_candidate(cand: Candidate, pcts: dict[str, float | None]) -> dict[str, Any]:
    cov = coverage_meta(cand)
    short_core = scoring.weighted(
        {"return_1m": pcts.get("return_1m"), "return_3m": pcts.get("return_3m"),
         "sharpe": pcts.get("sharpe"), "sortino": pcts.get("sortino"),
         "vol": pcts.get("vol")}, T.SHORT_CORE_WEIGHTS)
    long_pcts = {k: pcts.get(k) for k in T.LONG_CORE_WEIGHTS}
    # 5 年 MDD 只有覆盖 >=90% 时才按“5 年风险”计权；3.1 年的 MDD 不能冒充 5 年。
    # 不足时置 None，由 weighted(renormalize=True) 在剩余维度上归一，并反映到 coverage。
    if not cov["is_full_5y"]:
        long_pcts["mdd_5y"] = None
    long_cov = scoring.coverage(long_pcts, T.LONG_CORE_WEIGHTS)
    long_core = scoring.weighted(long_pcts, T.LONG_CORE_WEIGHTS, renormalize=True)
    # P0-2：3 年覆盖不足 90% 的基金不得进入 long_core；它们走新锐榜，
    # 而不是靠“缺失按 0”被静默判死，也不是靠 1y 风险冒充 3y 风险。
    if (not cov["is_full_3y"]) or long_cov < T.MIN_LONG_COVERAGE:
        long_core = 0.0
    composite = round(T.CORE_COMBO["short"] * short_core + T.CORE_COMBO["long"] * long_core, 4)
    short_formal = scoring.weighted(
        {k: pcts.get(k) for k in T.SHORT_FORMAL_WEIGHTS}, T.SHORT_FORMAL_WEIGHTS)
    long_formal_pcts = {k: pcts.get(k) for k in T.LONG_FORMAL_WEIGHTS}
    long_formal_cov = scoring.coverage(long_formal_pcts, T.LONG_FORMAL_WEIGHTS)
    long_formal = scoring.weighted(long_formal_pcts, T.LONG_FORMAL_WEIGHTS, renormalize=True)
    if (not cov["is_full_3y"]) or long_formal_cov < T.MIN_LONG_COVERAGE:
        long_formal = 0.0
    formal = round(T.FORMAL_COMBO["short"] * short_formal + T.FORMAL_COMBO["long"] * long_formal, 4)
    # P1 长期核心观察榜：只用长期分与长期综合分，不掺 6 个月动量；
    # 目的是回答“把 3y/5y 证据单独拿出来排序，是否比 1 年主导的稳健榜更稳”。
    long_score = round(sum(T.LONG_SCORE_WEIGHTS[k] * (long_formal if k == "long_formal" else long_core)
                           for k in T.LONG_SCORE_WEIGHTS), 4)
    # 排序分（ADR-062）：质量（综合分）+ 动量（6 个月收益分位），只用于排序，不参与准入。
    rank_score = round(sum(T.RANK_WEIGHTS[k] * (composite if k == "composite" else (pcts.get(k) or 0.0))
                           for k in T.RANK_WEIGHTS), 4)
    return {"short_core": short_core, "long_core": long_core, "composite": composite,
            "short_formal": short_formal, "long_formal": long_formal, "formal_score": formal,
            "long_score": long_score, "rank_score": rank_score,
            "coverage": {**cov, "long_core_weight": long_cov, "long_formal_weight": long_formal_cov,
                         "long_core_eligible": bool(cov["is_full_3y"] and long_cov >= T.MIN_LONG_COVERAGE),
                         "long_formal_eligible": bool(cov["is_full_3y"] and long_formal_cov >= T.MIN_LONG_COVERAGE)}}


def history_days(cand: Candidate) -> int | None:
    """净值覆盖天数（首末净值日期之差）。历史不足 3 年的基金走新锐榜，不进正式榜。"""
    first, last = cand.get("nav_first_date"), cand.get("nav_last_date")
    if not first or not last:
        return None
    try:
        return (date.fromisoformat(last) - date.fromisoformat(first)).days
    except ValueError:
        return None


def is_rookie(cand: Candidate) -> bool:
    d = history_days(cand)
    return d is not None and T.ROOKIE_MIN_HISTORY_DAYS <= d < T.ROOKIE_MAX_HISTORY_DAYS


def rookie_score(pcts: dict[str, float]) -> float:
    """新锐分（0–100）：只用 1 年窗口的同风格分位加权，不用 3 年项。"""
    # 分位本身是 0–100 量纲（scoring.percentile_ranks），不要再乘 100。
    return scoring.weighted({k: pcts.get(k, 0.0) for k in T.ROOKIE_WEIGHTS}, T.ROOKIE_WEIGHTS)


def history_3y_check(scores: dict[str, Any]) -> dict[str, Any]:
    """P0-2：3 年完整业绩的显式检查项（不是靠 composite 上限被动淘汰）。"""
    ok = bool((scores.get("coverage") or {}).get("is_full_3y"))
    return {"value": ok, "min": True, "pass": ok, "unit": "bool", "window": "3y",
            "reason": None if ok else "below_min"}


def scale_pit_check(cand: Candidate) -> dict[str, Any]:
    """P0-5：正式榜只接受 as-of 规模/份额，profile 当前值不得冒充 PIT。

    只加在正式榜路径（fixed/enhanced），观察榜仍可展示并带 data_flags；
    这样不会因为缺少规模历史而把可解释的观察对象整个抹掉。
    """
    ok = (cand.get("scale_source") == "scale_history_asof"
          and cand.get("share_scale_source") == "scale_history_asof")
    return {"value": ok, "min": True, "pass": ok, "unit": "bool", "window": "as_of",
            "reason": None if ok else "not_pit"}


def fixed_checks(cand: Candidate, scores: dict[str, Any]) -> dict[str, dict[str, Any]]:
    annual = _m(cand, 365, "annual_dec")
    # v2：删除 scale 项——规模已由 L5（按成立年限分层 + 短债 1.5 亿）单点决定，
    # 固定线里再来一条 2.0 亿只会让 18 只短债在同一维度被二次淘汰（ADR-052）。
    checks = {
        "composite": {"value": scores["composite"], "min": T.FIXED["composite_min"]},
        "annual_1y": {"value": None if annual is None else annual * 100,
                      "min": T.FIXED["annual_1y_min_pct"]},
        "sharpe": {"value": _m(cand, 365, "sharpe"), "min": T.FIXED["sharpe_1y_min"]},
        "calmar": {"value": _m(cand, 365, "calmar_dec"), "min": T.FIXED["calmar_1y_min"]},
        "non_negative": {"value": _m(cand, 365, "non_negative_ratio_pct"),
                         "min": T.FIXED["non_negative_1y_min_pct"]},
        "mdd": {"value": _m(cand, 365, "mdd_dec"), "max": T.FIXED["mdd_1y_max_dec"]},
        "vol": {"value": _m(cand, 365, "vol_dec"), "max": T.FIXED["vol_1y_max_dec"]},
        "fee": {"value": _cost_1y(cand), "max": T.FIXED["fee_max_pct"]},
        "history_3y": history_3y_check(scores),
    }
    for name, c in checks.items():
        c["unit"] = _unit(name)
        if c["value"] is None:
            c["pass"] = False
            c["reason"] = "missing"
        elif "min" in c:
            c["pass"] = c["value"] >= c["min"]
            c["reason"] = None if c["pass"] else "below_min"
        else:
            c["pass"] = c["value"] <= c["max"]
            c["reason"] = None if c["pass"] else "above_max"
    return checks


def dynamic_reasons(dyn_ok: bool, dyn_fails: list[str],
                   dyn_checks: dict[str, Any], ok_n: int, gate_k: int) -> list[str]:
    """只有动态门槛真的没过，才把分项失败写进 reason_codes。

    k-of-n 规则下"某一项低于分位但整体仍通过"属于软标记，已保存在 thresholds.dynamic 里，
    不应污染 reason_codes（否则一只 L8 被经理拦下的基金看起来像被动态门槛拦下）。
    """
    if dyn_ok:
        return []
    out: list[str] = []
    for name in dyn_fails:
        if name == "min_dimensions":
            out.append("dyn_min_dimensions:%d<%d" % (ok_n, gate_k))
        elif isinstance(dyn_checks.get(name), dict):
            out.append("dyn_%s:%s" % (name, dyn_checks[name].get("reason")))
    return out


def manager_gate(cand: Candidate, min_days: int = T.MANAGER_CAREER_MIN_DAYS,
                 require_known: bool = T.MANAGER_CAREER_REQUIRE_KNOWN,
                 min_tenure: int = T.MANAGER_TENURE_MIN_DAYS,
                 require_tenure_known: bool = T.MANAGER_TENURE_REQUIRE_KNOWN) -> tuple[bool, list[str]]:
    """L8 经理后过滤：职业年限 + 现任经理在本基金的最短任职。

    缺值默认放行（缺失≠差），但一旦有值就按弱环节口径阻断：008340 尤之奇
    2026-10-27 上任（6 天）不能再靠“职业年限足够”进入稳健榜。
    """
    blocked = False
    reasons: list[str] = []
    career = cand.get("manager_career_min_days")
    if career is None:
        if require_known:
            blocked = True
            reasons.append("manager_career_unknown")
    elif career < min_days:
        blocked = True
        reasons.append("manager_career_below_%dd:%dd" % (min_days, career))
    tenure = cand.get("manager_tenure_min_days")
    if tenure is None:
        if require_tenure_known:
            blocked = True
            reasons.append("manager_tenure_unknown")
    elif tenure < min_tenure:
        blocked = True
        reasons.append("manager_tenure_below_%dd:%dd" % (min_tenure, tenure))
    return blocked, reasons


def enhanced_checks(cand: Candidate, style: str, period_days: int = 365) -> dict[str, dict[str, Any]]:
    """增强债准入。period_days 默认 365（正式榜口径）；影子榜用 1095 复算同一套阈值。"""
    spec = T.ENHANCED_ADMISSION.get(style)
    if not spec:
        return {}
    annual = _m(cand, period_days, "annual_dec")
    checks = {
        "annual_1y": {"value": None if annual is None else annual * 100, "min": spec["annual"]},
        "sharpe": {"value": _m(cand, period_days, "sharpe"), "min": spec["sharpe"]},
        "calmar": {"value": _m(cand, period_days, "calmar_dec"), "min": spec["calmar"]},
        "mdd": {"value": _m(cand, period_days, "mdd_dec"), "max": spec["mdd"]},
        "positive": {"value": _m(cand, period_days, "positive_ratio_pct"), "min": spec["positive"]},
        "fee": {"value": _cost_1y(cand), "max": spec["fee"]},
        "scale": {"value": cand["asset_scale_billion"], "min": spec["scale"]},
    }
    for name, c in checks.items():
        c["unit"] = _unit(name)
        if c["value"] is None:
            c["pass"] = False
            c["reason"] = "missing"
        elif "min" in c:
            c["pass"] = c["value"] >= c["min"]
            c["reason"] = None if c["pass"] else "below_min"
        else:
            c["pass"] = c["value"] <= c["max"]
            c["reason"] = None if c["pass"] else "above_max"
    return checks


def enhanced_strict_overlay(strict_checks: dict[str, dict[str, Any]],
                            minimum: int) -> dict[str, Any] | None:
    """严格口径通过项数门槛（ADR-080）。

    相对准入（ADR-031）把绝对线换成了同风格分位，单年逆风时可能放进
    绝对指标很弱的基金。历史回测显示，在相对准入之上再要求 7 项严格
    检查至少通过 5 项，可在 2018/2022/2023 改善年化与夏普，其余年份
    基本不变；门槛 6 项会过度集中（2023 只剩 6 只）。minimum=0 表示关闭。
    """
    if minimum <= 0 or not strict_checks:
        return None
    n = sum(1 for ch in strict_checks.values() if ch.get("pass"))
    return {"value": n, "min": int(minimum), "pass": n >= minimum,
            "unit": "count", "window": "strict_7",
            "reason": None if n >= minimum else "below_min"}


def _floor_peer(style: str) -> dict[str, Any]:
    """样本不足时的等价阈值：只用绝对地板（不再叠加分位）。"""
    f = T.ENHANCED_RELATIVE["floors"][style]
    return {"n": 0, "fallback": True, "annual_min_pct": f["annual"], "calmar_min": f["calmar"],
            "sharpe_min": f["sharpe"], "positive_min_pct": f["positive"], "mdd_max": f["mdd"],
            "vol_max": f["vol"], "fee_max_pct": f["fee"], "scale_min": f["scale"],
            "formal_score_min": T.ENHANCED_RELATIVE["formal_score_min"]}


def enhanced_peer_stats(members: list[Candidate]) -> dict[str, dict[str, Any]]:
    """按风格估计增强债的相对门槛（同风格分位 + 绝对地板）。

    母体 = 本轮进入 L6E 的增强债候选（有 1 年期指标者）。样本 < min_peers 时
    该风格回退到地板值，并在 meta 里标 fallback=true，避免用几十只样本估分位。
    """
    qs = T.ENHANCED_RELATIVE["quantiles"]
    groups: dict[str, list[Candidate]] = {}
    for c in members:
        if c["style_code"] in T.ENHANCED_STYLES:
            groups.setdefault(c["style_code"], []).append(c)
    out: dict[str, dict[str, Any]] = {}
    for style, group in groups.items():
        if style not in T.ENHANCED_RELATIVE["floors"]:
            continue
        floor = _floor_peer(style)

        def vals(key: str) -> list[float]:
            if key == "annual_1y":
                raw = [blend_annual(c)[0] for c in group]
            else:
                raw = [_raw(c, key) for c in group]
            return [v for v in raw if v is not None]

        def q(key: str, qq: float) -> float | None:
            vs = vals(key)
            return None if not vs else scoring.quantile(scoring.winsorize(vs), qq)

        def pick_max(base_v: float, qv: float | None) -> float:
            # 分位缺失（该组无此指标）时退回地板，绝不产生 None 阈值
            return round(base_v if qv is None else max(base_v, qv), 6)

        def pick_min(base_v: float, qv: float | None) -> float:
            return round(base_v if qv is None else min(base_v, qv), 6)

        n = len(vals("annual_1y"))
        windows = [blend_annual(c)[1] for c in group]
        window_mix = {k: windows.count(k) for k in ("1y_3y", "1y_only", "3y_only", "none")
                      if windows.count(k)}
        annual_q = q("annual_1y", qs["annual_1y"])
        out[style] = {
            "n": n,
            "fallback": n < T.ENHANCED_RELATIVE["min_peers"],
            "annual_min_pct": pick_max(floor["annual_min_pct"],
                                       None if annual_q is None else annual_q * 100),
            "calmar_min": pick_max(floor["calmar_min"], q("calmar", qs["calmar"])),
            "sharpe_min": pick_max(floor["sharpe_min"], q("sharpe", qs["sharpe"])),
            "positive_min_pct": pick_max(floor["positive_min_pct"], q("positive", qs["positive"])),
            "mdd_max": pick_min(floor["mdd_max"], q("mdd", qs["mdd"])),
            "vol_max": pick_min(floor["vol_max"], q("vol", qs["vol"])),
            "fee_max_pct": pick_min(floor["fee_max_pct"], q("fee", qs["fee"])),
            "scale_min": floor["scale_min"],
            "formal_score_min": floor["formal_score_min"],
            "annual_window": "1y_3y",
            "annual_window_mix": window_mix,
        }
    return out


def enhanced_relative_checks(cand: Candidate, style: str, peers: dict[str, Any] | None,
                             formal_score: float | None,
                             period_days: int = 365) -> dict[str, dict[str, Any]]:
    """增强债相对准入：同风格分位阈值 + 绝对地板 + 正式分下限（ADR-031）。"""
    spec = peers or _floor_peer(style)
    annual, window = blend_annual(cand)
    checks = {
        "annual_1y": {"value": None if annual is None else annual * 100,
                      "min": spec["annual_min_pct"], "window": window},
        "sharpe": {"value": _m(cand, period_days, "sharpe"), "min": spec["sharpe_min"]},
        "calmar": {"value": _m(cand, period_days, "calmar_dec"), "min": spec["calmar_min"]},
        "mdd": {"value": _m(cand, period_days, "mdd_dec"), "max": spec["mdd_max"]},
        "positive": {"value": _m(cand, period_days, "positive_ratio_pct"),
                     "min": spec["positive_min_pct"]},
        "fee": {"value": _cost_1y(cand), "max": spec["fee_max_pct"]},
        "scale": {"value": cand["asset_scale_billion"], "min": spec["scale_min"]},
        "formal_score": {"value": formal_score, "min": spec["formal_score_min"]},
    }
    for name, c in checks.items():
        c["unit"] = _unit(name)
        if c["value"] is None:
            c["pass"] = False
            c["reason"] = "missing"
        elif "min" in c:
            c["pass"] = c["value"] >= c["min"]
            c["reason"] = None if c["pass"] else "below_min"
        else:
            c["pass"] = c["value"] <= c["max"]
            c["reason"] = None if c["pass"] else "above_max"
    return checks


def dynamic_thresholds(mother: list[Candidate], scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """按风格（样本≥15）或全局估计分位阈值；收益取 max(底线,分位)，风险费用取 min(上限,分位)。

    v2（ADR-050/052）：
    * 母体由调用方传入"可投域"（L1–L5.5 通过），不再先留固定线通过者再估分位——
      v1 的母体实测把 composite q70 从 55.7 抬到 71.0，等于加了第三条绝对线；
    * 维度去掉 mdd/vol（与 calmar/vol 相关 0.62–0.89，且固定线已覆盖），保留 6 项；
    * 每条线同时给出 exit 档（enter ∓ DYNAMIC_HYSTERESIS），供已上榜者保留席位。
    """
    out: dict[str, Any] = {"by_style": {}, "global": None, "note": "winsor 5%-95% 仅用于估阈值"}
    groups: dict[str, list[Candidate]] = {}
    for c in mother:
        groups.setdefault(c["style_code"], []).append(c)

    def estimate(members: list[Candidate], label: str) -> dict[str, Any]:
        def vals(key: str) -> list[float]:
            raw = [_raw(c, key) for c in members]
            return [v for v in raw if v is not None]

        def q(key: str, qq: float) -> float | None:
            vs = vals(key)
            if not vs:
                return None
            return round(scoring.quantile(scoring.winsorize(vs), qq), 6)

        comp_vals = [scores[c["fcode"]]["composite"] for c in members]
        # P0-3：综合分阈值改在**完整可投域**上取无条件 q70，再与固定线 62 取 max。
        # 旧实现先筛 >=58 再取 q70，是"条件分位"：q70 被 58 锚死，池子结构一变
        # 通过率就漂移，且 L7 名义 7 维实际只剩 composite 一维。无条件 q70 若低于
        # 固定线，说明动态线没有额外选择性，必须显式记 dynamic_inactive，而不是假装它在工作。
        comp_q = round(scoring.quantile(scoring.winsorize(comp_vals),
                                       T.DYNAMIC_QUANTILES["composite"]), 4) if comp_vals else None
        floor = T.style_floor(label if label in T.STYLE_FLOORS else "DEFAULT")
        annual_q = q("annual_1y", T.DYNAMIC_QUANTILES["annual_1y"])
        th = {
            "n": len(members),
            "composite_min": None if comp_q is None else max(T.FIXED["composite_min"], comp_q),
            "annual_min_pct": None if annual_q is None else max(floor["annual"], annual_q * 100),
            "sharpe_min": _max_or_none(floor["sharpe"], q("sharpe", T.DYNAMIC_QUANTILES["sharpe_1y"])),
            "sortino_min": _max_or_none(floor["sortino"], q("sortino", T.DYNAMIC_QUANTILES["sortino_1y"])),
            "calmar_min": _max_or_none(floor["calmar"], q("calmar", T.DYNAMIC_QUANTILES["calmar_1y"])),
            "non_negative_min_pct": _max_or_none(floor["non_negative"],
                                                    q("non_negative", T.DYNAMIC_QUANTILES["non_negative_1y"])),
            "fee_max_pct": _min_or_none(floor["fee"], q("fee", T.DYNAMIC_QUANTILES["fee"])),
            "composite_q70_raw": comp_q,
            "composite_floor": T.FIXED["composite_min"],
            "dynamic_inactive": bool(comp_q is not None and comp_q < T.FIXED["composite_min"]),
            "style_floor": floor,
        }
        return with_band(th)

    out["global"] = estimate(mother, "DEFAULT")
    for style, members in groups.items():
        if len(members) >= T.DYNAMIC_MIN_STYLE_N:
            out["by_style"][style] = estimate(members, style)
        else:
            out["by_style"][style] = {"n": len(members), "fallback": "global"}
    return out


def _max_or_none(a: float, b: float | None) -> float | None:
    return None if b is None else round(max(a, b), 6)


def _min_or_none(a: float, b: float | None) -> float | None:
    return None if b is None else round(min(a, b), 6)


# 上限型指标：值越小越好，exit 档要"放宽"（+delta）；下限型则 −delta。
_BAND_MAX_KEYS = ("fee_max_pct",)


def with_band(th: dict[str, Any]) -> dict[str, Any]:
    """给每条阈值补一个 exit 档（双侧带滞回，ADR-050）。"""
    out = dict(th)
    h = T.DYNAMIC_HYSTERESIS
    delta = {
        "composite_min": h["composite"],
        "annual_min_pct": h["annual_1y"],
        "sharpe_min": h["sharpe"],
        "sortino_min": h["sortino"],
        "calmar_min": h["calmar"],
        "non_negative_min_pct": h["non_negative_1y"],
        "fee_max_pct": h["fee"],
    }
    for key, d in delta.items():
        v = out.get(key)
        if v is None:
            out[key + "_exit"] = None
        elif key in _BAND_MAX_KEYS:
            out[key + "_exit"] = round(v + d, 6)
        else:
            out[key + "_exit"] = round(v - d, 6)
    return out


# 动态门维度 → (阈值键, 方向)。v2 去掉 mdd/vol，保留 6 项（ADR-052）。
# 阈值分位由 round(...,6) 产生，边界值在 float 下可能 55.305299999999996
# 与 55.3053 比较失败，导致同日榜单翻转。比较时留 1e-9 容差。
DYNAMIC_EPSILON = 1e-9

DYNAMIC_FIELDS = (
    ("composite", "composite_min", "min"),
    ("annual_1y", "annual_min_pct", "min"),
    ("sharpe", "sharpe_min", "min"),
    ("sortino", "sortino_min", "min"),
    ("calmar", "calmar_min", "min"),
    ("non_negative", "non_negative_min_pct", "min"),
    ("fee", "fee_max_pct", "max"),
)


def apply_dynamic(cand: Candidate, scores: dict[str, Any], dyn: dict[str, Any],
                  incumbent: bool = False) -> dict[str, dict[str, Any]]:
    """把动态阈值落到一只基金上。

    incumbent=True 表示该基金已在上批榜单里，用 exit 档（放宽）判定，
    避免"名次没变、榜单变了"的抖动（ADR-050）。
    """
    style_dyn = dyn["by_style"].get(cand["style_code"]) or {}
    use_global = (not style_dyn) or style_dyn.get("fallback") == "global"
    # 再兜一层：风格 scope 里没有可用阈值（历史冻结数据/空壳 scope）也回退全局，
    # 否则 bound=None → threshold_unavailable → 6 维全过，等于免检。
    if not use_global and style_dyn.get("composite_min") is None \
            and style_dyn.get("composite_min_exit") is None:
        use_global = True
    th = dyn["global"] if use_global else style_dyn
    used = "global" if use_global else "style"
    suffix = "_exit" if incumbent else ""
    annual = _m(cand, 365, "annual_dec")
    values = {
        "composite": scores["composite"],
        "annual_1y": None if annual is None else annual * 100,
        "sharpe": _m(cand, 365, "sharpe"),
        "sortino": _m(cand, 365, "sortino"),
        "calmar": _m(cand, 365, "calmar_dec"),
        "non_negative": _m(cand, 365, "non_negative_ratio_pct"),
        "fee": _cost_1y(cand),
    }
    checks: dict[str, Any] = {}
    for name, key, kind in DYNAMIC_FIELDS:
        bound = (th or {}).get(key + suffix)
        if bound is None:
            bound = (th or {}).get(key)
        c: dict[str, Any] = {"value": values[name], "unit": _unit(name), kind: bound,
                             "band": "exit" if incumbent else "enter"}
        if c["value"] is None:
            c["pass"] = False
            c["reason"] = "missing_value"
        elif bound is None:
            # P0-3：缺失阈值不能当"免检"。阈值不可得时按失败处理，并留下
            # threshold_unavailable，供 binding 统计与发布检查发现。
            c["pass"] = False
            c["reason"] = "threshold_unavailable"
        elif kind == "min":
            c["pass"] = c["value"] >= bound - DYNAMIC_EPSILON
            c["reason"] = None if c["pass"] else "below_dynamic"
        else:
            c["pass"] = c["value"] <= bound + DYNAMIC_EPSILON
            c["reason"] = None if c["pass"] else "above_dynamic"
        checks[name] = c
    checks["_meta"] = {"threshold_source": used, "style_n": style_dyn.get("n"),
                       "band": "exit" if incumbent else "enter"}
    return checks


def dynamic_gate(checks: dict[str, Any], rule: str, k: int) -> tuple[bool, list[str], int]:
    """按规则判定动态门槛是否通过。

    返回 (gate_pass, fail_items, dimensions_passed)。
    rule="all"：全部维度通过（架构书字面口径）。
    rule="composite_and_k_of_n"：综合分必须通过，其余 6 维至少 k 项通过。
    v2 起其余维度为 annual/sharpe/sortino/calmar/non_negative/fee，默认 k=5。
    """
    items = [name for name in checks if name != "_meta"]
    fails = [name for name in items if not checks[name]["pass"]]
    if rule == "all":
        return (not fails), fails, len(items) - len(fails)
    others = [name for name in items if name != "composite"]
    ok_n = sum(1 for name in others if checks[name]["pass"])
    if not checks.get("composite", {}).get("pass", False):
        comp_fail = [name for name in fails if name == "composite"]
        return False, (comp_fail or ["composite"]), ok_n
    if ok_n >= k:
        return True, fails, ok_n
    return False, fails + ["min_dimensions"], ok_n


_DQ_METRICS = ("annual_dec", "sharpe", "sortino", "calmar_dec", "positive_ratio_pct",
                "mdd_dec", "vol_dec", "dd_duration_obs")


def data_quality(cand: Candidate, scores: dict[str, Any] | None = None) -> tuple[float, dict[str, Any]]:
    """数据质量分 0–100（ADR-054）。只描述"数据够不够干净"，不参与排序、不因缓释打折。

    v1 的"信心分"是 12 项边际分加权，实测 205 只入选者 median 10.416/满分 10.454、
    99.8% 落在 high，且被当作 _rank 第一排序键 → 恒等键，等于没有排序。
    """
    m365 = cand["metrics"].get(365) or {}
    values = {
        "nav_points": cand.get("nav_points"),
        "gap_max_days": cand.get("gap_max_days"),
        "freshness_days": cand.get("freshness_days"),
        "metrics_filled": sum(1 for k in _DQ_METRICS if m365.get(k) is not None),
        "profile_filled": sum(1 for k in ("asset_scale_billion", "recurring_fee_pct", "manager_count")
                              if cand.get(k) is not None),
    }
    total = 0.0
    detail: dict[str, Any] = {}
    for key, (target, points, direction) in T.DATA_QUALITY_ITEMS.items():
        v = values.get(key)
        if v is None:
            detail[key] = {"value": None, "credit": 0.0}
            continue
        if direction == "lower":
            ratio = target / v if v > 0 else 1.0
        else:
            ratio = v / target if target else 0.0
        credit = round(points * max(0.0, min(1.0, ratio)), 4)
        detail[key] = {"value": v, "target": target, "credit": credit}
        total += credit
    total = round(total, 3)
    tier = ("high" if total >= T.DATA_QUALITY_HIGH
            else ("mid" if total >= T.DATA_QUALITY_MID else "low"))
    return total, {"tier": tier, "items": detail, "max_possible": 100.0}


# ── 批次冻结（ADR-050）：同一 batch 内所有 run 共用一套分位阈值 ──────────
_QUANTILE_BY_KEY = {
    "composite_min": "composite", "annual_min_pct": "annual_1y", "sharpe_min": "sharpe_1y",
    "sortino_min": "sortino_1y", "calmar_min": "calmar_1y",
    "non_negative_min_pct": "non_negative_1y", "fee_max_pct": "fee",
}


def freeze_thresholds(conn, batch_id: str, dyn: dict[str, Any], anchor_date: str,
                      config_hash: str | None = None, formula_contract: str | None = None,
                      note: str | None = None, baseline_run_id: str | None = None) -> int:
    """把本次估计出的分位阈值与滞回基线固化到 screen_batch，返回写入行数。

    baseline_run_id 是批次创建时确定的“上一批”run；同一 batch 的所有 run
    都必须用它算 incumbents/board_diff，不能按 created_at 临时找最近一次，
    否则第一次与第二次重跑会看到不同的前序榜单（ADR-081）。
    """
    now = db.now_iso()
    existing = conn.execute("SELECT screen_contract FROM screen_batch WHERE batch_id=?",
                            (batch_id,)).fetchone()
    if existing and existing["screen_contract"] not in (None, T.SCREEN_CONTRACT):
        raise ValueError("batch_screen_contract_mismatch: batch=%s frozen=%s current=%s"
                         % (batch_id, existing["screen_contract"], T.SCREEN_CONTRACT))
    conn.execute(
        "INSERT INTO screen_batch(batch_id, profile, anchor_business_date, config_hash,"
        " formula_contract, screen_contract, frozen_at, status, note, baseline_run_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(batch_id) DO UPDATE SET frozen_at=excluded.frozen_at,"
        " status=excluded.status,"
        " screen_contract=COALESCE(screen_batch.screen_contract, excluded.screen_contract),"
        " note=COALESCE(excluded.note, screen_batch.note),"
        " baseline_run_id=COALESCE(screen_batch.baseline_run_id, excluded.baseline_run_id)",
        (batch_id, T.SCREEN_PROFILE, anchor_date, config_hash, formula_contract,
         T.SCREEN_CONTRACT, now, "frozen", note, baseline_run_id))
    rows = []
    # 小样本风格只有 {"n":…, "fallback":"global"} 标记，没有阈值：不能写进 screen_threshold。
    # 写了会让 load_frozen 把它们读成"风格阈值存在但全为 NULL"，apply_dynamic 里
    # threshold_unavailable 一律放行——实测同批次重跑 watch 30→32，等于给小风格开后门。
    scopes = [("global", dyn["global"])] + [(k, v) for k, v in dyn["by_style"].items()
                                            if not v.get("fallback")]
    for scope, th in scopes:
        for metric, qname in _QUANTILE_BY_KEY.items():
            rows.append((batch_id, scope, metric, th.get(metric), th.get(metric + "_exit"),
                         th.get("n"), T.DYNAMIC_QUANTILES.get(qname), now))
    conn.execute("DELETE FROM screen_threshold WHERE batch_id=?", (batch_id,))
    conn.executemany(
        "INSERT INTO screen_threshold(batch_id, scope, metric, enter_value, exit_value, n,"
        " quantile, created_at) VALUES(?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def load_frozen(conn, batch_id: str | None) -> dict[str, Any] | None:
    """读回冻结阈值；没有则返回 None（调用方回退到实时估计）。"""
    if not batch_id:
        return None
    rows = conn.execute(
        "SELECT scope, metric, enter_value, exit_value, n FROM screen_threshold"
        " WHERE batch_id=?", (batch_id,)).fetchall()
    if not rows:
        return None
    glob: dict[str, Any] = {}
    by_style: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["scope"] == "global":
            target = glob
        else:
            # 防御历史脏数据：全 NULL 的风格 scope 视同不存在，让它回退全局。
            if r["enter_value"] is None and r["exit_value"] is None:
                by_style.setdefault(r["scope"], {"fallback": "global"})
                continue
            target = by_style.setdefault(r["scope"], {})
        target[r["metric"]] = r["enter_value"]
        target[r["metric"] + "_exit"] = r["exit_value"]
        if r["n"] is not None:
            target["n"] = r["n"]
    by_style = {k: v for k, v in by_style.items() if not v.get("fallback")}
    return {"global": glob, "by_style": by_style, "frozen": True}


def _previous_business_run(conn, run_id: str, business_date: str | None) -> str | None:
    """取当前业务日之前最近一次 done run；没有历史业务日时退回最近一次 run。

    只允许严格早于当前 business_date，避免未来/实验 run 反向污染当天的滞回基线。
    """
    if business_date:
        row = conn.execute(
            "SELECT run_id FROM screen_run WHERE run_id<>? AND status='done'"
            " AND business_date<? ORDER BY business_date DESC, created_at DESC, run_id DESC"
            " LIMIT 1", (run_id, business_date)).fetchone()
        if row:
            return row["run_id"]
    row = conn.execute(
        "SELECT run_id FROM screen_run WHERE run_id<>? AND status='done'"
        " ORDER BY created_at DESC, run_id DESC LIMIT 1", (run_id,)).fetchone()
    return row["run_id"] if row else None


def _resolve_baseline(conn, run_id: str, business_date: str | None,
                      batch_id: str | None) -> str | None:
    """返回并冻结该 batch 的滞回基线。

    优先级：
    1. screen_batch.baseline_run_id 已冻结 → 直接复用（同批次重跑完全一致）；
    2. 该 batch 已有更早的 done run → 用最早的一次（兼容 0019 之前创建的批次）；
    3. 否则用当前业务日之前最近一次 done run（新批次的第一轮）。
    """
    if not batch_id:
        return _previous_business_run(conn, run_id, business_date)
    row = conn.execute(
        "SELECT baseline_run_id FROM screen_batch WHERE batch_id=?", (batch_id,)).fetchone()
    if row and row["baseline_run_id"]:
        return row["baseline_run_id"]
    row = conn.execute(
        "SELECT run_id FROM screen_run WHERE run_id<>? AND status='done' AND batch_id=?"
        " ORDER BY created_at ASC, run_id ASC LIMIT 1", (run_id, batch_id)).fetchone()
    baseline = row["run_id"] if row else _previous_business_run(conn, run_id, business_date)
    # 批次可能尚未 freeze（第一轮），此时 UPDATE 影响 0 行，由 freeze_thresholds 写入。
    conn.execute("UPDATE screen_batch SET baseline_run_id=? WHERE batch_id=?",
                 (baseline, batch_id))
    conn.commit()
    return baseline


def board_picks(conn, run_id: str | None) -> dict[str, list[str]]:
    if not run_id:
        return {}
    out: dict[str, list[str]] = {}
    for r in conn.execute("SELECT board, fcode FROM screen_pick WHERE run_id=?"
                          " ORDER BY board, rank", (run_id,)):
        out.setdefault(r["board"], []).append(r["fcode"])
    return out


def board_diff(conn, run_id: str, picks: dict[str, list[dict[str, Any]]],
               prev_run_id: str | None) -> dict[str, Any]:
    """本批 vs 上批的进出明细，用于发布门禁与"名单漂移"归因。"""
    prev = board_picks(conn, prev_run_id)
    out: dict[str, Any] = {"run_id": run_id, "prev_run_id": prev_run_id, "boards": {},
                           "dropped": picks.get("_dropped", [])}
    for board, items in picks.items():
        if board.startswith("_"):
            continue
        cur = [it["fcode"] for it in items]
        old = prev.get(board, [])
        cur_set, old_set = set(cur), set(old)
        out["boards"][board] = {
            "n": len(cur), "prev_n": len(old), "kept": len(cur_set & old_set),
            "added": [c for c in cur if c not in old_set],
            "removed": [c for c in old if c not in cur_set],
        }
    return out


def quality_of(cand: Candidate, sc: dict[str, Any], dyn: dict[str, Any],
               enh_peers: dict[str, Any], gate_rule: str, gate_k: int,
               incumbent: bool = False, strict_min: int = 0) -> tuple[bool, dict[str, Any]]:
    """判定一只"进不了母体"的基金质量是否达标（L2 观察榜 / L3 受限榜用）。

    只做质量判定，不给正式名额；不套用单一原因缓释（保守口径）。
    """
    style = cand["style_code"]
    if style in T.ENHANCED_STYLES:
        checks = enhanced_relative_checks(cand, style, enh_peers.get(style), sc.get("formal_score"))
        overlay = enhanced_strict_overlay(enhanced_checks(cand, style), strict_min)
        if overlay:
            checks["strict_overlay"] = overlay
        fails = [k for k, v in checks.items() if not v["pass"]]
        return (not fails), {"fixed_or_admission": checks, "dynamic": None}
    checks = fixed_checks(cand, sc)
    fails = [k for k, v in checks.items() if not v["pass"]]
    dyn_checks = apply_dynamic(cand, sc, dyn, incumbent=incumbent)
    ok, dyn_fails, ok_n = dynamic_gate(dyn_checks, gate_rule, gate_k)
    dyn_checks["_meta"].update({"rule": gate_rule, "min_dimensions": gate_k,
                                "dimensions_passed": ok_n, "gate_pass": ok})
    return (not fails and ok), {"fixed_or_admission": checks, "dynamic": dyn_checks}


def run_screen(conn, cfg, run_id: str, business_date: str, formula_contract: str,
               batch_id: str | None = None) -> dict[str, Any]:
    """跑一轮完整筛选。

    batch_id 给定时：分位阈值优先取该批次冻结值（同日重跑必须一致），
    并对上一批已上榜者套用 exit 档滞回（ADR-050）。
    """
    types = fundtypes.resolve(cfg=cfg)
    # 有效净值日：回看窗口内覆盖率 >=90% 的最新日期。11-03 只有 185 只
    # 披露 11-02 净值时，as_of 必须是 10-30，而不是 MAX(nav_date)=11-02。
    fresh = freshness.assess(conn, business_date=business_date, types=types,
                             formula_contract=formula_contract)
    as_of = fresh.get("as_of") or business_date
    nav_age = int((cfg.get("quality", default={}) or {}).get("nav_age_limit_days", 10))
    screen_cfg = cfg.get("screen", default={}) or {}
    strict_min = int(screen_cfg.get("enhanced_strict_min", 0) or 0)
    batch_id = batch_id or screen_cfg.get("batch_id")
    # P0-1：一个 batch 只允许一个契约。旧 batch 若已冻结在 v9，禁止用 v10 重跑，
    # 否则 run 标 v10、batch 仍是 v9，发布门禁会把换口径误读成换基金。
    if batch_id:
        row = conn.execute("SELECT screen_contract, formula_contract FROM screen_batch"
                           " WHERE batch_id=?", (batch_id,)).fetchone()
        if row and row["screen_contract"] and row["screen_contract"] != T.SCREEN_CONTRACT:
            raise ValueError("batch_screen_contract_mismatch: batch=%s frozen=%s current=%s"
                             % (batch_id, row["screen_contract"], T.SCREEN_CONTRACT))
        # P0-1：指标契约同样必须与批次一致。旧批次冻结的阈值里没有 non_negative_min_pct，
        # 若允许 11.5 重跑，apply_dynamic 会把缺失阈值当成“免检”，等于悄悄放宽门槛。
        if row and row["formula_contract"] and row["formula_contract"] != formula_contract:
            raise ValueError("batch_formula_contract_mismatch: batch=%s frozen=%s current=%s"
                             % (batch_id, row["formula_contract"], formula_contract))
    cands = _load_candidates(conn, business_date, formula_contract, types, as_of=as_of)

    decisions: dict[str, dict[str, Any]] = {}
    mother: list[Candidate] = []
    l1_pass: list[Candidate] = []
    for c in cands:
        layer, reasons = base_gate(c, as_of, nav_age)
        decisions[c["fcode"]] = {"layer_reached": layer, "passed": 0, "reasons": list(reasons),
                                 "style_code": c["style_code"],
                                 "data_flags": {"metric_end_date": c.get("metric_end_date"),
                                                "metric_stale_days": c.get("metric_stale_days"),
                                                "scale_source": c.get("scale_source"),
                                                "scale_asof": c.get("scale_asof"),
                                                "scale_lag_days": c.get("scale_lag_days"),
                                                "share_scale_source": c.get("share_scale_source"),
                                                "share_scale_asof": c.get("share_scale_asof")}}
        if layer != "L1":
            l1_pass.append(c)
        if not reasons:
            mother.append(c)

    # P0-4：正式榜分位只在可投域（L1–L5.5 通过者）内计算；观察/受限榜用
    # L1 通过者分布评分，避免 87/88 个月摊余成本定开债把 q70/q85 抬高。
    clean_mother = {c["fcode"] for c in mother if c.get("metric_comparable", True)}
    pcts = compute_percentiles(mother, use_style=True, dist_codes=clean_mother)
    scores = {c["fcode"]: score_candidate(c, pcts[c["fcode"]]) for c in mother}
    clean_l1 = {c["fcode"] for c in l1_pass if c.get("metric_comparable", True)}
    pcts_all = compute_percentiles(l1_pass, use_style=True, dist_codes=clean_l1)
    scores_all = {c["fcode"]: score_candidate(c, pcts_all[c["fcode"]]) for c in l1_pass}
    for c in cands:
        sc = scores.get(c["fcode"]) or scores_all.get(c["fcode"])
        if sc is not None:
            decisions[c["fcode"]]["scores"] = sc

    # 第一轮：固定线 / 增强债相对准入（增强债走独立路径，不参与动态分位）
    # 增强债的严格绝对口径仍然逐只计算，只作为诊断存进 thresholds_json.enhanced_strict，
    # 用来解释"为什么严格口径下一只都不通过"（ADR-031）。
    enh_peers = enhanced_peer_stats(mother)
    strict_checks: dict[str, dict[str, dict[str, Any]]] = {}
    fixed_state: dict[str, tuple[dict, list[str], str, str]] = {}
    for c in mother:
        sc = scores[c["fcode"]]
        style = c["style_code"]
        if style in T.ENHANCED_STYLES:
            strict = enhanced_checks(c, style)
            strict_checks[c["fcode"]] = strict
            checks = enhanced_relative_checks(c, style, enh_peers.get(style),
                                              sc.get("formal_score"))
            checks["history_3y"] = history_3y_check(sc)
            checks["scale_pit"] = scale_pit_check(c)
            overlay = enhanced_strict_overlay(strict, strict_min)
            if overlay:
                checks["strict_overlay"] = overlay
            board, path = "bond_enhanced", "enhanced_admission"
        else:
            checks, board, path = fixed_checks(c, sc), "bond_stable", "fixed_then_dynamic"
            checks["scale_pit"] = scale_pit_check(c)
        fixed_state[c["fcode"]] = (checks, [k for k, v in checks.items() if not v["pass"]], board, path)
    # L6D 单一原因边界缓释（ADR-047）：恰好一项硬比较失败且满足边界条件时放行。
    # 增强债走独立准入，不适用缓释；两项及以上失败一律不缓释。
    screen_cfg = cfg.get("screen", default={}) or {}
    relief_state: dict[str, dict[str, Any]] = {}
    # P0-3：动态门 binding 统计。旧实现只记“通过/不通过”，无法回答 7 个维度里
    # 到底哪一维在起约束；L7 实测 201 条退出全部只失败 composite。这里同时记
    # 全母体与“固定线已过”的子集：只有后者才说明动态门真的改变了结果。
    dyn_binding: Counter[str] = Counter()
    dyn_binding_fixed_pass: Counter[str] = Counter()
    dyn_blocked_fixed_pass = 0
    dyn_blocked_composite_only = 0
    dyn_threshold_unavailable = 0
    if bool(screen_cfg.get("relief_enabled", True)):
        for c in mother:
            code = c["fcode"]
            checks, fails, board, path = fixed_state[code]
            if path != "fixed_then_dynamic" or len(fails) != 1:
                continue
            allowed, rtype, detail = relief_mod.try_relief(checks, fails, scores[code])
            # 未放行也要留痕：relief_json 记录"检查过但被哪一条挡住"，与"没检查"区分
            relief_state[code] = {"type": rtype, "allowed": bool(allowed), "detail": detail}
            if allowed:
                fixed_state[code] = (checks, [], board, path)
    # 动态分位母体 = 可投域（基础门禁 L1–L5.5 通过者），**不再**先留固定线通过者。
    # v1 用"固定线通过者"当母体，等于在分位线之前又加了一条绝对线：实测把 composite
    # 的 q70 从 55.7 抬到 71.0，并把 2021/2024 的通过率压到 19%/43%（ADR-052）。
    # 增强债走独立准入，仍不进稳健榜母体（否则转债/二级债会抬高分位线）。
    dyn_mother = [c for c in mother if c["style_code"] not in T.ENHANCED_STYLES]
    # 滞回基线必须先于阈值冻结解析：同一 batch 的第一次 run 把“上一批”写进
    # screen_batch.baseline_run_id，后续重跑无论 created_at 如何都复用它（ADR-081）。
    prev_run = _resolve_baseline(conn, run_id, business_date, batch_id)
    dyn = load_frozen(conn, batch_id)
    frozen = dyn is not None
    if not frozen:
        dyn = dynamic_thresholds(dyn_mother, scores)
        if batch_id:
            # 首次跑该批次：把阈值与基线一起固化，同日重跑直接复用（ADR-050/081）
            freeze_thresholds(conn, batch_id, dyn, as_of, cfg.config_hash, formula_contract,
                              baseline_run_id=prev_run)
            frozen = True
    gate_rule = str(screen_cfg.get("dynamic_gate_rule", T.DYNAMIC_GATE_RULE))
    gate_k = int(screen_cfg.get("dynamic_min_dimensions_passed", T.DYNAMIC_MIN_DIMENSIONS_PASSED))
    # 只有正式榜（稳健/增强）成员享 exit 档；观察榜/受限榜不算"已上榜"，
    # 否则 L2/L3 的边缘基金每轮都会被自己的 exit 档反复续命（实测 watch 148→151）。
    _prev = board_picks(conn, prev_run)
    incumbents = {code for b in T.FORMAL_BOARDS for code in _prev.get(b, [])}
    mgr_min_days = int(screen_cfg.get("manager_career_min_days", T.MANAGER_CAREER_MIN_DAYS))
    mgr_require_known = bool(screen_cfg.get("manager_career_require_known",
                                            T.MANAGER_CAREER_REQUIRE_KNOWN))
    mgr_tenure_min_days = int(screen_cfg.get("manager_tenure_min_days",
                                             T.MANAGER_TENURE_MIN_DAYS))
    mgr_tenure_require_known = bool(screen_cfg.get("manager_tenure_require_known",
                                                   T.MANAGER_TENURE_REQUIRE_KNOWN))
    for c in mother:
        code = c["fcode"]
        sc = scores[code]
        checks, fails, board, path = fixed_state[code]
        reasons = ["%s:%s" % (k, checks[k]["reason"]) for k in fails]
        if code in relief_state and relief_state[code]["allowed"]:
            reasons.append("relief:%s" % relief_state[code]["type"])
        if path == "fixed_then_dynamic" and fails:
            # 新锐榜（P1-1）：历史 1–3 年者单开一榜。P0-2 之后 3 年覆盖不足
            # 会显式失败 history_3y；composite 因 long_core=0 也通常同时失败。
            # 只允许这两类原因，其他固定线失败仍不进新锐榜。
            # 不占正式名额、不进正式榜。
            rookie_fails = {"composite", "history_3y"}
            if fails and set(fails) <= rookie_fails and is_rookie(c):
                sc["rookie"] = rookie_score(pcts[code])
                conf, conf_detail = data_quality(c, sc)
                decisions[code]["confidence"] = conf
                decisions[code]["confidence_detail"] = conf_detail
                if sc["rookie"] >= T.ROOKIE_MIN_SCORE and conf >= T.ROOKIE_DATA_QUALITY_MIN:
                    decisions[code]["board"] = T.BOARD_ROOKIE
                    decisions[code]["quality_passed"] = 1
            decisions[code].update({"layer_reached": "L6C", "reasons": reasons,
                                    "thresholds": checks, "scores": sc,
                                    "relief": relief_state.get(code)})
            continue
        if path == "enhanced_admission":
            dyn_checks: dict[str, Any] = {"_meta": {
                "threshold_source": "enhanced_relative",
                "rule": "admission", "gate_pass": not fails,
                "peers": (enh_peers.get(c["style_code"]) or {}).get("n"),
                "fallback": (enh_peers.get(c["style_code"]) or {}).get("fallback", True)}}
            dyn_fails: list[str] = []
            dyn_ok = True
        else:
            dyn_checks = apply_dynamic(c, sc, dyn, incumbent=code in incumbents)
            dyn_ok, dyn_fails, ok_n = dynamic_gate(dyn_checks, gate_rule, gate_k)
            dyn_checks["_meta"].update({"rule": gate_rule, "min_dimensions": gate_k,
                                        "dimensions_passed": ok_n, "gate_pass": dyn_ok})
            # 只统计“真的被动态门挡住”的失败；k=5 下软失败不构成 binding。
            if not dyn_ok:
                for name in dyn_fails:
                    dyn_binding[name] += 1
                    if not fails:
                        dyn_binding_fixed_pass[name] += 1
                if not fails:
                    dyn_blocked_fixed_pass += 1
                    if set(dyn_fails) == {"composite"}:
                        dyn_blocked_composite_only += 1
            for name, chk in dyn_checks.items():
                if name != "_meta" and chk.get("reason") == "threshold_unavailable":
                    dyn_threshold_unavailable += 1
            reasons.extend(dynamic_reasons(dyn_ok, dyn_fails, dyn_checks, ok_n, gate_k))
        # 数据质量分（ADR-054）：只描述数据干净程度，不参与排序，也不因缓释打折。
        conf, conf_detail = data_quality(c, sc)
        passed = 1 if (not fails and dyn_ok) else 0
        # L8 经理后过滤：职业年限（现任经理中最短者，弱环节口径）。缺值不伪造低分。
        career = c.get("manager_career_min_days")
        mgr_block = False
        if passed:
            mgr_block, mgr_reasons = manager_gate(
                c, mgr_min_days, mgr_require_known,
                mgr_tenure_min_days, mgr_tenure_require_known)
            if mgr_block:
                reasons.extend(mgr_reasons)
                passed = 0
        # P0-3 独立风险门：经理门之后、进入 L9 之前做持仓/压力体检。block 的
        # 基金不是“收益差”，而是“收益可能来自没被指标捕获的集中度/信用风险”，
        # 因此单独记 L8R，保留风险明细供复盘。
        risk = None
        risk_block = False
        if passed:
            risk = risk_gate.assess(conn, code, c["style_code"], as_of,
                                    name=c.get("name"))
            if risk["summary"]["blocked"]:
                reasons.extend("risk:%s" % f for f in risk["summary"]["blocked_flags"])
                passed = 0
                risk_block = True
        if passed:
            layer_reached = "L9"
        elif risk_block:
            layer_reached = "L8R"
        elif mgr_block:
            layer_reached = "L8"
        elif path == "enhanced_admission":
            layer_reached = "L6E"
        elif fails:
            layer_reached = "L6C"
        else:
            layer_reached = "L7"
        decisions[code].update({
            "layer_reached": layer_reached,
            "passed": passed, "reasons": reasons, "board": board if passed else None,
            "thresholds": {"fixed_or_admission": checks, "dynamic": dyn_checks,
                           "enhanced_strict": strict_checks.get(code),
                           "enhanced_relative_meta": enh_peers.get(c["style_code"])},
            "scores": sc, "confidence": conf, "confidence_detail": conf_detail,
            "relief": relief_state.get(code), "risk": risk,
            "manager": {"count": c.get("manager_count"), "career_min_days": career,
                        "tenure_min_days": c.get("manager_tenure_min_days"),
                        "career_max_days": c.get("manager_career_max_days"),
                        "power_score": c.get("manager_power_score")},
        })

    g = dyn.get("global") or {}
    dynamic_binding = {
        "rule": gate_rule,
        "min_dimensions": gate_k,
        "composite_inactive": bool(g.get("dynamic_inactive")),
        "composite_q70_raw": g.get("composite_q70_raw"),
        "composite_floor": g.get("composite_floor"),
        "composite_min": g.get("composite_min"),
        "binding_counts_all": dict(sorted(dyn_binding.items())),
        "binding_counts_fixed_pass": dict(sorted(dyn_binding_fixed_pass.items())),
        "fixed_pass_blocked_by_dynamic": dyn_blocked_fixed_pass,
        "fixed_pass_blocked_composite_only": dyn_blocked_composite_only,
        "threshold_unavailable": dyn_threshold_unavailable,
        "styles_with_own": sorted(k for k, v in dyn.get("by_style", {}).items()
                                  if not v.get("fallback")),
        "styles_dynamic_inactive": sorted(
            k for k, v in dyn.get("by_style", {}).items()
            if not v.get("fallback") and v.get("dynamic_inactive")),
    }

    # L2/L3 质量扫描（ADR-051/053）：合格但当期买不到（L2）/持有期受限（L3）者
    # 分别进观察榜与受限榜。只做质量判定，不给正式名额，避免"买不到"与"不够好"混为一谈。
    for c in cands:
        d = decisions[c["fcode"]]
        if d["layer_reached"] not in ("L2", "L3"):
            continue
        code = c["fcode"]
        sc_watch = scores_all.get(code) or scores.get(code) or {}
        ok, ths = quality_of(c, sc_watch, dyn, enh_peers, gate_rule, gate_k,
                             incumbent=code in incumbents, strict_min=strict_min)
        risk = risk_gate.assess(conn, code, c["style_code"], as_of,
                                name=c.get("name"))
        d["risk"] = risk
        # 观察榜/受限榜不是正式推荐：风险门 block 只记录风险原因，不把基金从
        # 观察榜里删掉。否则一只基金因缺最新持仓就被藏起来，用户看不到“它为什么
        # 被风险门拦住”；正式榜的 block 已在 L8R 单独执行，不在这里重复淘汰。
        if ok and risk["summary"]["blocked"]:
            d["reasons"] = list(d.get("reasons") or []) + [
                "risk:%s" % f for f in risk["summary"]["blocked_flags"]]
        d["quality_passed"] = 1 if ok else 0
        d["thresholds"] = ths
        d["scores"] = sc_watch
        d["confidence"], d["confidence_detail"] = data_quality(c, sc_watch)
        if ok:
            d["board"] = T.BOARD_WATCH if d["layer_reached"] == "L2" else T.BOARD_LOCKED

    target_max = int(screen_cfg.get("formal_target_max", T.FORMAL_TARGET_MAX))
    group_min = float(screen_cfg.get("group_min_ratio", T.GROUP_MIN_RATIO))
    # P2-2：core10 需要日收益做相关性选样；只给通过的核型候选加载 365 天，
    # 不在 _rank 里连库，保持排序函数可单测。
    series_by_code: dict[str, list[tuple[str, float]]] = {}
    corr_start = (date.fromisoformat(as_of) - timedelta(days=365)).isoformat()
    for code, d in decisions.items():
        if d.get("passed") and (d.get("style_code") in T.CORE_STYLES):
            s = load_daily_returns(conn, code, corr_start, as_of)
            if s:
                series_by_code[code] = s
    picks = _rank(decisions, scores, cands, target_max=target_max, group_min_ratio=group_min,
                  series_by_code=series_by_code)
    diff = board_diff(conn, run_id, picks, prev_run)
    diff_path = None
    binding_path = None
    try:
        out_dir = Path(cfg.report_root) / "screen" / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "board_diff.json"
        path.write_text(json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8")
        diff_path = str(path)
        bpath = out_dir / "dynamic_binding.json"
        bpath.write_text(json.dumps(dynamic_binding, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        binding_path = str(bpath)
    except OSError:
        diff_path = None
        binding_path = None
    _persist(conn, cfg, run_id, decisions, picks, cands, as_of, formula_contract,
             base_pass=len(mother), business_date=business_date,
             batch_id=batch_id, diff_path=diff_path, pcts=pcts, pcts_all=pcts_all,
             freshness_info=fresh)
    pool_rows = pool_mod.build_pool(
        conn, run_id, mother, scores, decisions,
        pool_min=int(screen_cfg.get("research_pool_min", 24)),
        pool_max=int(screen_cfg.get("research_pool_max", 36)))
    return {
        "run_id": run_id, "as_of": as_of, "universe": len(cands), "base_pass": len(mother),
        "relief": sum(1 for v in relief_state.values() if v["allowed"]),
        "relief_checked": len(relief_state), "research_pool": len(pool_rows),
        "formal": sum(1 for d in decisions.values() if d.get("passed")),
        "l9_pass": sum(1 for d in decisions.values() if d.get("passed")),
        "published": (len(picks.get(T.BOARD_STABLE, []))
                      + len(picks.get(T.BOARD_ENHANCED, []))),
        "locked": sum(1 for d in decisions.values() if d["layer_reached"] == "L3"),
        "locked_qualified": sum(1 for d in decisions.values() if d.get("board") == T.BOARD_LOCKED),
        "watch": sum(1 for d in decisions.values() if d.get("board") == T.BOARD_WATCH),
        "rookie": sum(1 for d in decisions.values() if d.get("board") == T.BOARD_ROOKIE),
        "core": len(picks.get(T.BOARD_CORE, [])),
        "core10": len(picks.get(T.BOARD_CORE10, [])),
        "long": len(picks.get(T.BOARD_LONG, [])),
        "boards": {b: len(v) for b, v in picks.items() if not b.startswith("_")},
        "batch_id": batch_id, "batch_frozen": frozen, "board_diff": diff_path,
        "dynamic_binding": dynamic_binding, "dynamic_binding_path": binding_path,
        "nav_as_of": fresh.get("as_of"), "metric_as_of": (fresh.get("metric") or {}).get("metric_as_of"),
        "freshness_status": fresh.get("status"), "freshness_warnings": fresh.get("warnings"),
        "board_diff_summary": {b: {"added": len(v["added"]), "removed": len(v["removed"])}
                               for b, v in diff["boards"].items()},
        "uncovered": T.UNCOVERED_SCOPE,
        "dynamic_thresholds": {
            "styles_with_own": sorted(k for k, v in dyn["by_style"].items()
                                      if not v.get("fallback")),
            "global": dyn["global"], "frozen": frozen},
    }


def _core10_local_search(kept: list[dict[str, Any]],
                         eligible: list[tuple[str, dict[str, Any], Candidate, str]],
                         style_caps: dict[str, int], comp_cap: int,
                         series_by_code: dict[str, list[tuple[str, float]]]) -> list[dict[str, Any]]:
    """P2-2b：贪婪选样后的有界单换局部搜索。

    只接受“排序分损失 <= CORE10_SWAP_MAX_SCORE_LOSS 且 effective bets 提升
    >= CORE10_SWAP_MIN_GAIN”的换入；每轮换 1 只，最多 CORE10_SWAP_MAX_ROUNDS 轮。
    风格/家族/公司上限与单对相关 <= CORE10_MAX_PAIR_CORR 全部沿用，不放松约束。
    这是局部改进，不保证全局最优；达不到目标时仍由 _core10_risk_budget 标 downgraded。
    """
    if len(kept) < 2 or not eligible:
        return kept
    cache: dict[tuple[str, str], float | None] = {}

    def rho(a: str, b: str) -> float | None:
        key = (a, b) if a < b else (b, a)
        if key not in cache:
            value, _ = pair_corr(series_by_code.get(a) or [], series_by_code.get(b) or [],
                                 min_obs=T.CORE10_MIN_COMMON_OBS)
            cache[key] = None if value is None else float(value)
        return cache[key]

    def eff_bets(codes: list[str]) -> float | None:
        rhos = [r for i, a in enumerate(codes) for b in codes[i + 1:]
                if (r := rho(a, b)) is not None]
        if not rhos:
            return None
        return effective_bets(len(codes), sum(rhos) / len(rhos))

    current_eff = eff_bets([item["fcode"] for item in kept])
    if current_eff is None:
        return kept
    meta = {code: (d, c, style) for code, d, c, style in eligible}
    for _round in range(int(T.CORE10_SWAP_MAX_ROUNDS)):
        best = None
        kept_codes = {item["fcode"] for item in kept}
        for i, item in enumerate(kept):
            removed = item["fcode"]
            base = [x for x in kept if x["fcode"] != removed]
            base_codes = [x["fcode"] for x in base]
            style_seen: dict[str, int] = {}
            fam_seen: set[str] = set()
            comp_seen: dict[str, int] = {}
            for x in base:
                _xd, xc, xs = meta.get(x["fcode"], ({}, {}, ""))
                style_seen[xs] = style_seen.get(xs, 0) + 1
                fam_seen.add((xc.get("family_id") if xc else None) or x["fcode"])
                comp = (xc.get("company_name") if xc else "") or ""
                if comp:
                    comp_seen[comp] = comp_seen.get(comp, 0) + 1
            removed_score = float((item.get("scores") or {}).get("rank_score") or 0.0)
            for code, d, c, style in eligible:
                if code in kept_codes:
                    continue
                cap = style_caps.get(style)
                if cap is not None and style_seen.get(style, 0) + 1 > cap:
                    continue
                fam = (c.get("family_id") if c else None) or code
                if fam in fam_seen:
                    continue
                comp = (c.get("company_name") if c else "") or ""
                if comp and comp_seen.get(comp, 0) + 1 > comp_cap:
                    continue
                if any((r := rho(code, other)) is not None and r > T.CORE10_MAX_PAIR_CORR
                       for other in base_codes):
                    continue
                cand_score = float((d.get("scores") or {}).get("rank_score") or 0.0)
                loss = removed_score - cand_score
                if loss > float(T.CORE10_SWAP_MAX_SCORE_LOSS) + 1e-9:
                    continue
                value = eff_bets(base_codes + [code])
                if value is None:
                    continue
                gain = value - current_eff
                if gain < float(T.CORE10_SWAP_MIN_GAIN):
                    continue
                key = (gain, -loss, cand_score, code)
                if best is None or key > best[0]:
                    best = (key, i, code, d, value)
        if best is None:
            break
        _, i, code, d, value = best
        kept = kept[:i] + [{"fcode": code, **d}] + kept[i + 1:]
        current_eff = value
    return kept


def _core10_risk_budget(kept: list[dict[str, Any]],
                         series_by_code: dict[str, list[tuple[str, float]]]) -> dict[str, Any]:
    """core10 选样后的风险预算标记（P1-9）：不改变名次，只决定是否可称分散。

    P2-2 的贪婪选样只限制单对相关 <=0.85，没有组合层有效 bet 约束；这里按
    选中的 10 只计算两两平均相关与 effective bets，低于目标就明确标记
    downgraded，报告/API 不得把它当成已分散组合。
    """
    codes = [item["fcode"] for item in kept]
    if len(codes) < 2:
        return {"status": "unavailable", "target": T.CORE10_TARGET_EFFECTIVE_BETS,
                "effective_bets": None, "avg_pairwise_corr": None,
                "n_pairs": 0, "note": "少于 2 只，无法计算组合分散度"}
    rhos: list[float] = []
    for i, a in enumerate(codes):
        for b in codes[i + 1:]:
            sa = series_by_code.get(a) or []
            sb = series_by_code.get(b) or []
            rho, _ = pair_corr(sa, sb, min_obs=T.CORE10_MIN_COMMON_OBS)
            if rho is not None:
                rhos.append(float(rho))
    if not rhos:
        return {"status": "unavailable", "target": T.CORE10_TARGET_EFFECTIVE_BETS,
                "effective_bets": None, "avg_pairwise_corr": None,
                "n_pairs": 0, "note": "共同净值不足，无法计算组合分散度"}
    rho_bar = sum(rhos) / len(rhos)
    eff = effective_bets(len(codes), rho_bar)
    target = float(T.CORE10_TARGET_EFFECTIVE_BETS)
    met = eff is not None and eff >= target
    return {
        "status": "meets_target" if met else "downgraded",
        "target": target,
        "effective_bets": None if eff is None else round(eff, 3),
        "avg_pairwise_corr": round(rho_bar, 4),
        "n_pairs": len(rhos),
        "n_funds": len(codes),
        "note": ("达到目标，但仍低于 10 个独立 bet" if met else
                 "低于目标：core10 只能作为集中观察组合，不能宣称分散化"),
    }


def _family_rep_key(code: str, d: dict[str, Any], c: Candidate) -> tuple:
    """家族代表排序键：总成本最低优先；成本未知排最后；平手取正式分/排序分高者。"""
    cost = c.get("cost_1y_pct")
    sc = d.get("scores") or {}
    return (cost is None, float(cost) if cost is not None else 0.0,
            -float(sc.get("formal_score") or 0.0),
            -float(sc.get("rank_score") or 0.0), code)


def _fold_family_pool(passed: list[tuple[str, dict[str, Any]]],
                      by_code: dict[str, Candidate], dropped: list[dict[str, Any]]
                      ) -> list[tuple[str, dict[str, Any]]]:
    """正式候选先按 family 折叠：A/C/D/E 只留总成本最低的份额。

    必须在公司上限与榜单配额之前做，否则同一基金的其他份额会先占掉名额，
    再把低成本份额当成“折叠损失”。这里保留被折叠记录，报告才能拆出真实原因。
    """
    best: dict[str, tuple[str, dict[str, Any]]] = {}
    order: list[str] = []
    for code, d in passed:
        c = by_code.get(code) or {}
        fam = c.get("family_id") or code
        cur = best.get(fam)
        if cur is None:
            best[fam] = (code, d)
            order.append(fam)
            continue
        if _family_rep_key(code, d, c) < _family_rep_key(cur[0], cur[1], by_code.get(cur[0]) or {}):
            dropped.append({"board": d.get("board"), "fcode": cur[0], "family_id": fam,
                            "reason": "family_rep_lost"})
            best[fam] = (code, d)
        else:
            dropped.append({"board": d.get("board"), "fcode": code, "family_id": fam,
                            "reason": "family_rep_lost"})
    return sorted((best[fam] for fam in order), key=lambda it: _rank_sort_key(it, by_code))


def _rank_sort_key(item: tuple[str, dict[str, Any]], by_code: dict[str, Candidate]) -> tuple:
    code, d = item
    sc = d.get("scores") or {}
    return (-(sc.get("rank_score") or 0.0), -float(sc.get("formal_score") or 0.0),
            -float(sc.get("composite") or 0.0),
            -(_m(by_code.get(code) or {}, 365, "annual_dec") or -9),
            -(_m(by_code.get(code) or {}, 365, "sharpe") or -9), code)


def _apply_company_cap(items: list[dict[str, Any]], by_code: dict[str, Candidate],
                       dropped: list[dict[str, Any]], *, board: str,
                       cap: int = T.COMPANY_CAP) -> list[dict[str, Any]]:
    """按公司折叠；固定上限，不再按榜单规模 5% 浮动。"""
    kept: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for item in items:
        c = by_code.get(item["fcode"]) or {}
        comp = c.get("company_name") or ""
        if comp and seen.get(comp, 0) >= cap:
            dropped.append({"board": board, "fcode": item["fcode"], "company": comp,
                            "reason": "company_cap"})
            continue
        if comp:
            seen[comp] = seen.get(comp, 0) + 1
        kept.append(item)
    return kept


def _formal_board_counts(n_stable: int, n_enhanced: int, target: int,
                         ratio: float) -> tuple[int, int]:
    """正式双榜的前缀截取数量。

    目标上限 320，同时要求较小的榜至少占最终发布数的 ratio（默认 25%）。
    两个榜都非空时，等价于大榜不超过小榜的 1/ratio - 1 倍；一侧为空时
    不做比例约束，避免因为样本不足把另一侧也清零。
    """
    s, e = max(0, int(n_stable)), max(0, int(n_enhanced))
    target = max(0, int(target))
    if target == 0:
        return 0, 0
    if target < 2:
        return (min(s, target), 0) if s >= e else (0, min(e, target))
    if s == 0 or e == 0:
        if s >= e:
            return min(s, target), 0
        return 0, min(e, target)
    ratio = min(max(float(ratio), 0.0), 0.5)
    total = s + e
    cap = min(total, target if target > 0 else total)
    if ratio > 0:
        cap = min(cap, int(min(s, e) / ratio))
    cap = max(2, cap)
    # 从大到小找可行的总数；通常第一次就可行，循环只处理舍入边界。
    for m in range(cap, 1, -1):
        lower = int(math.ceil(ratio * m)) if ratio > 0 else 0
        lo = max(lower, m - e, 0)
        hi = min(s, m - lower)
        if lo <= hi:
            ns = int(round(m * s / total))
            ns = max(lo, min(hi, ns))
            return ns, m - ns
    return min(s, cap // 2), min(e, cap - min(s, cap // 2))


def _select_concentrated(passed: list[tuple[str, dict[str, Any]]],
                         by_code: dict[str, Candidate],
                         dropped: list[dict[str, Any]], *, board: str,
                         quota: int, enh_cap: int, conv_cap: int,
                         score_key: str = "rank_score",
                         min_5y_coverage: float | None = None,
                         series_by_code: dict[str, list[tuple[str, float]]] | None = None
                         ) -> list[dict[str, Any]]:
    """把已排序的正式候选折叠成集中榜（核心 50 / 收益精选 10 / 长期 20）。

    只改变展示集中度，不新增信号：默认沿用排序分；长期榜改用 long_score。
    先按风格上限过滤，再按 family/company 折叠，折叠掉的名额由后面的基金补位。

    P2-2：core10 在静态资格过滤后改用贪婪分散选样。每轮在剩余候选里选
    rank_score - CORE10_CORR_PENALTY * max_corr 最高者，并拒绝与已选基金
    最大相关超过 CORE10_MAX_PAIR_CORR 的候选；其他榜仍按原顺序截取。
    """
    kept: list[dict[str, Any]] = []
    fam_seen: set[str] = set()
    comp_seen: dict[str, int] = {}
    is_core = board in (T.BOARD_CORE, T.BOARD_CORE10, T.BOARD_LONG)
    comp_cap = T.COMPANY_CAP if is_core else max(T.COMPANY_CAP, int(round(quota * 0.05)))
    style_caps = T.CORE10_STYLE_CAPS if board == T.BOARD_CORE10 else T.CORE_STYLE_CAPS
    style_seen: dict[str, int] = {}
    n_enh = n_conv = 0
    if score_key == "rank_score":
        ordered = passed
    else:
        ordered = sorted(passed, key=lambda it: (-(it[1].get("scores", {}).get(score_key) or 0.0),
                                                  it[0]))

    # 先做与选样顺序无关的静态资格过滤；风格/家族/公司上限仍留到动态循环。
    eligible: list[tuple[str, dict[str, Any], Candidate, str]] = []
    for code, d in ordered:
        style = d.get("style_code") or ""
        if is_core and style not in T.CORE_STYLES:
            dropped.append({"board": board, "fcode": code, "style": style,
                            "reason": "core_style_excluded"})
            continue
        c = by_code.get(code) or {}
        if is_core:
            # P0-6/P0-7/P0-8：核心榜只收目标份额 >=2 亿、现任经理 >=2 年、
            # 3 年覆盖 >=90% 的稳定风格；不满足者留在稳健/增强榜，不降级充数。
            # P1 长期榜再要求 5 年覆盖 >=60%，否则“长期”只是名字。
            min_share = T.LONG_SHARE_SCALE_MIN if board == T.BOARD_LONG else T.CORE_SHARE_SCALE_MIN
            min_tenure = (T.LONG_MANAGER_TENURE_MIN_DAYS if board == T.BOARD_LONG
                          else T.CORE_MANAGER_TENURE_MIN_DAYS)
            share = c.get("share_scale_billion")
            if share is None or share < min_share:
                dropped.append({"board": board, "fcode": code, "reason": "core_share_scale"})
                continue
            tenure = c.get("manager_tenure_min_days")
            if tenure is None or tenure < min_tenure:
                dropped.append({"board": board, "fcode": code, "reason": "core_manager_tenure"})
                continue
            if board == T.BOARD_LONG:
                # P1-8：长期榜的“3 年业绩由当前团队创造”必须用覆盖率证明；
                # 730 天仅覆盖 66.7%，不能只看固定天数。
                coverage = min(1.0, float(tenure) / 1095.0)
                if coverage < T.LONG_MANAGER_COVERAGE_MIN:
                    dropped.append({"board": board, "fcode": code,
                                    "reason": "long_manager_coverage"})
                    continue
            cov = ((d.get("scores") or {}).get("coverage") or {})
            if not cov.get("is_full_3y"):
                dropped.append({"board": board, "fcode": code, "reason": "core_not_full_3y"})
                continue
            if board == T.BOARD_LONG:
                need = (T.LONG_MIN_5Y_COVERAGE if min_5y_coverage is None
                        else float(min_5y_coverage))
                if float(cov.get("coverage_ratio_5y") or 0.0) < need:
                    dropped.append({"board": board, "fcode": code,
                                    "reason": "long_5y_coverage"})
                    continue
                if not cov.get("has_5y_annual"):
                    dropped.append({"board": board, "fcode": code,
                                    "reason": "long_5y_annual_missing"})
                    continue
        eligible.append((code, d, c, style))

    if board == T.BOARD_CORE10 and series_by_code:
        remaining = eligible[:]
        while len(kept) < quota and remaining:
            best = None
            for idx, (code, d, c, style) in enumerate(remaining):
                cap = style_caps.get(style)
                if cap is not None and style_seen.get(style, 0) >= cap:
                    continue
                if style == "bond_convertible" and n_conv >= conv_cap:
                    continue
                if style in T.ENHANCED_STYLES and n_enh >= enh_cap:
                    continue
                fam = c.get("family_id") or code
                comp = c.get("company_name") or ""
                if fam in fam_seen:
                    continue
                if comp and comp_seen.get(comp, 0) >= comp_cap:
                    continue
                mx = 0.0
                for prev in kept:
                    rho, _ = pair_corr(series_by_code[code], series_by_code[prev["fcode"]],
                                       min_obs=T.CORE10_MIN_COMMON_OBS)
                    if rho is not None and rho > mx:
                        mx = float(rho)
                if mx > T.CORE10_MAX_PAIR_CORR:
                    continue
                score = (d.get("scores") or {}).get(score_key) or 0.0
                value = float(score) - T.CORE10_CORR_PENALTY * mx
                if best is None or value > best[0] or (value == best[0] and idx < best[1]):
                    best = (value, idx, code, d, c, style)
            if best is None:
                break
            _, idx, code, d, c, style = best
            fam = c.get("family_id") or code
            comp = c.get("company_name") or ""
            fam_seen.add(fam)
            if comp:
                comp_seen[comp] = comp_seen.get(comp, 0) + 1
            kept.append({"fcode": code, **d})
            style_seen[style] = style_seen.get(style, 0) + 1
            if style in T.ENHANCED_STYLES:
                n_enh += 1
            if style == "bond_convertible":
                n_conv += 1
            remaining.pop(idx)
        kept = _core10_local_search(kept, eligible, style_caps, comp_cap, series_by_code)
        return kept

    for code, d, c, style in eligible:
        cap = style_caps.get(style)
        if cap is not None and style_seen.get(style, 0) >= cap:
            dropped.append({"board": board, "fcode": code, "style": style,
                            "reason": "core_style_cap"})
            continue
        if style == "bond_convertible" and n_conv >= conv_cap:
            continue
        if style in T.ENHANCED_STYLES and n_enh >= enh_cap:
            continue
        fam = c.get("family_id") or code
        comp = c.get("company_name") or ""
        if fam in fam_seen:
            dropped.append({"board": board, "fcode": code, "family_id": fam,
                            "reason": "family_cap"})
            continue
        if comp and comp_seen.get(comp, 0) >= comp_cap:
            dropped.append({"board": board, "fcode": code, "company": comp,
                            "reason": "company_cap"})
            continue
        fam_seen.add(fam)
        if comp:
            comp_seen[comp] = comp_seen.get(comp, 0) + 1
        kept.append({"fcode": code, **d})
        style_seen[style] = style_seen.get(style, 0) + 1
        if style in T.ENHANCED_STYLES:
            n_enh += 1
        if style == "bond_convertible":
            n_conv += 1
        if len(kept) >= quota:
            break
    return kept


def _rank(decisions: dict[str, dict[str, Any]], scores: dict[str, dict[str, Any]],
          cands: list[Candidate], target_max: int | None = None,
          group_min_ratio: float | None = None,
          series_by_code: dict[str, list[tuple[str, float]]] | None = None
          ) -> dict[str, list[dict[str, Any]]]:
    """榜单排序与折叠。

    v3 变化（ADR-062）：排序主键改为"排序分"（综合分 85% + 6 个月收益分位 15%），
    综合分仍决定准入、正式分降为次级键。

    v2 变化（ADR-051/054）：
    * 去掉 confidence tier 第一排序键——v1 里 99.8% 都是 high，等于恒等键；
    * 正式榜按 family 折叠，同一只基金的 A/C/D/E 只留正式分最高的一条；
    * 观察榜（买不到）/受限榜（锁定期）单独成榜，不与"不够好"混在一起。
    """
    by_code = {c["fcode"]: c for c in cands}
    target = int(target_max if target_max is not None else T.FORMAL_TARGET_MAX)
    ratio = float(group_min_ratio if group_min_ratio is not None else T.GROUP_MIN_RATIO)

    passed = [(code, d) for code, d in decisions.items() if d.get("passed")]
    dropped: list[dict[str, Any]] = []
    # P0：家族折叠必须发生在任何榜单配额之前。否则同基金的低成本份额可能
    # 先被高成本份额挤出，报告再把这种损失误记成公司/规模上限。
    pool = _fold_family_pool(passed, by_code, dropped)
    boards: dict[str, list[dict[str, Any]]] = {T.BOARD_STABLE: [], T.BOARD_ENHANCED: [],
                                               T.BOARD_CORE: [], T.BOARD_CORE10: [],
                                               T.BOARD_LONG: [],
                                               T.BOARD_WATCH: [], T.BOARD_LOCKED: [],
                                               T.BOARD_ROOKIE: []}
    # 核心精选榜（v5 / ADR-071）与收益精选 10 榜（v6 / ADR-072）：正式候选按排序分
    # 集中，跨风格但限制增强/转债占比。pool 已按家族折叠，名额不会白丢。
    boards[T.BOARD_CORE] = _select_concentrated(
        pool, by_code, dropped, board=T.BOARD_CORE,
        quota=T.CORE_QUOTA, enh_cap=T.CORE_ENHANCED_CAP, conv_cap=T.CORE_CONVERTIBLE_CAP)
    core10 = _select_concentrated(
        pool, by_code, dropped, board=T.BOARD_CORE10,
        quota=T.CORE10_QUOTA, enh_cap=T.CORE10_ENHANCED_CAP,
        conv_cap=T.CORE10_CONVERTIBLE_CAP, series_by_code=series_by_code)
    core10_budget = _core10_risk_budget(core10, series_by_code or {})
    for item in core10:
        item.setdefault("scores", {})["core10_risk_budget"] = core10_budget
    boards[T.BOARD_CORE10] = core10
    # P1 长期核心观察榜：与 core 使用同一套风格/公司/家族上限，但按 long_score
    # 选样并要求 5 年覆盖 >=60%。它是影子榜，不替换稳健榜，也不参与正式配额。
    boards[T.BOARD_LONG] = _select_concentrated(
        pool, by_code, dropped, board=T.BOARD_LONG,
        quota=T.LONG_QUOTA, enh_cap=T.CORE_ENHANCED_CAP,
        conv_cap=T.CORE_CONVERTIBLE_CAP, score_key="long_score")
    for code, d in pool:
        board = d.get("board")
        if board in T.FORMAL_BOARDS:
            boards[board].append({"fcode": code, **d})
    # P0：正式榜公司上限固定 2，不再随榜单规模浮动到 5%。
    for board in T.FORMAL_BOARDS:
        boards[board] = _apply_company_cap(boards[board], by_code, dropped,
                                           board=board, cap=T.COMPANY_CAP)
    # 增强榜配额（ADR-031）：相对门槛会放进数百只，用每风格配额 + 总量上限收敛，
    # 保证一级/二级/转债各有位置，又不至于淹没稳健榜。
    enh_kept: list[dict[str, Any]] = []
    per_style: dict[str, int] = {}
    for item in boards[T.BOARD_ENHANCED]:
        style = item.get("style_code") or ""
        if per_style.get(style, 0) >= T.ENHANCED_RELATIVE["quota_per_style"]:
            dropped.append({"board": T.BOARD_ENHANCED, "fcode": item["fcode"],
                            "style": style, "reason": "enhanced_style_quota"})
            continue
        per_style[style] = per_style.get(style, 0) + 1
        enh_kept.append(item)
    enh_total = int(T.ENHANCED_RELATIVE["quota_total"])
    if len(enh_kept) > enh_total:
        for item in enh_kept[enh_total:]:
            dropped.append({"board": T.BOARD_ENHANCED, "fcode": item["fcode"],
                            "reason": "enhanced_total_quota"})
        enh_kept = enh_kept[:enh_total]
    boards[T.BOARD_ENHANCED] = enh_kept
    # 观察榜 / 受限榜：L2（买不到）、L3（锁定期）里质量达标的，单独排序
    for code, d in decisions.items():
        board = d.get("board")
        if board in (T.BOARD_WATCH, T.BOARD_LOCKED) and not d.get("passed"):
            boards[board].append({"fcode": code, **d})
    for board in (T.BOARD_WATCH, T.BOARD_LOCKED):
        # fcode 兜底：rank_score 完全并列时不能依赖 dict/查询返回顺序。
        boards[board].sort(key=lambda it: (-(it["scores"].get("rank_score") or 0.0),
                                           it["fcode"]))
    # 新锐榜（P1-1）：历史 1–3 年、只差综合分这一条者，按新锐分取前 N。
    # 先按家族/公司去重再截配额——榜单只有 30 个位置，先截后折会白丢名额
    # （实测 30→24，六只被同公司/同基金的其他份额挤掉）。
    rookie_pool = [{"fcode": code, **d} for code, d in decisions.items()
                   if d.get("board") == T.BOARD_ROOKIE and not d.get("passed")]
    rookie_pool.sort(key=lambda it: (-it["scores"].get("rookie", 0.0), it["fcode"]))
    fam_seen: set[str] = set()
    comp_seen: dict[str, int] = {}
    rookie_kept: list[dict[str, Any]] = []
    for item in rookie_pool:
        c = by_code.get(item["fcode"]) or {}
        fam = c.get("family_id") or item["fcode"]
        comp = c.get("company_name") or ""
        if fam in fam_seen:
            dropped.append({"board": T.BOARD_ROOKIE, "fcode": item["fcode"],
                            "family_id": fam, "reason": "family_cap"})
            continue
        if comp and comp_seen.get(comp, 0) >= T.COMPANY_CAP:
            dropped.append({"board": T.BOARD_ROOKIE, "fcode": item["fcode"],
                            "company": comp, "reason": "company_cap"})
            continue
        fam_seen.add(fam)
        if comp:
            comp_seen[comp] = comp_seen.get(comp, 0) + 1
        rookie_kept.append(item)
        if len(rookie_kept) >= T.ROOKIE_QUOTA:
            break
    boards[T.BOARD_ROOKIE] = rookie_kept
    # 配额：目标上限 + 双榜最小占比。前缀截取，保证排名连续。
    ns, ne = _formal_board_counts(len(boards[T.BOARD_STABLE]),
                                  len(boards[T.BOARD_ENHANCED]), target, ratio)
    for board, n in ((T.BOARD_STABLE, ns), (T.BOARD_ENHANCED, ne)):
        items = boards[board]
        for item in items[n:]:
            dropped.append({"board": board, "fcode": item["fcode"],
                            "reason": "formal_target_trim"})
        boards[board] = items[:n]
    for board, items in boards.items():
        for idx, item in enumerate(items, start=1):
            item["rank"] = idx
    boards["_dropped"] = dropped
    return boards


def _persist(conn, cfg, run_id: str, decisions: dict[str, dict[str, Any]],
             picks: dict[str, list[dict[str, Any]]], cands: list[Candidate],
             as_of: str, formula_contract: str, base_pass: int,
             business_date: str | None = None, batch_id: str | None = None,
             diff_path: str | None = None, pcts: dict | None = None,
             pcts_all: dict | None = None,
             freshness_info: dict[str, Any] | None = None) -> None:
    now = db.now_iso()
    rows = []
    for code, d in decisions.items():
        rows.append((run_id, code, d["layer_reached"], d.get("passed", 0),
                     json.dumps(d.get("reasons", []), ensure_ascii=False), d.get("style_code"),
                     json.dumps(d.get("thresholds"), ensure_ascii=False, default=str)
                     if d.get("thresholds") else None,
                     json.dumps(d.get("scores"), ensure_ascii=False) if d.get("scores") else None,
                     d.get("confidence"),
                     json.dumps(d.get("relief"), ensure_ascii=False) if d.get("relief") else None,
                     json.dumps(d.get("risk"), ensure_ascii=False, default=str)
                     if d.get("risk") else None,
                     json.dumps(d.get("data_flags"), ensure_ascii=False, default=str)
                     if d.get("data_flags") else None))
    conn.executemany(
        "INSERT INTO screen_decision(run_id, fcode, layer_reached, passed, reason_codes_json,"
        " style_code, thresholds_json, scores_json, confidence, relief_json, risk_json,"
        " data_flags_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(run_id, fcode) DO UPDATE SET layer_reached=excluded.layer_reached,"
        " passed=excluded.passed, reason_codes_json=excluded.reason_codes_json,"
        " style_code=excluded.style_code, thresholds_json=excluded.thresholds_json,"
        " scores_json=excluded.scores_json, confidence=excluded.confidence,"
        " relief_json=excluded.relief_json, risk_json=excluded.risk_json,"
        " data_flags_json=excluded.data_flags_json", rows)
    pick_rows = []
    for board, items in picks.items():
        if board.startswith("_"):
            continue
        for item in items:
            sc = item["scores"]
            # stability_rank 需要历史入选次数/新高新低，首轮无历史 → NULL（不伪造分数）
            pick_rows.append((run_id, board, item["rank"], item["fcode"],
                              sc.get("formal_score"), None,
                              item.get("confidence"), sc.get("rank_score")))
    conn.executemany(
        "INSERT INTO screen_pick(run_id, board, rank, fcode, formal_score, stability_rank,"
        " confidence, rank_score)"
        " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id, board, fcode) DO UPDATE SET"
        " rank=excluded.rank, formal_score=excluded.formal_score,"
        " stability_rank=excluded.stability_rank, confidence=excluded.confidence,"
        " rank_score=excluded.rank_score", pick_rows)
    fresh = freshness_info or {}
    nav_as_of = fresh.get("as_of")
    metric_as_of = (fresh.get("metric") or {}).get("metric_as_of") or nav_as_of
    # 三个计数必须分开落库：L9 通过 / 正式发布 / 各原因折叠。旧实现只写一个
    # formal_count（=L9），报告和 gate 再把“L9 - 发布”统称 family/company 折叠，
    # 掩盖了增强配额、双榜截断等真实原因。
    l9_pass = sum(1 for d in decisions.values() if d.get("passed"))
    published = (len(picks.get(T.BOARD_STABLE, []))
                 + len(picks.get(T.BOARD_ENHANCED, [])))
    trim: dict[str, dict[str, Any]] = {}
    for d in picks.get("_dropped", []):
        # 只统计正式双榜的折叠：core/long/rookie 的落选与 L9→发布差额无关，
        # 混进来会让报告把“核心榜风格不符”误写成正式榜折叠原因。
        board = d.get("board")
        if board not in T.FORMAL_BOARDS:
            continue
        reason = d.get("reason") or "unknown"
        ent = trim.setdefault(reason, {"n": 0, "boards": {}, "examples": []})
        ent["n"] += 1
        ent["boards"][board] = ent["boards"].get(board, 0) + 1
        if len(ent["examples"]) < 20 and d.get("fcode"):
            ent["examples"].append(d["fcode"])
    trim_json = json.dumps(trim, ensure_ascii=False) if trim else None
    conn.execute(
        "INSERT INTO screen_run(run_id, profile, config_hash, code_version, formula_contract,"
        " screen_contract, universe_count, base_pass_count, formal_count, l9_pass_count,"
        " published_count, board_trim_json, locked_count, status,"
        " created_at, business_date, batch_id, board_diff_path, nav_as_of, metric_as_of,"
        " freshness_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET"
        " universe_count=excluded.universe_count, base_pass_count=excluded.base_pass_count,"
        " formal_count=excluded.formal_count, l9_pass_count=excluded.l9_pass_count,"
        " published_count=excluded.published_count, board_trim_json=excluded.board_trim_json,"
        " locked_count=excluded.locked_count,"
        " status=excluded.status, business_date=excluded.business_date,"
        " batch_id=excluded.batch_id, board_diff_path=excluded.board_diff_path,"
        " screen_contract=excluded.screen_contract, nav_as_of=excluded.nav_as_of,"
        " metric_as_of=excluded.metric_as_of, freshness_json=excluded.freshness_json",
        (run_id, T.SCREEN_PROFILE, cfg.config_hash, cfg.code_version, formula_contract,
         T.SCREEN_CONTRACT, len(cands),
         base_pass,
         l9_pass, l9_pass, published, trim_json,
         len(picks[T.BOARD_LOCKED]), "done", now, business_date, batch_id, diff_path,
         nav_as_of, metric_as_of,
         json.dumps(fresh, ensure_ascii=False, default=str) if fresh else None))
    pit.record_inputs(conn, run_id, as_of, cands, decisions, pcts=pcts, pcts_all=pcts_all)
    conn.commit()