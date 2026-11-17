"""组合相关性诊断：榜单是不是"十只基金、一个风险"（P1-1，research_only）。

只读模块。输入是各基金的日收益序列与静态元数据（公司/家族/风格），输出：
- 平均两两相关、最大相关对、有效独立 bet；
- 公司/家族/风格集中度；
- 可解释的旗标（高相关、近似重复、单公司、同家族、单风格）。

口径与局限：
- 等权组合假设；相关性用共同披露日的收益计算，共同样本 < MIN_PAIR_OBS 的对不参与；
- 只衡量净值同涨同跌，不区分"同策略"与"同风险源"；债券基金的相关性会被共同利率因子抬高；
- 有效 bet = n / (1 + (n-1) * rho_bar)，rho_bar 为可计算对的平均相关，缺失对不插补；
- 这是诊断，不进入筛选门禁，不改变正式榜单。
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Any, Mapping, Sequence

MIN_PAIR_OBS = 40


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """皮尔逊相关；样本不足或任一侧无波动时返回 None。"""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 1e-18 or vy <= 1e-18:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(vx * vy)


def pair_corr(a: Sequence[tuple[str, float]], b: Sequence[tuple[str, float]],
              min_obs: int = MIN_PAIR_OBS) -> tuple[float | None, int]:
    """两只基金在共同日期上的收益相关；返回 (corr, 共同样本数)。"""
    da = dict(a)
    db = dict(b)
    common = sorted(set(da) & set(db))
    if len(common) < min_obs:
        return None, len(common)
    xs = [da[d] for d in common]
    ys = [db[d] for d in common]
    return _pearson(xs, ys), len(common)


def effective_bets(n: int, rho_bar: float | None) -> float | None:
    """有效独立 bet：n 个等权、两两平均相关 rho_bar 的组合。"""
    if n <= 0:
        return None
    if n == 1:
        return 1.0
    if rho_bar is None:
        return None
    denom = 1.0 + (n - 1) * rho_bar
    if denom <= 0:
        return float(n)
    return min(float(n), max(1.0, n / denom))


def _counts(values: Sequence[str | None]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        if not v:
            continue
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


SUMMARY_KEYS = (
    "n", "n_pairs", "n_pairs_skipped", "avg_pairwise_corr", "max_pairwise_corr",
    "effective_bets", "diversification_ratio", "top_company", "top_family",
    "top_style", "flags", "note",
)


def summarize(result: Mapping[str, Any]) -> dict[str, Any]:
    """只保留诊断摘要，用于基线对比（不复制逐对明细）。"""
    return {k: result.get(k) for k in SUMMARY_KEYS}


def diagnose(series_by_code: Mapping[str, Sequence[tuple[str, float]]],
             meta: Mapping[str, Mapping[str, str | None]] | None = None,
             min_pair_obs: int = MIN_PAIR_OBS) -> dict[str, Any]:
    """对一组基金做相关性与集中度诊断。

    series_by_code: {fcode: [(date, daily_return), ...]}
    meta: {fcode: {"company": ..., "family": ..., "style": ...}}
    """
    codes = sorted(series_by_code)
    meta = meta or {}
    pairs: list[dict[str, Any]] = []
    rhos: list[float] = []
    skipped: list[dict[str, Any]] = []
    for a, b in combinations(codes, 2):
        rho, n = pair_corr(series_by_code[a], series_by_code[b], min_pair_obs)
        if rho is None:
            skipped.append({"a": a, "b": b, "common_obs": n})
            continue
        rhos.append(rho)
        pairs.append({"a": a, "b": b, "corr": round(rho, 4), "common_obs": n})
    pairs.sort(key=lambda p: (-p["corr"], p["a"], p["b"]))
    rho_bar = _mean(rhos)
    eff = effective_bets(len(codes), rho_bar)

    companies = _counts([(meta.get(c) or {}).get("company") for c in codes])
    families = _counts([(meta.get(c) or {}).get("family") for c in codes])
    styles = _counts([(meta.get(c) or {}).get("style") for c in codes])
    n = len(codes)

    def top(counts: dict[str, int]) -> dict[str, Any] | None:
        if not counts:
            return None
        name, count = next(iter(counts.items()))
        return {"name": name, "count": count, "share": round(count / n, 4) if n else None}

    flags: list[str] = []
    if eff is not None and n >= 3 and eff < max(2.0, 0.5 * n):
        flags.append("high_correlation")
    if pairs and pairs[0]["corr"] >= 0.90:
        flags.append("near_duplicate_pair")
    company_top = top(companies)
    if company_top and company_top["count"] >= 3 and (company_top["share"] or 0) >= 0.30:
        flags.append("company_concentration")
    family_top = top(families)
    if family_top and family_top["count"] >= 2:
        flags.append("family_duplicate")
    style_top = top(styles)
    if style_top and (style_top["share"] or 0) >= 0.70 and n >= 3:
        flags.append("style_concentration")

    return {
        "n": n,
        "n_pairs": len(pairs),
        "n_pairs_skipped": len(skipped),
        "avg_pairwise_corr": None if rho_bar is None else round(rho_bar, 4),
        "max_pairwise_corr": pairs[0] if pairs else None,
        "effective_bets": None if eff is None else round(eff, 3),
        "diversification_ratio": None if eff is None or not n else round(eff / n, 4),
        "companies": companies,
        "families": families,
        "styles": styles,
        "top_company": company_top,
        "top_family": family_top,
        "top_style": style_top,
        "flags": flags,
        "pairs": pairs,
        "skipped_pairs": skipped,
        "note": "等权、共同披露日、未插补；有效 bet 只衡量净值相关性，不是风险预算结论",
    }
