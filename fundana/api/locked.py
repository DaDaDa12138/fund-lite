"""持有期受限榜（bond_locked）：把"为什么买不到"讲清楚。

这一榜的基金在 L3 因最短持有期 > 90 天被排除出正式榜，但**不是不合格**：
定开基金的开放期由基金合同规定，持有期基金则是"买入后 N 天才能赎回"。
页面需要区分这两类，否则用户会把"每年开一次"当成"随时能买"。

数据来源与口径：
* 锁定天数 = fund_holding_policy.effective_days（结构化与名称推断取大者）
* 下次开放日 = 当日申赎快照里的 next_open_date（**可能是过去的日期**，
  上游给的是"本次开放期起始日"，所以这里显式区分 upcoming / observed_past，
  绝不把过去的日期说成"未来解锁日"）
* 净值指标取 1 年期（与正式榜同契约），只做参考，不参与任何评分
"""
from __future__ import annotations

import datetime as _dt

from ..fees import holding as holding_mod
from ..labels import (LOCK_BUCKETS, label_board, label_buyable, label_layer,
                      label_lockup, label_reason, label_style)

COST_1Y_SQL = holding_mod.cost_sql("p.fcode")
COST_3Y_SQL = holding_mod.cost_sql("p.fcode", horizon_days=1095)
COST_5Y_SQL = holding_mod.cost_sql("p.fcode", horizon_days=1825)

BUCKET_UNKNOWN = "unknown"
BUCKET_LABEL = {b[0]: b[1] for b in LOCK_BUCKETS}
BUCKET_LABEL[BUCKET_UNKNOWN] = "未判定"


class LockedError(ValueError):
    pass


def _bucket_case() -> str:
    parts = ["CASE"]
    for code, _label, lo, hi in LOCK_BUCKETS:
        parts.append("WHEN h.effective_days BETWEEN %d AND %d THEN '%s'" % (lo, hi, code))
    parts.append("ELSE '%s' END" % BUCKET_UNKNOWN)
    return " ".join(parts)


def _open_state(next_open: str | None, as_of: str | None) -> tuple[int | None, str]:
    if not next_open or not as_of:
        return None, "unknown"
    try:
        d0 = _dt.date.fromisoformat(as_of[:10])
        d1 = _dt.date.fromisoformat(next_open[:10])
    except ValueError:
        return None, "unknown"
    days = (d1 - d0).days
    return days, ("upcoming" if days >= 0 else "observed_past")


def _hint(lockup: str | None, days_until_open: int | None, next_open: str | None,
          effective_days: int | None) -> str:
    """一句话解释"现在能不能买/什么时候能赎回"。

    days_until_open 是相对当日快照的天数（可为负），effective_days 是合同锁定期，
    两者不能混用——早先版本把锁定期当成倒计时，540 天定开债被显示成"距下次开放日 540 天"。
    """
    if lockup == "periodic_open":
        if next_open and days_until_open is not None and days_until_open >= 0:
            return "距下次开放日 %d 天" % days_until_open
        if next_open:
            return "开放期已于 %s 开始（上游未给下一次）" % next_open[:10]
        return "上游未披露下次开放日"
    if lockup == "holding":
        return "买入后 %d 天可赎回" % (effective_days or 0)
    return "持有期 %d 天" % (effective_days or 0)

from . import sorting

SORT_MAP = {
    "rank": "p.rank", "lockup": "h.effective_days",
    "annual_1y": "m.annual_dec", "sharpe": "m.sharpe", "mdd": "m.mdd_dec",
    "positive": "m.positive_ratio_pct", "scale": "fp.asset_scale_billion",
    # 与正式榜同口径：1 年持有期总成本（ADR-059）
    "fee": COST_1Y_SQL, "cost_1y": COST_1Y_SQL, "cost_3y": COST_3Y_SQL,
    "ret_1w": "w1.total_return_pct", "ret_1m": "w2.total_return_pct",
    "ret_3m": "w3.total_return_pct", "ret_6m": "w4.total_return_pct",
    "annual_1w": "w1.annual_dec", "annual_1m": "w2.annual_dec",
    "annual_3m": "w3.annual_dec", "annual_6m": "w4.annual_dec",
}


def rows(conn, formula_contract: str, lockup: str | None = None, bucket: str | None = None,
         limit: int = 200, offset: int = 0, run_id: str | None = None,
         sort: str = "rank", order: str = "asc") -> dict:
    if run_id is None:
        row = conn.execute(
            "SELECT run_id FROM screen_run ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return {"run_id": None, "rows": [], "summary": None, "total": 0}
        run_id = row["run_id"]
    as_of_row = conn.execute(
        "SELECT MAX(business_date) AS d FROM fund_buyable_daily").fetchone()
    as_of = as_of_row["d"] if as_of_row else None
    base_sql = (
        "SELECT p.rank, p.fcode, f.name, f.type_text, d.style_code, d.layer_reached,"
        " d.reason_codes_json, h.lockup_type, h.effective_days, h.structured_days,"
        " h.name_inferred_days, b.purchase_status, b.redemption_status, b.buyable_state,"
        " b.next_open_date, fp.asset_scale_billion, fp.recurring_fee_pct,"
        " " + COST_1Y_SQL + " AS cost_1y_pct, " + COST_3Y_SQL + " AS cost_3y_pct,"
        " " + COST_5Y_SQL + " AS cost_5y_pct,"
        " m.annual_dec, m.sharpe, m.mdd_dec, m.positive_ratio_pct, m.n_obs,"
        " w1.total_return_pct AS ret_1w_pct, w1.annual_dec AS annual_1w_dec,"
        " w2.total_return_pct AS ret_1m_pct, w2.annual_dec AS annual_1m_dec,"
        " w3.total_return_pct AS ret_3m_pct, w3.annual_dec AS annual_3m_dec,"
        " w4.total_return_pct AS ret_6m_pct, w4.annual_dec AS annual_6m_dec,"
        " %s AS bucket"
        " FROM screen_pick p JOIN fund f ON f.fcode=p.fcode"
        " LEFT JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
        " LEFT JOIN fund_holding_policy h ON h.fcode=p.fcode"
        " LEFT JOIN fund_buyable_daily b ON b.fcode=p.fcode AND b.business_date=?"
        " LEFT JOIN fund_profile fp ON fp.fcode=p.fcode"
        " LEFT JOIN fund_fee_meta fm ON fm.fcode=p.fcode"
        " LEFT JOIN fund_metric m ON m.fcode=p.fcode AND m.period_days=365"
        "   AND m.formula_contract=?"
        " LEFT JOIN fund_metric w1 ON w1.fcode=p.fcode AND w1.period_days=7"
        "   AND w1.formula_contract=?"
        " LEFT JOIN fund_metric w2 ON w2.fcode=p.fcode AND w2.period_days=30"
        "   AND w2.formula_contract=?"
        " LEFT JOIN fund_metric w3 ON w3.fcode=p.fcode AND w3.period_days=90"
        "   AND w3.formula_contract=?"
        " LEFT JOIN fund_metric w4 ON w4.fcode=p.fcode AND w4.period_days=180"
        "   AND w4.formula_contract=?"
        " WHERE p.run_id=? AND p.board='bond_locked'"
    ) % _bucket_case()
    where = []
    args: list = [as_of, formula_contract, formula_contract, formula_contract,
                  formula_contract, formula_contract, run_id]
    if lockup:
        where.append("h.lockup_type=?")
        args.append(lockup)
    if bucket:
        where.append("bucket=?")
        args.append(bucket)
    spec = sorting.parse(sort, order, SORT_MAP)
    order_sql = sorting.order_sql(spec, SORT_MAP)
    sql = base_sql + (" AND " + " AND ".join(where) if where else "") + " ORDER BY " + order_sql
    all_rows = conn.execute(sql, args).fetchall()
    items = []
    for r in all_rows:
        days, state = _open_state(r["next_open_date"], as_of)
        reasons = []
        if r["reason_codes_json"]:
            try:
                import json
                reasons = [{"code": c, "label": label_reason(c)}
                           for c in json.loads(r["reason_codes_json"])]
            except (TypeError, ValueError):
                reasons = []
        items.append({
            "rank": r["rank"],
            "fcode": r["fcode"],
            "name": r["name"],
            "type_text": r["type_text"],
            "style_code": r["style_code"],
            "style_label": label_style(r["style_code"]),
            "lockup_type": r["lockup_type"],
            "lockup_label": label_lockup(r["lockup_type"]),
            "effective_days": r["effective_days"],
            "structured_days": r["structured_days"],
            "name_inferred_days": r["name_inferred_days"],
            "bucket": r["bucket"],
            "bucket_label": BUCKET_LABEL.get(r["bucket"] or BUCKET_UNKNOWN),
            "next_open_date": r["next_open_date"],
            "days_until_open": days,
            "open_state": state,
            "hint": _hint(r["lockup_type"], days, r["next_open_date"], r["effective_days"]),
            "purchase_status": r["purchase_status"],
            "redemption_status": r["redemption_status"],
            "buyable_state": r["buyable_state"],
            "buyable_label": label_buyable(r["buyable_state"]),
            "ret_1w_pct": r["ret_1w_pct"], "annual_1w_dec": r["annual_1w_dec"],
            "ret_1m_pct": r["ret_1m_pct"], "annual_1m_dec": r["annual_1m_dec"],
            "ret_3m_pct": r["ret_3m_pct"], "annual_3m_dec": r["annual_3m_dec"],
            "ret_6m_pct": r["ret_6m_pct"], "annual_6m_dec": r["annual_6m_dec"],
            "annual_1y_pct": None if r["annual_dec"] is None else round(r["annual_dec"] * 100, 4),
            "sharpe_1y": r["sharpe"],
            "mdd_1y_pct": None if r["mdd_dec"] is None else round(r["mdd_dec"] * 100, 4),
            "positive_1y_pct": r["positive_ratio_pct"],
            "n_obs_1y": r["n_obs"],
            "scale_billion": r["asset_scale_billion"],
            "recurring_fee_pct": r["recurring_fee_pct"],
            "cost_1y_pct": r["cost_1y_pct"], "cost_3y_pct": r["cost_3y_pct"],
            "cost_5y_pct": r["cost_5y_pct"],
            "layer_label": label_layer(r["layer_reached"]),
            "reason_codes": reasons,
        })
    by_lockup: dict[str, int] = {}
    by_bucket: dict[str, int] = {}
    for it in items:
        by_lockup[it["lockup_type"] or "unknown"] = by_lockup.get(it["lockup_type"] or "unknown", 0) + 1
        by_bucket[it["bucket"] or BUCKET_UNKNOWN] = by_bucket.get(it["bucket"] or BUCKET_UNKNOWN, 0) + 1
    summary = {
        "board": "bond_locked",
        "board_label": label_board("bond_locked"),
        "total": len(items),
        "as_of": as_of,
        "with_next_open": sum(1 for it in items if it["next_open_date"]),
        "upcoming": sum(1 for it in items if it["open_state"] == "upcoming"),
        "observed_past": sum(1 for it in items if it["open_state"] == "observed_past"),
        "by_lockup": [{"code": k, "label": label_lockup(k), "count": v}
                      for k, v in sorted(by_lockup.items(), key=lambda kv: -kv[1])],
        "by_bucket": [{"code": b[0], "label": b[1], "count": by_bucket.get(b[0], 0)}
                      for b in LOCK_BUCKETS]
                     + ([{"code": BUCKET_UNKNOWN, "label": BUCKET_LABEL[BUCKET_UNKNOWN],
                          "count": by_bucket[BUCKET_UNKNOWN]}]
                        if BUCKET_UNKNOWN in by_bucket else []),
        "median_days": _median([it["effective_days"] for it in items if it["effective_days"]]),
        "note": "持有期受限不等于不合格：定开基金只在开放期可申赎，持有期基金是买入后 N 天可赎回。"
                "本榜不参与正式评分与排序。",
    }
    page = items[offset:offset + limit]
    return {"run_id": run_id, "as_of": as_of, "total": len(items),
            "limit": limit, "offset": offset, "rows": page, "summary": summary}


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    n = len(vs)
    mid = n // 2
    return float(vs[mid]) if n % 2 else round((vs[mid - 1] + vs[mid]) / 2, 1)
