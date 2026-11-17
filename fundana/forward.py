"""前瞻验证（§15 / ADR-048）：冻结每轮正式榜，用**之后的**净值检验它。

三条纪律（缺一不可，否则前瞻就是自证）：
1. 只取 nav_date > cohort.business_date 的净值 —— 绝不用建仓日之前的数据（防未来函数）；
2. 样本不足一律 not_ready，**绝不写 0 收益**；净值断流（清盘/停牌）记 unverifiable 且不剔除，
   因为"消失的基金"本身就是结论的一部分；
3. 证据等级按"有多少独立决策月份的成熟 cohort"决定，不允许用同一批数据重复计数升级。

对照组分两类：
- control_style：同风格、基础门禁通过但未入选 —— 回答"选出来的比没选的好吗"；
- control_risk：同风格里冻结时点风险最接近的 3 只 —— 回答"同样的风险预算下更好吗"。
"""
from __future__ import annotations

import json
import statistics
from datetime import date, timedelta
from typing import Any

from . import db

FORMAL_BOARDS = ("bond_stable", "bond_enhanced")
NON_FORMAL_LAYERS = ("L6C", "L7", "L8")


def _f(cfg, key: str, default):
    v = (cfg.get("forward", default={}) or {}) if cfg is not None else {}
    return v.get(key, default)


def create_cohort(conn, cfg, run_id: str, business_date: str | None = None,
                  publication_id: str | None = None, source: str = "live") -> dict[str, Any]:
    run = conn.execute("SELECT * FROM screen_run WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        raise ValueError("run_id 不存在: %s" % run_id)
    business_date = business_date or (run["business_date"] or "")
    if not business_date:
        business_date = conn.execute(
            "SELECT MAX(nav_date) FROM fund_nav_daily").fetchone()[0] or db.today_cn()

    picks = conn.execute(
        "SELECT p.fcode, p.board, p.formal_score, d.style_code"
        " FROM screen_pick p LEFT JOIN screen_decision d"
        "   ON d.run_id = p.run_id AND d.fcode = p.fcode"
        " WHERE p.run_id = ? AND p.board IN (?,?)", (run_id,) + FORMAL_BOARDS).fetchall()
    formal = [dict(r) for r in picks]
    formal_codes = {r["fcode"] for r in formal}

    ctrl = conn.execute(
        "SELECT d.fcode, d.style_code FROM screen_decision d"
        " WHERE d.run_id = ? AND d.passed = 0 AND d.layer_reached IN (?,?,?)",
        (run_id,) + NON_FORMAL_LAYERS).fetchall()
    ctrl = [dict(r) for r in ctrl if r["fcode"] not in formal_codes]

    # 冻结时点的风险画像：只用 cohort 之前已存在的指标行，绝不看未来
    contract = run["formula_contract"]
    risk: dict[str, tuple[float | None, float | None]] = {}
    for m in conn.execute("SELECT fcode, vol_dec, mdd_dec FROM fund_metric"
                          " WHERE period_days = 365 AND formula_contract = ?", (contract,)):
        risk[m["fcode"]] = (m["vol_dec"], m["mdd_dec"])

    neighbors = int(_f(cfg, "control_risk_neighbors", 3))
    by_style: dict[str, list[dict]] = {}
    for r in ctrl:
        by_style.setdefault(r["style_code"] or "bond_pure", []).append(r)

    members: list[dict[str, Any]] = []
    for r in formal:
        members.append({"cohort_id": run_id, "fcode": r["fcode"], "role": "formal",
                        "matched_to": "", "style_code": r["style_code"],
                        "formal_score": r["formal_score"]})
        style = r["style_code"] or "bond_pure"
        pool = by_style.get(style) or []
        if not pool:
            continue
        fv, fm = risk.get(r["fcode"], (None, None))
        scored = []
        for p in pool:
            pv, pm = risk.get(p["fcode"], (None, None))
            if None in (fv, fm, pv, pm):
                dist = 1e6
            else:
                dist = abs(pv - fv) / 0.01 + abs(pm - fm) / 0.01
            scored.append((dist, p["fcode"]))
        scored.sort()
        for _dist, code in scored[:neighbors]:
            members.append({"cohort_id": run_id, "fcode": code, "role": "control_risk",
                            "matched_to": r["fcode"], "style_code": style,
                            "formal_score": None})

    seen: set[tuple] = set()
    for r in ctrl:
        key = (r["fcode"], "control_style")
        if key in seen:
            continue
        seen.add(key)
        members.append({"cohort_id": run_id, "fcode": r["fcode"], "role": "control_style",
                        "matched_to": "", "style_code": r["style_code"],
                        "formal_score": None})

    freeze = {"formula_contract": contract, "risk_free_rate": (cfg.get("risk", default={}) or {}).get("risk_free_rate"),
              "config_hash": run["config_hash"], "code_version": run["code_version"],
              "formal": len(formal), "control_style": sum(1 for m in members if m["role"] == "control_style"),
              "control_risk": sum(1 for m in members if m["role"] == "control_risk"),
              "frozen_at": db.now_iso(), "source": source}

    conn.execute(
        "INSERT OR REPLACE INTO forward_cohort(cohort_id, run_id, publication_id, business_date,"
        " source, strategy_version, formula_contract, universe_count, investable_count,"
        " freeze_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, run_id, publication_id, business_date, source,
         str(run["profile"]), contract, int(run["universe_count"] or 0),
         int(run["base_pass_count"] or 0), json.dumps(freeze, ensure_ascii=False), freeze["frozen_at"]))
    conn.executemany(
        "INSERT OR REPLACE INTO forward_member(cohort_id, fcode, role, matched_to, style_code,"
        " formal_score) VALUES(:cohort_id, :fcode, :role, :matched_to, :style_code, :formal_score)",
        members)
    conn.commit()
    return {"cohort_id": run_id, "business_date": business_date, "members": len(members),
            "formal": len(formal), "control_style": freeze["control_style"],
            "control_risk": freeze["control_risk"], "source": source}


def _series(conn, fcode: str, start: str, end: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT nav_date, unit_nav, growth_pct FROM fund_nav_daily"
        " WHERE fcode = ? AND nav_date > ? AND nav_date <= ? ORDER BY nav_date",
        (fcode, start, end))]


def _chain(rows: list[dict]) -> tuple[float | None, float | None, int]:
    """返回 (total_return_pct, mdd_dec, max_gap_days)。优先日增长率链式复利。"""
    wealth = 1.0
    peak = 1.0
    mdd = 0.0
    prev_nav = None
    prev_date = None
    max_gap = 0
    used = 0
    for r in rows:
        g = r["growth_pct"]
        if g is None and prev_nav and r["unit_nav"]:
            g = (r["unit_nav"] / prev_nav - 1.0) * 100.0
        if g is not None:
            wealth *= 1.0 + float(g) / 100.0
            peak = max(peak, wealth)
            if peak > 0:
                mdd = max(mdd, 1.0 - wealth / peak)
            used += 1
        if r["unit_nav"]:
            prev_nav = float(r["unit_nav"])
        if prev_date:
            max_gap = max(max_gap, (date.fromisoformat(r["nav_date"]) - date.fromisoformat(prev_date)).days)
        prev_date = r["nav_date"]
    if used == 0:
        return None, None, max_gap
    return (wealth - 1.0) * 100.0, mdd, max_gap


def _observe(conn, cohort: dict, fcode: str, horizon: int, min_ratio: float,
             grace_days: int) -> dict[str, Any]:
    start = cohort["business_date"][:10]
    try:
        target = (date.fromisoformat(start) + timedelta(days=horizon)).isoformat()
    except ValueError:
        return {"status": "not_ready", "reason_code": "bad_business_date"}
    today = db.today_cn()
    rows = _series(conn, fcode, start, target)
    if not rows:
        return {"status": "not_ready", "reason_code": "no_nav_after_cohort"}
    end = rows[-1]["nav_date"]
    lag = (date.fromisoformat(target) - date.fromisoformat(end)).days
    total, mdd, max_gap = _chain(rows)
    expected = max(3, int(round(horizon * 252.0 / 365.0 * min_ratio)))
    base = {"start_date": rows[0]["nav_date"], "end_date": end, "n_obs": len(rows),
            "max_gap_days": max_gap, "start_lag_days":
                (date.fromisoformat(rows[0]["nav_date"]) - date.fromisoformat(start)).days,
            "total_return_pct": total, "mdd_dec": mdd}
    if target > today:
        base.update({"status": "not_ready", "reason_code": "horizon_not_elapsed",
                     "total_return_pct": None, "mdd_dec": None})
        return base
    if lag > grace_days:
        base.update({"status": "unverifiable", "reason_code": "nav_stopped_%dd" % lag,
                     "total_return_pct": None, "mdd_dec": None})
        return base
    if len(rows) < expected:
        base.update({"status": "not_ready", "reason_code": "insufficient_points_%d<%d" % (len(rows), expected),
                     "total_return_pct": None, "mdd_dec": None})
        return base
    years = horizon / 365.25
    annual = None
    if total is not None and years > 0:
        annual = (1.0 + total / 100.0) ** (1.0 / years) - 1.0
    base.update({"status": "matured", "reason_code": None, "annual_dec": annual})
    return base


def evaluate(conn, cfg, cohort_id: str | None = None) -> dict[str, Any]:
    horizons = [int(h) for h in _f(cfg, "horizons_days", [30, 90, 180, 365])]
    min_ratio = float(_f(cfg, "min_points_ratio", 0.8))
    grace = int(_f(cfg, "grace_days", 10))
    if cohort_id:
        cohorts = [dict(r) for r in conn.execute("SELECT * FROM forward_cohort WHERE cohort_id=?",
                                                 (cohort_id,))]
    else:
        cohorts = [dict(r) for r in conn.execute("SELECT * FROM forward_cohort ORDER BY created_at")]
    counts = {"matured": 0, "not_ready": 0, "unverifiable": 0}
    for co in cohorts:
        members = [dict(r) for r in conn.execute(
            "SELECT fcode, role FROM forward_member WHERE cohort_id=?", (co["cohort_id"],))]
        for m in members:
            for h in horizons:
                obs = _observe(conn, co, m["fcode"], h, min_ratio, grace)
                counts[obs["status"]] = counts.get(obs["status"], 0) + 1
                conn.execute(
                    "INSERT OR REPLACE INTO forward_observation(cohort_id, fcode, horizon_days,"
                    " status, start_date, end_date, n_obs, max_gap_days, start_lag_days,"
                    " total_return_pct, annual_dec, mdd_dec, reason_code, computed_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (co["cohort_id"], m["fcode"], h, obs["status"], obs.get("start_date"),
                     obs.get("end_date"), obs.get("n_obs"), obs.get("max_gap_days"),
                     obs.get("start_lag_days"), obs.get("total_return_pct"),
                     obs.get("annual_dec"), obs.get("mdd_dec"), obs.get("reason_code"),
                     db.now_iso()))
        conn.commit()
    return {"cohorts": len(cohorts), **counts}


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    s = sorted(values)
    def pct(p: float) -> float:
        k = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
        return round(s[k], 6)
    return {"n": len(s), "median": round(statistics.median(s), 6),
            "mean": round(statistics.fmean(s), 6), "p25": pct(0.25), "p10": pct(0.10),
            "worst": round(s[0], 6), "best": round(s[-1], 6)}


def summary(conn, cohort_id: str | None = None) -> dict[str, Any]:
    if cohort_id:
        cohorts = [dict(r) for r in conn.execute("SELECT * FROM forward_cohort WHERE cohort_id=?",
                                                 (cohort_id,))]
    else:
        cohorts = [dict(r) for r in conn.execute("SELECT * FROM forward_cohort ORDER BY created_at")]
    out: list[dict[str, Any]] = []
    matured_formal_total = 0
    months: set[str] = set()
    excess_positive = False
    today = date.fromisoformat(db.today_cn())
    for co in cohorts:
        horizons = [r[0] for r in conn.execute(
            "SELECT DISTINCT horizon_days FROM forward_observation WHERE cohort_id=?"
            " ORDER BY horizon_days", (co["cohort_id"],))]
        for h in horizons:
            rows = [dict(r) for r in conn.execute(
                "SELECT o.*, m.role FROM forward_observation o JOIN forward_member m"
                "  ON m.cohort_id = o.cohort_id AND m.fcode = o.fcode"
                " WHERE o.cohort_id = ? AND o.horizon_days = ?", (co["cohort_id"], h))]
            formal = [r["total_return_pct"] for r in rows
                      if r["role"] == "formal" and r["status"] == "matured" and r["total_return_pct"] is not None]
            style = [r["total_return_pct"] for r in rows
                     if r["role"] == "control_style" and r["status"] == "matured" and r["total_return_pct"] is not None]
            risk = [r["total_return_pct"] for r in rows
                    if r["role"] == "control_risk" and r["status"] == "matured" and r["total_return_pct"] is not None]
            f_stats, s_stats, r_stats = _stats(formal), _stats(style), _stats(risk)
            excess = None
            if f_stats.get("n") and s_stats.get("n"):
                excess = round(f_stats["median"] - s_stats["median"], 6)
                if excess > 0:
                    excess_positive = True
            win = None
            if formal and style:
                base = statistics.median(style)
                win = round(100.0 * sum(1 for v in formal if v > base) / len(formal), 2)
            if f_stats.get("n"):
                matured_formal_total += f_stats["n"]
                months.add(str(co["business_date"])[:7])
            try:
                target = (date.fromisoformat(str(co["business_date"])[:10])
                          + timedelta(days=h))
            except ValueError:
                target = None
            elapsed = ((today - date.fromisoformat(str(co["business_date"])[:10])).days
                       if target else 0)
            out.append({"cohort_id": co["cohort_id"], "business_date": co["business_date"],
                        "source": co["source"], "horizon_days": h,
                        "formal": f_stats, "control_style": s_stats, "control_risk": r_stats,
                        "excess_median_pct": excess, "excess_win_rate_pct": win,
                        "maturity_date": target.isoformat() if target else None,
                        "days_to_maturity": (target - today).days if target else None,
                        "progress_pct": (max(0.0, min(100.0, round(100.0 * elapsed / h, 1)))
                                         if target and h > 0 else None),
                        "status_counts": {s: sum(1 for r in rows if r["status"] == s)
                                          for s in ("matured", "not_ready", "unverifiable")}})
    if not cohorts:
        level = 0
    elif matured_formal_total == 0:
        level = 1
    elif len(months) >= 12 and matured_formal_total >= 100 and excess_positive:
        level = 3
    else:
        level = 2
    pending = [r for r in out
               if (r.get("status_counts") or {}).get("not_ready")
               and r.get("days_to_maturity") is not None
               and r["days_to_maturity"] >= 0]
    nxt = min(pending, key=lambda r: (r["days_to_maturity"], r["horizon_days"])) if pending else None
    return {"evidence_level": level, "evidence_label": "LEVEL_%d" % level,
            "cohorts": len(cohorts), "matured_formal_observations": matured_formal_total,
            "distinct_decision_months": len(months), "rows": out,
            "next_maturity_date": nxt.get("maturity_date") if nxt else None,
            "next_maturity_days": nxt.get("days_to_maturity") if nxt else None,
            "next_maturity_horizon_days": nxt.get("horizon_days") if nxt else None,
            "note": "LEVEL_3 需 ≥12 个不同决策月份的成熟 180 天 cohort 且 ≥100 个独立样本；"
                    "LEVEL_4 需费后净收益验证，本阶段不宣称。"}

def cohort_list(conn, limit: int = 20) -> list[dict[str, Any]]:
    """cohort 一览：前端"前瞻验证"页与验收都用它看每个 cohort 的成员与观测状态。"""
    out: list[dict[str, Any]] = []
    for co in conn.execute(
            "SELECT * FROM forward_cohort ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)):
        cid = co["cohort_id"]
        members = {r["role"]: r["n"] for r in conn.execute(
            "SELECT role, COUNT(*) AS n FROM forward_member WHERE cohort_id=? GROUP BY 1", (cid,))}
        obs = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM forward_observation WHERE cohort_id=? GROUP BY 1",
            (cid,))}
        horizons = [r[0] for r in conn.execute(
            "SELECT DISTINCT horizon_days FROM forward_observation WHERE cohort_id=? ORDER BY 1",
            (cid,))]
        out.append({"cohort_id": cid, "run_id": co["run_id"],
                    "publication_id": co["publication_id"],
                    "business_date": co["business_date"], "source": co["source"],
                    "universe_count": co["universe_count"],
                    "investable_count": co["investable_count"],
                    "members": members, "observations": obs, "horizons": horizons,
                    "created_at": co["created_at"]})
    return out
