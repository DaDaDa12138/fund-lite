"""窗口选取：按业务日切片，另取边界锚点用于计算真实时间跨度。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Sequence

from . import contract


@dataclass
class NavRow:
    nav_date: str
    unit_nav: float | None
    acc_nav: float | None
    growth_pct: float | None


@dataclass
class Window:
    period_days: int
    rows: list[NavRow]
    anchor_date: str | None            # 窗口起点之前的最后一个观测（用于 D 与覆盖判断）
    end_date: str
    start_date: str
    missing_growth: int = 0
    proxy_rows: int = 0
    max_gap_days: int = 0
    median_gap_days: float | None = None
    p75_gap_days: float | None = None
    min_points_used: int | None = None
    anchor_row: "NavRow | None" = None
    reconstructed_rows: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.rows)

    @property
    def span_days(self) -> int:
        first = self.anchor_date or self.rows[0].nav_date
        return (date.fromisoformat(self.rows[-1].nav_date) - date.fromisoformat(first)).days


def min_points(period_days: int) -> int:
    floor = contract.MIN_POINTS_FLOOR_SHORT if period_days <= 7 else contract.MIN_POINTS_FLOOR_DEFAULT
    expected = round(period_days * contract.TRADING_DAYS_PER_YEAR / 365)
    return max(floor, -(-int(expected * contract.MIN_POINTS_RATIO * 100) // 100))


def max_gap(period_days: int) -> int:
    return min(45, max(period_days // 3, 7))


def build_window(series: Sequence[NavRow], period_days: int, as_of: str,
                 coverage_grace_days: int = 7) -> Window:
    """series 必须按 nav_date 升序，且只含 <= as_of 的观测。"""
    rows_in = [r for r in series if r.nav_date <= as_of]
    if not rows_in:
        return Window(period_days, [], None, as_of, "", reasons=["no_observations"])
    start = (date.fromisoformat(as_of) - timedelta(days=period_days)).isoformat()
    window_rows = [r for r in rows_in if r.nav_date > start]
    anchor = None
    anchor_row = None
    for r in rows_in:
        if r.nav_date <= start:
            anchor = r.nav_date
            anchor_row = r
        else:
            break
    w = Window(period_days, window_rows, anchor, as_of, start, anchor_row=anchor_row)
    if not window_rows:
        w.reasons.append("empty_window")
        return w

    # 覆盖率：仅当窗口起点之前没有任何观测（历史不足/长期停牌）时才判缺口；
    # 有边界锚点时由下面的 max_gap 检查负责，避免把交易所长假误判为数据缺口。
    first_date = date.fromisoformat(window_rows[0].nav_date)
    lag = (first_date - date.fromisoformat(start)).days
    if anchor is None and lag > max_gap(period_days):
        w.reasons.append(f"coverage_gap:{lag}d")

    # 最大间隔与采样节奏
    prev = date.fromisoformat(anchor) if anchor else date.fromisoformat(window_rows[0].nav_date)
    worst = 0
    gaps: list[int] = []
    for r in window_rows:
        cur = date.fromisoformat(r.nav_date)
        gaps.append((cur - prev).days)
        worst = max(worst, (cur - prev).days)
        prev = cur
    w.max_gap_days = worst
    if worst > max_gap(period_days):
        w.reasons.append(f"gap_exceeded:{worst}d>{max_gap(period_days)}d")

    if gaps:
        ordered = sorted(gaps)
        mid = len(ordered) // 2
        median = float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
        w.median_gap_days = median
        # p75（最近秩）：混合披露（开放期日更、封闭期周更）的中位数会落在 1 天，
        # 但 p75 仍能反映"每 4 次观测里至少有 1 次间隔 ≥ N 天"，比中位数稳健。
        idx = max(0, min(len(ordered) - 1, -(-len(ordered) * 3 // 4) - 1))
        w.p75_gap_days = float(ordered[idx])
    w.min_points_used = _effective_min_points(period_days, w.p75_gap_days)
    if w.n < w.min_points_used:
        w.reasons.append(f"insufficient_points:{w.n}<{w.min_points_used}")
    return w


def _effective_min_points(period_days: int, cadence_gap_days: float | None) -> int:
    """日频序列用 80% 交易日；周频/月频/混合披露按自身节奏折算（只放宽不收紧）。"""
    base = min_points(period_days)
    if not cadence_gap_days or cadence_gap_days <= contract.CADENCE_DAILY_MAX_GAP_DAYS:
        return base
    floor = contract.MIN_POINTS_FLOOR_SHORT if period_days <= 7 else contract.MIN_POINTS_FLOOR_DEFAULT
    expected = period_days / cadence_gap_days
    cadence_need = max(floor, -(-int(expected * contract.CADENCE_MIN_POINTS_RATIO * 100) // 100))
    return min(base, cadence_need)


def reconstruct_return(cur: NavRow, prev: NavRow | None) -> float | None:
    """用净值环比还原一步收益（用于伪零/缺失的 growth_pct）。

    分红日单位净值会因除息下跌，而累计净值不变；当累计净值环比明显高于单位净值环比时，
    说明这一步是分红除息而非真实亏损，取累计净值口径（单步近似等于真实收益）。
    """
    if prev is None or cur is None:
        return None
    if not cur.unit_nav or not prev.unit_nav:
        if cur.acc_nav and prev.acc_nav:
            return cur.acc_nav / prev.acc_nav - 1.0
        return None
    d_nav = cur.unit_nav - prev.unit_nav
    # 分红判定必须看"差额"而不是"比率"：acc = nav + 累计分红，所以正常日的 Δacc == Δnav，
    # 只有除息日 Δacc - Δnav == 每份分红 D。用比率比较会把所有下跌日都误判为分红（首版 bug）。
    if cur.acc_nav is not None and prev.acc_nav is not None:
        d_acc = cur.acc_nav - prev.acc_nav
        if (d_acc - d_nav) > 1e-4:
            return d_acc / prev.unit_nav      # 含分红的真实收益
    return d_nav / prev.unit_nav


def effective_returns(rows: Sequence[NavRow], anchor: NavRow | None = None) -> tuple[list[float], int]:
    """逐观测的有效收益序列（小数），返回 (returns, 还原行数)。

    growth_pct 缺失或为"伪零"（growth==0 但净值相对上一观测变动 >1e-6）时，用净值环比还原。
    """
    out: list[float] = []
    prev = anchor
    recon = 0
    for r in rows:
        g = r.growth_pct
        pseudo_zero = False
        if g is not None and abs(g) < 1e-12 and prev is not None and r.unit_nav and prev.unit_nav:
            pseudo_zero = abs(r.unit_nav / prev.unit_nav - 1.0) > 1e-6
        if g is None or pseudo_zero:
            rec = reconstruct_return(r, prev)
            if rec is not None:
                out.append(rec)
                recon += 1
                prev = r
                continue
        out.append((g or 0.0) / 100.0)
        prev = r
    return out, recon


def resolve_returns(window: Window, anchor: NavRow | None = None) -> tuple[list[float], str, int]:
    """按 daily_growth_pct > accum_nav > unit_nav 的优先级还原日收益序列。

    返回 (returns, basis, proxy_rows)。basis 记录实际使用的口径。
    伪零修正：定开债基等周频披露的基金，上游 equityReturn 在非连续披露日返回 0，
    直接链式会把真实波动抹平（ADR-051）。此处检测 growth==0 但净值变动 >1e-6 的行，
    改用净值环比还原，并把行数记在 window.reconstructed_rows。
    """
    if not window.rows:
        return [], "none", 0
    growth_ok = sum(1 for r in window.rows if r.growth_pct is not None)
    if growth_ok >= max(1, int(len(window.rows) * 0.9)):
        returns, recon = effective_returns(window.rows, anchor or window.anchor_row)
        window.reconstructed_rows = recon
        return returns, "daily_growth_pct", 0

    # accum_nav 代理：用累计净值差分（不含分红复利，属代理口径）
    acc_ok = sum(1 for r in window.rows if r.acc_nav)
    if acc_ok >= max(2, int(len(window.rows) * 0.9)):
        returns = _diff_returns(window, "acc_nav")
        if returns:
            return returns, "accum_proxy", len(returns)
    returns = _diff_returns(window, "unit_nav")
    if returns:
        return returns, "price_return_proxy", len(returns)
    return [], "none", 0


def _diff_returns(window: Window, field_name: str) -> list[float]:
    out: list[float] = []
    prev: float | None = None
    for r in window.rows:
        cur = getattr(r, field_name)
        if cur is None:
            out.append(0.0)
            continue
        if prev is None or prev == 0:
            out.append(0.0)
        else:
            out.append(cur / prev - 1.0)
        prev = cur
    return out if out else []