"""Walkforward：在历史 as-of 上重建榜单，检验评分与排序键的前瞻表现（P2-1）。

回答的问题：给定某年 6-30 的信息，当前这套口径挑出来的前 N 只，接下来一年是不是真的更好？

刻意复用生产口径，避免"回测一套、上线另一套"：
- 指标：metrics.engine.compute_fund(dry_run=True) 按 as_of 现算（不落库）；
- 分位与评分：screen.engine.compute_percentiles + score_candidate（同风格分位，样本 <15 回退全局）；
- 门禁：base_gate（L1/L4/L5/L5.5）+ fixed_checks（8 项固定线）。

不做的（并说明为什么）：
- L2 可投性、L3 持有期：历史申赎状态不可回溯，硬套当前状态会引入前视，直接跳过；
- L7 动态层：按批次相对分位，跨队列不可比，留待批次内稳定性监控；
- 新锐榜/观察榜/受限榜：只检验正式榜（稳健+增强母体）。

增强债单独回测（--enhanced）：
- strict：ADR-031 之前的 7 项绝对准入，年化用 1 年窗口，不用 ADR-035 的 1y+3y 混合；
- relative：当前上线的 8 项相对准入（同风格分位 + 地板 + 正式分），再套生产同款
  每风格配额、总量上限与 family 折叠；
- 除通过/入选数外，还统计 6/7、5/7 近失组的前瞻表现，以及 checks_passed 与前瞻
  年化/夏普的 Spearman，回答"严格线是筛掉了差基金，还是只是筛掉了数量"。
- 严格口径历史通过者并不少（2020/2025 队列分别 47/76 只），但当前横截面只有个位数，
  所以不能拿"今天 0 只通过"直接推断历史。

口径警告（结论要按"上限"读）：
- 风格优先用 fund_style_history 的 as-of 快照（ADR-065）；历史表 2026-11 才有数据，
  之前年份回退当前标签，cohort 里会打印 style_asof_coverage，前视影响随时间递减；
- 规模用 fund_scale_history 的 as-of 值（季报口径，回看 45 天）；费率仍用当前值（慢变量）；
- 宇宙只含存续基金 → 幸存者偏差，前瞻收益整体偏高。

用法：
    python3 -m fundana walkforward --years 2020,2021,2022,2023,2024,2025 --top 118
    python3 -m fundana walkforward --years 2020,2021,2022,2023,2024,2025 --enhanced

2020 是当前最早可用的队列（固定线通过者 155 只 > top 118）；2019 及更早通过者
不足 top，六种排序键会退化成同一组，只能看覆盖率，不能比较排序键。
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Sequence

from .. import db, fundtypes
from ..fuse.scale import scale_asof_map
from ..fuse.style import style_asof_map
from ..metrics import contract as metrics_contract
from ..metrics import engine as metrics_engine
from ..metrics import risk as risk_mod
from ..metrics import window as window_mod
from ..screen import engine as screen_engine
from ..screen import thresholds as T

DEFAULT_YEARS = (2020, 2021, 2022, 2023, 2024, 2025)
MIN_FORWARD_OBS = 200
FORWARD_TOLERANCE_DAYS = 20
# Phase 2 研究键：long_score 是生产 long_core/long_formal 的 40/60 合成，
# annual_5y/mdd_5y 是纯 5 年证据，用来检验“长期权重是否真的改善前瞻表现”。
RANK_KEYS = ("rank_score", "composite", "formal_score", "sharpe", "return_6m", "return_1y",
             "long_score", "annual_3y", "annual_5y", "mdd_5y")


def cohort_dates(years: Sequence[int], month_day: str = "06-30") -> list[str]:
    return [f"{int(y)}-{month_day}" for y in years]


def _coverage(conn, as_of: str) -> dict[str, dict[str, Any]]:
    """as_of 时点的净值覆盖（点数/首末日期/新鲜度），替代 fund_coverage 的"当前"快照。"""
    out: dict[str, dict[str, Any]] = {}
    d1 = date.fromisoformat(as_of)
    for r in conn.execute(
            "SELECT fcode, COUNT(*) AS n, MIN(nav_date) AS f, MAX(nav_date) AS l"
            " FROM fund_nav_daily WHERE nav_date<=? GROUP BY fcode", (as_of,)):
        out[r["fcode"]] = {"nav_points": r["n"], "nav_first_date": r["f"],
                           "nav_last_date": r["l"],
                           "freshness_days": (d1 - date.fromisoformat(r["l"])).days}
    return out


def _forward(conn, cfg, fcode: str, start: str, end: str) -> dict[str, Any] | None:
    """前瞻窗口（start, end] 的收益与风险指标；观测不足返回 None。"""
    raw = conn.execute(
        "SELECT nav_date, unit_nav, acc_nav, growth_pct FROM fund_nav_daily"
        " WHERE fcode=? AND nav_date>? AND nav_date<=? ORDER BY nav_date",
        (fcode, start, end)).fetchall()
    rows = [window_mod.NavRow(r["nav_date"], r["unit_nav"], r["acc_nav"], r["growth_pct"])
            for r in raw]
    # 复用生产的有效收益序列（含 growth_pct 伪零还原），避免回测与上线口径分叉。
    rets, _recon = window_mod.effective_returns(rows)
    if len(rets) < MIN_FORWARD_OBS:
        return None
    span = (date.fromisoformat(rows[-1].nav_date) - date.fromisoformat(start)).days
    rf = float(cfg.get("risk", "risk_free_rate", default=0.0164))
    td = int(cfg.get("risk", "trading_days_per_year", default=252))
    caps = cfg.get("risk", "caps", default=metrics_contract.CAPS) or metrics_contract.CAPS
    m = risk_mod.compute(rets, span, risk_free_rate=rf, mar=0.0, period_days=365,
                         trading_days=td, caps=caps)
    return {"total_pct": m.get("total_return_pct"), "annual": m.get("annual_dec"),
            "sharpe": m.get("sharpe")}


def _metric_end_date(metrics: dict[int, dict[str, Any]]) -> str | None:
    """回测指标窗口的截止日；必须取现算窗口，不能沿用库里最新指标的 end_date。"""
    m365 = (metrics or {}).get(365) or {}
    end = m365.get("end_date")
    return str(end)[:10] if end else None


def _asof_candidates(conn, cfg, as_of: str, contract_id: str,
                     style_map: dict[str, str] | None = None,
                     scale_map: dict[str, dict] | None = None,
                     include_liquidated: bool = True) -> list[Any]:
    """当前静态字段 + as_of 现算指标。规模改用历史季报，费率/风格见模块 docstring。

    include_liquidated=False 只用于“回填清盘基金前”的对照基线：把 fund_lifecycle
    已确认清盘的代码全部排除，量化幸存者偏差校正前后的差异。
    """
    nav_age = int((cfg.get("quality", default={}) or {}).get("nav_age_limit_days", 10))
    cov = _coverage(conn, as_of)
    liquidated = ({r[0] for r in conn.execute(
        "SELECT DISTINCT fcode FROM fund_lifecycle WHERE status IN ('liquidated','merged')")}
        if not include_liquidated else set())
    out = []
    types = fundtypes.resolve(cfg=cfg)
    # 历史回测没有当时的申赎快照，显式退回库内最新快照（生产筛选不开启）。
    for cand in screen_engine._load_candidates(  # noqa: SLF001
            conn, as_of, contract_id, types, buyable_fallback=True):
        if cand["fcode"] in liquidated:
            continue
        dry = metrics_engine.compute_fund(conn, cfg, cand["fcode"], as_of, dry_run=True)
        if not dry.get("ok"):
            continue
        cand["metrics"] = dry["dry"]
        # 关键：_load_candidates 的 metric_end_date 来自库里最新指标（2026-11），
        # 回测 as_of 必须用现算窗口的 end_date，否则会误报 metric_lookahead。
        cand["metric_end_date"] = _metric_end_date(cand["metrics"])
        if style_map:
            cand["style_code"] = style_map.get(cand["fcode"], cand.get("style_code"))
        if scale_map:
            sc = scale_map.get(cand["fcode"])
            if sc and sc.get("asset_scale_billion") is not None:
                cand["asset_scale_billion"] = sc["asset_scale_billion"]
                cand["scale_report_period"] = sc.get("report_period")
        cand.update(cov.get(cand["fcode"], {"nav_points": 0, "nav_first_date": None,
                                            "nav_last_date": None, "freshness_days": None}))
        cand.update(cov.get(cand["fcode"], {"nav_points": 0, "nav_first_date": None,
                                            "nav_last_date": None, "freshness_days": None}))
        # 历史申赎/持有期不可回溯：跳过 L2/L3，只保留可复现的门禁。
        cand["buyable_state"] = "confirmed_yes"
        cand["effective_days"] = None
        cand["lockup_type"] = None
        cand["_nav_age_limit"] = nav_age
        out.append(cand)
    return out


def _ranks(values: dict[str, float]) -> dict[str, float]:
    """平均名次（并列取平均），用于 Spearman。"""
    items = sorted(values.items(), key=lambda kv: kv[1])
    out: dict[str, float] = {}
    i = 0
    while i < len(items):
        j = i
        while j + 1 < len(items) and items[j + 1][1] == items[i][1]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            out[items[k][0]] = avg
        i = j + 1
    return out


def spearman(xs: dict[str, float], ys: dict[str, float]) -> float | None:
    keys = [k for k in xs if k in ys and xs[k] is not None and ys[k] is not None]
    if len(keys) < 20:
        return None
    rx = _ranks({k: xs[k] for k in keys})
    ry = _ranks({k: ys[k] for k in keys})
    n = len(keys)
    mx = sum(rx.values()) / n
    my = sum(ry.values()) / n
    num = sum((rx[k] - mx) * (ry[k] - my) for k in keys)
    dx = sum((rx[k] - mx) ** 2 for k in keys) ** 0.5
    dy = sum((ry[k] - my) ** 2 for k in keys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 4)


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _key_value(cand, scores: dict[str, Any], key: str) -> float | None:
    if key == "rank_score":
        return scores.get("rank_score")
    if key == "composite":
        return scores.get("composite")
    if key == "formal_score":
        return scores.get("formal_score")
    if key == "long_score":
        return scores.get("long_score")
    return screen_engine._raw(cand, key)  # noqa: SLF001


def _bucket_stats(codes: Sequence[str], fwd: dict[str, dict[str, Any]],
                  base_annual: float | None) -> dict[str, Any]:
    """一组基金的前瞻统计；只把有 365 天前瞻值的基金计入均值/命中率。"""
    picked = list(codes)
    annual = [fwd[c]["annual"] for c in picked
              if (fwd.get(c) or {}).get("annual") is not None]
    sharpe = [fwd[c]["sharpe"] for c in picked
              if (fwd.get(c) or {}).get("sharpe") is not None]
    hits = sum(1 for c in picked
               if (fwd.get(c) or {}).get("annual") is not None
               and base_annual is not None and fwd[c]["annual"] > base_annual)
    return {
        "n": len(picked),
        "n_forward": len(annual),
        "annual_mean_pct": None if not annual else round(100 * sum(annual) / len(annual), 2),
        "annual_median_pct": None if not annual else round(100 * _median(annual), 2),
        "sharpe_mean": None if not sharpe else round(sum(sharpe) / len(sharpe), 3),
        "sharpe_median": None if not sharpe else round(_median(sharpe), 3),
        "hit_rate": (round(hits / len(annual), 4)
                     if annual and base_annual is not None else None),
    }


def _check_failure_counts(checks_by_code: dict[str, dict[str, dict[str, Any]]]) -> dict[str, dict[str, int]]:
    """逐项统计严格准入的通过/失败/缺值，定位是哪一条线在卡人。"""
    out: dict[str, dict[str, int]] = {}
    for checks in checks_by_code.values():
        for name, ch in checks.items():
            row = out.setdefault(name, {"n": 0, "pass": 0, "fail": 0, "missing": 0})
            row["n"] += 1
            if ch.get("pass"):
                row["pass"] += 1
            else:
                row["fail"] += 1
                if ch.get("reason") == "missing":
                    row["missing"] += 1
    return out


def _ic_pair(values: dict[str, float | None], fwd: dict[str, dict[str, Any]],
             field: str) -> float | None:
    xs: dict[str, float] = {}
    ys: dict[str, float] = {}
    for code, val in values.items():
        f = fwd.get(code)
        if f is None or val is None or f.get(field) is None:
            continue
        xs[code] = float(val)
        ys[code] = float(f[field])
    return spearman(xs, ys)


def _enhanced_picks(codes: Sequence[str], mother: list[Any], scores: dict[str, dict[str, Any]],
                    cands: list[Any], cfg) -> list[str]:
    """用生产同款 _rank 做增强榜配额 + family 折叠，返回最终入选代码。"""
    if not codes:
        return []
    by_code = {c["fcode"]: c for c in mother}
    decisions = {
        code: {"passed": 1, "board": T.BOARD_ENHANCED, "scores": scores[code],
               "style_code": by_code[code]["style_code"]}
        for code in codes
    }
    screen_cfg = cfg.get("screen", default={}) or {}
    target_max = int(screen_cfg.get("formal_target_max", T.FORMAL_TARGET_MAX))
    group_min = float(screen_cfg.get("group_min_ratio", T.GROUP_MIN_RATIO))
    picks = screen_engine._rank(decisions, scores, cands, target_max=target_max,  # noqa: SLF001
                                group_min_ratio=group_min)
    return [item["fcode"] for item in picks.get(T.BOARD_ENHANCED, [])]


def _enhanced_block(conn, cfg, as_of: str, cands: list[Any], gate_pass: list[Any],
                    end: str) -> dict[str, Any]:
    """增强债 strict vs relative 的历史准入回测（--enhanced）。"""
    mother = [c for c in gate_pass if c["style_code"] in T.ENHANCED_STYLES]
    if not mother:
        return {"n": 0, "note": "no_enhanced_candidates", "strict": {}, "relative": {}}

    # 相对准入的正式分要用生产同款全量分位母体（不是 walkforward 固定线的 gate_pass），
    # 否则 formal_score 会因母体不同而系统性偏高。
    pcts = screen_engine.compute_percentiles(cands)
    scores = {c["fcode"]: screen_engine.score_candidate(c, pcts[c["fcode"]]) for c in cands}
    by_code = {c["fcode"]: c for c in mother}

    strict_checks = {c["fcode"]: screen_engine.enhanced_checks(c, c["style_code"]) for c in mother}
    strict_codes = [code for code, checks in strict_checks.items()
                    if checks and all(ch["pass"] for ch in checks.values())]
    peers = screen_engine.enhanced_peer_stats(mother)
    relative_checks = {
        c["fcode"]: screen_engine.enhanced_relative_checks(
            c, c["style_code"], peers.get(c["style_code"]), scores[c["fcode"]].get("formal_score"))
        for c in mother
    }
    relative_codes = [code for code, checks in relative_checks.items()
                      if checks and all(ch["pass"] for ch in checks.values())]

    strict_picks = _enhanced_picks(strict_codes, mother, scores, cands, cfg)
    relative_picks = _enhanced_picks(relative_codes, mother, scores, cands, cfg)

    fwd = {c["fcode"]: _forward(conn, cfg, c["fcode"], as_of, end) for c in mother}
    fwd = {code: row for code, row in fwd.items() if row}
    base_annual = _median([row["annual"] for row in fwd.values()
                           if row.get("annual") is not None])
    checks_passed = {code: sum(1 for ch in checks.values() if ch["pass"])
                     for code, checks in strict_checks.items()}
    near6 = [code for code, n in checks_passed.items() if n == 6]
    near5 = [code for code, n in checks_passed.items() if n == 5]

    strict_set = set(strict_codes)
    strict_pick_set = set(strict_picks)
    relative_set = set(relative_codes)
    relative_pick_set = set(relative_picks)
    by_style: dict[str, Any] = {}
    for style in T.ENHANCED_STYLES:
        group = [c["fcode"] for c in mother if c["style_code"] == style]
        if not group:
            continue
        by_style[style] = {
            "n": len(group),
            "forward_n": sum(1 for code in group if code in fwd),
            "strict_pass_n": sum(1 for code in group if code in strict_set),
            "strict_pick_n": sum(1 for code in group if code in strict_pick_set),
            "relative_pass_n": sum(1 for code in group if code in relative_set),
            "relative_pick_n": sum(1 for code in group if code in relative_pick_set),
            "strict_pick_stats": _bucket_stats([c for c in group if c in strict_pick_set], fwd, base_annual),
            "relative_pick_stats": _bucket_stats([c for c in group if c in relative_pick_set], fwd, base_annual),
        }

    notes = []
    if not strict_picks:
        notes.append("strict_no_pick")
    if not relative_picks:
        notes.append("relative_no_pick")
    return {
        "n": len(mother),
        "forward_n": len(fwd),
        "strict": {
            "pass_n": len(strict_codes),
            "pick_n": len(strict_picks),
            "pass_stats": _bucket_stats(strict_codes, fwd, base_annual),
            "pick_stats": _bucket_stats(strict_picks, fwd, base_annual),
        },
        "relative": {
            "pass_n": len(relative_codes),
            "pick_n": len(relative_picks),
            "pass_stats": _bucket_stats(relative_codes, fwd, base_annual),
            "pick_stats": _bucket_stats(relative_picks, fwd, base_annual),
        },
        "near_miss": {
            "6of7": _bucket_stats(near6, fwd, base_annual),
            "5of7": _bucket_stats(near5, fwd, base_annual),
        },
        "checks": _check_failure_counts(strict_checks),
        "ic": {
            "checks_passed": {"annual": _ic_pair(checks_passed, fwd, "annual"),
                              "sharpe": _ic_pair(checks_passed, fwd, "sharpe")},
            "formal_score": {"annual": _ic_pair(
                {c["fcode"]: scores[c["fcode"]].get("formal_score") for c in mother}, fwd, "annual"),
                "sharpe": _ic_pair(
                {c["fcode"]: scores[c["fcode"]].get("formal_score") for c in mother}, fwd, "sharpe")},
            "rank_score": {"annual": _ic_pair(
                {c["fcode"]: scores[c["fcode"]].get("rank_score") for c in mother}, fwd, "annual"),
                "sharpe": _ic_pair(
                {c["fcode"]: scores[c["fcode"]].get("rank_score") for c in mother}, fwd, "sharpe")},
        },
        "pool": {
            "n": len(fwd),
            "annual_median_pct": None if base_annual is None else round(100 * base_annual, 2),
            "sharpe_median": None if not fwd else _median(
                [row["sharpe"] for row in fwd.values() if row.get("sharpe") is not None]),
        },
        "by_style": by_style,
        "note": ",".join(notes) if notes else None,
    }


def run_cohort(conn, cfg, as_of: str, *, top: int = 118, horizon_days: int = 365,
               contract_id: str | None = None, style_asof: bool = True,
               include_enhanced: bool = False,
               include_portfolio: bool = False,
               portfolio_weighting: str = "equal",
               include_liquidated: bool = True) -> dict[str, Any]:
    contract_id = contract_id or str(cfg.get("risk", "formula_contract",
                                             default=metrics_contract.FORMULA_CONTRACT))
    style_map, style_hits = style_asof_map(conn, as_of) if style_asof else ({}, 0)
    scale_map = scale_asof_map(conn, as_of)
    cands = _asof_candidates(conn, cfg, as_of, contract_id, style_map or None, scale_map,
                             include_liquidated=include_liquidated)
    gate_pass = [c for c in cands
                 if not screen_engine.base_gate(c, as_of, c["_nav_age_limit"])[1]]
    pcts = screen_engine.compute_percentiles(gate_pass)
    scores = {c["fcode"]: screen_engine.score_candidate(c, pcts[c["fcode"]]) for c in gate_pass}
    fixed_pass = [c for c in gate_pass
                  if all(ch["pass"] for ch in screen_engine.fixed_checks(c, scores[c["fcode"]]).values())]

    end = date.fromordinal(date.fromisoformat(as_of).toordinal() + horizon_days).isoformat()
    enhanced = (_enhanced_block(conn, cfg, as_of, cands, gate_pass, end)
                if include_enhanced else None)
    fwd = {c["fcode"]: _forward(conn, cfg, c["fcode"], as_of, end) for c in fixed_pass}
    pool = {k: v for k, v in fwd.items() if v}
    if not pool:
        out = {"as_of": as_of, "universe": len(cands), "gate_pass": len(gate_pass),
               "fixed_pass": len(fixed_pass), "note": "no_forward_window", "boards": {},
               "scale_asof_hits": len(scale_map), "style_asof_hits": style_hits}
        if enhanced is not None:
            out["enhanced"] = enhanced
        return out

    pool_annual = [v["annual"] for v in pool.values() if v["annual"] is not None]
    pool_sharpe = [v["sharpe"] for v in pool.values() if v["sharpe"] is not None]
    pool_med_annual = _median(pool_annual)

    boards: dict[str, Any] = {}
    for key in RANK_KEYS:
        ranked = sorted((c for c in fixed_pass if c["fcode"] in pool),
                        key=lambda c: (-(_key_value(c, scores[c["fcode"]], key) or -1e9), c["fcode"]))
        picked = ranked[:top]
        annual = [pool[c["fcode"]]["annual"] for c in picked
                  if pool[c["fcode"]]["annual"] is not None]
        sharpe = [pool[c["fcode"]]["sharpe"] for c in picked
                  if pool[c["fcode"]]["sharpe"] is not None]
        hits = sum(1 for c in picked if pool[c["fcode"]]["annual"] is not None
                   and pool_med_annual is not None
                   and pool[c["fcode"]]["annual"] > pool_med_annual)
        boards[key] = {
            "n": len(picked),
            "annual_mean_pct": None if not annual else round(100 * sum(annual) / len(annual), 2),
            "annual_median_pct": None if not annual else round(100 * _median(annual), 2),
            "sharpe_mean": None if not sharpe else round(sum(sharpe) / len(sharpe), 3),
            "sharpe_median": None if not sharpe else round(_median(sharpe), 3),
            "hit_rate": round(hits / len(picked), 4) if picked else None,
        }

    ic = {}
    for key in RANK_KEYS:
        xs = {}
        ys_annual, ys_sharpe = {}, {}
        for c in fixed_pass:
            f = pool.get(c["fcode"])
            v = _key_value(c, scores[c["fcode"]], key)
            if f is None or v is None:
                continue
            xs[c["fcode"]] = float(v)
            if f["annual"] is not None:
                ys_annual[c["fcode"]] = f["annual"]
            if f["sharpe"] is not None:
                ys_sharpe[c["fcode"]] = f["sharpe"]
        ic[key] = {"annual": spearman(xs, ys_annual), "sharpe": spearman(xs, ys_sharpe)}

    out = {
        "as_of": as_of,
        "forward_window": [as_of, end],
        "style_asof_hits": style_hits,
        "style_asof_coverage": round(style_hits / len(cands), 4) if cands else 0.0,
        "scale_asof_hits": len(scale_map),
        "scale_asof_coverage": round(len(scale_map) / len(cands), 4) if cands else 0.0,
        "universe": len(cands),
        "gate_pass": len(gate_pass),
        "fixed_pass": len(fixed_pass),
        "pool": {
            "n": len(pool),
            "annual_median_pct": None if pool_med_annual is None else round(100 * pool_med_annual, 2),
            "sharpe_median": None if not pool_sharpe else round(_median(pool_sharpe), 3),
        },
        "boards": boards,
        "ic": ic,
    }
    if enhanced is not None:
        out["enhanced"] = enhanced
    if include_portfolio:
        from . import portfolio as portfolio_mod
        out["portfolio"] = portfolio_mod.run_block(
            conn, cfg, as_of, cands, gate_pass, fixed_pass, scores, end,
            weighting=portfolio_weighting)
    return out


def run(conn, cfg, years: Sequence[int] = DEFAULT_YEARS, *, top: int = 118,
        horizon_days: int = 365, style_asof: bool = True,
        include_enhanced: bool = False,
        include_portfolio: bool = False,
        portfolio_weighting: str = "equal",
        include_liquidated: bool = True) -> dict[str, Any]:
    cohorts = [run_cohort(conn, cfg, d, top=top, horizon_days=horizon_days,
                          style_asof=style_asof, include_enhanced=include_enhanced,
                          include_portfolio=include_portfolio,
                          portfolio_weighting=portfolio_weighting,
                          include_liquidated=include_liquidated)
               for d in cohort_dates(years)]
    summary: dict[str, dict[str, Any]] = {}
    for key in RANK_KEYS:
        rows = [c["boards"][key] for c in cohorts if c.get("boards", {}).get(key)]
        if not rows:
            continue
        summary[key] = {
            "annual_mean_pct": round(sum(r["annual_mean_pct"] or 0.0 for r in rows) / len(rows), 2),
            "sharpe_mean": round(sum(r["sharpe_mean"] or 0.0 for r in rows) / len(rows), 3),
            "hit_rate": round(sum(r["hit_rate"] or 0.0 for r in rows) / len(rows), 4),
            "ic_annual": round(sum((c["ic"][key]["annual"] or 0.0) for c in cohorts
                                   if c.get("boards", {}).get(key)) / len(rows), 4),
            "ic_sharpe": round(sum((c["ic"][key]["sharpe"] or 0.0) for c in cohorts
                                   if c.get("boards", {}).get(key)) / len(rows), 4),
        }
    out = {"top": top, "horizon_days": horizon_days,
           "portfolio_weighting": portfolio_weighting,
           "cohorts": cohorts, "summary": summary}
    if include_portfolio:
        from . import portfolio as portfolio_mod
        out["portfolio_summary"] = portfolio_mod.summarize(cohorts)
    return out


def _num(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def render(result: dict[str, Any]) -> str:
    """纯文本表：每队列一行，键一列；--enhanced 时追加增强债 strict/relative 对照。"""
    lines = [f"walkforward top={result['top']} horizon={result['horizon_days']}d"]
    head = f"{'as_of':<12}{'gate':>7}{'fixed':>7}{'pool':>6}"
    keys = [k for k in RANK_KEYS if k in result["summary"]]
    for k in keys:
        head += f"{k[:11]:>13}"
    lines.append(head)
    for c in result["cohorts"]:
        if not c.get("boards"):
            lines.append(f"{c['as_of']:<12}{'-':>6}{'-':>7}  {c.get('note')}")
            continue
        row = f"{c['as_of']:<12}{c['gate_pass']:>7}{c['fixed_pass']:>7}{c['pool']['n']:>6}"
        for k in keys:
            b = c["boards"].get(k) or {}
            row += f"{(b.get('annual_mean_pct') or 0.0):>7.2f}/{(b.get('sharpe_mean') or 0.0):>5.2f}"
        lines.append(row)
    if result.get("cohorts"):
        cov = [c.get("scale_asof_coverage") or 0.0 for c in result["cohorts"] if c.get("boards")]
        if cov:
            lines.append(f"\nscale_asof 覆盖：均值 {sum(cov)/len(cov):.1%}，最低 {min(cov):.1%}（缺失时回退当前规模）")
    lines.append("")
    lines.append(f"{'key':<14}{'年化%':>8}{'夏普':>7}{'命中率':>8}{'IC年化':>8}{'IC夏普':>8}")
    for k in keys:
        s = result["summary"][k]
        lines.append(f"{k:<14}{s['annual_mean_pct']:>8.2f}{s['sharpe_mean']:>7.2f}"
                     f"{s['hit_rate']:>8.2%}{s['ic_annual']:>8.3f}{s['ic_sharpe']:>8.3f}")
    pf = [c for c in result["cohorts"] if c.get("portfolio")]
    if pf:
        lines.append("")
        weight_label = "等权" if result.get("portfolio_weighting", "equal") == "equal" else "逆波动率"
        lines.append(f"组合层回测（{weight_label}买入持有；基准=全市场可投债基等权组合）")
        lines.append(f"{'as_of':<12}{'board':<10}{'n':>4}{'年化%':>7}{'波动%':>7}"
                     f"{'夏普':>7}{'回撤%':>7}{'超额':>7}{'胜':>4}")
        for c in pf:
            b = c["portfolio"].get("benchmark") or {}
            for name in ("core10", "core50", "stable50", "enhanced"):
                s = (c["portfolio"].get("boards") or {}).get(name)
                if not s:
                    continue
                lines.append(
                    f"{c['as_of']:<12}{name:<10}{s.get('n_used', 0):>4}"
                    f"{_num(s.get('annual_pct')):>7}{_num(s.get('vol_pct')):>7}"
                    f"{_num(s.get('sharpe')):>7}{_num(s.get('mdd_pct')):>7}"
                    f"{_num(s.get('excess_pct')):>7}"
                    f"{'Y' if s.get('beat') else 'N':>4}")
            lines.append(f"{'':<12}{'benchmark':<10}{b.get('n_used', 0):>4}"
                         f"{_num(b.get('annual_pct')):>7}{_num(b.get('vol_pct')):>7}"
                         f"{_num(b.get('sharpe')):>7}{_num(b.get('mdd_pct')):>7}")
        ps = result.get("portfolio_summary") or {}
        if ps:
            lines.append("")
            lines.append(f"{'组合':<10}{'队列':>5}{'年化%':>8}{'夏普':>7}{'回撤%':>7}"
                         f"{'超额':>7}{'胜基准':>8}")
            for name in ("core10", "core50", "stable50", "enhanced", "benchmark"):
                s = ps.get(name)
                if not s:
                    continue
                lines.append(f"{name:<10}{s['cohorts']:>5}{s['annual_pct']:>8.2f}"
                             f"{s['sharpe']:>7.2f}{s['mdd_pct']:>7.2f}"
                             f"{s.get('excess_pct', 0.0):>7.2f}"
                             f"{_rate(s.get('beat_rate')):>8}")
        lines.append("注：不含交易成本/申赎费；基准为 base_gate 通过的全部债基等权；"
                     "000012 为价格指数，只看相对价格。")
    enhanced = [c for c in result["cohorts"] if c.get("enhanced")]
    if enhanced:
        lines.append("")
        lines.append("增强债准入回测（strict=7项绝对@1y；relative=当前相对口径；入选已套生产配额/family折叠）")
        lines.append(f"{'as_of':<12}{'enh':>5}{'strict':>7}{'pick':>5} |"
                     f"{'年化%':>7}{'夏普':>6}{'命中':>7} |"
                     f" {'relPass':>7} {'relPick':>7}{'年化%':>7}{'夏普':>6}{'命中':>7} |"
                     f"{'6/7':>5}{'年化%':>7}")
        for c in enhanced:
            e = c["enhanced"]
            s = e.get("strict") or {}
            r = e.get("relative") or {}
            ss = s.get("pick_stats") or {}
            rs = r.get("pick_stats") or {}
            nm = (e.get("near_miss") or {}).get("6of7") or {}
            lines.append(
                f"{c['as_of']:<12}{e.get('n', 0):>5}{s.get('pass_n', 0):>7}{s.get('pick_n', 0):>5} |"
                f"{_num(ss.get('annual_mean_pct')):>7}{_num(ss.get('sharpe_mean')):>6}"
                f"{_rate(ss.get('hit_rate')):>7} |"
                f" {r.get('pass_n', 0):>7} {r.get('pick_n', 0):>7}"
                f"{_num(rs.get('annual_mean_pct')):>7}{_num(rs.get('sharpe_mean')):>6}"
                f"{_rate(rs.get('hit_rate')):>7} |"
                f"{nm.get('n', 0):>5}{_num(nm.get('annual_mean_pct')):>7}")
        lines.append("注：strict 通过后按生产同款每风格 40 / 总量 110 / family 折叠入选；6/7 近失组不入选，只作对照。")
    lines.append("注：历史宇宙从当前 fund 表构造，无法纳入已移除的清盘基金；screen 的 as-of 生命周期子句只能排除仍在表的少数样本，前瞻收益与 IC 仍存在幸存者偏差。")
    return "\n".join(lines)
