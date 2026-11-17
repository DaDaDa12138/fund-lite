"""数据新鲜度：区分「有净值的最新日」与「覆盖足够基金的有效日」。

背景（2026-11-03 事故）：B1 快照在 11-03 只披露了 185 只基金的 11-02 净值，
但系统用 MAX(nav_date) 把全市场 as_of 抬到 11-02，指标、筛选、发布门禁、
数据健康页都误以为「最新数据日 = 11-02」。实际 8,000 多只基金仍停在 10-30。

本模块只读，不修改任何基金原始指标。所有口径都返回证据字段，调用方不得
再用 MAX(nav_date) 单独代表市场数据日。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import fundtypes

DEFAULT_MIN_NAV_COVERAGE = 0.90
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_METRIC_TOLERANCE_DAYS = 7
DEFAULT_MIN_METRIC_COVERAGE = 0.90


def _d(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _has_column(conn, table: str, column: str) -> bool:
    try:
        return any(r[1] == column for r in conn.execute("PRAGMA table_info(%s)" % table))
    except Exception:  # noqa: BLE001 - 旧库/测试桩没有该表
        return False


def _active_clause(conn) -> str:
    """当前可投宇宙：已确认清盘/合并的基金不再计入净值/指标覆盖率分母。

    历史清盘基金已回填到 fund/fund_nav_daily（用于 walkforward），但它们的
    净值停在清盘日；若继续计入当前覆盖率，会把有效净值日整体拉回过去。
    """
    if _has_table(conn, "fund") and _has_column(conn, "fund", "fund_status"):
        return " AND COALESCE(f.fund_status,'') NOT IN ('liquidated','merged')"
    return ""


def _universe_codes(conn, types=None) -> list[str]:
    """筛选宇宙的基金代码。没有 fund 表的旧库/测试桩退回净值表去重。"""
    if _has_table(conn, "fund"):
        clause, params = fundtypes.where(types, column="f.type_group")
        return [r[0] for r in conn.execute(
            "SELECT f.fcode FROM fund f WHERE " + clause + _active_clause(conn) +
            " ORDER BY f.fcode", params)]
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT fcode FROM fund_nav_daily ORDER BY fcode")]


def _nav_rows(conn, start: str, end: str, types=None):
    """区间内每个净值日的覆盖只数。"""
    has_unit = _has_column(conn, "fund_nav_daily", "unit_nav")
    unit = " AND n.unit_nav IS NOT NULL" if has_unit else ""
    if _has_table(conn, "fund"):
        clause, params = fundtypes.where(types, column="f.type_group")
        return conn.execute(
            "SELECT n.nav_date, COUNT(DISTINCT n.fcode) AS n FROM fund_nav_daily n"
            " JOIN fund f ON f.fcode=n.fcode"
            " WHERE n.nav_date BETWEEN ? AND ?" + unit + " AND " + clause +
            _active_clause(conn) +
            " GROUP BY n.nav_date ORDER BY n.nav_date DESC", (start, end, *params)).fetchall()
    unit2 = " AND unit_nav IS NOT NULL" if has_unit else ""
    return conn.execute(
        "SELECT nav_date, COUNT(DISTINCT fcode) AS n FROM fund_nav_daily"
        " WHERE nav_date BETWEEN ? AND ?" + unit2 +
        " GROUP BY nav_date ORDER BY nav_date DESC", (start, end)).fetchall()


def nav_coverage(conn, types=None, as_of: str | None = None,
                 min_coverage: float = DEFAULT_MIN_NAV_COVERAGE,
                 lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict[str, Any]:
    """按日期统计本类型 universe 的净值覆盖率，返回有效净值日。

    effective_date = 回看窗口内、覆盖率 >= min_coverage 的最新日期。
    max_date       = 窗口内出现过的最大净值日（可能只有少数基金）。
    """
    codes = _universe_codes(conn, types)
    universe = len(codes)
    end = (as_of or conn.execute("SELECT MAX(nav_date) FROM fund_nav_daily").fetchone()[0])
    end_d = _d(end)
    if not end_d or not universe:
        return {"status": "missing", "as_of": end, "effective_date": None, "max_date": None,
                "universe": universe, "min_coverage_pct": round(100.0 * min_coverage, 2),
                "lookback_days": lookback_days, "by_date": []}
    start = (end_d - timedelta(days=max(1, int(lookback_days)))).isoformat()
    rows = _nav_rows(conn, start, end_d.isoformat(), types)
    by_date = [{"date": r[0], "funds": int(r[1]),
                "coverage_pct": round(100.0 * int(r[1]) / universe, 2)} for r in rows]
    if not by_date:
        return {"status": "missing", "as_of": end_d.isoformat(), "effective_date": None,
                "max_date": None, "universe": universe,
                "min_coverage_pct": round(100.0 * min_coverage, 2),
                "lookback_days": lookback_days, "by_date": []}
    max_row = by_date[0]
    effective = next((r for r in by_date if r["funds"] / universe >= min_coverage), None)
    eff_date = effective["date"] if effective else None
    lag = None
    if eff_date and max_row["date"]:
        lag = (_d(max_row["date"]) - _d(eff_date)).days
    return {
        "status": "ok" if eff_date else "low_coverage",
        "as_of": end_d.isoformat(),
        "effective_date": eff_date,
        "effective_coverage_pct": effective["coverage_pct"] if effective else None,
        "max_date": max_row["date"],
        "max_coverage_pct": max_row["coverage_pct"],
        "universe": universe,
        "min_coverage_pct": round(100.0 * min_coverage, 2),
        "lookback_days": lookback_days,
        "lag_days": lag,
        "by_date": by_date[:30],
    }


def effective_nav_date(conn, types=None, as_of: str | None = None,
                       min_coverage: float = DEFAULT_MIN_NAV_COVERAGE,
                       lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> str | None:
    """简版：只要有效净值日。"""
    return nav_coverage(conn, types=types, as_of=as_of, min_coverage=min_coverage,
                        lookback_days=lookback_days).get("effective_date")


def _codes_with_nav(conn, as_of: str, types=None) -> list[str]:
    has_unit = _has_column(conn, "fund_nav_daily", "unit_nav")
    if _has_table(conn, "fund"):
        clause, params = fundtypes.where(types, column="f.type_group")
        unit = " AND n.unit_nav IS NOT NULL" if has_unit else ""
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT n.fcode FROM fund_nav_daily n JOIN fund f ON f.fcode=n.fcode"
            " WHERE n.nav_date=?" + unit + " AND " + clause + _active_clause(conn),
            (as_of, *params))]
    unit2 = " AND unit_nav IS NOT NULL" if has_unit else ""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT fcode FROM fund_nav_daily WHERE nav_date=?" + unit2, (as_of,))]


def metric_coverage(conn, as_of: str, formula_contract: str | None = None, types=None,
                    tolerance_days: int = DEFAULT_METRIC_TOLERANCE_DAYS) -> dict[str, Any]:
    """365 天指标相对 as_of 的覆盖：current / fresh / older / missing。

    分母是 as_of 当天有净值的基金，不是全库：没有净值的基金本来就不该被
    当成「指标过期」，它们会先被净值覆盖门挡住。
    """
    as_of_d = _d(as_of)
    if not as_of_d:
        return {"status": "missing", "as_of": as_of, "metric_as_of": None,
                "current_pct": None, "fresh_pct": None, "older_pct": None,
                "missing_pct": None, "denominator": 0, "tolerance_days": tolerance_days}
    codes = _codes_with_nav(conn, as_of_d.isoformat(), types)
    denominator = len(codes)
    if not denominator or not _has_table(conn, "fund_metric") or not _has_column(
            conn, "fund_metric", "end_date"):
        return {"status": "unknown" if denominator else "missing", "as_of": as_of_d.isoformat(),
                "metric_as_of": None, "current_pct": None, "fresh_pct": None,
                "older_pct": None, "missing_pct": None, "denominator": denominator,
                "tolerance_days": tolerance_days}
    params: list[Any] = [365]
    sql = ("SELECT m.fcode, MAX(m.end_date) AS ed FROM fund_metric m"
           " WHERE m.period_days=? AND m.end_date IS NOT NULL")
    if formula_contract:
        sql += " AND m.formula_contract=?"
        params.append(formula_contract)
    sql += " GROUP BY m.fcode"
    latest = {r[0]: (r[1] or "")[:10] for r in conn.execute(sql, params)}
    fresh_cut = (as_of_d - timedelta(days=max(0, int(tolerance_days)))).isoformat()
    current = fresh = older = missing = 0
    dates: dict[str, int] = {}
    for code in codes:
        ed = latest.get(code)
        if not ed:
            missing += 1
            continue
        dates[ed] = dates.get(ed, 0) + 1
        if ed == as_of_d.isoformat():
            current += 1
        elif ed >= fresh_cut:
            fresh += 1
        else:
            older += 1
    metric_as_of = max(dates, key=lambda d: dates[d]) if dates else None
    metric_max = max(dates) if dates else None

    def pct(n: int) -> float:
        return round(100.0 * n / denominator, 2)

    current_pct, fresh_pct = pct(current), pct(fresh)
    older_pct, missing_pct = pct(older), pct(missing)
    status = ("ok" if current_pct >= 100.0 * DEFAULT_MIN_METRIC_COVERAGE else
              "partial" if (current + fresh) / denominator >= DEFAULT_MIN_METRIC_COVERAGE else
              "stale" if dates else "missing")
    return {
        "status": status,
        "as_of": as_of_d.isoformat(),
        "metric_as_of": metric_as_of,
        "metric_max_date": metric_max,
        "current_pct": current_pct,
        "fresh_pct": fresh_pct,
        "older_pct": older_pct,
        "missing_pct": missing_pct,
        "current": current,
        "fresh": fresh,
        "older": older,
        "missing": missing,
        "denominator": denominator,
        "tolerance_days": tolerance_days,
        "by_end_date": sorted(dates.items(), key=lambda kv: kv[0], reverse=True)[:30],
    }


def assess(conn, business_date: str | None = None, types=None,
           formula_contract: str | None = None,
           min_nav_coverage: float = DEFAULT_MIN_NAV_COVERAGE,
           lookback_days: int = DEFAULT_LOOKBACK_DAYS,
           metric_tolerance_days: int = DEFAULT_METRIC_TOLERANCE_DAYS) -> dict[str, Any]:
    """组合净值覆盖 + 指标覆盖，给筛选/发布/健康页一个统一结论。"""
    nav = nav_coverage(conn, types=types, as_of=business_date,
                       min_coverage=min_nav_coverage, lookback_days=lookback_days)
    as_of = nav.get("effective_date") or business_date
    metric = metric_coverage(conn, as_of, formula_contract=formula_contract, types=types,
                             tolerance_days=metric_tolerance_days) if as_of else {
        "status": "missing", "as_of": None, "metric_as_of": None, "current_pct": None,
        "fresh_pct": None, "older_pct": None, "missing_pct": None, "denominator": 0,
        "tolerance_days": metric_tolerance_days}
    warnings: list[str] = []
    business_lag = None
    if business_date and nav.get("effective_date"):
        bd, ed = _d(business_date), _d(nav["effective_date"])
        if bd and ed:
            business_lag = (bd - ed).days
            nav["business_lag_days"] = business_lag
    if nav.get("effective_date") is None:
        warnings.append("nav_no_effective_date")
    if nav.get("max_date") and nav.get("effective_date") and nav["max_date"] > nav["effective_date"]:
        warnings.append("nav_partial_latest")
    if business_lag and business_lag > 0:
        warnings.append("nav_behind_business_date")
    if nav.get("effective_coverage_pct") is not None and nav["effective_coverage_pct"] < 100.0 * min_nav_coverage:
        warnings.append("nav_coverage_low")
    if metric.get("current_pct") is None:
        warnings.append("metric_unknown")
    elif metric["current_pct"] < 100.0 * DEFAULT_MIN_METRIC_COVERAGE:
        warnings.append("metric_not_current")
    if metric.get("missing_pct"):
        warnings.append("metric_missing")
    if metric.get("metric_max_date") and as_of and metric["metric_max_date"] > as_of:
        warnings.append("metric_lookahead")
    # blocked：没有有效净值日，或指标基本没跟上有效日。warning：指标部分跟上。
    if nav.get("effective_date") is None or metric.get("status") == "missing":
        status = "blocked"
    elif metric.get("current_pct") is None:
        status = "warning"
    elif metric["current_pct"] < 50.0:
        status = "blocked"
    elif metric["current_pct"] < 100.0 * DEFAULT_MIN_METRIC_COVERAGE:
        status = "warning"
    else:
        status = "ok"
    return {"status": status, "as_of": as_of, "business_date": business_date,
            "nav": nav, "metric": metric, "warnings": warnings,
            "publication_ok": status == "ok"}
