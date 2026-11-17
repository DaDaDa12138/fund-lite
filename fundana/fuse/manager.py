"""经理维度：从 pingzhongdata 的 Data_currentFundManager 解析现任经理证据。

口径（重要）：
* career_days 由 workTime（"13年又50天"）解析，是**证券从业年限**，不是本基金任职年限；
* tenure_return_pct 是 profit.series[0].data[0].y，即该经理**任期收益**（百分点）；
* power_avr 是东财 0–100 综合评分，**不是 MGOLD 10 分制**，两者禁止互相映射（ADR-019）。
缺失一律留 NULL，不补 0，也不伪造"低分"。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .. import db, fundtypes
from ..fetch.parsers import to_float

_CAREER_RE = re.compile(r"(?:(\d+)\s*年)?\s*(?:又)?\s*(?:(\d+)\s*天)?")


def parse_career_days(text: str | None) -> int | None:
    """把 "13年又50天" / "3年" / "240天" 解析成天数；无法解析返回 None。"""
    if not text:
        return None
    m = _CAREER_RE.search(str(text))
    if not m:
        return None
    years = int(m.group(1) or 0)
    days = int(m.group(2) or 0)
    if not years and not days:
        return None
    return years * 365 + days


def _power_avr(mgr: dict[str, Any]) -> float | None:
    power = mgr.get("power") or {}
    val = to_float(power.get("avr"))
    if val is None or val < 0 or val > 100:
        return None
    return val


def _tenure_return(mgr: dict[str, Any]) -> float | None:
    try:
        series = ((mgr.get("profit") or {}).get("series") or [])[0]
        point = (series.get("data") or [])[0]
        return to_float(point.get("y"))
    except (IndexError, AttributeError, TypeError):
        return None


def parse_managers(text: str | None) -> list[dict[str, Any]]:
    """解析现任经理数组；结构异常返回空列表（由调用方记 provider_null）。"""
    if not text:
        return []
    try:
        arr = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(arr, list):
        return []
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(arr):
        if not isinstance(item, dict):
            continue
        mgr_id = str(item.get("id") or "").strip()
        if not mgr_id:
            continue
        star = item.get("star")
        out.append({
            "mgr_id": mgr_id,
            "mgr_name": (item.get("name") or "").strip() or None,
            "is_lead": 1 if idx == 0 else 0,
            "career_days": parse_career_days(item.get("workTime")),
            "tenure_return_pct": _tenure_return(item),
            "power_avr": _power_avr(item),
            "star": int(star) if isinstance(star, (int, float)) else None,
            "fund_size_text": (item.get("fundSize") or "").strip() or None,
        })
    return out


def run_managers(conn, business_date: str, types=None) -> dict[str, Any]:
    """写入 fund_manager_evidence，并把汇总值回填 fund_profile。"""
    clause, params = fundtypes.where(types, column="f.type_group")
    rows = conn.execute(
        "SELECT p.code AS fcode, p.manager_json, p.raw_sha256"
        " FROM src_pzd_profile p JOIN fund f ON f.fcode = p.code"
        f" WHERE {clause}", params).fetchall()
    observed_at = db.now_iso()
    stats = {"funds": 0, "with_managers": 0, "without_managers": 0,
             "evidence_rows": 0, "career_known": 0, "power_known": 0}
    conn.execute("DELETE FROM fund_manager_evidence")
    for r in rows:
        stats["funds"] += 1
        mgrs = parse_managers(r["manager_json"])
        if not mgrs:
            stats["without_managers"] += 1
            conn.execute(
                "UPDATE fund_profile SET manager_count=0, manager_career_min_days=NULL,"
                " manager_career_max_days=NULL, manager_power_score=NULL WHERE fcode=?",
                (r["fcode"],))
            continue
        stats["with_managers"] += 1
        for m in mgrs:
            conn.execute(
                "INSERT OR REPLACE INTO fund_manager_evidence(fcode, mgr_id, mgr_name, is_lead,"
                " career_days, tenure_return_pct, power_avr, star, fund_size_text, observed_at,"
                " raw_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (r["fcode"], m["mgr_id"], m["mgr_name"], m["is_lead"], m["career_days"],
                 m["tenure_return_pct"], m["power_avr"], m["star"], m["fund_size_text"],
                 observed_at, r["raw_sha256"]))
            stats["evidence_rows"] += 1
        careers = [m["career_days"] for m in mgrs if m["career_days"] is not None]
        powers = [m["power_avr"] for m in mgrs if m["power_avr"] is not None]
        if careers:
            stats["career_known"] += 1
        if powers:
            stats["power_known"] += 1
        conn.execute(
            "UPDATE fund_profile SET manager_count=?, manager_career_min_days=?,"
            " manager_career_max_days=?, manager_power_score=? WHERE fcode=?",
            (len(mgrs), min(careers) if careers else None, max(careers) if careers else None,
             round(sum(powers) / len(powers), 4) if powers else None, r["fcode"]))
    conn.commit()
    return stats
