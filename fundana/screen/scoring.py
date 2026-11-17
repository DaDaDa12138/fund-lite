"""评分原语：同组百分位、加权合成、缺失处理。

默认 weighted 仍把缺失项按 0 计入（兼容旧行为）；long_core / long_formal
等“证据不足不能装成 0 分”的场景显式传 renormalize=True，并由 coverage()
把可用权重比例写进 scores_json，避免缺 3y/5y 的基金被静默判死。
"""
from __future__ import annotations

from typing import Any, Iterable


def percentile_ranks(values: dict[str, float | None], higher_is_better: bool = True) -> dict[str, float]:
    """rank_pct = 100*(avg_rank-0.5)/n，并列取平均名次；缺失值不出现在结果里（调用方按 0 处理）。"""
    items = [(k, v) for k, v in values.items() if v is not None]
    if not items:
        return {}
    items.sort(key=lambda kv: kv[1])
    n = len(items)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        pct = 100.0 * (avg_rank - 0.5) / n
        if not higher_is_better:
            pct = 100.0 - pct
        for k in range(i, j + 1):
            out[items[k][0]] = pct
        i = j + 1
    return out


def weighted(pcts: dict[str, float], weights: dict[str, float],
             renormalize: bool = False) -> float:
    """按权重合成百分位。

    renormalize=False（默认）：缺失项按 0 计入，旧契约行为，用于仍然要求
    “证据齐全才给高分”的 short_core / composite。
    renormalize=True：只在可用维度上重新归一化；调用方必须同时用 coverage()
    检查可用权重，低于最低覆盖时不得进入长期榜。
    """
    if not renormalize:
        total = 0.0
        for key, weight in weights.items():
            total += weight * (pcts.get(key) or 0.0)
        return round(total, 4)
    available = [(key, weight) for key, weight in weights.items()
                 if pcts.get(key) is not None]
    denom = sum(weight for _, weight in available)
    if denom <= 0:
        return 0.0
    total = sum(weight * (pcts[key] or 0.0) for key, weight in available)
    return round(total / denom, 4)


def coverage(pcts: dict[str, float], weights: dict[str, float]) -> float:
    """可用权重占比（0–1）。用于判断长期榜是否有足够 3y/5y 证据。"""
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    have = sum(weight for key, weight in weights.items() if pcts.get(key) is not None)
    return round(have / total, 4)


def quantile(sorted_values: list[float], q: float) -> float | None:
    """线性插值分位数（与 metrics.risk.percentile 同法）。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def winsorize(values: Iterable[float], low: float = 0.05, high: float = 0.95) -> list[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return []
    lo, hi = quantile(vals, low), quantile(vals, high)
    return [min(max(v, lo), hi) for v in vals]


def grade(formal_score: float, bands: list[tuple[str, float]]) -> str:
    for label, floor in bands:
        if formal_score >= floor:
            return label
    return "D"


def to_dict(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}
