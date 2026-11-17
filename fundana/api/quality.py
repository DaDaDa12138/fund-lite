"""质量看板（Batch A / A5）：把"这一轮数据靠不靠得住"摊平成一张可核对的表。

全部指标都来自已落库的事实（fetch_log / nav_cross_check / publication_gate /
fund_coverage / fund_profile / fund_nav_daily / fund_scale_history / fund_manager_tenure /
fund_fee_schedule / fund_fee_meta），
不额外发请求，也不做推断性打分：
成功率就是成功率，NULL 率就是 NULL 率，谁都不许用"看起来还行"代替。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from .. import db, freshness, fundtypes

CN_TZ = db.CN


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def build(conn, cfg, days: int = 7) -> dict[str, Any]:
    since = (datetime.now(CN_TZ) - timedelta(days=days)).isoformat(timespec="seconds")

    sources: list[dict[str, Any]] = []
    for r in conn.execute(
            "SELECT source_id, COUNT(*) AS calls,"
            " SUM(CASE WHEN business_ok=1 THEN 1 ELSE 0 END) AS ok,"
            " SUM(CASE WHEN business_ok=0 THEN 1 ELSE 0 END) AS bad,"
            " ROUND(AVG(duration_ms), 1) AS avg_ms, MAX(started_at) AS last_at"
            " FROM fetch_log WHERE started_at >= ? GROUP BY source_id ORDER BY source_id",
            (since,)):
        calls = r["calls"] or 0
        sources.append({"source_id": r["source_id"], "calls": calls,
                        "business_ok": r["ok"] or 0, "business_failed": r["bad"] or 0,
                        "success_rate_pct": _pct(r["ok"] or 0, calls),
                        "avg_ms": r["avg_ms"], "last_at": r["last_at"]})

    total = conn.execute("SELECT COUNT(*) FROM fund").fetchone()[0] or 1
    nulls = []
    for field, table, where in (
            ("asset_scale_billion", "fund_profile", "asset_scale_billion IS NULL"),
            ("recurring_fee_pct", "fund_profile", "recurring_fee_pct IS NULL"),
            ("estab_date", "fund", "estab_date IS NULL")):
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
        nulls.append({"field": field, "null_count": n, "null_ratio_pct": _pct(n, total)})

    nav = conn.execute(
        "SELECT COUNT(*) AS rows, COUNT(DISTINCT fcode) AS funds, MIN(nav_date) AS first_date,"
        " MAX(nav_date) AS last_date FROM fund_nav_daily").fetchone()
    latest_bd = conn.execute("SELECT MAX(business_date) FROM fund_buyable_daily").fetchone()[0]
    # 质量页也不能用 MAX(nav_date) 代表市场数据日：11-03 只有 85.5% 的基金
    # 披露，MAX 会让页面误报“数据已到 11-03”。有效日与覆盖率统一走 freshness。
    nav_cov = freshness.nav_coverage(conn, types=fundtypes.resolve(cfg=cfg),
                                     as_of=latest_bd)
    effective_nav = nav_cov.get("effective_date") or nav["last_date"]
    age = None
    max_age = None
    if effective_nav and latest_bd:
        age = (date.fromisoformat(str(latest_bd)[:10]) - date.fromisoformat(str(effective_nav)[:10])).days
    if nav["last_date"] and latest_bd:
        max_age = (date.fromisoformat(str(latest_bd)[:10]) - date.fromisoformat(str(nav["last_date"])[:10])).days

    scale = conn.execute(
        "SELECT COUNT(*) AS rows, COUNT(DISTINCT fcode) AS funds,"
        " MIN(report_period) AS first_period, MAX(report_period) AS last_period"
        " FROM fund_scale_history").fetchone()
    tenure = conn.execute(
        "SELECT COUNT(*) AS rows, COUNT(DISTINCT fcode) AS funds,"
        " SUM(CASE WHEN end_date IS NULL THEN 1 ELSE 0 END) AS current_rows"
        " FROM fund_manager_tenure").fetchone()
    fee = conn.execute(
        "SELECT COUNT(*) AS rows, COUNT(DISTINCT fcode) AS funds,"
        " SUM(CASE WHEN kind='red' THEN 1 ELSE 0 END) AS redemption_rows"
        " FROM fund_fee_schedule").fetchone()
    red = conn.execute(
        "SELECT COUNT(*) AS funds,"
        " SUM(CASE WHEN redemption_1y_pct IS NOT NULL THEN 1 ELSE 0 END) AS with_1y,"
        " SUM(CASE WHEN redemption_1y_pct > 0 THEN 1 ELSE 0 END) AS positive_1y"
        " FROM fund_fee_meta").fetchone()

    cross = conn.execute(
        "SELECT business_date, COUNT(*) AS n,"
        " SUM(CASE WHEN status='mismatch' THEN 1 ELSE 0 END) AS mismatch,"
        " SUM(CASE WHEN status='date_lag' THEN 1 ELSE 0 END) AS date_lag,"
        " SUM(CASE WHEN status LIKE 'missing%' THEN 1 ELSE 0 END) AS missing,"
        " MAX(checked_at) AS checked_at FROM nav_cross_check"
        " WHERE field != 'return_1y'"
        " GROUP BY business_date ORDER BY business_date DESC LIMIT 5").fetchall()
    cross_rows = [dict(r) for r in cross]
    for row in cross_rows:
        row["mismatch_ratio_pct"] = _pct(row["mismatch"] or 0, row["n"] or 0)

    gates = [dict(r) for r in conn.execute(
        "SELECT g.publication_id, g.run_id, g.status, g.created_at,"
        " (SELECT COUNT(*) FROM json_each(g.blocks_json)) AS blocks,"
        " (SELECT COUNT(*) FROM json_each(g.warnings_json)) AS warnings"
        " FROM publication_gate g ORDER BY g.created_at DESC LIMIT 5")]

    flags: dict[str, int] = {}
    # 只看最近一轮的覆盖标记：fund_coverage 按 run 累积，跨轮统计会把同一只基金数很多遍
    for r in conn.execute(
            "SELECT flags_json FROM fund_coverage WHERE flags_json IS NOT NULL AND run_id ="
            " (SELECT run_id FROM fund_coverage ORDER BY rowid DESC LIMIT 1)"):
        try:
            for f in json.loads(r["flags_json"]) or []:
                flags[f] = flags.get(f, 0) + 1
        except (ValueError, TypeError):
            continue

    return {"window_days": days, "since": since, "sources": sources,
            "field_nulls": nulls,
            "nav": {"rows": nav["rows"], "funds": nav["funds"],
                    "first_date": nav["first_date"],
                    "last_date": effective_nav, "effective_date": effective_nav,
                    "max_date": nav["last_date"],
                    "coverage_pct": nav_cov.get("effective_coverage_pct"),
                    "max_coverage_pct": nav_cov.get("max_coverage_pct"),
                    "partial_latest": bool(nav["last_date"] and effective_nav
                                           and str(nav["last_date"]) > str(effective_nav)),
                    "business_date": latest_bd, "nav_age_days": age,
                    "max_age_days": max_age,
                    "nav_age_limit_days": cfg.get("quality", "nav_age_limit_days", default=10)},
            "scale_history": dict(scale),
            "manager_tenure": dict(tenure),
            "fee_schedule": dict(fee),
            "redemption_snapshot": dict(red),
            "cross_check": cross_rows, "gates": gates, "coverage_flags": flags}
