"""按周 / 月 / 年聚合净值涨跌。

口径：区间内 `growth_pct` 链式复利（复投总回报），同时给出单位净值首末比作对照；
区间内最大回撤用同一条链式财富序列计算。首个周期与最后一个周期标记为“未完整”
（分别因为基金尚未成立 / 周期尚未结束），前端据此提示而不是把半截数据当成整段。
"""
from __future__ import annotations

import calendar
import datetime as dt

GRANULARITIES = ("week", "month", "year")
GRANULARITY_LABELS = {"week": "周", "month": "月", "year": "年"}
MAX_PERIODS = 600
MAX_PAGE_SIZE = 500


class PeriodError(ValueError):
    """参数不合法（调用方转 400）。"""


def _bounds(day: dt.date, granularity: str) -> tuple[str, str, dt.date, dt.date]:
    if granularity == "week":
        iso = day.isocalendar()
        start = dt.date.fromisocalendar(iso[0], iso[1], 1)
        return (f"{iso[0]}-W{iso[1]:02d}", f"{iso[0]} 年第 {iso[1]} 周",
                start, start + dt.timedelta(days=6))
    if granularity == "month":
        last = calendar.monthrange(day.year, day.month)[1]
        return (f"{day.year}-{day.month:02d}", f"{day.year} 年 {day.month} 月",
                dt.date(day.year, day.month, 1), dt.date(day.year, day.month, last))
    if granularity == "year":
        return (str(day.year), f"{day.year} 年", dt.date(day.year, 1, 1), dt.date(day.year, 12, 31))
    raise PeriodError("granularity 只能是 week/month/year")


def _max_drawdown(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    peak = values[0]
    mdd = 0.0
    for v in values[1:]:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, 1 - v / peak)
    return mdd


def _aggregate(obs: list[dict], granularity: str, *, partial_first: bool,
               partial_last: bool) -> dict:
    key, label, start, end = _bounds(dt.date.fromisoformat(obs[0]["nav_date"]), granularity)
    first_nav = obs[0]["unit_nav"]
    last_nav = obs[-1]["unit_nav"]
    wealth = 1.0
    series = [1.0]
    used = 0
    for r in obs:
        g = r.get("growth_pct")
        if g is None:
            continue
        wealth *= 1 + float(g) / 100.0
        series.append(wealth)
        used += 1
    if used:
        chain_pct = (wealth - 1) * 100
    else:
        chain_pct = (last_nav / first_nav - 1) * 100 if first_nav else None
        series = [float(r["unit_nav"]) for r in obs]
    mdd = _max_drawdown(series)
    nav_pct = (last_nav / first_nav - 1) * 100 if first_nav else None
    return {
        "period": key,
        "period_label": label,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "first_date": obs[0]["nav_date"],
        "last_date": obs[-1]["nav_date"],
        "first_nav": round(first_nav, 4),
        "last_nav": round(last_nav, 4),
        "chain_return_pct": None if chain_pct is None else round(chain_pct, 4),
        "nav_return_pct": None if nav_pct is None else round(nav_pct, 4),
        "mdd_pct": None if mdd is None else round(mdd * 100, 4),
        "n_obs": len(obs),
        "partial": bool(partial_first or partial_last),
        "partial_reason": ("数据起点" if partial_first else "") +
                          ("/周期未结束" if partial_last else ""),
    }


def period_rows(conn, fcode: str, granularity: str = "month", limit: int = 120) -> dict:
    if granularity not in GRANULARITIES:
        raise PeriodError("granularity 只能是 week/month/year")
    limit = max(1, min(int(limit), MAX_PERIODS))
    rows = conn.execute(
        "SELECT nav_date, unit_nav, growth_pct FROM fund_nav_daily"
        " WHERE fcode=? AND unit_nav IS NOT NULL ORDER BY nav_date", (fcode,)).fetchall()
    if not rows:
        return {"granularity": granularity, "granularity_label": GRANULARITY_LABELS[granularity],
                "rows": [], "total_periods": 0, "bounds": None}

    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    # 不依赖 row_factory：单测与外部调用方可能用默认元组游标（同 series.nav_series）
    for r in rows:
        rec = {"nav_date": r[0], "unit_nav": float(r[1]), "growth_pct": r[2]}
        key, _, _, _ = _bounds(dt.date.fromisoformat(rec["nav_date"]), granularity)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(rec)

    first_key, last_key = order[0], order[-1]
    out = []
    for key in order:
        out.append(_aggregate(buckets[key], granularity,
                              partial_first=(key == first_key),
                              partial_last=(key == last_key)))
    out.reverse()
    return {
        "granularity": granularity,
        "granularity_label": GRANULARITY_LABELS[granularity],
        "rows": out[:limit],
        "total_periods": len(out),
        "bounds": {"first_date": rows[0][0], "last_date": rows[-1][0],
                   "n_obs": len(rows)},
    }
