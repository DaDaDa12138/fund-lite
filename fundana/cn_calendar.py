"""中国交易日历（仅用于调度，不参与净值/指标口径）。

规则：周一至周五且不在法定节假日休市日。调休上班的周末不开市——基金公司
不会因为调休而在周六披露净值。节假日表来自中国政府网 2025-11-04 发布的
《国务院办公厅关于2026年部分节假日安排的通知》。

每年国务院发布下一年安排后必须补表；未登记年份按“工作日即交易日”回退，
并在 next_run_at 的行为上表现为可能多在节假日空跑一次（不会漏跑）。
"""
from __future__ import annotations

from datetime import date

# 休市日（不含周末）。只列法定节假日放假日期，不列调休上班日。
HOLIDAYS: dict[int, frozenset[str]] = {
    2025: frozenset({
        "2025-01-01",
        "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
        "2025-02-03", "2025-02-04",
        "2025-04-04",
        "2025-05-01", "2025-05-02", "2025-05-05",
        "2025-06-02",
        "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06",
        "2025-10-07", "2025-10-08",
    }),
    2026: frozenset({
        "2026-01-01", "2026-01-02",
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
        "2026-02-23",
        "2026-04-06",
        "2026-05-01", "2026-05-04", "2026-05-05",
        "2026-06-19",
        "2026-09-25",
        "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    }),
}


def is_holiday(day: date) -> bool:
    return day.isoformat() in HOLIDAYS.get(day.year, frozenset())


def is_trading_day(day: date) -> bool:
    """周一至周五且非休市日。周末调休上班仍然返回 False。"""
    return day.weekday() < 5 and not is_holiday(day)


def next_trading_day(day: date) -> date:
    from datetime import timedelta
    cur = day
    for _ in range(30):
        if is_trading_day(cur):
            return cur
        cur += timedelta(days=1)
    return cur
