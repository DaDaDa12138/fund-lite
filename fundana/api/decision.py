"""决策快照：把 screen_decision 里保存的门槛 JSON 翻译成"差在哪一项"。

设计要点：
* 门槛值一律来自本轮落库的 thresholds_json，不在展示层重算阈值——否则页面与决策会各说一套。
* 每项给出 value / threshold / op / margin / passed / window，前端据此画进度条；
  margin 是同单位下的"余量"，负数即差距（例如年化 1.2% vs 门槛 2.0% → -0.8 个百分点）。
* 没有进入评分层的基金（L1–L3 就被拦下）没有 thresholds，返回 groups=[] 并说明原因，
  不伪造门槛、也不把缺失当成 0。
"""
from __future__ import annotations

import json
import sqlite3

from .. import db, explain
from ..fees.holding import holding_cost
from ..fees.share_class import compare as share_class_compare
from ..labels import (check_unit, label_board, label_check, label_check_group,
                      label_layer, label_reason)

GROUP_ORDER = ("fixed_or_admission", "dynamic", "enhanced_strict")

WINDOW_LABELS = {
    "1y_3y": "1 年 + 3 年各半",
    "1y_only": "仅 1 年（3 年历史不足）",
    "3y_only": "仅 3 年",
    "none": "无可用窗口",
}

SOURCE_LABELS = {
    "style": "同风格分位",
    "global": "全局分位（同风格样本不足）",
    "enhanced_relative": "增强债同风格相对口径",
}

SCORE_LABELS = {
    "short_core": "短期核心分",
    "long_core": "长期核心分",
    "composite": "综合分",
    "short_formal": "短期正式分",
    "long_formal": "长期正式分",
    "formal_score": "正式分",
}


class DecisionError(ValueError):
    pass


def _item(key: str, raw: dict) -> dict:
    value = raw.get("value")
    op = "min" if "min" in raw else "max"
    threshold = raw.get("min") if op == "min" else raw.get("max")
    margin = None
    if value is not None and threshold is not None:
        margin = round(value - threshold, 6) if op == "min" else round(threshold - value, 6)
    reason = raw.get("reason")
    return {
        "key": key,
        "label": label_check(key),
        "unit": raw.get("unit") or check_unit(key),
        "value": value,
        "threshold": threshold,
        "op": op,
        "passed": bool(raw.get("pass")),
        "reason": reason,
        "reason_label": label_reason("%s:%s" % (key, reason)) if reason else None,
        "margin": margin,
        "window": raw.get("window"),
        "window_label": WINDOW_LABELS.get(raw.get("window") or "") if raw.get("window") else None,
    }


def _group(key: str, block: dict) -> dict | None:
    if not isinstance(block, dict):
        return None
    meta = block.get("_meta") or {}
    items = [_item(k, v) for k, v in block.items()
             if k != "_meta" and isinstance(v, dict) and ("value" in v or "min" in v or "max" in v)]
    if not items:
        return None
    passed_n = sum(1 for i in items if i["passed"])
    out = {
        "key": key,
        "title": label_check_group(key),
        "archive": key == "enhanced_strict",
        "items": items,
        "passed_count": passed_n,
        "total_count": len(items),
    }
    if meta:
        out["meta"] = {
            "threshold_source": meta.get("threshold_source"),
            "threshold_source_label": SOURCE_LABELS.get(meta.get("threshold_source") or "", None),
            "style_n": meta.get("style_n"),
            "rule": meta.get("rule"),
            "min_dimensions": meta.get("min_dimensions"),
            "dimensions_passed": meta.get("dimensions_passed"),
            "gate_pass": meta.get("gate_pass"),
            "peers": meta.get("peers"),
            "fallback": meta.get("fallback"),
            "annual_window": meta.get("annual_window"),
            "annual_window_mix": meta.get("annual_window_mix"),
        }
    return out


def rank_history(conn, fcode: str, limit: int = 40) -> dict:
    """这只基金在各轮筛选里的排名与正式分走势（按时间升序，供画折线）。

    同一轮里可能同时出现在正式榜与持有期受限榜，取正式榜（bond_locked 排后），
    与决策快照的口径保持一致。
    """
    rows = conn.execute(
        "SELECT d.run_id, r.created_at, d.layer_reached, d.passed,"
        " (SELECT p.board FROM screen_pick p WHERE p.run_id=d.run_id AND p.fcode=d.fcode"
        "  ORDER BY CASE WHEN p.board='bond_locked' THEN 1 ELSE 0 END, p.rank LIMIT 1) AS board,"
        " (SELECT p.rank FROM screen_pick p WHERE p.run_id=d.run_id AND p.fcode=d.fcode"
        "  ORDER BY CASE WHEN p.board='bond_locked' THEN 1 ELSE 0 END, p.rank LIMIT 1) AS rank,"
        " (SELECT p.formal_score FROM screen_pick p WHERE p.run_id=d.run_id AND p.fcode=d.fcode"
        "  ORDER BY CASE WHEN p.board='bond_locked' THEN 1 ELSE 0 END, p.rank LIMIT 1) AS formal_score,"
        " json_extract(d.scores_json, '$.formal_score') AS decision_score,"
        " json_extract(d.scores_json, '$.composite') AS composite"
        " FROM screen_decision d JOIN screen_run r ON r.run_id = d.run_id"
        " WHERE d.fcode = ? ORDER BY r.created_at ASC LIMIT ?", (fcode, limit)).fetchall()
    items = [{
        "run_id": r["run_id"],
        "created_at": r["created_at"],
        "board": r["board"],
        "board_label": label_board(r["board"]) if r["board"] else None,
        "rank": r["rank"],
        "formal_score": r["formal_score"],
        # 没进榜的轮次没有 pick，但决策快照里仍有本轮正式分与综合分；
        # 走势图用 score 画连续折线，否则 7,200 多只未入榜基金只剩一张空坐标系。
        "score": (r["formal_score"] if r["formal_score"] is not None else r["decision_score"]),
        "score_source": ("pick" if r["formal_score"] is not None
                         else ("decision" if r["decision_score"] is not None else None)),
        "composite": r["composite"],
        "layer_reached": r["layer_reached"],
        "layer_label": label_layer(r["layer_reached"]),
        "passed": bool(r["passed"]),
    } for r in rows]
    ranks = [i["rank"] for i in items if i["rank"] is not None]
    scores = [i["score"] for i in items if i["score"] is not None]
    best = min(ranks) if ranks else None
    worst = max(ranks) if ranks else None
    return {
        "items": items,
        "runs": len(items),
        "best_rank": best,
        "worst_rank": worst,
        "latest_rank": items[-1]["rank"] if items else None,
        "latest_score": items[-1]["score"] if items else None,
        "first_score": items[0]["score"] if items else None,
        "score_delta": (round(scores[-1] - scores[0], 4) if len(scores) > 1 else None),
    }


def _threshold_map(thresholds: dict) -> dict:
    """把 thresholds_json 摊平成 {(group, key): (threshold, value, passed, unit)}。

    只用于"上一轮 vs 本轮"的差异对比，不参与判定本身。
    """
    out = {}
    for group_key in GROUP_ORDER:
        block = thresholds.get(group_key)
        if not isinstance(block, dict):
            continue
        for key, raw in block.items():
            if key.startswith("_") or not isinstance(raw, dict):
                continue  # _meta 之类是说明性字段，不是可比较的门槛项
            op = "min" if "min" in raw else "max"
            out[(group_key, key)] = {
                "threshold": raw.get("min") if op == "min" else raw.get("max"),
                "value": raw.get("value"),
                "passed": raw.get("pass"),
                "op": op,
            }
    return out


def _changes(prev: dict, cur: dict) -> list:
    """列出门槛或自身指标发生变化的项目，按 |门槛变化| 降序。"""
    rows = []
    for (group_key, key), now in cur.items():
        before = prev.get((group_key, key))
        if not before:
            continue
        t_prev, t_now = before["threshold"], now["threshold"]
        v_prev, v_now = before["value"], now["value"]
        d_thr = None
        if isinstance(t_prev, (int, float)) and isinstance(t_now, (int, float)):
            d_thr = round(t_now - t_prev, 6)
        d_val = None
        if isinstance(v_prev, (int, float)) and isinstance(v_now, (int, float)):
            d_val = round(v_now - v_prev, 6)
        if (d_thr in (None, 0)) and (d_val in (None, 0)) and before["passed"] == now["passed"]:
            continue
        rows.append({
            "group": group_key,
            "group_title": label_check_group(group_key),
            "key": key,
            "label": label_check(key),
            "unit": check_unit(key),
            "prev_threshold": t_prev,
            "threshold": t_now,
            "threshold_delta": d_thr,
            "prev_value": v_prev,
            "value": v_now,
            "value_delta": d_val,
            "prev_passed": before["passed"],
            "passed": now["passed"],
        })
    rows.sort(key=lambda r: -abs(r["threshold_delta"] or 0))
    return rows


def history(conn, fcode: str, limit: int = 10) -> list:
    """这只基金出现过的筛选轮次（新到旧），供页面选择"对比哪一轮"。"""
    rows = conn.execute(
        "SELECT d.run_id, d.layer_reached, d.passed, r.created_at,"
        " (SELECT formal_score FROM screen_pick p WHERE p.run_id=d.run_id AND p.fcode=d.fcode"
        "  ORDER BY CASE WHEN p.board='bond_locked' THEN 1 ELSE 0 END, p.rank LIMIT 1) AS formal_score"
        " FROM screen_decision d JOIN screen_run r ON r.run_id = d.run_id"
        " WHERE d.fcode = ? ORDER BY r.created_at DESC LIMIT ?", (fcode, limit)).fetchall()
    return [{"run_id": r["run_id"], "created_at": r["created_at"],
             "layer_reached": r["layer_reached"], "layer_label": label_layer(r["layer_reached"]),
             "passed": bool(r["passed"]), "formal_score": r["formal_score"]} for r in rows]


def _row_payload(row, cur_map: dict, formal_score) -> dict:
    prev_thresholds = {}
    if row["thresholds_json"]:
        try:
            prev_thresholds = json.loads(row["thresholds_json"])
        except (TypeError, ValueError):
            prev_thresholds = {}
    prev_score = None
    if row["scores_json"]:
        try:
            prev_score = json.loads(row["scores_json"]).get("formal_score")
        except (TypeError, ValueError):
            prev_score = None
    score_delta = None
    if isinstance(prev_score, (int, float)) and isinstance(formal_score, (int, float)):
        score_delta = round(formal_score - prev_score, 4)
    return {
        "run_id": row["run_id"],
        "created_at": row["created_at"],
        "formal_score": prev_score,
        "score_delta": score_delta,
        "changes": _changes(_threshold_map(prev_thresholds), cur_map),
    }


def _previous(conn, fcode: str, run_id: str, thresholds: dict, formal_score,
              prev_run: str | None = None) -> dict | None:
    """同一只基金的历史轮次对比。

    默认不取"上一轮"，而是取**最近一次门槛真正发生变化**的那一轮：
    同一套门槛连续跑多轮（例如只重算指标）时，逐项对比全是 0，页面会显得没信息；
    取有差异的轮次才能回答"排名为什么变了"。找不到有差异的轮次时回退到最近一轮。
    """
    cur_map = _threshold_map(thresholds)
    if prev_run:
        row = conn.execute(
            "SELECT d.run_id, d.thresholds_json, d.scores_json, r.created_at"
            " FROM screen_decision d JOIN screen_run r ON r.run_id = d.run_id"
            " WHERE d.fcode = ? AND d.run_id = ?", (fcode, prev_run)).fetchone()
        return _row_payload(row, cur_map, formal_score) if row else None
    rows = conn.execute(
        "SELECT d.run_id, d.thresholds_json, d.scores_json, r.created_at"
        " FROM screen_decision d JOIN screen_run r ON r.run_id = d.run_id"
        " WHERE d.fcode = ? AND d.run_id <> ?"
        " ORDER BY r.created_at DESC LIMIT 12", (fcode, run_id)).fetchall()
    fallback = None
    for row in rows:
        payload = _row_payload(row, cur_map, formal_score)
        if fallback is None:
            fallback = payload
        if payload["changes"]:
            return payload
    return fallback


def _counterfactual(conn, fcode: str, thresholds: dict, formal_score,
                    previous: dict | None, board: str | None) -> dict | None:
    """反事实：如果今天仍按上一轮的门槛线，我现在能不能过？

    只做算术，不重跑筛选：拿上一轮的 min/max 与**本轮**的 value 逐项比较。
    这样做的好处是能明确回答"是门槛变了还是我变差了"；代价是忽略了排序与配额
    等后续步骤，所以名次只能按上一轮正式分分布估算，不能当成真实排名。
    """
    if not previous or not previous.get("run_id"):
        return None
    prev_run = previous["run_id"]
    row = conn.execute(
        "SELECT thresholds_json FROM screen_decision WHERE run_id=? AND fcode=?",
        (prev_run, fcode)).fetchone()
    if row is None or not row["thresholds_json"]:
        return None
    try:
        prev_thr = json.loads(row["thresholds_json"])
    except (TypeError, ValueError):
        return None
    groups = []
    failed = []
    n_items = 0
    n_passed = 0
    for group_key in GROUP_ORDER:
        block = prev_thr.get(group_key)
        if not isinstance(block, dict):
            continue
        cur_block = (thresholds or {}).get(group_key)
        cur_block = cur_block if isinstance(cur_block, dict) else {}
        items = []
        for key, raw in block.items():
            if key.startswith("_") or not isinstance(raw, dict):
                continue
            op = "min" if "min" in raw else ("max" if "max" in raw else None)
            if op is None:
                continue
            bound = raw.get(op)
            now = cur_block.get(key)
            value = now.get("value") if isinstance(now, dict) else None
            passed = None
            margin = None
            if value is not None and bound is not None:
                if op == "min":
                    passed = value >= bound
                    margin = round(value - bound, 6)
                else:
                    passed = value <= bound
                    margin = round(bound - value, 6)
            items.append({
                "key": key,
                "label": label_check(key),
                "unit": raw.get("unit") or check_unit(key),
                "op": op,
                "threshold": bound,
                "value": value,
                "margin": margin,
                "passed": passed,
            })
            n_items += 1
            if passed is True:
                n_passed += 1
            elif passed is False:
                failed.append(label_check(key))
        if items:
            known = [i["passed"] for i in items if i["passed"] is not None]
            groups.append({
                "key": group_key,
                "title": label_check_group(group_key),
                "items": items,
                "passed": all(known) if known else None,
            })
    if not groups:
        return None
    would_pass = all(g["passed"] for g in groups if g["passed"] is not None)

    estimate = None
    if isinstance(formal_score, (int, float)):
        target = board or "bond_stable"
        rows = conn.execute(
            "SELECT formal_score FROM screen_pick WHERE run_id=? AND board=?"
            " AND formal_score IS NOT NULL", (prev_run, target)).fetchall()
        scores = [r["formal_score"] for r in rows]
        if scores:
            better = sum(1 for s in scores if s > formal_score)
            estimate = {
                "board": target,
                "board_label": label_board(target),
                "position": better + 1,
                "sample_n": len(scores),
                "note": "按上一轮同榜正式分分布估算（比我高的有几个 + 1），不是重跑结果",
            }
    return {
        "run_id": prev_run,
        "created_at": previous.get("created_at"),
        "groups": groups,
        "n_items": n_items,
        "n_passed": n_passed,
        "would_pass": would_pass,
        "failed": failed,
        "rank_estimate": estimate,
        "note": "用上一轮门槛线判定本轮数值，只回答能不能过，不重算评分与排序",
    }


def _relief(dec) -> dict | None:
    """缓释记录（§11.9）：只有"恰好一项硬比较失败"且命中单一原因规则才会写这一列。"""
    keys = dec.keys()
    if "relief_json" not in keys or not dec["relief_json"]:
        return None
    try:
        return json.loads(dec["relief_json"])
    except (TypeError, ValueError):
        return None


def _cross(conn, fcode: str) -> list[dict]:
    """跨源校验中与这只基金相关的最近几行（单位净值 / 净值日期 / 阶段收益）。"""
    return [dict(r) for r in conn.execute(
        "SELECT business_date, field, source_a, source_b, value_a, value_b,"
        " delta_abs, delta_rel, status, detail_json, checked_at FROM nav_cross_check"
        " WHERE fcode=? ORDER BY checked_at DESC, field LIMIT 6", (fcode,))]


def _pct(value):
    return None if value is None else round(float(value) * 100, 4)


def _explain_row(conn, fcode: str, run_id: str, dec, pick, raw_scores: dict) -> dict:
    """构造 explain.build 需要的行；只查展示必需的字段，不重算筛选。"""
    prof = conn.execute(
        "SELECT recurring_fee_pct, max_sub_fee_pct, field_src_json FROM fund_profile"
        " WHERE fcode=?", (fcode,)).fetchone()
    fee = conn.execute(
        "SELECT has_discount FROM fund_fee_meta WHERE fcode=?", (fcode,)).fetchone()
    metrics = {}
    for m in conn.execute(
            "SELECT period_days, annual_dec, mdd_dec, sharpe, calmar_dec, years,"
            " non_negative_ratio_pct"
            " FROM fund_metric WHERE fcode=? AND period_days IN (365,1095,1825)"
            " AND formula_contract=(SELECT formula_contract FROM screen_run WHERE run_id=?)",
            (fcode, run_id)):
        metrics[m["period_days"]] = dict(m)
    m1 = metrics.get(365, {})
    m3 = metrics.get(1095, {})
    m5 = metrics.get(1825, {})
    share_scale = None
    if prof and prof["field_src_json"]:
        try:
            share_scale = json.loads(prof["field_src_json"]).get("share_class_scale_billion")
        except (TypeError, ValueError):
            share_scale = None
    tenure = conn.execute(
        "SELECT MIN(days) AS days FROM fund_manager_tenure WHERE fcode=? AND is_current=1",
        (fcode,)).fetchone()
    risk = {}
    if dec["risk_json"]:
        try:
            risk = json.loads(dec["risk_json"])
        except (TypeError, ValueError):
            risk = {}
    risk_summary = risk.get("summary") or {}
    cov = raw_scores.get("coverage") or {}
    cost = holding_cost(prof["recurring_fee_pct"] if prof else None,
                        prof["max_sub_fee_pct"] if prof else None,
                        fee["has_discount"] if fee else None, 365)
    return {
        "board": pick["board"] if pick else None,
        "formal_score": pick["formal_score"] if pick else None,
        "rank_score": raw_scores.get("rank_score"),
        "long_score": raw_scores.get("long_score"),
        "composite": raw_scores.get("composite"),
        "annual_1y_pct": _pct(m1.get("annual_dec")),
        "annual_3y_pct": _pct(m3.get("annual_dec")),
        "annual_5y_pct": _pct(m5.get("annual_dec")),
        "mdd_1y_pct": _pct(m1.get("mdd_dec")),
        "mdd_3y_pct": _pct(m3.get("mdd_dec")),
        "mdd_5y_pct": _pct(m5.get("mdd_dec")),
        "non_negative_1y_pct": m1.get("non_negative_ratio_pct"),
        "effective_years_3y": cov.get("effective_years_3y"),
        "coverage_ratio_3y": cov.get("coverage_ratio_3y"),
        "is_full_3y": cov.get("is_full_3y"),
        "effective_years_5y": cov.get("effective_years_5y"),
        "coverage_ratio_5y": cov.get("coverage_ratio_5y"),
        "is_full_5y": cov.get("is_full_5y"),
        "cost_1y_pct": None if cost is None else cost["total_pct"],
        "share_scale_billion": share_scale,
        "manager_tenure_min_days": tenure["days"] if tenure else None,
        "risk_status": risk_summary.get("status"),
        "risk_flags": ";".join(str(x) for x in (risk_summary.get("blocked_flags") or [])
                               + (risk_summary.get("watch_flags") or [])
                               + (risk_summary.get("unknown_flags") or [])),
        "quality_status": None,
        "ongoing_drawdown": None,
    }

def view(conn, fcode: str, run_id: str | None = None,
         prev_run: str | None = None) -> dict | None:
    """返回一只基金在某一轮筛选里的完整门槛快照；没有决策记录时返回 None。"""
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return _view(conn, fcode, run_id, prev_run)
    finally:
        conn.row_factory = old_factory


def _view(conn, fcode: str, run_id: str | None = None,
          prev_run: str | None = None) -> dict | None:
    if run_id is None:
        row = conn.execute(
            "SELECT run_id FROM screen_run ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        run_id = row["run_id"]
    dec = conn.execute(
        "SELECT * FROM screen_decision WHERE run_id=? AND fcode=?", (run_id, fcode)).fetchone()
    if not dec:
        return None
    thresholds = {}
    if dec["thresholds_json"]:
        try:
            thresholds = json.loads(dec["thresholds_json"])
        except (TypeError, ValueError):
            thresholds = {}
    groups = []
    for key in GROUP_ORDER:
        g = _group(key, thresholds.get(key))
        if g:
            groups.append(g)
    reasons = []
    if dec["reason_codes_json"]:
        try:
            codes = json.loads(dec["reason_codes_json"])
        except (TypeError, ValueError):
            codes = []
        reasons = [{"code": c, "label": label_reason(c)} for c in codes]
    scores = {}
    raw_scores = {}
    if dec["scores_json"]:
        try:
            raw_scores = json.loads(dec["scores_json"])
        except (TypeError, ValueError):
            raw_scores = {}
        for k, v in raw_scores.items():
            if isinstance(v, (int, float)):
                scores[k] = {"key": k, "label": SCORE_LABELS.get(k, "评分项"), "value": v}
    pick = conn.execute(
        "SELECT board, rank, formal_score FROM screen_pick WHERE run_id=? AND fcode=?"
        # 同一只基金可能同时出现在正式榜与持有期受限榜（例如 180 天持有期但已过基础门禁），
        # 决策快照要挂正式榜的排名，否则页面会把"受限榜第 2 名"当成本轮排名。
        " ORDER BY CASE WHEN board='bond_locked' THEN 1 ELSE 0 END, rank",
        (run_id, fcode)).fetchone()
    data_flags = {}
    if "data_flags_json" in dec.keys() and dec["data_flags_json"]:
        try:
            data_flags = json.loads(dec["data_flags_json"])
        except (TypeError, ValueError):
            data_flags = {}
    enhanced_meta = thresholds.get("enhanced_relative_meta") or {}
    if enhanced_meta:
        # 增强风格走的是"同风格分位 + 绝对地板"的独立准入，门槛值不是固定线；
        # 它落在 fixed_or_admission 组里（历史字段名），标题必须改，否则会误导成固定阈值。
        for g in groups:
            if g["key"] == "fixed_or_admission":
                g["title"] = "增强债准入（同风格分位 + 绝对地板）"
                g["meta"] = {
                    "threshold_source": "enhanced_relative",
                    "threshold_source_label": SOURCE_LABELS["enhanced_relative"],
                    "peers": enhanced_meta.get("n"),
                    "fallback": enhanced_meta.get("fallback"),
                    "annual_window": enhanced_meta.get("annual_window"),
                    "annual_window_mix": enhanced_meta.get("annual_window_mix"),
                }
    exp = explain.build(_explain_row(conn, fcode, run_id, dec, pick, raw_scores))
    share_compare = share_class_compare(conn, fcode)
    previous = _previous(conn, fcode, run_id, thresholds,
                         pick["formal_score"] if pick else None, prev_run)
    return {
        "fcode": fcode,
        "run_id": run_id,
        "layer_reached": dec["layer_reached"],
        "layer_label": label_layer(dec["layer_reached"]),
        "passed": bool(dec["passed"]),
        "confidence": dec["confidence"],
        "style_code": dec["style_code"],
        "board": pick["board"] if pick else None,
        "board_label": label_board(pick["board"]) if pick else None,
        "rank": pick["rank"] if pick else None,
        "formal_score": pick["formal_score"] if pick else None,
        "reason_codes": reasons,
        "data_flags": data_flags,
        "scores": [scores[k] for k in ("composite", "short_core", "long_core",
                                       "short_formal", "long_formal", "formal_score")
                   if k in scores],
        "why_selected": exp["why"],
        "caveat": exp["caveat"],
        "why_selected_text": explain.as_text(exp["why"]),
        "caveat_text": explain.as_text(exp["caveat"]),
        "share_class_compare": share_compare,
        "groups": groups,
        "enhanced_relative_meta": enhanced_meta or None,
        "previous": previous,
        "counterfactual": _counterfactual(
            conn, fcode, thresholds, pick["formal_score"] if pick else None,
            previous, pick["board"] if pick else None),
        "history": history(conn, fcode),
        "relief": _relief(dec),
        "cross_check": _cross(conn, fcode),
        "has_thresholds": bool(groups),
        "note": None if groups else "该基金在评分之前就被门禁拦下（%s），没有门槛快照"
                                    % label_layer(dec["layer_reached"]),
    }
