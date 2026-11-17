"""P1-2/P1-3 影子测试：只读一次已完成的 screen run，不改变生产榜单。

* 固定线风格化影子：把 STYLE_FLOORS 的 annual/calmar/mdd/vol/non_negative/fee/
  sharpe 套到稳健榜 L6C，保留 composite≥62 与 3 年完整；比较“如果生产改用风格
  地板，会多进/少进哪些基金”。
* 动态层影子：在可投域（L1–L5.5）上按风格重估 q（默认 q60），比较 q60/q70 与
  k=4/k=5 的通过集合；仍要求 composite 过线。

影子结果只写 JSON/报告，不写 screen_pick；生产阈值必须等 OOS/前瞻证据后再切换。
所有数值来自 screen_decision 与 fund_metric，不生成模拟值。
"""
from __future__ import annotations

import json
from typing import Any

from . import scoring
from . import thresholds as T

MOTHER_LAYERS = ("L6C", "L6E", "L7", "L8", "L8R", "L9")
DYNAMIC_FIELDS = ("annual_1y", "sharpe", "sortino", "calmar", "non_negative", "fee")


def _load(conn, run_id: str) -> list[dict[str, Any]]:
    rows = []
    for r in conn.execute(
            "SELECT fcode, layer_reached, style_code, thresholds_json, scores_json"
            " FROM screen_decision WHERE run_id=?", (run_id,)):
        try:
            th = json.loads(r["thresholds_json"] or "{}")
            sc = json.loads(r["scores_json"] or "{}")
        except ValueError:
            continue
        rows.append({"fcode": r["fcode"], "layer": r["layer_reached"],
                     "style": r["style_code"], "th": th, "scores": sc})
    return rows


def _fixed_values(row: dict[str, Any]) -> dict[str, float] | None:
    fixed = row["th"].get("fixed_or_admission") or {}
    if not fixed:
        return None
    out = {}
    for key, cell in fixed.items():
        if isinstance(cell, dict):
            out[key] = cell.get("value")
    return out


def _fixed_pass(row: dict[str, Any]) -> bool:
    fixed = row["th"].get("fixed_or_admission") or {}
    if not fixed:
        return False
    return all(bool(c.get("pass")) for c in fixed.values() if isinstance(c, dict))


def _stable(row: dict[str, Any]) -> bool:
    return row["style"] in T.STABLE_STYLES


def fixed_style_shadow(conn, run_id: str) -> dict[str, Any]:
    rows = [r for r in _load(conn, run_id) if _stable(r) and r["layer"] in MOTHER_LAYERS]
    actual = {r["fcode"] for r in rows if r["layer"] in ("L7", "L8", "L8R", "L9")}
    by_style: dict[str, dict[str, int]] = {}
    shadow: set[str] = set()
    for r in rows:
        v = _fixed_values(r)
        if not v:
            continue
        floor = T.style_floor(r["style"])
        ok = (
            v.get("composite") is not None and float(v["composite"]) >= T.FIXED["composite_min"]
            and bool(v.get("history_3y"))
            and v.get("annual_1y") is not None and float(v["annual_1y"]) >= floor["annual"]
            and v.get("calmar") is not None and float(v["calmar"]) >= floor["calmar"]
            and v.get("mdd") is not None and float(v["mdd"]) <= floor["mdd"]
            and v.get("vol") is not None and float(v["vol"]) <= floor["vol"]
            and v.get("non_negative") is not None
            and float(v["non_negative"]) >= floor["non_negative"]
            and v.get("fee") is not None and float(v["fee"]) <= floor["fee"]
            and v.get("sharpe") is not None and float(v["sharpe"]) >= floor["sharpe"]
        )
        if ok:
            shadow.add(r["fcode"])
        s = by_style.setdefault(r["style"], {"candidates": 0, "shadow_pass": 0,
                                             "actual_pass": 0})
        s["candidates"] += 1
        s["shadow_pass"] += 1 if ok else 0
        s["actual_pass"] += 1 if r["fcode"] in actual else 0
    return {
        "type": "fixed_style_floor", "run_id": run_id,
        "actual_count": len(actual), "shadow_count": len(shadow),
        "entered": sorted(shadow - actual), "exited": sorted(actual - shadow),
        "by_style": by_style,
        "note": "影子只替换 L6C 的风格地板（保留 composite≥62、3 年完整）；不写生产榜单。",
    }


def _metric_values(conn, run_id: str, contract: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for r in conn.execute(
            "SELECT fcode, annual_dec, sharpe, sortino, calmar_dec, non_negative_ratio_pct"
            " FROM fund_metric WHERE period_days=365 AND formula_contract=?", (contract,)):
        out[r["fcode"]] = {
            "annual_1y": None if r["annual_dec"] is None else float(r["annual_dec"]) * 100,
            "sharpe": r["sharpe"], "sortino": r["sortino"],
            "calmar": r["calmar_dec"],
            "non_negative": r["non_negative_ratio_pct"],
        }
    return out


def _q(values: list[float], q: float) -> float | None:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return round(scoring.quantile(scoring.winsorize(vals), q), 6)


def _thresholds(members: list[dict[str, Any]], metrics: dict[str, dict[str, float]],
                composite_q: float) -> dict[str, Any]:
    floor = T.style_floor(members[0]["style"] if members else "DEFAULT")
    shift = composite_q - T.DYNAMIC_QUANTILES["composite"]

    def q_of(key: str) -> float:
        return max(0.05, min(0.95, T.DYNAMIC_QUANTILES[key] + shift))

    def vals(key):
        return [metrics.get(m["fcode"], {}).get(key) for m in members]

    def q(key: str, qq: float) -> float | None:
        return _q(vals(key), qq)

    comp = [m["scores"].get("composite") for m in members]
    comp_q = _q([v for v in comp if v is not None], composite_q)
    annual_q = q("annual_1y", q_of("annual_1y"))
    return {
        "n": len(members),
        "composite_min": None if comp_q is None else max(T.FIXED["composite_min"], comp_q),
        "annual_min_pct": None if annual_q is None else max(floor["annual"], annual_q),
        "sharpe_min": None if q("sharpe", q_of("sharpe_1y")) is None else max(floor["sharpe"], q("sharpe", q_of("sharpe_1y"))),
        "sortino_min": None if q("sortino", q_of("sortino_1y")) is None else max(floor["sortino"], q("sortino", q_of("sortino_1y"))),
        "calmar_min": None if q("calmar", q_of("calmar_1y")) is None else max(floor["calmar"], q("calmar", q_of("calmar_1y"))),
        "non_negative_min_pct": None if q("non_negative", q_of("non_negative_1y")) is None else max(floor["non_negative"], q("non_negative", q_of("non_negative_1y"))),
        "fee_max_pct": None if q("fee", q_of("fee")) is None else min(floor["fee"], q("fee", q_of("fee"))),
        "composite_q": composite_q,
        "quantile_shift": round(shift, 4),
    }

def _dynamic_pass(row, metrics, th, k: int) -> tuple[bool, int]:
    m = metrics.get(row["fcode"], {})
    composite = row["scores"].get("composite")
    if composite is None or th["composite_min"] is None or float(composite) < th["composite_min"]:
        return False, 0
    checks = [
        (m.get("annual_1y") is not None and th["annual_min_pct"] is not None
         and float(m["annual_1y"]) >= th["annual_min_pct"]),
        (m.get("sharpe") is not None and th["sharpe_min"] is not None
         and float(m["sharpe"]) >= th["sharpe_min"]),
        (m.get("sortino") is not None and th["sortino_min"] is not None
         and float(m["sortino"]) >= th["sortino_min"]),
        (m.get("calmar") is not None and th["calmar_min"] is not None
         and float(m["calmar"]) >= th["calmar_min"]),
        (m.get("non_negative") is not None and th["non_negative_min_pct"] is not None
         and float(m["non_negative"]) >= th["non_negative_min_pct"]),
        (m.get("fee") is not None and th["fee_max_pct"] is not None
         and float(m["fee"]) <= th["fee_max_pct"]),
    ]
    return sum(1 for x in checks if x) >= k, sum(1 for x in checks if x)


def dynamic_shadow(conn, run_id: str, *, quantile: float = 0.60,
                   k: int = 4) -> dict[str, Any]:
    run = conn.execute("SELECT formula_contract FROM screen_run WHERE run_id=?",
                       (run_id,)).fetchone()
    contract = run["formula_contract"] if run else None
    rows = [r for r in _load(conn, run_id) if _stable(r) and r["layer"] in MOTHER_LAYERS]
    metrics = _metric_values(conn, run_id, contract) if contract else {}
    for r in rows:
        fee = (_fixed_values(r) or {}).get("fee")
        metrics.setdefault(r["fcode"], {})["fee"] = fee
    # 动态层母体是可投域 L1–L5.5；screen_decision 只保留到 L6C 的终态，
    # 因此这里用 L6C..L9 的并集近似可投域，与生产 mother 的差异会在 note 里披露。
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["style"], []).append(r)
    thresholds = {}
    for style, members in groups.items():
        if len(members) >= T.DYNAMIC_MIN_STYLE_N:
            thresholds[style] = _thresholds(members, metrics, quantile)
        else:
            thresholds[style] = None
    global_th = _thresholds(rows, metrics, quantile)
    fixed_pass = [r for r in rows if _fixed_pass(r)]
    actual = {r["fcode"] for r in rows if r["layer"] in ("L8", "L8R", "L9")}
    shadow: set[str] = set()
    details = []
    for r in fixed_pass:
        th = thresholds.get(r["style"]) or global_th
        ok, npass = _dynamic_pass(r, metrics, th, k)
        if ok:
            shadow.add(r["fcode"])
        details.append({"fcode": r["fcode"], "style": r["style"], "layer": r["layer"],
                        "dimensions_passed": npass, "shadow_pass": ok})
    return {
        "type": "dynamic_gate", "run_id": run_id, "quantile": quantile, "k": k,
        "mother": len(rows), "fixed_pass": len(fixed_pass),
        "actual_dynamic_pass": len(actual), "shadow_dynamic_pass": len(shadow),
        "entered": sorted(shadow - actual), "exited": sorted(actual - shadow),
        "thresholds": thresholds, "global": global_th,
        "details": details,
        "note": "母体用 screen_decision 的 L6C–L9 近似可投域；不写生产榜单，q/k 切换需 OOS 证据。",
    }


def report_summary(conn, run_id: str) -> dict[str, Any] | None:
    """报告用摘要：只给计数与少量代码，不改变生产榜单。"""
    if not conn.execute("SELECT 1 FROM screen_run WHERE run_id=?", (run_id,)).fetchone():
        return None
    fixed = fixed_style_shadow(conn, run_id)
    q70 = dynamic_shadow(conn, run_id, quantile=0.70, k=5)
    q60 = dynamic_shadow(conn, run_id, quantile=0.60, k=4)
    return {
        "run_id": run_id,
        "research_only": True,
        "fixed_style": {
            "actual_count": fixed["actual_count"],
            "shadow_count": fixed["shadow_count"],
            "entered": fixed["entered"][:20],
            "exited": fixed["exited"][:20],
            "by_style": fixed["by_style"],
        },
        "dynamic_q70_k5": {
            "shadow": q70["shadow_dynamic_pass"], "actual": q70["actual_dynamic_pass"],
            "entered": len(q70["entered"]), "exited": len(q70["exited"]),
        },
        "dynamic_q60_k4": {
            "shadow": q60["shadow_dynamic_pass"], "actual": q60["actual_dynamic_pass"],
            "entered": len(q60["entered"]), "exited": len(q60["exited"]),
        },
        "note": "影子只读 screen_decision，不写 screen_pick；q60/k4 的新增必须等 OOS/前瞻证据，"
                "当前生产阈值不切换。q70/k5 与生产 actual 的差异来自母体近似（L6C–L9 vs L1–L5.5）。",
    }
