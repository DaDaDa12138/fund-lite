"""P0-1：筛选输入的 point-in-time 快照与离线重放。

设计边界（务必写进报告）：
* 快照记录"本次筛选实际看到的输入"，不是重新构造历史。风格标签、费率、持仓
  仍来自当前表；这三类没有历史版本，快照只能冻结它们的当时值，不能消除前视。
* 历史回放读取 fund_scale_history / fund_manager_tenure 的 as-of 记录；规模按
  family 汇总，份额规模按份额取最近报告期，并统一套 45 天披露滞后。找不到历史时
  回退到 profile，并在 scale_source 里标记 profile_current，绝不静默当成 PIT。
* 重放只重算 base_gate(L1–L5.5) 与 score_candidate，不重跑 L6–L10、经理门与
  风险门；L2/L3 质量扫描追加的 risk:* 原因会被显式忽略并计数（post_gate_reasons_ignored），
  后者另有 batch 冻结与 shadow 对照。
"""
from __future__ import annotations

import json
from typing import Any

from .. import db
from . import thresholds as T


METRIC_FIELDS = (
    "n_obs", "years", "total_return_pct", "annual_dec", "vol_dec", "positive_ratio_pct",
    "flat_ratio_pct", "non_negative_ratio_pct",
    "mdd_dec", "sharpe", "sortino", "calmar_dec", "var95_dec", "cvar5_dec",
    "median_gap_days", "p75_gap_days", "max_gap_days", "min_points_used",
    "end_date", "computed_at",
)


def _clean(value: Any) -> Any:
    """JSON 里不允许 NaN/Infinity；非有限浮点按缺失处理。"""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _metric_json(metrics: dict[int, dict[str, Any]]) -> str:
    out: dict[str, dict[str, Any]] = {}
    for period, row in (metrics or {}).items():
        brief = {k: _clean(row.get(k)) for k in METRIC_FIELDS if row.get(k) is not None}
        if brief:
            out[str(period)] = brief
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(_clean(value), ensure_ascii=False, sort_keys=True)


def record_inputs(conn, run_id: str, as_of: str, cands: list[dict],
                  decisions: dict[str, dict], pcts: dict[str, dict] | None = None,
                  pcts_all: dict[str, dict] | None = None) -> int:
    """把本次 run 的输入逐只写入 screen_run_input；同一 run 重跑覆盖。"""
    pcts = pcts or {}
    pcts_all = pcts_all or {}
    now = db.now_iso()
    rows = []
    for cand in cands:
        code = cand["fcode"]
        d = decisions.get(code) or {}
        scores = d.get("scores") or {}
        if code in pcts:
            pool, pool_pcts = "investable", pcts[code]
        elif code in pcts_all:
            pool, pool_pcts = "l1_pass", pcts_all[code]
        else:
            pool, pool_pcts = None, None
        rows.append((
            run_id, code, as_of, d.get("layer_reached"), int(bool(d.get("passed"))),
            _json(d.get("reasons") or []), d.get("style_code"),
            "fund_style_current", cand.get("identity_status"), cand.get("estab_date"),
            cand.get("company_name"), cand.get("family_id"),
            _clean(cand.get("asset_scale_billion")), _clean(cand.get("share_scale_billion")),
            cand.get("scale_source"), _clean(cand.get("recurring_fee_pct")),
            _clean(cand.get("max_sub_fee_pct")), _clean(cand.get("max_red_fee_pct")),
            _clean(cand.get("redemption_1y_pct")),
            int(bool(cand.get("has_discount"))), "profile_current",
            _clean(cand.get("manager_tenure_min_days")), cand.get("tenure_source"),
            cand.get("effective_days"), cand.get("lockup_type"), cand.get("buyable_state"),
            cand.get("purchase_status"), cand.get("nav_points"), cand.get("nav_first_date"),
            cand.get("nav_last_date"), cand.get("freshness_days"), cand.get("gap_max_days"),
            _metric_json(cand.get("metrics") or {}), pool, _json(pool_pcts),
            _json(scores), _json(scores.get("coverage")),
            _json(cand.get("metric_flags") or []), now,
        ))
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO screen_run_input(run_id, fcode, as_of, layer_reached, passed,"
        " reason_codes_json, style_code, style_source, identity_status, estab_date,"
        " company_name, family_id, asset_scale_billion, share_scale_billion, scale_source,"
        " recurring_fee_pct, max_sub_fee_pct, max_red_fee_pct, redemption_1y_pct,"
        " has_discount, fee_source,"
        " manager_tenure_min_days, tenure_source, effective_days, lockup_type, buyable_state,"
        " purchase_status, nav_points, nav_first_date, nav_last_date, freshness_days,"
        " gap_max_days, metric_json, percentile_pool, pcts_json, scores_json, coverage_json,"
        " metric_flags_json, created_at) VALUES(" + ",".join("?" * 39) + ")"
        " ON CONFLICT(run_id, fcode) DO UPDATE SET as_of=excluded.as_of,"
        " layer_reached=excluded.layer_reached, passed=excluded.passed,"
        " reason_codes_json=excluded.reason_codes_json, style_code=excluded.style_code,"
        " style_source=excluded.style_source, identity_status=excluded.identity_status,"
        " estab_date=excluded.estab_date, company_name=excluded.company_name,"
        " family_id=excluded.family_id, asset_scale_billion=excluded.asset_scale_billion,"
        " share_scale_billion=excluded.share_scale_billion, scale_source=excluded.scale_source,"
        " recurring_fee_pct=excluded.recurring_fee_pct, max_sub_fee_pct=excluded.max_sub_fee_pct,"
        " max_red_fee_pct=excluded.max_red_fee_pct,"
        " redemption_1y_pct=excluded.redemption_1y_pct, has_discount=excluded.has_discount,"
        " fee_source=excluded.fee_source, manager_tenure_min_days=excluded.manager_tenure_min_days,"
        " tenure_source=excluded.tenure_source, effective_days=excluded.effective_days,"
        " lockup_type=excluded.lockup_type, buyable_state=excluded.buyable_state,"
        " purchase_status=excluded.purchase_status, nav_points=excluded.nav_points,"
        " nav_first_date=excluded.nav_first_date, nav_last_date=excluded.nav_last_date,"
        " freshness_days=excluded.freshness_days, gap_max_days=excluded.gap_max_days,"
        " metric_json=excluded.metric_json, percentile_pool=excluded.percentile_pool,"
        " pcts_json=excluded.pcts_json, scores_json=excluded.scores_json,"
        " coverage_json=excluded.coverage_json, metric_flags_json=excluded.metric_flags_json", rows)
    return len(rows)


def load_inputs(conn, run_id: str) -> list[dict]:
    cur = conn.execute("SELECT * FROM screen_run_input WHERE run_id=? ORDER BY fcode", (run_id,))
    return [dict(r) for r in cur.fetchall()]


def _candidate(row: dict) -> dict:
    metrics = {int(k): v for k, v in json.loads(row.get("metric_json") or "{}").items()}
    cand = {
        "fcode": row["fcode"], "identity_status": row["identity_status"],
        "estab_date": row["estab_date"], "company_name": row["company_name"],
        "family_id": row["family_id"], "asset_scale_billion": row["asset_scale_billion"],
        "share_scale_billion": row["share_scale_billion"],
        "recurring_fee_pct": row["recurring_fee_pct"],
        "max_sub_fee_pct": row["max_sub_fee_pct"],
        "max_red_fee_pct": row["max_red_fee_pct"],
        "has_discount": bool(row["has_discount"]),
        "manager_tenure_min_days": row["manager_tenure_min_days"],
        "effective_days": row["effective_days"], "lockup_type": row["lockup_type"],
        "buyable_state": row["buyable_state"], "purchase_status": row["purchase_status"],
        "nav_points": row["nav_points"], "freshness_days": row["freshness_days"],
        "gap_max_days": row["gap_max_days"], "style_code": row["style_code"],
        "metric_flags": json.loads(row.get("metric_flags_json") or "[]"),
        "metrics": metrics,
    }
    cand["metric_comparable"] = not cand["metric_flags"]
    # base_gate 的 metric_end_before_as_of/metric_lookahead 依赖该字段；
    # 它由 _load_candidates 从 365 天指标的 end_date 派生，不是独立列。
    m365 = metrics.get(365) or {}
    cand["metric_end_date"] = (str(m365.get("end_date"))[:10]
                               if m365.get("end_date") else None)
    from .engine import _attach_cost
    _attach_cost(cand)
    return cand


def replay(conn, run_id: str, nav_age_limit: int = 10) -> dict:
    """按快照重算 base_gate 与评分，返回差异摘要（不写库）。"""
    from .engine import base_gate, score_candidate
    rows = load_inputs(conn, run_id)
    if not rows:
        return {"run_id": run_id, "checked": 0, "error": "no_snapshot"}
    score_bad, gate_bad, details = 0, 0, []
    lookahead = 0
    post_gate_ignored = 0
    for row in rows:
        cand = _candidate(row)
        pcts = json.loads(row.get("pcts_json") or "{}")
        expected = json.loads(row.get("scores_json") or "{}")
        metric_json = json.loads(row.get("metric_json") or "{}")
        if any((m or {}).get("end_date") and (m or {})["end_date"] > row["as_of"]
               for m in metric_json.values()):
            lookahead += 1
        layer, reasons = base_gate(cand, row["as_of"], nav_age_limit)
        expected_reasons = json.loads(row.get("reason_codes_json") or "[]")
        # base_gate 只覆盖 L1–L5.5；L2/L3 的质量扫描会在其后追加 risk:*。
        # 本命令不重跑质量扫描，因此这些“后置门禁原因”不能算 base_gate 差异；
        # 单独计数并在 scope 中声明，避免把未覆盖误报成不一致。
        if row["layer_reached"] in ("L2", "L3"):
            base_reasons = [r for r in expected_reasons if not str(r).startswith("risk:")]
            post_gate_ignored += len(expected_reasons) - len(base_reasons)
            expected_reasons = base_reasons
        # base_gate 只覆盖 L1–L5.5；通过后 layer_reached 会被 L6–L10 覆盖，
        # 因此“通过”只能校验 reasons 为空，不能拿 L5.5 去比 L10。
        bad_gate = False
        if reasons:
            bad_gate = (layer, reasons) != (row["layer_reached"], expected_reasons)
        elif row["layer_reached"] in ("L1", "L2", "L3", "L4", "L5", "L5.5"):
            bad_gate = True
        if bad_gate:
            gate_bad += 1
            if len(details) < 20:
                details.append({"fcode": row["fcode"], "kind": "gate",
                                "expected": [row["layer_reached"], expected_reasons],
                                "actual": [layer, reasons]})
        got = score_candidate(cand, pcts) if pcts else {}
        # 只校验 score_candidate 自己产出的核心键；rookie 等榜单派生分在
        # L9 之后追加，不属于可重放评分。
        for key, gv in got.items():
            if key == "coverage" or key not in (expected or {}):
                continue
            wanted = expected[key]
            if isinstance(wanted, (int, float)) and isinstance(gv, (int, float)):
                if abs(float(wanted) - float(gv)) > 1e-9:
                    score_bad += 1
                    if len(details) < 20:
                        details.append({"fcode": row["fcode"], "kind": "score", "field": key,
                                        "expected": wanted, "actual": gv})
                    break
            elif wanted != gv:
                score_bad += 1
                if len(details) < 20:
                    details.append({"fcode": row["fcode"], "kind": "score", "field": key,
                                    "expected": wanted, "actual": gv})
                break
    return {"run_id": run_id, "checked": len(rows), "score_mismatches": score_bad,
            "gate_mismatches": gate_bad, "lookahead_metrics": lookahead,
            "post_gate_reasons_ignored": post_gate_ignored,
            "scope": "base_gate(L1-L5.5)+score_candidate；不重跑 L6-L10、经理门与风险门",
            "details": details}
