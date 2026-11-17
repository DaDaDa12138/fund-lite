"""覆盖报告：来源分布、冲突、NULL 统计，供 docs 与发布 diagnostics 使用。"""
from __future__ import annotations

import json
from typing import Any

from .. import db


def build_report(conn, run_id: str) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) AS c FROM fund WHERE identity_status='ok'").fetchone()["c"]
    quarantined = conn.execute(
        "SELECT COUNT(*) AS c FROM fund WHERE identity_status='quarantine'").fetchone()["c"]
    by_group = {r["type_group"]: r["c"] for r in conn.execute(
        "SELECT type_group, COUNT(*) AS c FROM fund GROUP BY type_group ORDER BY c DESC")}
    by_style = {r["style_code"]: r["c"] for r in conn.execute(
        "SELECT style_code, COUNT(*) AS c FROM fund_style GROUP BY style_code ORDER BY c DESC")}
    by_conf = {r["confidence"]: r["c"] for r in conn.execute(
        "SELECT confidence, COUNT(*) AS c FROM fund_style GROUP BY confidence")}
    scale_src = {r["asset_scale_src"] or "missing": r["c"] for r in conn.execute(
        "SELECT asset_scale_src, COUNT(*) AS c FROM fund_profile GROUP BY asset_scale_src")}
    conflicts = conn.execute(
        "SELECT COUNT(*) AS c FROM fund_profile WHERE conflict_json IS NOT NULL").fetchone()["c"]
    buyable = {r["buyable_state"]: r["c"] for r in conn.execute(
        "SELECT buyable_state, COUNT(*) AS c FROM fund_buyable_daily WHERE business_date=("
        "SELECT MAX(business_date) FROM fund_buyable_daily) GROUP BY buyable_state")}
    lockup = {r["lockup_type"]: r["c"] for r in conn.execute(
        "SELECT lockup_type, COUNT(*) AS c FROM fund_holding_policy GROUP BY lockup_type")}
    cov = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN nav_points IS NULL OR nav_points=0 THEN 1 ELSE 0 END) AS no_nav,"
        " SUM(CASE WHEN freshness_days > 10 THEN 1 ELSE 0 END) AS stale,"
        " MIN(nav_first_date) AS first, MAX(nav_last_date) AS last,"
        " AVG(nav_points) AS avg_points FROM fund_coverage WHERE run_id=?", (run_id,)).fetchone()
    nulls = {}
    for field in ("estab_date", "asset_scale_billion", "recurring_fee_pct", "benchmark_text"):
        table = "fund_profile" if field != "estab_date" else "fund"
        row = conn.execute(f"SELECT SUM(CASE WHEN {field} IS NULL THEN 1 ELSE 0 END) AS c,"
                           f" COUNT(*) AS t FROM {table}").fetchone()
        nulls[field] = {"null": row["c"], "total": row["t"],
                        "ratio": round((row["c"] or 0) / row["t"], 4) if row["t"] else None}
    report = {
        "run_id": run_id,
        "generated_at": db.now_iso(),
        "funds_ok": total,
        "funds_quarantined": quarantined,
        "by_type_group": by_group,
        "by_style": by_style,
        "style_confidence": by_conf,
        "scale_source": scale_src,
        "profile_conflicts": conflicts,
        "buyable_state": buyable,
        "lockup_type": lockup,
        "nav_coverage": {
            "funds": cov["n"], "no_nav": cov["no_nav"] or 0, "stale": cov["stale"] or 0,
            "first_date": cov["first"], "last_date": cov["last"],
            "avg_points": round(cov["avg_points"], 1) if cov["avg_points"] else None,
            "total_nav_rows": conn.execute("SELECT COUNT(*) AS c FROM fund_nav_daily").fetchone()["c"],
        },
        "null_ratios": nulls,
    }
    return report


def write_report(report: dict[str, Any], path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
