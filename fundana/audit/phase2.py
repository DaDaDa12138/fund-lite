"""Phase 2 影子研究：长期权重、久期/信用暴露、杠杆与基准匹配。

边界：
* 只读 screen_run/screen_run_input/fund_factor_exposure/src_pzd_profile；
* 不写 screen_pick，不改生产权重，不参与发布；
* 5 年倾斜组合只与当前 core10 对照，输出到 reports/analysis/phase2/；
* 杠杆是资产配置表“债券+股票+现金占净比”的代理，不是合同杠杆；
* 基准分类是文本启发式，只能发现明显错配，不能替代基金合同；
* 因子 beta 来自指数回归，r2 低时只作参考。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence

from .. import db, fundtypes
from ..screen import engine, scoring
from ..screen import thresholds as T
from . import quality

BOARDS: tuple[tuple[str, str], ...] = (
    (T.BOARD_STABLE, "稳健"),
    (T.BOARD_ENHANCED, "增强"),
    (T.BOARD_CORE, "核心50"),
    (T.BOARD_CORE10, "核心10"),
    (T.BOARD_LONG, "长期20"),
)

# 5 年倾斜：5 年收益/回撤/Calmar 合计 58%，3 年风险 23%，1 年只留 3%。
LONG5_WEIGHTS = {
    "annual_5y": 0.20, "annual_3y": 0.12, "consistency": 0.10,
    "mdd_5y": 0.18, "mdd_3y": 0.10, "vol_3y": 0.05,
    "calmar_5y": 0.10, "calmar_3y": 0.08, "sortino_3y": 0.04, "calmar": 0.03,
}
LONG5_MIN_COVERAGE = 0.60
LONG5_SHORT_WEIGHT = 0.20
LONG5_LONG_WEIGHT = 0.80

_BENCH_RATE = ("国债", "国开", "进出口", "农发", "口行", "政策性金融", "地方政府债", "政金债", "利率债")
_BENCH_CREDIT = ("信用债", "信用类", "高信用等级", "投资级", "产业债", "城投", "二级资本债", "永续债", "中期票据", "短期融资券", "公司债", "企业债")
_BENCH_CONV = ("可转换", "转债", "可交换")
_BENCH_CASH = ("活期存款", "定期存款", "银行存款", "货币市场", "现金")
_BENCH_COMPOSITE = ("综合债", "全债", "债券总指数", "中债综合")
_BENCH_EQUITY = ("沪深300", "中证500", "股票", "权益")


def classify_benchmark(text: str | None) -> dict[str, Any]:
    """把业绩比较基准文本分成 rate/credit/convertible/cash/composite/equity/other。

    多类同时出现记为 mixed；mixed 不直接判错配。
    """
    raw = (text or "").strip()
    if not raw:
        return {"primary": "unknown", "classes": [], "mixed": False, "text": raw}
    classes: list[str] = []
    if any(k in raw for k in _BENCH_CONV):
        classes.append("convertible")
    if any(k in raw for k in _BENCH_RATE):
        classes.append("rate")
    if any(k in raw for k in _BENCH_CREDIT):
        classes.append("credit")
    if any(k in raw for k in _BENCH_CASH):
        classes.append("cash")
    if any(k in raw for k in _BENCH_COMPOSITE):
        classes.append("composite")
    if any(k in raw for k in _BENCH_EQUITY):
        classes.append("equity")
    classes = list(dict.fromkeys(classes))
    if not classes:
        primary = "other"
    elif len(classes) == 1:
        primary = classes[0]
    else:
        primary = "mixed"
    return {"primary": primary, "classes": classes, "mixed": len(classes) > 1, "text": raw}


def benchmark_mismatch(style_code: str | None, profile: dict[str, Any]) -> str | None:
    """只报明显错配；复合基准与指数/纯债不报。"""
    style = style_code or ""
    classes = set(profile.get("classes") or [])
    if not classes or profile.get("primary") in ("unknown", "other"):
        return None
    if profile.get("mixed"):
        return None
    if style == "bond_rate" and "rate" not in classes:
        return "rate_style_but_benchmark_%s" % profile["primary"]
    if style == "bond_credit" and "credit" not in classes:
        return "credit_style_but_benchmark_%s" % profile["primary"]
    if style == "bond_convertible" and "convertible" not in classes:
        return "convertible_style_but_benchmark_%s" % profile["primary"]
    if style == "bond_cash_enhanced" and "cash" not in classes:
        return "cash_style_but_benchmark_%s" % profile["primary"]
    return None


def parse_asset_alloc(raw: str | None) -> dict[str, Any] | None:
    """解析东财资产配置 JSON，取最后一个报告期债券/股票/现金占净比。

    gross_pct 是三者之和；>100.5% 视为杠杆代理。
    """
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    series = doc.get("series") or []
    cats = doc.get("categories") or []
    if not series or not cats:
        return None
    values: dict[str, float] = {}
    for s in series:
        name = str(s.get("name") or "")
        data = s.get("data") or []
        if not data:
            continue
        val = data[-1]
        if val is None:
            continue
        try:
            values[name] = float(val)
        except (TypeError, ValueError):
            continue
    bond = values.get("债券占净比")
    stock = values.get("股票占净比")
    cash = values.get("现金占净比")
    if bond is None and stock is None and cash is None:
        return None
    gross = sum(v for v in (bond, stock, cash) if v is not None)
    return {
        "period": str(cats[-1])[:10],
        "bond_pct": bond, "stock_pct": stock, "cash_pct": cash,
        "gross_pct": round(gross, 2),
        "leveraged": bool(gross > 100.5 or (bond is not None and bond > 100.5)),
    }


def _latest_run(conn, run_id: str | None) -> dict[str, Any]:
    if run_id:
        row = conn.execute("SELECT * FROM screen_run WHERE run_id=?", (run_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM screen_run WHERE status IN ('done','success')"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        raise ValueError("screen_run_not_found")
    return dict(row)


def _picks(conn, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.board, p.rank, p.fcode, p.rank_score, p.formal_score,"
        " d.style_code, d.scores_json, i.coverage_json, i.family_id,"
        " f.name, f.company_name, fp.benchmark_text, fp.mgr_fee_pct,"
        " fp.trust_fee_pct, fp.recurring_fee_pct, fs.confidence AS style_confidence"
        " FROM screen_pick p"
        " LEFT JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
        " LEFT JOIN screen_run_input i ON i.run_id=p.run_id AND i.fcode=p.fcode"
        " LEFT JOIN fund f ON f.fcode=p.fcode"
        " LEFT JOIN fund_profile fp ON fp.fcode=p.fcode"
        " LEFT JOIN fund_style fs ON fs.fcode=p.fcode"
        " WHERE p.run_id=? ORDER BY p.board, p.rank", (run_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["scores"] = json.loads(d.pop("scores_json") or "{}")
        except ValueError:
            d["scores"] = {}
        try:
            d["coverage"] = json.loads(d.pop("coverage_json") or "{}")
        except ValueError:
            d["coverage"] = {}
        out.append(d)
    return out


def _factor_map(conn, as_of: str, codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    marks = ",".join("?" * len(codes))
    rows = conn.execute(
        "SELECT f.* FROM fund_factor_exposure f"
        " JOIN (SELECT fcode, MAX(as_of) AS m FROM fund_factor_exposure"
        "       WHERE as_of<=? AND fcode IN (%s) GROUP BY fcode) t"
        " ON t.fcode=f.fcode AND t.m=f.as_of" % marks,
        (as_of, *codes)).fetchall()
    return {r["fcode"]: dict(r) for r in rows}


def _alloc_map(conn, codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not codes:
        return {}
    marks = ",".join("?" * len(codes))
    rows = conn.execute(
        "SELECT p.code, p.business_date, p.asset_alloc_json FROM src_pzd_profile p"
        " JOIN (SELECT code, MAX(business_date) AS m FROM src_pzd_profile"
        "       WHERE code IN (%s) AND asset_alloc_json IS NOT NULL"
        "       AND asset_alloc_json!='' GROUP BY code) t"
        " ON t.code=p.code AND t.m=p.business_date" % marks, tuple(codes)).fetchall()
    out = {}
    for r in rows:
        parsed = parse_asset_alloc(r["asset_alloc_json"])
        if parsed:
            parsed["business_date"] = r["business_date"]
            out[r["code"]] = parsed
    return out


def _metric(metric_json: str | None, period: str, field: str) -> float | None:
    try:
        doc = json.loads(metric_json or "{}")
    except ValueError:
        return None
    val = ((doc.get(period) or {}).get(field))
    return float(val) if val is not None else None


def _pct_metric(metric_json: str | None, period: str, field: str) -> float | None:
    """metric_json 里的 annual/mdd/vol 是小数（0.03=3%），报告统一转成百分点。"""
    val = _metric(metric_json, period, field)
    return None if val is None else val * 100.0


def _num(val: Any) -> float | None:
    try:
        return round(float(val), 4) if val is not None else None
    except (TypeError, ValueError):
        return None


def _median(vals: Iterable[float | None]) -> float | None:
    xs = [float(v) for v in vals if v is not None]
    return round(median(xs), 4) if xs else None


def _enrich(conn, run: dict[str, Any], picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    codes = sorted({p["fcode"] for p in picks})
    as_of = str(run.get("nav_as_of") or run.get("business_date"))[:10]
    index_cache = quality.index_cache(conn, as_of)
    factors = _factor_map(conn, as_of, codes)
    allocs = _alloc_map(conn, codes)
    inputs = {}
    rows = conn.execute(
        "SELECT fcode, pcts_json, metric_json FROM screen_run_input WHERE run_id=?",
        (run["run_id"],)).fetchall()
    for r in rows:
        try:
            inputs[r["fcode"]] = {
                "pcts": json.loads(r["pcts_json"] or "{}"),
                "metric": r["metric_json"],
            }
        except ValueError:
            inputs[r["fcode"]] = {"pcts": {}, "metric": None}
    for p in picks:
        f = factors.get(p["fcode"]) or {}
        a = allocs.get(p["fcode"]) or {}
        inp = inputs.get(p["fcode"]) or {}
        bench = classify_benchmark(p.get("benchmark_text"))
        p["benchmark"] = bench
        p["benchmark_mismatch"] = benchmark_mismatch(p.get("style_code"), bench)
        p["beta_duration"] = f.get("beta_duration_raw")
        p["beta_credit"] = f.get("beta_credit_raw")
        p["beta_convertible"] = f.get("beta_convertible_raw")
        p["factor_r2"] = f.get("r2")
        p["factor_n_obs"] = f.get("n_obs")
        p["alpha_annual_pct"] = f.get("alpha_annual_pct")
        p["alpha_t"] = f.get("alpha_t")
        p["alpha_reliable"] = bool(f.get("alpha_reliable"))
        p["alpha_method"] = f.get("alpha_method")
        contract = quality.contract_metrics(conn, p.get("benchmark_text"), as_of, index_cache)
        p["benchmark_contract"] = contract
        p["mgr_fee_pct"] = _num(p.get("mgr_fee_pct"))
        p["trust_fee_pct"] = _num(p.get("trust_fee_pct"))
        p["recurring_fee_pct"] = _num(p.get("recurring_fee_pct"))
        p["alloc"] = a
        p["leverage"] = a.get("leveraged")
        p["annual_3y"] = _pct_metric(inp.get("metric"), "1095", "annual_dec")
        p["contract_excess_3y_pct"] = (
            round(p["annual_3y"] - contract["annual_3y_pct"], 4)
            if (contract.get("quality") == "exact"
                and p.get("annual_3y") is not None
                and contract.get("annual_3y_pct") is not None) else None)
        # 加回持续计提费率（管理+托管+销售服务）近似“扣费前”超额；
        # 申购/赎回等一次性费用不在此口径内。
        p["contract_excess_3y_gross_pct"] = (
            round(p["contract_excess_3y_pct"] + p["recurring_fee_pct"], 4)
            if (p.get("contract_excess_3y_pct") is not None
                and p.get("recurring_fee_pct") is not None) else None)
        # proxy 只用于 B/C 分层：不是合同精确基准，不能与 exact 混算。
        p["proxy_excess_3y_pct"] = (
            round(p["annual_3y"] - contract["annual_3y_pct"], 4)
            if (contract.get("quality") == "proxy"
                and p.get("annual_3y") is not None
                and contract.get("annual_3y_pct") is not None) else None)
        p["mdd_3y"] = _pct_metric(inp.get("metric"), "1095", "mdd_dec")
        p["dd_duration_obs"] = _metric(inp.get("metric"), "1095", "dd_duration_obs")
        p["vol_3y"] = _pct_metric(inp.get("metric"), "1095", "vol_dec")
        p["annual_5y"] = _pct_metric(inp.get("metric"), "1825", "annual_dec")
        p["mdd_5y"] = _pct_metric(inp.get("metric"), "1825", "mdd_dec")
        cov = p.get("coverage") or {}
        p["coverage_5y"] = cov.get("coverage_ratio_5y")
        p["has_5y_annual"] = bool(cov.get("has_5y_annual"))
        p["is_full_3y"] = bool(cov.get("is_full_3y"))
    return picks


def _core10_pool(conn, run: dict[str, Any], as_of: str) -> list[dict[str, Any]]:
    """core10 合格池：L7 通过、核心风格、规模/经理年限/3年覆盖达标。

    注意 core10 不是 core50 的前 10：它从更宽的核心风格池选，core50 是
    风格限额后的前 50；两者语义不同，报告必须分别说明。
    """
    rows = conn.execute(
        "SELECT i.fcode, i.style_code, i.family_id, i.company_name,"
        " i.share_scale_billion, i.manager_tenure_min_days,"
        " i.coverage_json, i.scores_json, f.name"
        " FROM screen_run_input i"
        " JOIN screen_decision d ON d.run_id=i.run_id AND d.fcode=i.fcode"
        " LEFT JOIN fund f ON f.fcode=i.fcode"
        " WHERE i.run_id=? AND d.passed=1"
        " AND d.style_code IN ('bond_pure','bond_short','bond_credit','bond_rate','bond_index')",
        (run["run_id"],)).fetchall()
    out = []
    for r in rows:
        try:
            cov = json.loads(r["coverage_json"] or "{}")
            scores = json.loads(r["scores_json"] or "{}")
        except ValueError:
            continue
        if (r["share_scale_billion"] or 0) < T.CORE_SHARE_SCALE_MIN:
            continue
        if (r["manager_tenure_min_days"] or 0) < T.CORE_MANAGER_TENURE_MIN_DAYS:
            continue
        if not cov.get("is_full_3y"):
            continue
        out.append({
            "fcode": r["fcode"], "name": r["name"],
            "style_code": r["style_code"], "family_id": r["family_id"],
            "company_name": r["company_name"],
            "share_scale_billion": r["share_scale_billion"],
            "manager_tenure_min_days": r["manager_tenure_min_days"],
            "coverage": cov, "scores": scores,
            "rank_score": float(scores.get("rank_score") or 0.0),
        })
    allocs = _alloc_map(conn, [x["fcode"] for x in out])
    for x in out:
        x["leverage"] = bool((allocs.get(x["fcode"]) or {}).get("leveraged"))
    series = quality._load_series(conn, [x["fcode"] for x in out], as_of)
    for x in out:
        x["series"] = series.get(x["fcode"])
    return [x for x in out if x.get("series")]


def _board_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items)
    return {
        "n": n,
        "factor_covered": sum(1 for x in items if x.get("beta_duration") is not None),
        "benchmark_covered": sum(1 for x in items if (x.get("benchmark") or {}).get("primary") not in (None, "unknown")),
        "alloc_covered": sum(1 for x in items if x.get("alloc")),
        "style_confidence": dict(Counter(x.get("style_confidence") or "unknown" for x in items)),
        "avg_duration_beta": _median(x.get("beta_duration") for x in items),
        "avg_credit_beta": _median(x.get("beta_credit") for x in items),
        "high_duration": [x["fcode"] for x in items
                          if x.get("beta_duration") is not None and x["beta_duration"] >= 0.75],
        "leveraged": [x["fcode"] for x in items if x.get("leverage")],
        "benchmark_mismatch": [
            {"fcode": x["fcode"], "name": x.get("name"), "style": x.get("style_code"),
             "benchmark": (x.get("benchmark") or {}).get("text"),
             "reason": x["benchmark_mismatch"]}
            for x in items if x.get("benchmark_mismatch")
        ],
        "enhanced_3y_covered": sum(1 for x in items if x.get("mdd_3y") is not None),
        "median_mdd_3y": _median(x.get("mdd_3y") for x in items),
        "median_vol_3y": _median(x.get("vol_3y") for x in items),
        "median_gross_pct": _median((x.get("alloc") or {}).get("gross_pct") for x in items),
        "text_heuristic": [x["fcode"] for x in items
                           if x.get("style_confidence") == "text_heuristic"],
        "worst_mdd_3y": [
            {"fcode": x["fcode"], "name": x.get("name"), "mdd_3y": x.get("mdd_3y")}
            for x in sorted((y for y in items if y.get("mdd_3y") is not None),
                            key=lambda y: y["mdd_3y"])[:5]
        ],
        "five_year_covered": sum(1 for x in items if (x.get("coverage_5y") or 0) >= 0.6),
        "contract_excess_3y_n": len([x for x in items
                                     if x.get("contract_excess_3y_pct") is not None]),
        "contract_excess_3y_median_pct": _median(
            x.get("contract_excess_3y_pct") for x in items),
        "contract_excess_3y_positive_share": (
            round(sum(1 for x in items if (x.get("contract_excess_3y_pct") or 0) > 0)
                  / len([x for x in items if x.get("contract_excess_3y_pct") is not None]), 4)
            if any(x.get("contract_excess_3y_pct") is not None for x in items) else None),
        "median_recurring_fee_pct": _median(x.get("recurring_fee_pct") for x in items),
        "contract_excess_3y_gross_n": len([x for x in items
                                           if x.get("contract_excess_3y_gross_pct") is not None]),
        "contract_excess_3y_gross_median_pct": _median(
            x.get("contract_excess_3y_gross_pct") for x in items),
        "contract_excess_3y_gross_positive_share": (
            round(sum(1 for x in items if (x.get("contract_excess_3y_gross_pct") or 0) > 0)
                  / len([x for x in items if x.get("contract_excess_3y_gross_pct") is not None]), 4)
            if any(x.get("contract_excess_3y_gross_pct") is not None for x in items) else None),
    }


def long5_score(pcts: dict[str, Any], short_core: float | None) -> dict[str, Any]:
    """5 年倾斜影子分；覆盖不足 60% 时不给分，避免拿 1 年数据冒充长期。"""
    vals = {k: pcts.get(k) for k in LONG5_WEIGHTS}
    cov = scoring.coverage(vals, LONG5_WEIGHTS)
    if cov < LONG5_MIN_COVERAGE:
        return {"score": None, "long5": None, "coverage": cov}
    long5 = scoring.weighted(vals, LONG5_WEIGHTS, renormalize=True)
    short = float(short_core or 0.0)
    return {"score": round(LONG5_SHORT_WEIGHT * short + LONG5_LONG_WEIGHT * long5, 4),
            "long5": long5, "coverage": cov}


def _long5_board(conn, cfg, run: dict[str, Any], picks: list[dict[str, Any]]) -> dict[str, Any]:
    types = fundtypes.resolve(cfg=cfg)
    cands = engine._load_candidates(conn, run["business_date"], run["formula_contract"], types)
    by_code = {c["fcode"]: c for c in cands}
    rows = conn.execute(
        "SELECT i.fcode, i.pcts_json, i.scores_json, i.coverage_json, d.style_code"
        " FROM screen_run_input i JOIN screen_decision d"
        " ON d.run_id=i.run_id AND d.fcode=i.fcode"
        " WHERE i.run_id=? AND i.passed=1", (run["run_id"],)).fetchall()
    passed: list[tuple[str, dict[str, Any]]] = []
    for r in rows:
        try:
            pcts = json.loads(r["pcts_json"] or "{}")
            scores = json.loads(r["scores_json"] or "{}")
            cov = json.loads(r["coverage_json"] or "{}")
        except ValueError:
            continue
        if not cov.get("is_full_3y"):
            continue
        if float(cov.get("coverage_ratio_5y") or 0.0) < LONG5_MIN_COVERAGE:
            continue
        if not cov.get("has_5y_annual"):
            continue
        ls = long5_score(pcts, scores.get("short_core"))
        if ls["score"] is None:
            continue
        d = {"fcode": r["fcode"], "style_code": r["style_code"],
             "scores": {"rank_score": ls["score"], "long5_score": ls["score"],
                        "formal_score": scores.get("formal_score"), "coverage": cov,
                        "long5": ls["long5"], "long5_coverage": ls["coverage"]}}
        passed.append((r["fcode"], d))
    passed.sort(key=lambda it: (-(it[1]["scores"].get("long5_score") or 0.0), it[0]))
    dropped: list[dict[str, Any]] = []
    selected = engine._select_concentrated(
        passed, by_code, dropped, board=T.BOARD_CORE10, quota=T.CORE10_QUOTA,
        enh_cap=T.CORE10_ENHANCED_CAP, conv_cap=T.CORE10_CONVERTIBLE_CAP,
        score_key="long5_score")
    current = [p["fcode"] for p in picks if p["board"] == T.BOARD_CORE10]
    cur, alt = set(current), {x["fcode"] for x in selected}
    union = cur | alt
    return {
        "eligible": len(passed),
        "current": current,
        "long5": [x["fcode"] for x in selected],
        "overlap": len(cur & alt),
        "added": sorted(alt - cur),
        "removed": sorted(cur - alt),
        "jaccard": round(len(cur & alt) / len(union), 4) if union else 1.0,
        "rows": [{k: x.get(k) for k in ("fcode", "style_code")} | {
            "long5_score": (x.get("scores") or {}).get("long5_score"),
            "long5": (x.get("scores") or {}).get("long5"),
            "long5_coverage": (x.get("scores") or {}).get("long5_coverage"),
            "name": (by_code.get(x["fcode"]) or {}).get("name"),
        } for x in selected],
        "note": "只用 3 年完整、5 年覆盖>=60% 的正式候选；5 年倾斜 80%，1 年短期分 20%。",
    }


def _concentration(items: list[dict[str, Any]]) -> dict[str, Any]:
    companies = Counter((x.get("company_name") or "未知") for x in items)
    return {
        "companies": dict(companies.most_common()),
        "max_company": companies.most_common(1)[0] if companies else (None, 0),
        "max_company_share": (round(companies.most_common(1)[0][1] / len(items), 4)
                              if items and companies else None),
    }


def diagnose(conn, cfg, run_id: str | None = None) -> dict[str, Any]:
    run = _latest_run(conn, run_id)
    picks = _enrich(conn, run, _picks(conn, run["run_id"]))
    as_of = str(run.get("nav_as_of") or run.get("business_date"))[:10]
    quality_tiers = quality.absolute_quality_tiers(conn, picks, as_of)
    tier_by_code = {x["fcode"]: x for x in (quality_tiers.get("items") or [])}
    for p in picks:
        row = tier_by_code.get(p["fcode"]) or {}
        p["quality_tier"] = row.get("tier")
        p["quality_tier_reasons"] = row.get("reasons") or []
        p["quality_rolling_win_rate"] = row.get("rolling_win_rate")
        p["quality_rolling_windows"] = row.get("rolling_windows")
        p["quality_excess_pct"] = row.get("excess_pct")
    boards: dict[str, list[dict[str, Any]]] = {}
    for p in picks:
        boards.setdefault(p["board"], []).append(p)
    core10 = boards.get(T.BOARD_CORE10, [])
    core50 = boards.get(T.BOARD_CORE, [])
    if core50:
        series50 = quality._load_series(conn, [x["fcode"] for x in core50], as_of)
        for x in core50:
            x["series"] = series50.get(x["fcode"])

    board_summaries: dict[str, dict[str, Any]] = {}
    for board, items in boards.items():
        summary = {**_board_summary(items), **quality.exposure_metrics(items)}
        tiers = Counter((x.get("quality_tier") or "unrated") for x in items)
        summary.update({
            "quality_A": tiers.get("A", 0),
            "quality_B": tiers.get("B", 0),
            "quality_C": tiers.get("C", 0),
            "quality_unrated": tiers.get("unrated", 0),
        })
        if board == T.BOARD_ENHANCED:
            summary.update(quality.enhanced_risk_metrics(items))
        board_summaries[board] = summary

    core_rows: list[dict[str, Any]] = []
    for x in core10:
        bc = x.get("benchmark_contract") or {}
        core_rows.append({
            **{k: x.get(k) for k in (
                "fcode", "name", "company_name", "style_code", "style_confidence",
                "beta_duration", "beta_credit", "beta_convertible", "factor_r2",
                "leverage", "coverage_5y", "annual_3y", "mdd_3y", "annual_5y", "mdd_5y",
                "benchmark_mismatch", "alpha_annual_pct", "alpha_t", "alpha_reliable",
                "quality_tier")},
            "benchmark": (x.get("benchmark") or {}).get("primary"),
            "benchmark_quality": bc.get("quality"),
            "benchmark_class": bc.get("primary_class"),
            "benchmark_maturity": bc.get("maturity"),
            "benchmark_3y_pct": bc.get("annual_3y_pct"),
            "alloc": x.get("alloc"),
            "flags": quality.quality_flags(x),
        })

    independence = quality.core10_independence(conn, core10, as_of)
    core_pool = _core10_pool(conn, run, as_of)
    frontier = quality.core10_shadow_frontier(core_pool, [x["fcode"] for x in core10])
    ceiling = quality.best_effort_core10(core50, as_of, conn)
    ceiling["current_effective_bets"] = independence.get("effective_bets")
    ceiling["greedy_is_upper_bound"] = False
    out = {
        "research_only": True,
        "run_id": run["run_id"],
        "business_date": run.get("business_date"),
        "nav_as_of": run.get("nav_as_of"),
        "generated_at": db.now_iso(),
        "boards": board_summaries,
        "core10": {
            "codes": [x["fcode"] for x in core10],
            "concentration": _concentration(core10),
            "rows": core_rows,
            "independence": independence,
        },
        "quality_tiers": quality_tiers,
        "quality": {
            "core10": {**independence, "rows": core_rows},
            "core10_ceiling": ceiling,
            "core10_frontier": frontier,
            "core10_pool_size": len(core_pool),
            "index_meta": quality.INDEX_META,
        },
        "long5": _long5_board(conn, cfg, run, picks),
        "walkforward": _walkforward_summary(cfg),
        "evidence": dict(cfg.get("evidence") or {}),
        "notes": [
            "因子 beta 来自指数回归，r2 低时只作参考；杠杆来自资产配置表，不是合同杠杆。",
            "基准分类是文本启发式，只能发现明显错配；复合基准不判错配。",
            "合同基准只映射到项目已采集的 4 个指数；短端/信用分层/1-3年基准只能标代理，不伪造精确超额。",
            "alpha 是扣除三因子暴露后的残差，仍含票息 carry，不是纯经理技能；只作同风格参考线，不能当质量通过率。",
            "5 年倾斜组合是影子研究，不进入生产榜单；要切生产必须先过前瞻与 OOS。",
            "幸存者偏差仍只部分校正，不能把本报告当作“已验证优质基金发现器”。",
        ],
    }
    try:
        from .. import survivorship
        out["survivorship"] = survivorship.summary(conn, lookback_days=30, limit=10)
    except Exception as exc:  # noqa: BLE001
        out["survivorship"] = {"status": "error", "error": str(exc)[:200]}
    return out


def _walkforward_summary(cfg: dict[str, Any]) -> dict[str, Any] | None:
    root = Path((cfg.get("paths") or {}).get("report_root") or "reports")
    path = root / "analysis" / "walkforward-2026-11-10.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {"path": str(path), "cohorts": len(data.get("cohorts") or []),
            "horizon_days": data.get("horizon_days"), "summary": data.get("summary") or {}}


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return ("%%.%df" % digits) % float(value)
    except (TypeError, ValueError):
        return str(value)


def render(result: dict[str, Any]) -> str:
    lines = ["# Phase 2 影子研究：长期权重、暴露与基准匹配", ""]
    lines.append("运行 %s，业务日 %s，净值日 %s。本报告只读，不改变生产榜单。"
                 % (result["run_id"], result.get("business_date"), result.get("nav_as_of")))
    lines.append("")
    lines.append("## 1. 榜单数据质量与暴露")
    lines.append("")
    lines.append("| 榜单 | 只数 | 因子覆盖 | 基准覆盖 | 杠杆代理 | 久期β中位 | 信用β中位 | 基准错配 | 5年覆盖 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for board, label in BOARDS:
        b = result["boards"].get(board)
        if not b:
            continue
        lines.append("| %s | %s | %s/%s | %s/%s | %s | %s | %s | %s | %s/%s |"
                     % (label, b["n"], b["factor_covered"], b["n"], b["benchmark_covered"], b["n"],
                        len(b["leveraged"]), _fmt(b["avg_duration_beta"], 3),
                        _fmt(b["avg_credit_beta"], 3), len(b["benchmark_mismatch"]),
                        b["five_year_covered"], b["n"]))
    lines.append("")
    lines.append("## 2. 核心 10 只的暴露")
    lines.append("")
    lines.append("| 代码 | 名称 | 公司 | 风格 | 置信度 | 久期β | 信用β | 转债β | r2 | 杠杆 | 5年覆盖 | 基准 | 质量分层 |")
    lines.append("|---|---|---|---|---|---:|---:|---:|---:|---|---:|---|---|")
    for x in result["core10"]["rows"]:
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                     % (x["fcode"], x.get("name") or "", x.get("company_name") or "",
                        x.get("style_code") or "", x.get("style_confidence") or "",
                        _fmt(x.get("beta_duration"), 3), _fmt(x.get("beta_credit"), 3),
                        _fmt(x.get("beta_convertible"), 3), _fmt(x.get("factor_r2"), 2),
                        "是" if x.get("leverage") else "否",
                        _fmt(x.get("coverage_5y"), 2), x.get("benchmark") or "—",
                        x.get("quality_tier") or "—"))
    lines.append("")
    conc = result["core10"]["concentration"]
    lines.append("前三大公司：%s；单一公司最高 %s 只（%s）。"
                 % ("、".join("%s %s" % (k, v) for k, v in list(conc["companies"].items())[:3]),
                    conc["max_company"][1], _fmt(conc["max_company_share"], 2)))
    lines.append("")
    enh = result["boards"].get(T.BOARD_ENHANCED) or {}
    if enh:
        lines.append("## 3. 增强债的 3 年风险")
        lines.append("")
        lines.append("覆盖 %s/%s；3 年最大回撤中位 %s%%，P90 %s%%；3 年波动中位 %s%%，P90 %s%%；"
                     "文本启发式风格 %s 只。"
                     % (enh["enhanced_3y_covered"], enh["n"], _fmt(enh["median_mdd_3y"], 2),
                        _fmt(enh.get("mdd_3y_p90_pct"), 2), _fmt(enh["median_vol_3y"], 2),
                        _fmt(enh.get("vol_3y_p90_pct"), 2), len(enh["text_heuristic"])))
        if enh["worst_mdd_3y"]:
            lines.append("")
            lines.append("回撤最大的 5 只：" + "、".join(
                "%s %s(%s%%)" % (x["fcode"], x.get("name") or "", _fmt(x.get("mdd_3y"), 2))
                for x in enh["worst_mdd_3y"]))
        lines.append("")
    lines.append("## 4. 5 年倾斜影子组合（对照，不发布）")
    lines.append("")
    l5 = result["long5"]
    lines.append("合格候选 %s 只；与当前核心10 重叠 %s 只，Jaccard %s；新增 %s，移出 %s。"
                 % (l5["eligible"], l5["overlap"], _fmt(l5["jaccard"], 3),
                    len(l5["added"]), len(l5["removed"])))
    lines.append("")
    lines.append("| 代码 | 名称 | 风格 | 5年倾斜分 | 5年覆盖 |")
    lines.append("|---|---|---|---:|---:|")
    for x in l5["rows"]:
        lines.append("| %s | %s | %s | %s | %s |"
                     % (x["fcode"], x.get("name") or "", x.get("style_code") or "",
                        _fmt(x.get("long5_score"), 2), _fmt(x.get("long5_coverage"), 2)))
    lines.append("")
    wf = result.get("walkforward")
    if wf and wf.get("summary"):
        lines.append("## 5. 前瞻证据重算（%s 个半年队列，horizon %s 天）" % (wf["cohorts"], wf.get("horizon_days") or "—"))
        lines.append("")
        lines.append("| 指标 | 年化% | 夏普 | 命中率 | IC年化 | IC夏普 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for key in ("rank_score", "return_6m", "annual_3y", "annual_5y", "mdd_5y"):
            s = wf["summary"].get(key) or {}
            lines.append("| %s | %s | %s | %s%% | %s | %s |"
                         % (key, _fmt(s.get("annual_mean_pct")), _fmt(s.get("sharpe_mean")),
                            _fmt((s.get("hit_rate") or 0) * 100, 1), _fmt(s.get("ic_annual"), 3),
                            _fmt(s.get("ic_sharpe"), 3)))
        lines.append("")
        lines.append("5 年证据（annual_5y、mdd_5y）的 IC 年化不低，但 IC 夏普为负、队列间不稳定；"
                     "rank_score、return_6m、annual_3y 的命中率和 IC 夏普更好。因此“5 年倾斜”只能作为影子对照，"
                     "不能据此替换生产权重。")
        lines.append("")
    lines.extend(quality.render_quality_sections(result))
    lines.append("## 10. 结论与限制")
    lines.append("")
    lines.append("- 因子/杠杆/基准三项覆盖仍不完整；覆盖不足的榜单不能据此下“暴露健康”的结论。")
    lines.append("- 合同基准只有 4 个指数可映射；短端/1-3年/信用分层合同只能标代理，不能据此宣称精确超额。")
    lines.append("- 在合同可精确映射 CBC 的样本内，3 年超额中位为负、正超额占比低（见第 6 节）；扣费后没有稳定跑赢总回报指数，不能把候选池称为已实现绝对超额。")
    lines.append("- alpha 是扣除三因子暴露后的残差，仍含票息 carry 与选券；增强债约一半 t<2，不能把 alpha 当质量通过率或唯一质量证据。")
    lines.append("- core10 个体 alpha 大多过参考线，组合独立性仍未达标；1.8 只能作为披露/分级目标，不能单独硬判。")
    lines.append("- 5 年倾斜组合回答“若把权重移向 5 年证据会选谁”；6 队列前瞻证据不支持它替换生产权重，只能留作影子对照。")
    lines.append("- 基准错配是文本启发式，需回到基金合同核对；复合基准不判错配。")
    lines.append("- 幸存者偏差只部分校正；报告只能写“候选池”，不能写“已验证优质基金”。")
    return "\n".join(lines)


def write_outputs(result: dict[str, Any], report_root: str | Path) -> tuple[Path, Path]:
    out_dir = Path(report_root) / "analysis" / "phase2"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / ("%s.json" % result["run_id"])
    md_path = out_dir / ("%s.md" % result["run_id"])
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render(result), encoding="utf-8")
    return json_path, md_path
