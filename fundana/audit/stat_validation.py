"""统计验证：块自举与 Deflated Sharpe（P1-3，research_only）。

只读工具。回答两个问题：
1. 榜单相对基准的超额，在日收益自相关下是否只是噪声？用 circular block bootstrap
   给出年化超额均值的 95% 区间与 P(均值<=0)。
2. 在尝试过 N 个筛选配置后，观察到的 Sharpe 是否仍然显著？用 Bailey & López de Prado
   的 Deflated Sharpe Ratio（DSR）做多重试验惩罚。

局限：
- 自举只重采样历史日收益，不生成新的市场情景；区间是"给定这段历史"的条件区间。
- DSR 假设收益近似正态、试验次数 N 是有效独立试验数；N 只能近似（配置/榜单变体数）。
- 1 年窗口的结论不能外推到完整信用周期；报告必须同时给出窗口与样本量。
"""
from __future__ import annotations

import math
import random
from statistics import NormalDist
from typing import Any, Sequence

TRADING_DAYS = 252.0
_N = NormalDist()


def block_bootstrap_mean(values: Sequence[float], block_size: int = 20,
                         n_boot: int = 5000, seed: int = 42) -> dict[str, Any]:
    """Circular block bootstrap 的均值分布；返回年化前的原始均值区间。"""
    n = len(values)
    if n < 2:
        return {"n": n, "mean": (values[0] if values else None), "ci95": [None, None],
                "p_mean_le_zero": None, "n_boot": 0, "block_size": block_size,
                "note": "样本不足"}
    block_size = max(1, min(int(block_size), n))
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_boot):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            for j in range(block_size):
                sample.append(values[(start + j) % n])
        sample = sample[:n]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * (n_boot - 1))]
    hi = means[int(0.975 * (n_boot - 1))]
    mean = sum(values) / n
    p_le = sum(1 for m in means if m <= 0.0) / n_boot
    return {
        "n": n,
        "mean": mean,
        "ci95": [lo, hi],
        "p_mean_le_zero": p_le,
        "n_boot": n_boot,
        "block_size": block_size,
        "note": "circular block bootstrap；区间为给定历史的条件下区间",
    }


def deflated_sharpe(observed_sr: float, n_trials: int, n_obs: int,
                    skew: float = 0.0, kurt: float = 3.0) -> dict[str, Any]:
    """Bailey & López de Prado (2014) Deflated Sharpe Ratio。

    observed_sr / n_obs 用同一频率（这里是非年化日 Sharpe 与日样本数）。
    n_trials 为有效独立试验数；越大，SR0 越高，DSR 越低。
    """
    if n_obs < 2 or n_trials < 1:
        return {"dsr": None, "sr0": None, "note": "样本或试验数不足"}
    var = (1.0 - skew * observed_sr + (kurt - 1.0) / 4.0 * observed_sr ** 2) / (n_obs - 1.0)
    if var <= 0:
        return {"dsr": None, "sr0": None, "note": "方差非正"}
    se = math.sqrt(var)
    e = math.e
    gamma = 0.5772156649015329  # Euler-Mascheroni
    if n_trials == 1:
        expected_max = 0.0
    else:
        expected_max = ((1.0 - gamma) * _N.inv_cdf(1.0 - 1.0 / n_trials)
                        + gamma * _N.inv_cdf(1.0 - 1.0 / (n_trials * e)))
    sr0 = expected_max * se
    dsr = _N.cdf((observed_sr - sr0) / se)
    return {
        "observed_sr": observed_sr,
        "n_trials": n_trials,
        "n_obs": n_obs,
        "skew": skew,
        "kurt": kurt,
        "sr0": sr0,
        "expected_max_sharpe": expected_max,
        "dsr": dsr,
        "note": "DSR>0.95 才可在 5% 水平拒绝'多重试验后仍无技能'",
    }


def moments(values: Sequence[float]) -> dict[str, float]:
    """均值、标准差、偏度、峰度（总体矩，峰度非超额）。"""
    n = len(values)
    if n < 2:
        return {"mean": (values[0] if values else 0.0), "std": 0.0, "skew": 0.0, "kurt": 3.0}
    mean = sum(values) / n
    m2 = sum((x - mean) ** 2 for x in values) / n
    if m2 <= 0:
        return {"mean": mean, "std": 0.0, "skew": 0.0, "kurt": 3.0}
    m3 = sum((x - mean) ** 3 for x in values) / n
    m4 = sum((x - mean) ** 4 for x in values) / n
    return {"mean": mean, "std": math.sqrt(m2), "skew": m3 / m2 ** 1.5,
            "kurt": m4 / m2 ** 2}


def daily_returns(curve: dict[str, Any]) -> list[tuple[str, float]]:
    """从 _portfolio 的曲线生成 (date, return)，跳过首日。"""
    dates = curve.get("dates") or []
    vals = curve.get("values") or []
    out = []
    for i in range(1, min(len(dates), len(vals))):
        prev, cur = vals[i - 1], vals[i]
        if prev and prev > 0:
            out.append((dates[i], cur / prev - 1.0))
    return out


def aligned_excess(a: Sequence[tuple[str, float]], b: Sequence[tuple[str, float]]) -> list[float]:
    """按共同日期对齐，返回 a-b 的日超额。"""
    db = dict(b)
    return [x - db[d] for d, x in a if d in db]


def summarize_excess(excess: Sequence[float], block_size: int = 20,
                     n_boot: int = 5000, seed: int = 42) -> dict[str, Any]:
    boot = block_bootstrap_mean(excess, block_size, n_boot, seed)
    mean = boot["mean"]
    return {
        "n_days": len(excess),
        "annual_excess_pct": None if mean is None else round(100 * mean * TRADING_DAYS, 4),
        "ci95_annual_pct": [None if x is None else round(100 * x * TRADING_DAYS, 4)
                            for x in boot["ci95"]],
        "p_mean_le_zero": boot["p_mean_le_zero"],
        "n_boot": boot["n_boot"],
        "block_size": boot["block_size"],
        "note": boot["note"],
    }
