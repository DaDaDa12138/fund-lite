"""V2 尾部风险指标（架构书 §10.5）。

设计约束：
- 全部基于同一冻结有效总回报日序列（daily_growth_pct 链式），不另造口径。
- 历史不足一律 NULL，禁止用 1 年回撤代替 3 年回撤。
- 首期只计算与展示，不进入 L1–L10 硬门槛；是否升级为门槛须走 §15 影子验证。
"""
from __future__ import annotations

import bisect
import json
import math
from datetime import date, timedelta
from typing import Any, Sequence

from .. import db
from . import contract, risk, window as window_mod

TAIL_CONTRACT = "fundana-tail-2026.11.2"
TAIL_WINDOW_DAYS = 1095          # 近 3 年
ROLLING_TOLERANCE_DAYS = 7       # 自然日锚点允许的边界误差
CVAR_MIN_OBS = 100               # 架构书要求有效样本 >= 100
MIN_MONTH_OBS = 10               # 自然月至少 10 个有效观测才计入（排除首尾残缺月）
WEEK_OBS = 5                     # 周收益 = 5 个连续有效观测（proxy，非自然周）
TAIL_COMPONENTS = (
    "mdd_3y", "rolling_90d", "rolling_180d", "cvar5",
    "worst_day", "worst_week", "positive_month_ratio", "underwater",
)


def _compound(growths: Sequence[float]) -> float:
    w = 1.0
    for g in growths:
        w *= 1.0 + g
    return w


def _null(reasons: list[str]) -> dict[str, Any]:
    return {
        "start_date": None, "end_date": None, "n_obs": 0, "mdd_dec": None,
        "worst_rolling90d_pct": None, "worst_rolling90d_start": None,
        "worst_rolling90d_end": None, "worst_rolling180d_pct": None,
        "worst_rolling180d_start": None, "worst_rolling180d_end": None,
        "cvar5_dec": None, "worst_day_pct": None, "worst_day_date": None,
        "worst_week_pct": None, "worst_week_end_date": None,
        "positive_month_ratio_pct": None, "months_total": 0, "months_positive": 0,
        "max_underwater_obs": None, "ongoing_drawdown": 0,
        "completeness": 0.0, "undefined_reasons": reasons,
    }


def rolling_worst(rows: Sequence[window_mod.NavRow], eff: Sequence[float], window_days: int, *,
                  tail_start: str, tolerance_days: int = ROLLING_TOLERANCE_DAYS
                  ) -> dict[str, Any] | None:
    """最差滚动 window_days 收益：按自然日锚点，边界误差超过 tolerance 的窗口不参与。

    只统计结束日落在 (tail_start, 最后观测] 的已实现窗口；起点允许早于 tail_start
    最多 tolerance 天（作为边界锚点），不允许把缺口当连续收益滚过去。
    """
    if not rows:
        return None
    dates = [r.nav_date for r in rows]
    tail_dt = date.fromisoformat(tail_start)
    best: dict[str, Any] | None = None
    for i, end_row in enumerate(rows):
        end_dt = date.fromisoformat(end_row.nav_date)
        if end_dt <= tail_dt:
            continue
        target = (end_dt - timedelta(days=window_days)).isoformat()
        j = bisect.bisect_right(dates, target) - 1
        if j < 0 or j >= i:
            continue
        start_dt = date.fromisoformat(dates[j])
        if (date.fromisoformat(target) - start_dt).days > tolerance_days:
            continue
        if start_dt < tail_dt - timedelta(days=tolerance_days):
            continue
        pct = (_compound(eff[j + 1:i + 1]) - 1.0) * 100.0
        if best is None or pct < best["pct"]:
            best = {"pct": pct, "start": dates[j], "end": end_row.nav_date}
    return best


def monthly_positive(rows: Sequence[window_mod.NavRow], eff: Sequence[float], *, tail_start: str,
                     min_month_obs: int = MIN_MONTH_OBS) -> tuple[float | None, int, int]:
    """自然月正收益比例（%）。只计入观测数 >= min_month_obs 的月份。"""
    buckets: dict[str, list[float]] = {}
    for idx, r in enumerate(rows):
        if r.nav_date <= tail_start:
            continue
        buckets.setdefault(r.nav_date[:7], []).append(eff[idx])
    total = 0
    positive = 0
    for growths in buckets.values():
        if len(growths) < min_month_obs:
            continue
        total += 1
        if _compound(growths) > 1.0:
            positive += 1
    if total == 0:
        return None, 0, 0
    return 100.0 * positive / total, total, positive


def underwater_stats(returns: Sequence[float]) -> tuple[int, bool, int]:
    """返回 (最长水下观测数, 是否仍未恢复, 当前水下长度)。"""
    if not returns:
        return 0, False, 0
    wealth = risk.wealth_series(returns)
    peak = wealth[0]
    longest = 0
    run = 0
    for w in wealth:
        if w >= peak:
            peak = w
            run = 0
        else:
            run += 1
            longest = max(longest, run)
    ongoing = wealth[-1] < peak - 1e-12
    return longest, ongoing, (run if ongoing else 0)


def _cvar(returns: Sequence[float], min_obs: int = CVAR_MIN_OBS) -> float | None:
    if len(returns) < min_obs:
        return None
    ordered = sorted(returns)
    k = max(1, math.ceil(0.05 * len(ordered)))
    return sum(ordered[:k]) / k


def compute(series: Sequence[window_mod.NavRow], as_of: str, *,
            window_days: int = TAIL_WINDOW_DAYS,
            coverage_grace_days: int = 7) -> dict[str, Any]:
    win = window_mod.build_window(series, window_days, as_of, coverage_grace_days)
    fatal = [r for r in win.reasons
             if r.startswith(("empty_window", "insufficient_points", "coverage_gap", "gap_exceeded"))]
    if not win.rows or fatal:
        return _null(win.reasons or ["no_observations"])
    returns, basis, proxy_rows = window_mod.resolve_returns(win, win.anchor_row)
    if not returns:
        return _null(["no_returns"])
    # 全序列的有效收益（含伪零还原，ADR-051）；滚动窗口与自然月都用它，避免另造口径。
    eff, recon = window_mod.effective_returns(series)
    reasons = list(win.reasons)
    if basis not in ("daily_growth_pct", "none"):
        reasons.append(f"return_basis:{basis}")
    if proxy_rows:
        reasons.append(f"proxy_rows:{proxy_rows}")
    if recon:
        reasons.append(f"growth_reconstructed:{recon}")

    mdd, _ = risk.drawdown_stats(returns)
    tail_start = win.start_date
    r90 = rolling_worst(series, eff, 90, tail_start=tail_start)
    r180 = rolling_worst(series, eff, 180, tail_start=tail_start)
    cvar = _cvar(returns)
    if cvar is None:
        reasons.append(f"cvar_insufficient_observations:{len(returns)}<{CVAR_MIN_OBS}")

    base_idx = len(series) - len(win.rows)
    win_eff = eff[base_idx:] if base_idx >= 0 else eff
    worst_day_idx = min(range(len(win_eff)), key=lambda k: win_eff[k]) if win_eff else None
    worst_day = win.rows[worst_day_idx] if worst_day_idx is not None else None

    worst_week_pct = None
    worst_week_end = None
    for i in range(len(win_eff) - WEEK_OBS, -1, -1):
        chunk = win_eff[i:i + WEEK_OBS]
        pct = (_compound(chunk) - 1.0) * 100.0
        if worst_week_pct is None or pct < worst_week_pct:
            worst_week_pct = pct
            worst_week_end = win.rows[i + WEEK_OBS - 1].nav_date

    month_ratio, months_total, months_positive = monthly_positive(win.rows, eff[base_idx:] if base_idx >= 0 else eff, tail_start=tail_start)
    longest, ongoing, _ = underwater_stats(returns)

    values = {
        "mdd_3y": mdd,
        "rolling_90d": r90["pct"] if r90 else None,
        "rolling_180d": r180["pct"] if r180 else None,
        "cvar5": cvar,
        "worst_day": (win_eff[worst_day_idx] * 100.0) if worst_day_idx is not None else None,
        "worst_week": worst_week_pct,
        "positive_month_ratio": month_ratio,
        "underwater": longest if returns else None,
    }
    available = sum(1 for v in values.values() if v is not None)
    completeness = available / len(TAIL_COMPONENTS)
    for key, val in values.items():
        if val is None and key not in ("mdd_3y", "underwater"):
            reasons.append(f"{key}_unavailable")

    return {
        "start_date": win.start_date,
        "end_date": win.rows[-1].nav_date,
        "n_obs": win.n,
        "mdd_dec": mdd,
        "worst_rolling90d_pct": r90["pct"] if r90 else None,
        "worst_rolling90d_start": r90["start"] if r90 else None,
        "worst_rolling90d_end": r90["end"] if r90 else None,
        "worst_rolling180d_pct": r180["pct"] if r180 else None,
        "worst_rolling180d_start": r180["start"] if r180 else None,
        "worst_rolling180d_end": r180["end"] if r180 else None,
        "cvar5_dec": cvar,
        "worst_day_pct": worst_day.growth_pct if worst_day else None,
        "worst_day_date": worst_day.nav_date if worst_day else None,
        "worst_week_pct": worst_week_pct,
        "worst_week_end_date": worst_week_end,
        "positive_month_ratio_pct": month_ratio,
        "months_total": months_total,
        "months_positive": months_positive,
        "max_underwater_obs": longest if returns else None,
        "ongoing_drawdown": 1 if ongoing else 0,
        "completeness": round(completeness, 4),
        "undefined_reasons": reasons,
    }


def compute_fund(conn, cfg, fcode: str, as_of: str, *, dry_run: bool = False) -> dict[str, Any]:
    from . import engine

    series = engine.load_series(conn, fcode, as_of)
    if not series:
        return {"fcode": fcode, "ok": False, "reason": "no_nav"}
    grace = int(cfg.get("risk", "coverage_grace_days", default=7))
    res = compute(series, as_of, coverage_grace_days=grace)
    if dry_run:
        return {"fcode": fcode, "ok": True, "tail": res, "nav_sig": engine.nav_signature(series)}
    sig = engine.nav_signature(series)
    conn.execute(
        "INSERT INTO fund_tail_metric(fcode, window_days, formula_contract, nav_sig, start_date,"
        " end_date, n_obs, mdd_dec, worst_rolling90d_pct, worst_rolling90d_start,"
        " worst_rolling90d_end, worst_rolling180d_pct, worst_rolling180d_start,"
        " worst_rolling180d_end, cvar5_dec, worst_day_pct, worst_day_date, worst_week_pct,"
        " worst_week_end_date, positive_month_ratio_pct, months_total, months_positive,"
        " max_underwater_obs, ongoing_drawdown, completeness, undefined_reasons_json, computed_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode, window_days, formula_contract) DO UPDATE SET nav_sig=excluded.nav_sig,"
        " start_date=excluded.start_date, end_date=excluded.end_date, n_obs=excluded.n_obs,"
        " mdd_dec=excluded.mdd_dec, worst_rolling90d_pct=excluded.worst_rolling90d_pct,"
        " worst_rolling90d_start=excluded.worst_rolling90d_start,"
        " worst_rolling90d_end=excluded.worst_rolling90d_end,"
        " worst_rolling180d_pct=excluded.worst_rolling180d_pct,"
        " worst_rolling180d_start=excluded.worst_rolling180d_start,"
        " worst_rolling180d_end=excluded.worst_rolling180d_end, cvar5_dec=excluded.cvar5_dec,"
        " worst_day_pct=excluded.worst_day_pct, worst_day_date=excluded.worst_day_date,"
        " worst_week_pct=excluded.worst_week_pct, worst_week_end_date=excluded.worst_week_end_date,"
        " positive_month_ratio_pct=excluded.positive_month_ratio_pct,"
        " months_total=excluded.months_total, months_positive=excluded.months_positive,"
        " max_underwater_obs=excluded.max_underwater_obs,"
        " ongoing_drawdown=excluded.ongoing_drawdown, completeness=excluded.completeness,"
        " undefined_reasons_json=excluded.undefined_reasons_json, computed_at=excluded.computed_at",
        (fcode, TAIL_WINDOW_DAYS, TAIL_CONTRACT, sig, res["start_date"], res["end_date"],
         res["n_obs"], res["mdd_dec"], res["worst_rolling90d_pct"], res["worst_rolling90d_start"],
         res["worst_rolling90d_end"], res["worst_rolling180d_pct"], res["worst_rolling180d_start"],
         res["worst_rolling180d_end"], res["cvar5_dec"], res["worst_day_pct"], res["worst_day_date"],
         res["worst_week_pct"], res["worst_week_end_date"], res["positive_month_ratio_pct"],
         res["months_total"], res["months_positive"], res["max_underwater_obs"],
         res["ongoing_drawdown"], res["completeness"],
         json.dumps(res["undefined_reasons"], ensure_ascii=False), db.now_iso()))
    return {"fcode": fcode, "ok": True, "completeness": res["completeness"],
            "undefined": res["undefined_reasons"]}