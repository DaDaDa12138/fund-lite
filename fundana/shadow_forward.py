"""v9 同池影子排序的前瞻观察（research_only）。

目标不是证明 v9 更好，而是把"同一批正式候选按 v9 公式会选谁"冻结下来，
用 cohort.business_date **之后**的净值同时观察 v9 与 v10 的 core10/core50。
三条纪律与 forward.py 一致：
1. 只取 business_date 之后的净值；
2. 样本不足写 not_ready，绝不写 0 收益；净值断流写 unverifiable 且不剔除；
3. 影子结果与正式 forward_* 分表存放，不能混入正式证据等级。

边界：shadow_rank 只替换排序公式，不重建 v9 的完整准入池；因此这里观察的是
"同池排序差异"，不是完整 v9 生产管线的前瞻。
"""
from __future__ import annotations

import json
from typing import Any

from . import db
from . import forward as fwd
from .screen import thresholds as T

BOARDS = ("core10", "core50")
STRATEGIES = ("v10", "v9")
BOARD_CODES = {"core10": T.BOARD_CORE10, "core50": T.BOARD_CORE}
STRATEGY_VERSION = "v9-shadow-same-pool"


def _f(cfg, key: str, default):
    v = (cfg.get("forward", default={}) or {}) if cfg is not None else {}
    return v.get(key, default)


def _run_row(conn, run_id: str | None) -> dict[str, Any]:
    if run_id:
        row = conn.execute(
            "SELECT run_id, business_date, formula_contract, screen_contract, status"
            " FROM screen_run WHERE run_id=?", (run_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT run_id, business_date, formula_contract, screen_contract, status"
            " FROM screen_run WHERE status='done' ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        raise ValueError("screen_run_not_found")
    return dict(row)


def _dedupe(codes: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for code in codes or []:
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def record(conn, cfg, run_id: str | None = None, shadow: dict[str, Any] | None = None,
           publication_id: str | None = None, business_date: str | None = None) -> dict[str, Any]:
    """冻结一次 v9/v10 core10/core50；同一 run 重跑会覆盖，不产生重复 cohort。"""
    if shadow is None:
        from .screen import shadow_rank
        shadow = shadow_rank.run(conn, cfg, run_id)
    meta = shadow.get("meta") or {}
    run_id = meta.get("run_id") or _run_row(conn, run_id)["run_id"]
    run = _run_row(conn, run_id)
    business_date = business_date or meta.get("business_date") or run["business_date"]
    if not business_date:
        raise ValueError("business_date_missing")
    if publication_id is None:
        row = conn.execute(
            "SELECT publication_id FROM publication WHERE run_id=?"
            " ORDER BY rowid DESC LIMIT 1", (run_id,)).fetchone()
        publication_id = row[0] if row else None

    cohort_id = "shadow-v9-%s" % run_id
    style = {r["fcode"]: r["style_code"] for r in conn.execute(
        "SELECT fcode, style_code FROM screen_decision WHERE run_id=?", (run_id,))}
    v9_scores = {r.get("fcode"): r.get("v9_rank_score") for r in (shadow.get("rows") or [])}
    v10_scores: dict[str, dict[str, float | None]] = {b: {} for b in BOARDS}
    for board in BOARDS:
        for r in conn.execute(
                "SELECT fcode, rank_score FROM screen_pick WHERE run_id=? AND board=?",
                (run_id, BOARD_CODES[board])):
            v10_scores[board][r["fcode"]] = r["rank_score"]

    members: list[dict[str, Any]] = []
    for board in BOARDS:
        cmp = meta.get(board) or {}
        for strategy in STRATEGIES:
            codes = _dedupe(cmp.get(strategy) or [])
            for rank, code in enumerate(codes, 1):
                score = (v10_scores[board].get(code) if strategy == "v10"
                         else v9_scores.get(code))
                members.append({"cohort_id": cohort_id, "board": board, "strategy": strategy,
                                "fcode": code, "rank": rank, "style_code": style.get(code),
                                "rank_score": score})

    freeze = {
        "research_only": True,
        "run_id": run_id,
        "publication_id": publication_id,
        "business_date": business_date,
        "formula_contract": meta.get("formula_contract") or run["formula_contract"],
        "screen_contract": meta.get("screen_contract") or run["screen_contract"],
        "v9_weights": meta.get("v9_weights"),
        "v9_core_combo": meta.get("v9_core_combo"),
        "v9_rank_weights": meta.get("v9_rank_weights"),
        "core10": meta.get("core10"),
        "core50": meta.get("core50"),
        "note": "同池影子：保留 v10 正式候选与 core 约束，只替换 v9 长期分/排序分；"
                "不是完整 v9 生产管线。",
    }
    conn.execute(
        "INSERT OR REPLACE INTO shadow_cohort(cohort_id, run_id, publication_id, business_date,"
        " strategy_version, formula_contract, screen_contract, freeze_json, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (cohort_id, run_id, publication_id, business_date, STRATEGY_VERSION,
         freeze["formula_contract"], freeze["screen_contract"],
         json.dumps(freeze, ensure_ascii=False), db.now_iso()))
    # 同一 run 重跑可能改变成员；先清掉旧成员与旧观测，避免留下孤儿行。
    conn.execute("DELETE FROM shadow_observation WHERE cohort_id=?", (cohort_id,))
    conn.execute("DELETE FROM shadow_member WHERE cohort_id=?", (cohort_id,))
    conn.executemany(
        "INSERT INTO shadow_member(cohort_id, board, strategy, fcode, rank, style_code, rank_score)"
        " VALUES(:cohort_id, :board, :strategy, :fcode, :rank, :style_code, :rank_score)",
        members)
    conn.commit()
    return {"cohort_id": cohort_id, "run_id": run_id, "business_date": business_date,
            "members": len(members),
            "core10": {s: len(meta.get("core10", {}).get(s) or []) for s in STRATEGIES},
            "core50": {s: len(meta.get("core50", {}).get(s) or []) for s in STRATEGIES}}


def evaluate(conn, cfg, cohort_id: str | None = None) -> dict[str, Any]:
    horizons = [int(h) for h in _f(cfg, "horizons_days", [30, 90, 180, 365])]
    min_ratio = float(_f(cfg, "min_points_ratio", 0.8))
    grace = int(_f(cfg, "grace_days", 10))
    if cohort_id:
        cohorts = [dict(r) for r in conn.execute(
            "SELECT * FROM shadow_cohort WHERE cohort_id=?", (cohort_id,))]
    else:
        cohorts = [dict(r) for r in conn.execute("SELECT * FROM shadow_cohort ORDER BY created_at")]
    counts = {"matured": 0, "not_ready": 0, "unverifiable": 0}
    for co in cohorts:
        members = [r[0] for r in conn.execute(
            "SELECT DISTINCT fcode FROM shadow_member WHERE cohort_id=?", (co["cohort_id"],))]
        for fcode in members:
            for h in horizons:
                obs = fwd._observe(conn, co, fcode, h, min_ratio, grace)
                counts[obs["status"]] = counts.get(obs["status"], 0) + 1
                conn.execute(
                    "INSERT OR REPLACE INTO shadow_observation(cohort_id, fcode, horizon_days,"
                    " status, start_date, end_date, n_obs, max_gap_days, start_lag_days,"
                    " total_return_pct, annual_dec, mdd_dec, reason_code, computed_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (co["cohort_id"], fcode, h, obs["status"], obs.get("start_date"),
                     obs.get("end_date"), obs.get("n_obs"), obs.get("max_gap_days"),
                     obs.get("start_lag_days"), obs.get("total_return_pct"),
                     obs.get("annual_dec"), obs.get("mdd_dec"), obs.get("reason_code"),
                     db.now_iso()))
        conn.commit()
    return {"cohorts": len(cohorts), **counts}


def _obs(conn, cohort_id: str, horizon: int, board: str, strategy: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT o.* FROM shadow_observation o JOIN shadow_member m"
        "  ON m.cohort_id = o.cohort_id AND m.fcode = o.fcode"
        " WHERE o.cohort_id=? AND o.horizon_days=? AND m.board=? AND m.strategy=?"
        " ORDER BY m.rank", (cohort_id, horizon, board, strategy))]


def _metric(rows: list[dict], key: str) -> dict[str, Any]:
    values = [r[key] for r in rows
              if r["status"] == "matured" and r.get(key) is not None]
    return fwd._stats(values)


def summary(conn, cohort_id: str | None = None) -> dict[str, Any]:
    if cohort_id:
        cohorts = [dict(r) for r in conn.execute(
            "SELECT * FROM shadow_cohort WHERE cohort_id=?", (cohort_id,))]
    else:
        cohorts = [dict(r) for r in conn.execute("SELECT * FROM shadow_cohort ORDER BY created_at")]
    rows_out: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    months: set[str] = set()
    matured_total = 0
    for co in cohorts:
        horizons = [r[0] for r in conn.execute(
            "SELECT DISTINCT horizon_days FROM shadow_observation WHERE cohort_id=?"
            " ORDER BY horizon_days", (co["cohort_id"],))]
        for h in horizons:
            by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for board in BOARDS:
                for strategy in STRATEGIES:
                    obs = _obs(conn, co["cohort_id"], h, board, strategy)
                    item = {
                        "cohort_id": co["cohort_id"], "business_date": co["business_date"],
                        "horizon_days": h, "board": board, "strategy": strategy,
                        "return": _metric(obs, "total_return_pct"),
                        "mdd": _metric(obs, "mdd_dec"),
                        "annual": _metric(obs, "annual_dec"),
                        "status_counts": {s: sum(1 for r in obs if r["status"] == s)
                                          for s in ("matured", "not_ready", "unverifiable")},
                    }
                    rows_out.append(item)
                    by_key[(board, strategy)] = item
                    if item["return"].get("n"):
                        matured_total += item["return"]["n"]
                        months.add(str(co["business_date"])[:7])
            for board in BOARDS:
                v10 = by_key[(board, "v10")]
                v9 = by_key[(board, "v9")]
                base = v10["return"].get("median")
                v9_vals = [r["total_return_pct"] for r in _obs(conn, co["cohort_id"], h, board, "v9")
                           if r["status"] == "matured" and r["total_return_pct"] is not None]
                comparisons.append({
                    "cohort_id": co["cohort_id"], "business_date": co["business_date"],
                    "horizon_days": h, "board": board,
                    "v10_n": v10["return"].get("n", 0), "v9_n": v9["return"].get("n", 0),
                    "v10_median_pct": base, "v9_median_pct": v9["return"].get("median"),
                    "v9_minus_v10_median_pct": (None if base is None or v9["return"].get("median") is None
                                                else round(v9["return"]["median"] - base, 6)),
                    "v9_win_rate_vs_v10_median_pct": (
                        None if base is None or not v9_vals
                        else round(100.0 * sum(1 for v in v9_vals if v > base) / len(v9_vals), 2)),
                    "v10_mdd_median": v10["mdd"].get("median"),
                    "v9_mdd_median": v9["mdd"].get("median"),
                })
    if not cohorts:
        level = 0
    elif matured_total == 0:
        level = 1
    else:
        level = 2
        for c in comparisons:
            if (c["board"] == "core10" and c["horizon_days"] == 180
                    and len(months) >= 12 and matured_total >= 100
                    and (c["v9_minus_v10_median_pct"] or 0) > 0):
                level = 3
                break
    return {"evidence_level": level, "evidence_label": "LEVEL_%d" % level,
            "research_only": True, "cohorts": len(cohorts),
            "matured_observations": matured_total,
            "distinct_decision_months": len(months),
            "rows": rows_out, "comparisons": comparisons,
            "note": "影子证据只回答同池排序差异；LEVEL_3 需 ≥12 个决策月、≥100 条成熟观测且 "
                    "v9 core10 的 180 天中位数高于 v10，仍不构成生产切换依据。"}


def cohort_list(conn, limit: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for co in conn.execute(
            "SELECT * FROM shadow_cohort ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)):
        members = {f"{r['board']}/{r['strategy']}": r["n"] for r in conn.execute(
            "SELECT board, strategy, COUNT(*) AS n FROM shadow_member WHERE cohort_id=?"
            " GROUP BY board, strategy", (co["cohort_id"],))}
        obs = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM shadow_observation WHERE cohort_id=? GROUP BY 1",
            (co["cohort_id"],))}
        horizons = [r[0] for r in conn.execute(
            "SELECT DISTINCT horizon_days FROM shadow_observation WHERE cohort_id=? ORDER BY 1",
            (co["cohort_id"],))]
        out.append({"cohort_id": co["cohort_id"], "run_id": co["run_id"],
                    "publication_id": co["publication_id"],
                    "business_date": co["business_date"], "source": "shadow_v9",
                    "members": members, "observations": obs, "horizons": horizons,
                    "created_at": co["created_at"]})
    return out
