"""v9 影子排序：只读复算，不改变 v10 正式榜。

为什么需要它：12 个季度队列复核没有确认 v10 的 3y/5y 风险倾斜，
但也不能证明 v9 更好。与其继续调权重，不如在每次生产 run 后同时记录
"同一批正式候选按 v9 长期分排序会选谁"，用真实前瞻做对照。

边界：
* 只在当前 v10 的可投域/正式候选内复算，不重建 v9 的完整准入池；
* 保留 v10 的 core 风格/份额/tenure/覆盖约束，只替换 v9 长期分与排序分，
  因此差异来自"排序公式"，不是"门槛变化"；
* 只写 reports/analysis/shadow_rank/，不写 screen_pick，不影响发布。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .. import db, fundtypes
from ..metrics import engine as metrics_engine
from . import engine, scoring, thresholds as T

# v9 的长期分（ADR-062 之前的契约）：3y 收益/胜率 + 1y 风险。
V9_LONG_WEIGHTS = {"annual_3y": 0.30, "consistency": 0.25, "mdd": 0.20,
                   "sortino": 0.15, "calmar": 0.10}
V9_CORE_COMBO = {"short": 0.40, "long": 0.60}
V9_RANK_WEIGHTS = {"composite": 0.85, "return_6m": 0.15}
CSV_FIELDS = ["fcode", "name", "style_code", "v10_core10", "v10_core50",
              "v9_core10", "v9_core50", "v10_rank_score", "v9_rank_score",
              "delta_rank_score", "formal_score", "v9_long", "v9_composite",
              "missing_v9_dims"]


def v9_long_score(pcts: dict[str, float | None]) -> float:
    """v9 long_core：缺失项按 0，复现旧契约行为。"""
    return round(sum(w * (pcts.get(k) or 0.0) for k, w in V9_LONG_WEIGHTS.items()), 4)


def v9_scores(pcts: dict[str, float | None]) -> dict[str, Any]:
    """由同一组分位复算 v9 的 short/long/composite/rank，避免混用两次分位池。"""
    short = scoring.weighted({k: pcts.get(k) for k in T.SHORT_CORE_WEIGHTS},
                             T.SHORT_CORE_WEIGHTS)
    long = v9_long_score(pcts)
    composite = round(V9_CORE_COMBO["short"] * short + V9_CORE_COMBO["long"] * long, 4)
    rank = round(V9_RANK_WEIGHTS["composite"] * composite
                 + V9_RANK_WEIGHTS["return_6m"] * (pcts.get("return_6m") or 0.0), 4)
    missing = sorted({k for k in (*V9_LONG_WEIGHTS, *T.SHORT_CORE_WEIGHTS)
                      if pcts.get(k) is None})
    return {"v9_long": long, "v9_composite": composite, "v9_rank_score": rank,
            "v9_missing_dims": missing}


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


def _production_picks(conn, run_id: str) -> tuple[dict[str, list[str]], dict[str, float]]:
    rows = conn.execute(
        "SELECT board, fcode, rank_score FROM screen_pick"
        " WHERE run_id=? AND board IN (?,?) ORDER BY board, rank",
        (run_id, T.BOARD_CORE, T.BOARD_CORE10)).fetchall()
    boards = {T.BOARD_CORE: [], T.BOARD_CORE10: []}
    scores: dict[str, float] = {}
    for r in rows:
        boards[r["board"]].append(r["fcode"])
        scores[r["fcode"]] = r["rank_score"]
    return boards, scores


def _select(passed: list[tuple[str, dict[str, Any]]], by_code: dict[str, Any], board: str) -> list[dict[str, Any]]:
    if board == T.BOARD_CORE10:
        quota, enh_cap, conv_cap = T.CORE10_QUOTA, T.CORE10_ENHANCED_CAP, T.CORE10_CONVERTIBLE_CAP
    else:
        quota, enh_cap, conv_cap = T.CORE_QUOTA, T.CORE_ENHANCED_CAP, T.CORE_CONVERTIBLE_CAP
    return engine._select_concentrated(passed, by_code, [], board=board, quota=quota,
                                       enh_cap=enh_cap, conv_cap=conv_cap)


def _compare(prod_codes: list[str], shadow_items: list[dict[str, Any]]) -> dict[str, Any]:
    v9_codes = [x["fcode"] for x in shadow_items]
    prod, shadow = set(prod_codes), set(v9_codes)
    union = prod | shadow
    return {"v10": prod_codes, "v9": v9_codes,
            "overlap": len(prod & shadow), "added": [c for c in v9_codes if c not in prod],
            "removed": [c for c in prod_codes if c not in shadow],
            "jaccard": round(len(prod & shadow) / len(union), 4) if union else 1.0}


def run(conn, cfg, run_id: str | None = None) -> dict[str, Any]:
    run = _run_row(conn, run_id)
    run_id = run["run_id"]
    types = fundtypes.resolve(cfg=cfg)
    as_of = metrics_engine.market_as_of(conn, types=types) or run["business_date"]
    nav_age = int((cfg.get("quality", default={}) or {}).get("nav_age_limit_days", 10))
    cands = engine._load_candidates(conn, run["business_date"], run["formula_contract"], types)
    mother = [c for c in cands if not engine.base_gate(c, as_of, nav_age)[1]]
    pcts = engine.compute_percentiles(mother, use_style=True)
    by_code = {c["fcode"]: c for c in cands}

    rows = conn.execute(
        "SELECT fcode, style_code, scores_json FROM screen_decision"
        " WHERE run_id=? AND passed=1 ORDER BY fcode", (run_id,)).fetchall()
    passed: list[tuple[str, dict[str, Any]]] = []
    shadow_by_code: dict[str, dict[str, Any]] = {}
    for r in rows:
        code = r["fcode"]
        p = pcts.get(code) or {}
        if not p:
            continue
        sc = json.loads(r["scores_json"] or "{}")
        vs = v9_scores(p)
        shadow_sc = {**sc, **vs}
        d = {"fcode": code, "style_code": r["style_code"], "scores": shadow_sc}
        passed.append((code, d))
        shadow_by_code[code] = vs
    passed.sort(key=lambda item: (-(item[1]["scores"].get("v9_rank_score") or 0.0),
                                  -(item[1]["scores"].get("formal_score") or 0.0),
                                  -(item[1]["scores"].get("v9_composite") or 0.0), item[0]))

    v9_core10 = _select(passed, by_code, T.BOARD_CORE10)
    v9_core50 = _select(passed, by_code, T.BOARD_CORE)
    prod_boards, prod_scores = _production_picks(conn, run_id)
    core10 = _compare(prod_boards[T.BOARD_CORE10], v9_core10)
    core50 = _compare(prod_boards[T.BOARD_CORE], v9_core50)
    v9_core10_codes = {x["fcode"] for x in v9_core10}
    v9_core50_codes = {x["fcode"] for x in v9_core50}
    v10_core10_codes = set(prod_boards[T.BOARD_CORE10])
    v10_core50_codes = set(prod_boards[T.BOARD_CORE])

    out_rows = []
    for code, d in passed:
        c = by_code.get(code) or {}
        vs = shadow_by_code.get(code, {})
        out_rows.append({
            "fcode": code, "name": c.get("name") or "", "style_code": d.get("style_code") or "",
            "v10_core10": int(code in v10_core10_codes),
            "v10_core50": int(code in v10_core50_codes),
            "v9_core10": int(code in v9_core10_codes),
            "v9_core50": int(code in v9_core50_codes),
            "v10_rank_score": prod_scores.get(code),
            "v9_rank_score": vs.get("v9_rank_score"),
            "delta_rank_score": (None if prod_scores.get(code) is None or vs.get("v9_rank_score") is None
                                 else round(vs["v9_rank_score"] - prod_scores[code], 4)),
            "formal_score": (d.get("scores") or {}).get("formal_score"),
            "v9_long": vs.get("v9_long"), "v9_composite": vs.get("v9_composite"),
            "missing_v9_dims": "|".join(vs.get("v9_missing_dims") or []),
        })

    out_dir = Path(cfg.report_root) / "analysis" / "shadow_rank"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / ("%s.json" % run_id)
    csv_path = out_dir / ("%s.csv" % run_id)
    meta = {
        "research_only": True, "run_id": run_id, "business_date": run["business_date"],
        "formula_contract": run["formula_contract"], "screen_contract": run["screen_contract"],
        "generated_at": db.now_iso(), "v9_weights": V9_LONG_WEIGHTS,
        "v9_core_combo": V9_CORE_COMBO, "v9_rank_weights": V9_RANK_WEIGHTS,
        "candidates": len(cands), "base_pass": len(mother), "formal_passed": len(passed),
        "core10": core10, "core50": core50,
        "note": "只读复算；在当前 v10 正式候选内替换 v9 长期分/排序分，保留 v10 core 约束；不写 screen_pick。",
    }
    json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(out_rows)
    _append_history(out_dir, meta)
    return {"meta": meta, "rows": out_rows}


def _append_history(out_dir: Path, meta: dict[str, Any]) -> None:
    path = out_dir / "history.csv"
    fields = ["run_id", "business_date", "generated_at", "board", "v10_n", "v9_n",
              "overlap", "added", "removed", "jaccard"]
    old: list[dict[str, Any]] = []
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            old = [r for r in csv.DictReader(fh) if r.get("run_id") != meta["run_id"]]
    for board in ("core10", "core50"):
        cmp = meta[board]
        old.append({"run_id": meta["run_id"], "business_date": meta["business_date"],
                    "generated_at": meta["generated_at"], "board": board,
                    "v10_n": len(cmp["v10"]), "v9_n": len(cmp["v9"]),
                    "overlap": cmp["overlap"], "added": len(cmp["added"]),
                    "removed": len(cmp["removed"]), "jaccard": cmp["jaccard"]})
    old.sort(key=lambda r: (r.get("business_date") or "", r.get("run_id") or "", r.get("board") or ""))
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(old)
