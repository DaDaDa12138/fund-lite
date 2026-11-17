"""日收益序列加载（P2）：供组合诊断与 core10 分散选样共用。

口径与 audit.portfolio 原实现一致：
* 读 (start, end] 的净值行，锚点取 start 之前最后一个观测；
* 用 window.effective_returns 还原分红/拆分后的日收益；
* 样本 < min_obs、首笔离 start 超过 start_tolerance、末笔离 end 超过
  end_tolerance 时返回 None，不插补、不把缺失日当 0。
"""
from __future__ import annotations

from datetime import date

from .metrics import window as window_mod

MIN_OBS = 40
START_TOLERANCE_DAYS = 45
END_TOLERANCE_DAYS = 20


def load_daily_returns(conn, fcode: str, start: str, end: str,
                       min_obs: int = MIN_OBS,
                       start_tolerance: int = START_TOLERANCE_DAYS,
                       end_tolerance: int = END_TOLERANCE_DAYS):
    """返回 [(nav_date, daily_return)]；数据不足/覆盖不合格返回 None。"""
    anchor = conn.execute(
        "SELECT nav_date, unit_nav, acc_nav, growth_pct FROM fund_nav_daily"
        " WHERE fcode=? AND nav_date<=? ORDER BY nav_date DESC LIMIT 1", (fcode, start)).fetchone()
    rows = conn.execute(
        "SELECT nav_date, unit_nav, acc_nav, growth_pct FROM fund_nav_daily"
        " WHERE fcode=? AND nav_date>? AND nav_date<=? ORDER BY nav_date",
        (fcode, start, end)).fetchall()
    if len(rows) < min_obs:
        return None
    first = date.fromisoformat(rows[0]["nav_date"])
    last = date.fromisoformat(rows[-1]["nav_date"])
    if (first - date.fromisoformat(start)).days > start_tolerance:
        return None
    if (date.fromisoformat(end) - last).days > end_tolerance:
        return None
    nav_rows = [window_mod.NavRow(r["nav_date"], r["unit_nav"], r["acc_nav"], r["growth_pct"])
                for r in rows]
    anchor_row = (window_mod.NavRow(anchor["nav_date"], anchor["unit_nav"], anchor["acc_nav"],
                                    anchor["growth_pct"]) if anchor else None)
    rets, _ = window_mod.effective_returns(nav_rows, anchor_row)
    return [(r.nav_date, float(ret)) for r, ret in zip(nav_rows, rets)]
