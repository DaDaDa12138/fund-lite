"""经理/规模事件研究：事件发生后的前瞻收益是否更差（或更好）。

回答的问题：
- 近 12 个月换过基金经理的基金，接下来一年是否跑输？
- 现任经理任期很短（<1 年）是否意味着更高的不确定性？
- 规模一年内骤降/骤增，是否预示后续收益变化？

这是审计工具，不进入 L1–L10 准入；事件只用于解释榜单与产品告警。
事件特征全部按 as_of 截断，规模按 45 天披露滞后取最近一期，避免前视。

用法：
    python3 -m fundana events --years 2020,2021,2022,2023,2024,2025
    python3 -m fundana events --years 2020,2021,2022,2023,2024,2025 --out reports/analysis/events.json
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Sequence

from ..fuse.scale import family_scale_rows, scale_asof_map
from ..fuse.style import style_asof_map
from ..metrics import contract as metrics_contract
from ..screen import engine as screen_engine
from . import walkforward as wf

MANAGER_LOOKBACK_DAYS = 365
SCALE_LAG_DAYS = 45
TENURE_BUCKETS = (("lt180", 0, 180), ("180_365", 180, 365),
                   ("365_730", 365, 730), ("gte730", 730, None))
AUM_YOY_BUCKETS = (("lt_-30", None, -30.0), ("-30_0", -30.0, 0.0),
                   ("0_50", 0.0, 50.0), ("gte50", 50.0, None))
AUM_6M_BUCKETS = (("lt_-20", None, -20.0), ("-20_0", -20.0, 0.0),
                  ("0_30", 0.0, 30.0), ("gte30", 30.0, None))


def _parse(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def manager_features(conn, fcode: str, as_of: str,
                     lookback_days: int = MANAGER_LOOKBACK_DAYS) -> dict[str, Any]:
    """按 as_of 截断的经理任期特征；没有任职记录返回全 None。"""
    d = date.fromisoformat(as_of)
    start = (d - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        "SELECT mgr_id, mgr_name, start_date, end_date FROM fund_manager_tenure"
        " WHERE fcode=? AND start_date<=? ORDER BY start_date", (fcode, as_of)).fetchall()
    if not rows:
        return {"current_count": None, "max_tenure_days": None,
                "newest_tenure_days": None, "changes_365d": None,
                "last_change_days": None}
    current = [r for r in rows
               if not r["end_date"] or r["end_date"] >= as_of]
    tenures = []
    for r in current:
        s = _parse(r["start_date"])
        if s is not None:
            tenures.append((d - s).days)
    changes = [r for r in rows if r["start_date"] and r["start_date"] > start]
    last = max((r["start_date"] for r in rows if r["start_date"]), default=None)
    last_days = None
    if last:
        last_d = _parse(last)
        if last_d is not None:
            last_days = (d - last_d).days
    return {
        "current_count": len(current),
        "max_tenure_days": max(tenures) if tenures else None,
        "newest_tenure_days": min(tenures) if tenures else None,
        "changes_365d": len(changes),
        "last_change_days": last_days,
    }


def _nearest_row(rows, target: str, tolerance_days: int = 45):
    """在按报告期升序的 rows 里找目标日前后最近的一期；没有则退回 target 之前最近一期。"""
    target_d = date.fromisoformat(target)
    best = None
    best_diff = None
    for r in rows:
        rd = _parse(r["report_period"])
        if rd is None:
            continue
        diff = abs((rd - target_d).days)
        if diff <= tolerance_days and (best_diff is None or diff < best_diff):
            best, best_diff = r, diff
    if best is not None:
        return best
    prior = [r for r in rows if r["report_period"] <= target]
    return prior[-1] if prior else None


def _pct_change(latest: float | None, base: float | None) -> float | None:
    if latest is None or base is None or base <= 0:
        return None
    return round((latest / base - 1.0) * 100.0, 4)


def scale_features(conn, fcode: str, as_of: str,
                   lag_days: int = SCALE_LAG_DAYS) -> dict[str, Any]:
    """最近一期基金总规模与同比/半年变化；按 family 汇总份额，按 as_of-lag 截断。"""
    rows = family_scale_rows(conn, fcode, as_of, lag_days)
    if not rows:
        return {"report_period": None, "aum_billion": None, "aum_yoy_pct": None,
                "aum_6m_pct": None, "share_yoy_pct": None}
    latest = rows[-1]
    lp = _parse(latest["report_period"])
    if lp is None:
        return {"report_period": latest["report_period"], "aum_billion": None,
                "aum_yoy_pct": None, "aum_6m_pct": None, "share_yoy_pct": None}
    base_y = _nearest_row(rows, (lp - timedelta(days=365)).isoformat())
    base_6 = _nearest_row(rows, (lp - timedelta(days=182)).isoformat())
    return {
        "report_period": latest["report_period"],
        "aum_billion": latest["asset_scale_billion"],
        "aum_yoy_pct": _pct_change(latest["asset_scale_billion"],
                                   base_y["asset_scale_billion"] if base_y else None),
        "aum_6m_pct": _pct_change(latest["asset_scale_billion"],
                                  base_6["asset_scale_billion"] if base_6 else None),
        "share_yoy_pct": _pct_change(latest["share_scale_billion"],
                                     base_y["share_scale_billion"] if base_y else None),
    }


def _bucket(value: float | None, spec) -> str:
    if value is None:
        return "unknown"
    for name, lo, hi in spec:
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return name
    return "unknown"


def event_buckets(mf: dict[str, Any], sf: dict[str, Any]) -> dict[str, str]:
    changes = mf.get("changes_365d")
    change_bucket = ("unknown" if changes is None else
                     "0" if changes == 0 else "1" if changes == 1 else "2+")
    return {
        "manager_changes": change_bucket,
        "manager_tenure": _bucket(mf.get("max_tenure_days"), TENURE_BUCKETS),
        "aum_yoy": _bucket(sf.get("aum_yoy_pct"), AUM_YOY_BUCKETS),
        "aum_6m": _bucket(sf.get("aum_6m_pct"), AUM_6M_BUCKETS),
    }


def _cohort(conn, cfg, as_of: str, horizon_days: int,
            contract_id: str) -> dict[str, Any]:
    style_map, style_hits = style_asof_map(conn, as_of)
    scale_map = scale_asof_map(conn, as_of)
    cands = wf._asof_candidates(conn, cfg, as_of, contract_id, style_map or None, scale_map)
    gate_pass = [c for c in cands
                 if not screen_engine.base_gate(c, as_of, c["_nav_age_limit"])[1]]
    pcts = screen_engine.compute_percentiles(gate_pass)
    scores = {c["fcode"]: screen_engine.score_candidate(c, pcts[c["fcode"]]) for c in gate_pass}
    fixed_pass = [c for c in gate_pass
                  if all(ch["pass"] for ch in screen_engine.fixed_checks(c, scores[c["fcode"]]).values())]
    end = (date.fromisoformat(as_of) + timedelta(days=horizon_days)).isoformat()
    fwd = {c["fcode"]: wf._forward(conn, cfg, c["fcode"], as_of, end) for c in fixed_pass}
    pool = {k: v for k, v in fwd.items() if v}
    if not pool:
        return {"as_of": as_of, "universe": len(cands), "gate_pass": len(gate_pass),
                "fixed_pass": len(fixed_pass), "note": "no_forward_window",
                "style_asof_hits": style_hits, "scale_asof_hits": len(scale_map)}
    base_annual = wf._median([v["annual"] for v in pool.values() if v["annual"] is not None])
    rows = []
    for c in fixed_pass:
        if c["fcode"] not in pool:
            continue
        mf = manager_features(conn, c["fcode"], as_of)
        sf = scale_features(conn, c["fcode"], as_of)
        rows.append({"fcode": c["fcode"], "manager": mf, "scale": sf,
                     "buckets": event_buckets(mf, sf)})
    buckets: dict[str, dict[str, Any]] = {}
    for dim in ("manager_changes", "manager_tenure", "aum_yoy", "aum_6m"):
        groups: dict[str, list[str]] = {}
        for r in rows:
            groups.setdefault(r["buckets"][dim], []).append(r["fcode"])
        buckets[dim] = {k: wf._bucket_stats(v, pool, base_annual) for k, v in groups.items()}
    ic = {}
    for dim, getter in (("manager_changes", lambda r: r["manager"]["changes_365d"]),
                        ("manager_tenure", lambda r: r["manager"]["max_tenure_days"]),
                        ("aum_yoy", lambda r: r["scale"]["aum_yoy_pct"]),
                        ("aum_6m", lambda r: r["scale"]["aum_6m_pct"])):
        xs = {r["fcode"]: float(getter(r)) for r in rows if getter(r) is not None}
        ys = {r["fcode"]: pool[r["fcode"]]["annual"] for r in rows
              if r["fcode"] in xs and pool[r["fcode"]].get("annual") is not None}
        ic[dim] = wf.spearman(xs, ys)
    return {
        "as_of": as_of,
        "style_asof_hits": style_hits,
        "scale_asof_hits": len(scale_map),
        "universe": len(cands),
        "gate_pass": len(gate_pass),
        "fixed_pass": len(fixed_pass),
        "pool_n": len(pool),
        "pool_annual_median_pct": (None if base_annual is None
                                   else round(100 * base_annual, 2)),
        "event_rows": len(rows),
        "buckets": buckets,
        "ic": ic,
    }


def _avg(values: Sequence[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def summarize(cohorts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dim in ("manager_changes", "manager_tenure", "aum_yoy", "aum_6m"):
        names: list[str] = []
        for c in cohorts:
            for k in (c.get("buckets", {}).get(dim, {}) or {}):
                if k not in names:
                    names.append(k)
        out[dim] = {}
        for name in names:
            stats = [(c.get("buckets", {}).get(dim, {}) or {}).get(name)
                     for c in cohorts]
            stats = [s for s in stats if s]
            out[dim][name] = {
                "cohorts": len(stats),
                "n": sum(s.get("n") or 0 for s in stats),
                "annual_mean_pct": _avg([s.get("annual_mean_pct") for s in stats]),
                "sharpe_mean": _avg([s.get("sharpe_mean") for s in stats]),
                "hit_rate": _avg([s.get("hit_rate") for s in stats]),
            }
    out["ic"] = {k: _avg([c.get("ic", {}).get(k) for c in cohorts])
                 for k in ("manager_changes", "manager_tenure", "aum_yoy", "aum_6m")}
    return out


def run(conn, cfg, years: Sequence[int] = wf.DEFAULT_YEARS, *, horizon_days: int = 365,
        contract_id: str | None = None) -> dict[str, Any]:
    contract_id = contract_id or str(cfg.get("risk", "formula_contract",
                                             default=metrics_contract.FORMULA_CONTRACT))
    cohorts = [_cohort(conn, cfg, as_of, horizon_days, contract_id)
               for as_of in wf.cohort_dates(years)]
    return {"years": list(years), "horizon_days": horizon_days,
            "cohorts": cohorts, "summary": summarize(cohorts)}


def _num(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def render(result: dict[str, Any]) -> str:
    lines = ["经理/规模事件研究（前瞻 %d 天）" % result["horizon_days"], ""]
    lines.append("按队列的前瞻年化（%）与样本数：")
    for dim in ("manager_changes", "manager_tenure", "aum_yoy", "aum_6m"):
        lines.append("")
        lines.append(f"[{dim}]")
        for name, s in (result["summary"].get(dim, {}) or {}).items():
            lines.append(f"  {name:>8}  n={s['n']:>6}  年化={_num(s['annual_mean_pct'])}  "
                         f"夏普={_num(s['sharpe_mean'], 3)}  命中={_num(s['hit_rate'], 3)}")
    lines.append("")
    lines.append("IC（事件特征 vs 前瞻年化，跨队列均值）：")
    for k, v in (result["summary"].get("ic", {}) or {}).items():
        lines.append(f"  {k:>18}  {_num(v, 4)}")
    return "\n".join(lines)


def write(conn, cfg, years, out_path: str, *, horizon_days: int = 365) -> dict[str, Any]:
    result = run(conn, cfg, years, horizon_days=horizon_days)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return result
