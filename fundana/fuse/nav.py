"""净值覆盖与质量：只做 QC 与覆盖统计，不重写 NAV（NAV 在采集阶段已按 (fcode,nav_date) 落库）。"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from .. import db


def _d(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def run_coverage(conn, run_id: str, as_of: str, nav_age_limit_days: int = 10,
                 max_gap_days: int = 14) -> dict[str, Any]:
    """对每只基金统计首末净值日、点数、最大缺口、新鲜度与标记。"""
    rows = conn.execute(
        "SELECT fcode, COUNT(*) AS n, MIN(nav_date) AS d0, MAX(nav_date) AS d1,"
        " SUM(CASE WHEN growth_pct IS NULL THEN 1 ELSE 0 END) AS g_null,"
        " SUM(CASE WHEN unit_nav IS NULL THEN 1 ELSE 0 END) AS u_null"
        " FROM fund_nav_daily GROUP BY fcode").fetchall()
    as_of_d = _d(as_of)
    out, flags_count = [], {"no_nav": 0, "stale": 0, "gap": 0, "short_history": 0, "growth_missing": 0}
    for r in rows:
        code = r["fcode"]
        d0, d1 = _d(r["d0"]), _d(r["d1"])
        dates = [x["nav_date"] for x in conn.execute(
            "SELECT nav_date FROM fund_nav_daily WHERE fcode=? ORDER BY nav_date", (code,))]
        gap_max = 0
        prev = None
        for ds in dates:
            cur = _d(ds)
            if prev is not None and cur is not None:
                delta = (cur - prev).days
                if delta > gap_max:
                    gap_max = delta
            prev = cur
        freshness = (as_of_d - d1).days if (as_of_d and d1) else None
        flags = []
        if not r["n"]:
            flags.append("no_nav")
        if freshness is not None and freshness > nav_age_limit_days:
            flags.append("stale")
        if gap_max > max_gap_days:
            flags.append("gap")
        if d0 and d1 and (d1 - d0).days < 365:
            flags.append("short_history")
        if r["g_null"] and r["n"] and r["g_null"] / r["n"] > 0.10:
            flags.append("growth_missing")
        for f in flags:
            flags_count[f] = flags_count.get(f, 0) + 1
        out.append((code, run_id, r["d0"], r["d1"], r["n"], gap_max, freshness,
                    json.dumps(flags, ensure_ascii=False)))
    conn.executemany(
        "INSERT INTO fund_coverage(fcode, run_id, nav_first_date, nav_last_date, nav_points,"
        " gap_max_days, freshness_days, flags_json) VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode, run_id) DO UPDATE SET nav_first_date=excluded.nav_first_date,"
        " nav_last_date=excluded.nav_last_date, nav_points=excluded.nav_points,"
        " gap_max_days=excluded.gap_max_days, freshness_days=excluded.freshness_days,"
        " flags_json=excluded.flags_json", out)
    conn.commit()
    return {"funds": len(out), "flags": flags_count,
            "nav_rows": conn.execute("SELECT COUNT(*) AS c FROM fund_nav_daily").fetchone()["c"]}
