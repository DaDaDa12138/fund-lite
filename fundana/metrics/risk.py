"""风险与收益指标（纯函数）。所有除零/样本不足一律返回 None 并记录原因，禁止 Infinity/NaN。"""
from __future__ import annotations

import math
from typing import Any, Sequence

from . import contract


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _stdev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mu = _mean(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))


def percentile(sorted_xs: Sequence[float], q: float) -> float:
    """线性插值分位（q∈[0,1]），输入需已升序。"""
    n = len(sorted_xs)
    if n == 0:
        raise ValueError("空序列")
    if n == 1:
        return sorted_xs[0]
    pos = (n - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_xs[lo]
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def wealth_series(returns: Sequence[float]) -> list[float]:
    wealth = [1.0]
    for r in returns:
        wealth.append(wealth[-1] * (1.0 + r))
    return wealth


def drawdown_stats(returns: Sequence[float]) -> tuple[float, int]:
    """返回 (最大回撤, 最长水下观测数)。peak 含初始财富 1.0。"""
    wealth = wealth_series(returns)
    peak = wealth[0]
    mdd = 0.0
    underwater = 0
    longest = 0
    for w in wealth:
        peak = max(peak, w)
        dd = 1.0 - (w / peak if peak else 1.0)
        if dd > 0:
            underwater += 1
            longest = max(longest, underwater)
        else:
            underwater = 0
        mdd = max(mdd, dd)
    return mdd, longest


def compute(returns: Sequence[float], span_days: int, *, risk_free_rate: float, mar: float,
            period_days: int, trading_days: int = contract.TRADING_DAYS_PER_YEAR,
            caps: dict[str, float] | None = None) -> dict[str, Any]:
    caps = caps or contract.CAPS
    reasons: list[str] = []
    n = len(returns)
    out: dict[str, Any] = {
        "n_obs": n, "years": None, "total_return_pct": None, "annual_dec": None,
        "vol_dec": None, "positive_ratio_pct": None, "flat_ratio_pct": None,
        "non_negative_ratio_pct": None, "mdd_dec": None, "dd_duration_obs": None,
        "sharpe": None, "sharpe_t": None, "sortino": None, "calmar_dec": None,
        "var95_dec": None, "cvar5_dec": None, "raw_sharpe": None, "raw_sortino": None,
        "raw_calmar": None, "sharpe_cap_hit": 0, "sortino_cap_hit": 0, "calmar_cap_hit": 0,
    }
    if n == 0 or span_days <= 0:
        out["undefined_reasons"] = ["empty_window" if n == 0 else "non_positive_span"]
        return out

    years = span_days / contract.ANNUALIZATION_BASIS_DAYS
    out["years"] = years
    wealth = wealth_series(returns)
    total = wealth[-1] - 1.0
    out["total_return_pct"] = total * 100.0

    if years <= 0:
        reasons.append("non_positive_years")
        annual = None
    else:
        annual = wealth[-1] ** (1.0 / years) - 1.0 if wealth[-1] > 0 else None
        if annual is None:
            reasons.append("non_positive_wealth")
    out["annual_dec"] = annual

    freq = max(1.0, min(float(trading_days), n / years)) if years > 0 else 1.0
    mean_r = _mean(returns)
    std_r = _stdev(returns)
    vol = std_r * math.sqrt(freq)
    out["vol_dec"] = vol
    if std_r == 0.0:
        reasons.append("zero_volatility")

    # 日胜率三分解：上涨 / 持平 / 下跌。债券基金（尤其低波动短债）大量交易日
    # 四舍五入后是 0.00%，把 0 当作"未上涨"会系统性惩罚低波动产品。
    # positive 只用于展示；门槛与打分使用 non_negative（上涨 + 持平）。
    positives = sum(1 for r in returns if r > 0)
    flats = sum(1 for r in returns if r == 0.0)
    out["positive_ratio_pct"] = 100.0 * positives / n
    out["flat_ratio_pct"] = 100.0 * flats / n
    out["non_negative_ratio_pct"] = 100.0 * (positives + flats) / n

    mdd, longest = drawdown_stats(returns)
    out["mdd_dec"] = mdd
    out["dd_duration_obs"] = longest
    if mdd == 0.0:
        reasons.append("zero_drawdown")

    if annual is not None and vol > 0:
        raw_sharpe = (annual - risk_free_rate) / vol
        out["raw_sharpe"] = raw_sharpe
        capped, hit = _cap(raw_sharpe, caps["sharpe"])
        out["sharpe"] = capped
        out["sharpe_cap_hit"] = hit
        if std_r > 0:
            out["sharpe_t"] = (mean_r / std_r) * math.sqrt(n)
    else:
        reasons.append("sharpe_undefined")

    mar_per = mar / freq
    downs = [min(r - mar_per, 0.0) for r in returns]
    downside_dev = math.sqrt(sum(d * d for d in downs) / n) * math.sqrt(freq)
    if downside_dev > 0 and annual is not None:
        raw_sortino = (annual - mar) / downside_dev
        out["raw_sortino"] = raw_sortino
        capped, hit = _cap(raw_sortino, caps["sortino"])
        out["sortino"] = capped
        out["sortino_cap_hit"] = hit
    else:
        reasons.append("zero_downside" if downside_dev == 0 else "sortino_undefined")

    if mdd > 0 and annual is not None:
        raw_calmar = annual / mdd
        out["raw_calmar"] = raw_calmar
        capped, hit = _cap(raw_calmar, caps["calmar"])
        out["calmar_dec"] = capped
        out["calmar_cap_hit"] = hit
    else:
        reasons.append("calmar_undefined")

    ordered = sorted(returns)
    var95 = percentile(ordered, 0.05)
    out["var95_dec"] = var95
    tail = [r for r in ordered if r <= var95]
    out["cvar5_dec"] = _mean(tail) if tail else None

    out["undefined_reasons"] = reasons
    return out


def _cap(value: float, cap: float) -> tuple[float, int]:
    if value > cap:
        return cap, 1
    if value < -cap:
        return -cap, 1
    return value, 0


def is_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)
