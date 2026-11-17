"""P2-3：把 as-of 指标写成季度快照，供 12 队列 PIT 复现。

为什么需要单独的表：
* `fund_metric` 只保留当前一期；回填历史会覆盖生产指标，不能用来做 PIT。
* walkforward 现在每次用 `compute_fund(dry_run=True)` 重算，可复现但没有落盘，
  12 队列无法直接审计“当时看到的 3y/5y 指标”。
* 本模块只写 `fund_metric_snapshot`，不碰 `fund_metric`。
"""
from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Iterable

from .. import db
from ..metrics import engine as metrics_engine

DEFAULT_PERIODS = (365, 1095, 1825)


def _quarter_end(d: date) -> date:
    month = ((d.month - 1) // 3 + 1) * 3
    return date(d.year, month, calendar.monthrange(d.year, month)[1])


def quarter_ends(end: str, n: int = 12) -> list[str]:
    """返回截至 end（含）的最近 n 个季末，按时间升序。"""
    d = _quarter_end(date.fromisoformat(end[:10]))
    out = []
    for _ in range(max(1, n)):
        out.append(d.isoformat())
        # 上一个季末：当前季末减 3 个月，再取月末
        month = d.month - 3
        year = d.year
        if month <= 0:
            month += 12
            year -= 1
        d = date(year, month, calendar.monthrange(year, month)[1])
    return sorted(out)


def snapshot_as_of(conn, cfg, as_of: str, *, periods: Iterable[int] = DEFAULT_PERIODS,
                   fcodes: list[str] | None = None, limit: int | None = None) -> dict[str, Any]:
    as_of = as_of[:10]
    formula = cfg.get("risk", "formula_contract", default="")
    if fcodes is None:
        fcodes = [r["fcode"] for r in conn.execute(
            "SELECT DISTINCT f.fcode FROM fund f JOIN fund_nav_daily n ON n.fcode=f.fcode"
            " WHERE f.estab_date IS NOT NULL AND f.estab_date<=? ORDER BY f.fcode", (as_of,))]
    if limit:
        fcodes = fcodes[:limit]
    periods = tuple(int(p) for p in periods)
    rows = []
    ok = fail = 0
    for code in fcodes:
        try:
            res = metrics_engine.compute_fund(conn, cfg, code, as_of, dry_run=True)
        except Exception:                              # noqa: BLE001 - 单只失败不中断
            fail += 1
            continue
        if not res.get("ok"):
            fail += 1
            continue
        ok += 1
        for p in periods:
            m = (res.get("dry") or {}).get(p) or {}
            if not m:
                continue
            n_obs = m.get("n_obs")
            rows.append((code, as_of, p, formula, m.get("start_date"), m.get("end_date"),
                         n_obs, m.get("years"), m.get("annual_dec"), m.get("vol_dec"),
                         m.get("mdd_dec"), m.get("sharpe"), m.get("sortino"),
                         m.get("calmar_dec"), m.get("positive_ratio_pct"),
                         m.get("non_negative_ratio_pct"), m.get("flat_ratio_pct"),
                         (round(float(n_obs) / 252.0, 4) if n_obs else 0.0),
                         res.get("nav_sig"), db.now_iso()))
    if rows:
        conn.executemany(
            "INSERT INTO fund_metric_snapshot(fcode,as_of,period_days,formula_contract,"
            " start_date,end_date,n_obs,years,annual_dec,vol_dec,mdd_dec,sharpe,sortino,"
            " calmar_dec,positive_ratio_pct,non_negative_ratio_pct,flat_ratio_pct,"
            " coverage_ratio,nav_sig,computed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(fcode,as_of,period_days,formula_contract) DO UPDATE SET"
            " start_date=excluded.start_date, end_date=excluded.end_date, n_obs=excluded.n_obs,"
            " years=excluded.years, annual_dec=excluded.annual_dec, vol_dec=excluded.vol_dec,"
            " mdd_dec=excluded.mdd_dec, sharpe=excluded.sharpe, sortino=excluded.sortino,"
            " calmar_dec=excluded.calmar_dec, positive_ratio_pct=excluded.positive_ratio_pct,"
            " non_negative_ratio_pct=excluded.non_negative_ratio_pct,"
            " flat_ratio_pct=excluded.flat_ratio_pct, coverage_ratio=excluded.coverage_ratio,"
            " nav_sig=excluded.nav_sig, computed_at=excluded.computed_at", rows)
        conn.commit()
    return {"as_of": as_of, "funds": len(fcodes), "ok": ok, "failed": fail,
            "rows": len(rows), "formula_contract": formula}


def snapshot_quarters(conn, cfg, end: str, n: int = 12, **kwargs) -> list[dict[str, Any]]:
    return [snapshot_as_of(conn, cfg, q, **kwargs) for q in quarter_ends(end, n)]
