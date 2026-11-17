"""组合层回测：把榜单当成组合持有（默认等权，可选逆波动率），比较组合收益/风险与基准（P0）。

与 walkforward 的区别：
- walkforward 统计"入选基金前瞻收益的均值"，每只基金权重相同但不做复利；
- 本模块按建仓日买入（默认等权；--portfolio-weighting inverse_vol 用逆波动率）、持有到期末，按日净值复利成组合净值曲线，
  再算年化、波动、夏普、最大回撤，并与"全市场可投债基等权组合"和国债指数比较。

口径与局限（必须和结果一起读）：
- 建仓日只用 as-of 可得信息；不做中途调仓，不做交易成本/申赎费扣减；
- 基金池只含存续基金（幸存者偏差），且不含 L2/L3 的当期不可投/锁定期判断；
- 基准 1 = base_gate 通过的全部债基等权（含增强债），基准 2 = 000012 国债指数
  （价格指数，不含票息，因此只能看"相对价格"而不是完整总回报）；
- 周频/月频披露的基金按披露日收益计入，未披露日按 0 收益延续；
- 组合层结果对建仓日、费率、分红处理敏感，只用于方向性判断，不替代正式回测。
"""
from __future__ import annotations

import math
from bisect import bisect_right
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from ..daily_returns import load_daily_returns
from ..screen import engine as screen_engine
from ..screen import thresholds as T
from . import walkforward as wf

MIN_OBS = 40
START_TOLERANCE_DAYS = 45
END_TOLERANCE_DAYS = 20
TRADING_DAYS = 252.0


def _load_series(conn, fcode: str, start: str, end: str) -> list[tuple[str, float]] | None:
    """读 (start, end] 的日收益；锚点取 start 之前最后一个观测。"""
    return load_daily_returns(conn, fcode, start, end)


def _trailing_vol(conn, fcode: str, as_of: str, lookback_days: int = 365) -> float | None:
    """as-of 前 lookback_days 的年化波动率（用于风险预算权重）。"""
    start = (date.fromisoformat(as_of) - timedelta(days=lookback_days)).isoformat()
    series = _load_series(conn, fcode, start, as_of)
    if not series or len(series) < 40:
        return None
    rets = [r for _, r in series]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def _curve(series: list[tuple[str, float]], start: str) -> tuple[list[str], list[float]]:
    """单只基金从 1.0 开始的累计净值曲线（起点计入）。"""
    dates = [start]
    cums = [1.0]
    cur = 1.0
    for d, ret in series:
        cur *= 1.0 + ret
        dates.append(d)
        cums.append(cur)
    return dates, cums


def _portfolio(series_by_code: dict[str, list[tuple[str, float]]], start: str,
               end: str, weights: dict[str, float] | None = None) -> dict[str, Any] | None:
    """买入并持有；weights 缺省等权，给了就按权重归一化。"""
    if not series_by_code:
        return None
    curves = {code: _curve(series, start) for code, series in series_by_code.items()}
    dates = sorted({d for ds, _ in curves.values() for d in ds} | {start})
    pv: list[float] = []
    for d in dates:
        pairs = []
        for code, (ds, cs) in curves.items():
            i = bisect_right(ds, d) - 1
            if i >= 0:
                pairs.append((code, cs[i]))
        if pairs:
            if weights:
                total_w = sum(max(0.0, weights.get(code, 0.0)) for code, _ in pairs)
                if total_w > 0:
                    pv.append(sum(cs * max(0.0, weights.get(code, 0.0)) for code, cs in pairs) / total_w)
                else:
                    pv.append(sum(cs for _, cs in pairs) / len(pairs))
            else:
                pv.append(sum(cs for _, cs in pairs) / len(pairs))
    if len(pv) < 2:
        return None
    rets = [pv[i] / pv[i - 1] - 1.0 for i in range(1, len(pv)) if pv[i - 1] > 0]
    days = (date.fromisoformat(dates[-1]) - date.fromisoformat(start)).days
    if days <= 0:
        return None
    annual = pv[-1] ** (365.0 / days) - 1.0
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    vol = math.sqrt(var) * math.sqrt(TRADING_DAYS)
    sharpe = (mean / math.sqrt(var) * math.sqrt(TRADING_DAYS)) if var > 0 else None
    peak = pv[0]
    mdd = 0.0
    for v in pv:
        peak = max(peak, v)
        mdd = max(mdd, 1.0 - v / peak if peak else 0.0)
    return {
        "n": len(series_by_code),
        "annual_pct": round(100 * annual, 2),
        "vol_pct": round(100 * vol, 2),
        "sharpe": None if sharpe is None else round(sharpe, 3),
        "mdd_pct": round(100 * mdd, 2),
        "days": days,
        "obs": len(rets),
        "curve": {"dates": dates, "values": [round(v, 6) for v in pv]},
    }


def _index_series(conn, code: str, start: str, end: str) -> list[tuple[str, float]] | None:
    anchor = conn.execute(
        "SELECT trade_date, close FROM market_index_daily"
        " WHERE index_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1",
        (code, start)).fetchone()
    rows = conn.execute(
        "SELECT trade_date, close, pct_chg FROM market_index_daily"
        " WHERE index_code=? AND trade_date>? AND trade_date<=? ORDER BY trade_date",
        (code, start, end)).fetchall()
    if len(rows) < 20:
        return None
    out: list[tuple[str, float]] = []
    prev = anchor["close"] if anchor else None
    for r in rows:
        if r["pct_chg"] is not None:
            ret = r["pct_chg"] / 100.0
        elif prev:
            ret = r["close"] / prev - 1.0
        else:
            ret = 0.0
        out.append((r["trade_date"], ret))
        prev = r["close"]
    return out


def _stats_for(conn, codes: Sequence[str], start: str, end: str,
               weights: dict[str, float] | None = None) -> dict[str, Any]:
    series = {}
    for code in codes:
        s = _load_series(conn, code, start, end)
        if s:
            series[code] = s
    if not series:
        return {"n": len(codes), "n_used": 0, "note": "no_forward_series"}
    result = _portfolio(series, start, end, weights) or {"n": len(codes), "n_used": 0}
    result["n_used"] = len(series)
    result["coverage"] = round(len(series) / len(codes), 4) if codes else 0.0
    return result


def _benchmark(conn, codes: Sequence[str], start: str, end: str) -> dict[str, Any]:
    result = _stats_for(conn, codes, start, end)
    idx = _index_series(conn, "000012", start, end)
    if idx:
        curve = _portfolio({"000012": idx}, start, end)
        if curve:
            result["index_000012_annual_pct"] = curve["annual_pct"]
            result["index_000012_mdd_pct"] = curve["mdd_pct"]
    return result


def _board_codes(cands, fixed_pass, scores, cfg, conn=None,
                 as_of: str | None = None) -> dict[str, list[str]]:
    """用生产同款排序/折叠规则构造组合候选（不跑 L2/L3/L7 动态层）。"""
    by_code = {c["fcode"]: c for c in cands}
    passed = sorted(((c["fcode"], {"scores": scores[c["fcode"]],
                                   "style_code": c["style_code"]}) for c in fixed_pass),
                    key=lambda item: (-(item[1]["scores"].get("rank_score") or 0.0),
                                      item[0]))
    dropped: list[dict[str, Any]] = []
    # 与生产 _rank 一致：core10 的有界单换需要 365 天日收益序列；
    # 历史 cohort 若没有 conn/as_of 就退回纯贪婪（结果会与生产不同，仅作旧口径）。
    series_by_code: dict[str, list[tuple[str, float]]] = {}
    if conn is not None and as_of:
        corr_start = (date.fromisoformat(as_of) - timedelta(days=365)).isoformat()
        for code, d in passed:
            if d.get("style_code") in T.CORE_STYLES:
                s = load_daily_returns(conn, code, corr_start, as_of)
                if s:
                    series_by_code[code] = s
    core10 = screen_engine._select_concentrated(  # noqa: SLF001
        passed, by_code, dropped, board=T.BOARD_CORE10, quota=T.CORE10_QUOTA,
        enh_cap=T.CORE10_ENHANCED_CAP, conv_cap=T.CORE10_CONVERTIBLE_CAP,
        series_by_code=series_by_code)
    core50 = screen_engine._select_concentrated(  # noqa: SLF001
        passed, by_code, dropped, board=T.BOARD_CORE, quota=T.CORE_QUOTA,
        enh_cap=T.CORE_ENHANCED_CAP, conv_cap=T.CORE_CONVERTIBLE_CAP)
    stable_pool = [item for item in passed
                   if item[1]["style_code"] in T.STABLE_STYLES]
    stable50 = screen_engine._select_concentrated(  # noqa: SLF001
        stable_pool, by_code, dropped, board="stable50", quota=50, enh_cap=0, conv_cap=0)
    enhanced_codes = [c["fcode"] for c in fixed_pass if c["style_code"] in T.ENHANCED_STYLES]
    enhanced = wf._enhanced_picks(enhanced_codes, fixed_pass, scores, cands, cfg)  # noqa: SLF001
    return {
        "core10": [x["fcode"] for x in core10],
        "core50": [x["fcode"] for x in core50],
        "stable50": [x["fcode"] for x in stable50],
        "enhanced": enhanced,
    }


def _inverse_vol_weights(conn, codes: Sequence[str], as_of: str) -> dict[str, float] | None:
    """逆波动率权重；有效波动少于一半时返回 None（回退等权）。"""
    vols = {}
    for code in codes:
        v = _trailing_vol(conn, code, as_of)
        if v and v > 0:
            vols[code] = v
    if len(vols) < max(3, len(codes) // 2):
        return None
    return {code: 1.0 / v for code, v in vols.items()}


def run_block(conn, cfg, as_of: str, cands, gate_pass, fixed_pass,
              scores: dict[str, dict[str, Any]], end: str,
              weighting: str = "equal") -> dict[str, Any]:
    """一个 cohort 的组合层结果。"""
    boards = _board_codes(cands, fixed_pass, scores, cfg, conn=conn, as_of=as_of)
    out: dict[str, Any] = {"boards": {}, "weighting": weighting}
    for name, codes in boards.items():
        weights = (_inverse_vol_weights(conn, codes, as_of)
                   if weighting == "inverse_vol" else None)
        stats = _stats_for(conn, codes, as_of, end, weights)
        stats["weighting"] = "equal" if weights is None else weighting
        out["boards"][name] = stats
    bench_codes = [c["fcode"] for c in gate_pass]
    out["benchmark"] = _benchmark(conn, bench_codes, as_of, end)
    for name, stats in out["boards"].items():
        b = out["benchmark"].get("annual_pct")
        a = stats.get("annual_pct")
        stats["excess_pct"] = None if (a is None or b is None) else round(a - b, 2)
        stats["beat"] = None if (a is None or b is None) else bool(a > b)
    return out


def summarize(cohorts: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """跨队列汇总：年化/夏普/回撤/超额/胜基准率。"""
    names: set[str] = set()
    for c in cohorts:
        names.update((c.get("portfolio") or {}).get("boards", {}))
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(names):
        rows = [c["portfolio"]["boards"][name] for c in cohorts
                if (c.get("portfolio") or {}).get("boards", {}).get(name)]
        rows = [r for r in rows if r.get("annual_pct") is not None]
        if not rows:
            continue
        out[name] = {
            "cohorts": len(rows),
            "annual_pct": round(sum(r["annual_pct"] for r in rows) / len(rows), 2),
            "sharpe": round(sum((r.get("sharpe") or 0.0) for r in rows) / len(rows), 3),
            "mdd_pct": round(sum(r["mdd_pct"] for r in rows) / len(rows), 2),
            "excess_pct": round(sum((r.get("excess_pct") or 0.0) for r in rows) / len(rows), 2),
            "beat_rate": round(sum(1 for r in rows if r.get("beat")) / len(rows), 4),
            "n_used": round(sum(r.get("n_used") or 0 for r in rows) / len(rows), 1),
        }
    bench = [c["portfolio"]["benchmark"] for c in cohorts
             if (c.get("portfolio") or {}).get("benchmark", {}).get("annual_pct") is not None]
    if bench:
        out["benchmark"] = {
            "cohorts": len(bench),
            "annual_pct": round(sum(r["annual_pct"] for r in bench) / len(bench), 2),
            "sharpe": round(sum((r.get("sharpe") or 0.0) for r in bench) / len(bench), 3),
            "mdd_pct": round(sum(r["mdd_pct"] for r in bench) / len(bench), 2),
            "n_used": round(sum(r.get("n_used") or 0 for r in bench) / len(bench), 1),
        }
    return out
