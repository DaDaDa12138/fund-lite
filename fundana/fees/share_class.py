"""同一基金家族内 A/C/D 份额的持有期成本比较（P1）。

只做“同一 family 内选哪一类份额”的建议，不改变榜单成员：榜单仍按基金策略
与风险指标选，份额选择是执行层问题。成本口径与 fundana.fees.holding 完全一致：
    cost(h) = recurring + max_sub × (0.1 if has_discount else 1) / (h/365)
赎回费不进入比较（与持有期成本门槛一致），只在结果里原样展示。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .holding import DEFAULT_HORIZONS, holding_cost


def _purchase_effective(max_sub: float | None, has_discount: bool | None) -> float:
    sub = float(max_sub or 0.0)
    return sub * 0.1 if has_discount else sub


def _break_even_years(cur: dict[str, Any], alt: dict[str, Any]) -> float | None:
    """当前份额与替代份额成本相等所需持有年数。

    cost_cur(h) - cost_alt(h) = (r_cur-r_alt) + (p_cur-p_alt)*365/h
    令其为 0：h = -(p_cur-p_alt)*365/(r_cur-r_alt)。
    只有 h>0 且有限时才有经济含义；否则说明一个份额在所有期限都更贵/更便宜。
    """
    dr = float(cur["recurring_pct"]) - float(alt["recurring_pct"])
    dp = float(cur["purchase_effective_pct"]) - float(alt["purchase_effective_pct"])
    if abs(dr) < 1e-12:
        return None
    h = -dp * 365.0 / dr
    if h <= 0 or h > 100 * 365:
        return None
    return round(h / 365.0, 2)


def compare(conn, fcode: str, horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> dict[str, Any]:
    """返回当前份额与同 family 兄弟份额的 1/3/5 年成本、推荐份额与盈亏平衡年数。"""
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return _compare(conn, fcode, horizons)
    finally:
        conn.row_factory = old_factory


def _compare(conn, fcode: str, horizons: tuple[int, ...]) -> dict[str, Any]:
    cur = conn.execute(
        "SELECT s.family_id, s.class_letter, p.recurring_fee_pct, p.max_sub_fee_pct,"
        " fm.has_discount, fm.redemption_1y_pct, fm.redemption_3y_pct,"
        " json_extract(p.field_src_json, '$.share_class_scale_billion') AS share_scale"
        " FROM fund_share_class s"
        " LEFT JOIN fund_profile p ON p.fcode=s.fcode"
        " LEFT JOIN fund_fee_meta fm ON fm.fcode=s.fcode"
        " WHERE s.fcode=?", (fcode,)).fetchone()
    if not cur:
        return {"fcode": fcode, "family_id": None, "class_letter": None,
                "members": [], "recommended": None, "recommended_by_horizon": {},
                "break_even_years": None,
                "note": "该基金没有份额分类记录，无法比较 A/C/D"}
    family = cur["family_id"]
    if not family:
        return {"fcode": fcode, "family_id": None, "class_letter": cur["class_letter"],
                "members": [], "recommended": None, "recommended_by_horizon": {},
                "break_even_years": None, "note": "该基金没有 family_id，无法比较兄弟份额"}
    rows = conn.execute(
        "SELECT s.fcode, s.class_letter, p.recurring_fee_pct, p.max_sub_fee_pct,"
        " fm.has_discount, fm.redemption_1y_pct, fm.redemption_3y_pct,"
        " json_extract(p.field_src_json, '$.share_class_scale_billion') AS share_scale"
        " FROM fund_share_class s"
        " LEFT JOIN fund_profile p ON p.fcode=s.fcode"
        " LEFT JOIN fund_fee_meta fm ON fm.fcode=s.fcode"
        " WHERE s.family_id=? ORDER BY s.class_letter, s.fcode", (family,)).fetchall()
    members: list[dict[str, Any]] = []
    for r in rows:
        costs: dict[str, dict[str, Any] | None] = {}
        for h in horizons:
            costs[str(h)] = holding_cost(r["recurring_fee_pct"], r["max_sub_fee_pct"],
                                         r["has_discount"], h)
        members.append({
            "fcode": r["fcode"], "class_letter": r["class_letter"],
            "recurring_fee_pct": r["recurring_fee_pct"],
            "max_sub_fee_pct": r["max_sub_fee_pct"],
            "has_discount": bool(r["has_discount"]),
            "share_scale_billion": r["share_scale"],
            "redemption_1y_pct": r["redemption_1y_pct"],
            "costs": costs,
            "cost_1y_pct": (costs.get("365") or {}).get("total_pct"),
            "cost_3y_pct": (costs.get("1095") or {}).get("total_pct"),
            "cost_5y_pct": (costs.get("1825") or {}).get("total_pct"),
        })
    usable = [m for m in members if m.get("cost_3y_pct") is not None]
    by_horizon: dict[str, str] = {}
    for h in horizons:
        key = str(h)
        candidates = [m for m in members if (m["costs"].get(key) or {}).get("total_pct") is not None]
        if candidates:
            best = min(candidates, key=lambda m: (m["costs"][key]["total_pct"], m["fcode"]))
            by_horizon[key] = best["fcode"]
    recommended = min(usable, key=lambda m: (m["cost_3y_pct"], m["cost_1y_pct"] or 9e9,
                                             m["fcode"])) if usable else None
    current_member = next((m for m in members if m["fcode"] == fcode), None)
    advantage = None
    break_even = None
    if current_member and recommended and current_member.get("cost_3y_pct") is not None:
        advantage = round(current_member["cost_3y_pct"] - recommended["cost_3y_pct"], 4)
        if current_member["fcode"] != recommended["fcode"]:
            cur_costs = current_member["costs"].get("1095")
            alt_costs = recommended["costs"].get("1095")
            if cur_costs and alt_costs:
                break_even = _break_even_years(cur_costs, alt_costs)
    return {
        "fcode": fcode, "family_id": family, "class_letter": cur["class_letter"],
        "members": members,
        "recommended": recommended,
        "recommended_by_horizon": by_horizon,
        "current_cost_3y_pct": (current_member or {}).get("cost_3y_pct"),
        "recommended_cost_3y_pct": (recommended or {}).get("cost_3y_pct"),
        "advantage_3y_pct": advantage,
        "break_even_years": break_even,
        "note": ("按 3 年持有期成本推荐；1/3/5 年各自最便宜见 recommended_by_horizon。"
                 "赎回费未计入，实际选择还需看持有期与赎回条款。"),
    }
