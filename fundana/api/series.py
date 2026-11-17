"""净值序列抽取（网页图表用）。

只做两件事：按自然日窗口截取，超过 max_points 时等距抽样（保留首尾）。
不做任何指标计算——指标只认 fund_metric，避免前端出现第二套口径。
"""
from __future__ import annotations

from datetime import date, timedelta

MAX_POINTS = 700


def nav_series(conn, fcode: str, days: int = 1095, max_points: int = MAX_POINTS) -> dict:
    end = conn.execute("SELECT MAX(nav_date) FROM fund_nav_daily WHERE fcode=?",
                       (fcode,)).fetchone()[0]
    if not end:
        return {"points": [], "n_total": 0, "n_points": 0, "start": None, "end": None,
                "downsampled": False}
    start = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT nav_date, unit_nav, acc_nav FROM fund_nav_daily"
        " WHERE fcode=? AND nav_date>? AND nav_date<=? AND unit_nav IS NOT NULL"
        " ORDER BY nav_date", (fcode, start, end)).fetchall()
    # 不依赖 row_factory：单测与外部调用方可能用默认元组游标
    pts = [{"nav_date": r[0], "unit_nav": r[1], "acc_nav": r[2]} for r in rows]
    n_total = len(pts)
    step = max(1, (n_total + max_points - 1) // max_points)
    sampled = pts[::step]
    if pts and sampled and sampled[-1]["nav_date"] != pts[-1]["nav_date"]:
        sampled.append(pts[-1])
    return {"points": sampled, "n_total": n_total, "n_points": len(sampled),
            "start": pts[0]["nav_date"] if pts else None, "end": end,
            "downsampled": step > 1}

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100


class RangeError(ValueError):
    """日期区间或分页参数不合法（调用方转 400）。"""


def _check_date(value: str | None, name: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise RangeError(f"{name} 需为 YYYY-MM-DD") from exc


def nav_rows(conn, fcode: str, start: str | None = None, end: str | None = None,
             page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """逐日净值明细分页查询（默认全历史，可按日期区间收窄）。

    返回按日期倒序，便于“最近在前”；同时给出整段可用区间，前端据此提示
    “这只基金只有 2002-09-19 起的数据”，而不是让用户盲猜。
    """
    start = _check_date(start, "start")
    end = _check_date(end, "end")
    if start and end and start > end:
        raise RangeError("start 不能晚于 end")
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))

    where = ["fcode=?", "unit_nav IS NOT NULL"]
    args: list = [fcode]
    if start:
        where.append("nav_date>=?")
        args.append(start)
    if end:
        where.append("nav_date<=?")
        args.append(end)
    clause = " AND ".join(where)
    total = conn.execute("SELECT COUNT(*) FROM fund_nav_daily WHERE " + clause,
                         args).fetchone()[0]
    bounds = conn.execute(
        "SELECT MIN(nav_date), MAX(nav_date), COUNT(*) FROM fund_nav_daily WHERE fcode=?",
        (fcode,)).fetchone()
    rows = conn.execute(
        "SELECT nav_date, unit_nav, acc_nav, growth_pct, nav_source FROM fund_nav_daily"
        " WHERE " + clause + " ORDER BY nav_date DESC LIMIT ? OFFSET ?",
        args + [page_size, (page - 1) * page_size]).fetchall()
    return {
        "rows": [{"nav_date": r[0], "unit_nav": r[1], "acc_nav": r[2],
                  "growth_pct": r[3], "nav_source": r[4]} for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "start": start,
        "end": end,
        "bounds": ({"first_date": bounds[0], "last_date": bounds[1], "n_obs": bounds[2]}
                   if bounds and bounds[2] else None),
    }
