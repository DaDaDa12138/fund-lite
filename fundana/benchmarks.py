"""基准：中债-新综合财富（总值）指数 + ETF 复权净值代理。

优先使用中国债券信息网公开 JSON 接口的真实债券总回报指数（CBC_CFZS_00）。
该指数覆盖银行间与交易所债券市场，是“债券全市场”的财富/总值指数；它不是
任何一只基金的合同业绩比较基准，也不是单风格指数，报告里必须如实标注。

ETF 复权净值只作风格代理补充：有二级市场价格、管理费、跟踪误差，净值通常
比基金净值晚 1–3 个交易日。所有数字都从数据库真实行计算，缺失返回 None，
绝不填 0，也不生成模拟价格。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

# 真实债券总回报基准（中国债券信息网公开接口，POST form）
REAL_INDEX = {
    "code": "CBC_CFZS_00",
    "name": "中债-新综合财富(总值)指数",
    "note": "中国债券信息网公开接口；覆盖全市场债券的财富/总值指数，不是单只基金合同基准，也不是风格专用指数。",
}

# 增强债含权益/转债暴露，全市场债券指数不是合适对照；用对应 ETF 复权净值作风格代理。
ENHANCED_STYLES = {"bond_convertible", "bond_secondary_enhanced",
                   "bond_primary_enhanced"}

# style_code -> (ETF code, 名称, 说明)
BENCHMARKS: dict[str, tuple[str, str, str]] = {
    "bond_short": ("511360", "短融ETF海富通", "短端信用/现金类代理"),
    "bond_cash_enhanced": ("511360", "短融ETF海富通", "短端信用/现金类代理"),
    "bond_pure": ("511220", "城投债ETF海富通", "中短久期信用债代理"),
    "bond_credit": ("511220", "城投债ETF海富通", "中短久期信用债代理"),
    "bond_rate": ("511260", "十年国债ETF国泰", "利率债代理"),
    "bond_index": ("511260", "十年国债ETF国泰", "利率债代理"),
    "bond_primary_enhanced": ("511380", "可转债ETF博时", "含权/转债代理"),
    "bond_secondary_enhanced": ("511380", "可转债ETF博时", "含权/转债代理"),
    "bond_convertible": ("511380", "可转债ETF博时", "含权/转债代理"),
}


def for_style(style_code: str) -> dict[str, Any] | None:
    item = BENCHMARKS.get(style_code)
    if not item:
        return None
    code, name, note = item
    return {"code": code, "name": name, "note": note, "type": "etf_proxy"}


def _annualized(first: float, last: float, years: float) -> float | None:
    if first <= 0 or last <= 0 or years <= 0:
        return None
    try:
        return (last / first) ** (1.0 / years) - 1.0
    except (OverflowError, ValueError, ZeroDivisionError):
        return None


def index_metrics(conn, code: str = REAL_INDEX["code"],
                  as_of: str | None = None) -> dict[str, Any] | None:
    """从 market_index_daily 计算真实指数的 1/3/5 年年化与 1 年最大回撤。

    只使用 <= as_of 的真实收盘行；窗口锚点缺失或覆盖不足 80% 时返回 None，
    不做插值、不补零。as_of 为空时取该指数最新交易日。
    """
    if as_of:
        rows = conn.execute(
            "SELECT trade_date, close FROM market_index_daily"
            " WHERE index_code=? AND trade_date<=? ORDER BY trade_date",
            (code, as_of)).fetchall()
    else:
        rows = conn.execute(
            "SELECT trade_date, close FROM market_index_daily"
            " WHERE index_code=? ORDER BY trade_date", (code,)).fetchall()
    if len(rows) < 2:
        return None
    end_day = date.fromisoformat(str(rows[-1]["trade_date"]))
    end_close = float(rows[-1]["close"])
    out: dict[str, Any] = {
        "code": code,
        "name": REAL_INDEX["name"],
        "type": "real_index",
        "as_of": end_day.isoformat(),
    }
    for days, key in ((365, "annual_1y"), (1095, "annual_3y"), (1825, "annual_5y")):
        target = end_day - timedelta(days=days)
        anchor = None
        for row in rows:
            day = date.fromisoformat(str(row["trade_date"]))
            if day > target:
                break
            anchor = (day, float(row["close"]))
        if anchor is None:
            out[key] = None
            continue
        years = (end_day - anchor[0]).days / 365.25
        # 覆盖不足 80% 的窗口不硬算年化，避免把短样本伪装成 1/3/5 年。
        if years < days / 365.25 * 0.80:
            out[key] = None
            continue
        value = _annualized(anchor[1], end_close, years)
        out[key] = None if value is None else round(value * 100.0, 4)
    # 1 年最大回撤（只用真实收盘价，按窗口内峰值回撤）
    start = end_day - timedelta(days=365)
    window = [float(r["close"]) for r in rows
              if date.fromisoformat(str(r["trade_date"])) > start]
    peak = None
    mdd = 0.0
    for value in window:
        peak = value if peak is None else max(peak, value)
        if peak and peak > 0:
            mdd = max(mdd, 1.0 - value / peak)
    out["mdd_1y"] = round(mdd * 100.0, 4) if window else None
    return out


def metrics(conn, code: str, formula_contract: str) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT period_days, annual_dec, mdd_dec, end_date FROM fund_metric"
        " WHERE fcode=? AND formula_contract=? AND period_days IN (365,1095,1825)",
        (code, formula_contract)).fetchall()
    if not rows:
        return None
    out: dict[str, Any] = {"code": code, "as_of": max((r["end_date"] or "") for r in rows)}
    for r in rows:
        key = {365: "annual_1y", 1095: "annual_3y", 1825: "annual_5y"}[int(r["period_days"])]
        out[key] = None if r["annual_dec"] is None else round(float(r["annual_dec"]) * 100, 4)
        out[key.replace("annual", "mdd")] = (None if r["mdd_dec"] is None
                                             else round(float(r["mdd_dec"]) * 100, 4))
    return out


def _median(values: list[float | None]) -> float | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _lag_days(bench_as_of: str | None, as_of: str | None) -> int | None:
    if not bench_as_of or not as_of:
        return None
    try:
        return max(0, (date.fromisoformat(str(as_of)) - date.fromisoformat(str(bench_as_of))).days)
    except ValueError:
        return None


def summary(conn, formula_contract: str, formal_rows: list[dict[str, Any]],
            as_of: str | None = None) -> dict[str, Any]:
    """按风格汇总：真实中债指数优先，ETF 代理补充；没有数据返回 None。"""
    real = index_metrics(conn, as_of=as_of)
    by_style: dict[str, list[dict[str, Any]]] = {}
    for row in formal_rows:
        by_style.setdefault(str(row.get("style_code") or "unknown"), []).append(row)
    rows: list[dict[str, Any]] = []
    for style in sorted(by_style):
        etf = for_style(style)
        etf_m = metrics(conn, etf["code"], formula_contract) if etf else None
        members = by_style[style]
        peer_1y = _median([r.get("annual_1y_pct") for r in members])
        peer_3y = _median([r.get("annual_3y_pct") for r in members])
        peer_5y = _median([r.get("annual_5y_pct") for r in members])
        # 稳健类风格用全市场债券总回报指数；增强类风格含权益/转债暴露，
        # 没有合适的单一债券指数。ETF 只作为“风格参考”展示，不能当基准算超额，
        # 否则会拿可转债 ETF 的跌幅去评价一级/二级债基，得出虚假的 +10pp 超额。
        if style in ENHANCED_STYLES:
            b, bm, scope = None, None, "not_applicable"
        elif real:
            b, bm, scope = real, real, "broad_market"
        else:
            b, bm, scope = etf, etf_m, "style_proxy"
        rows.append({
            "style_code": style,
            "benchmark": b,
            "benchmark_metrics": bm,
            "benchmark_scope": scope,
            "benchmark_lag_days": _lag_days((bm or {}).get("as_of"), as_of),
            "etf": etf,
            "etf_metrics": etf_m,
            "peer_median_1y_pct": peer_1y,
            "peer_median_3y_pct": peer_3y,
            "peer_median_5y_pct": peer_5y,
            "excess_1y_pct": (None if not bm or bm.get("annual_1y") is None or peer_1y is None
                              else round(peer_1y - bm["annual_1y"], 4)),
            "excess_3y_pct": (None if not bm or bm.get("annual_3y") is None or peer_3y is None
                              else round(peer_3y - bm["annual_3y"], 4)),
            "members": len(members),
        })
    if real:
        note = (REAL_INDEX["note"] + " 稳健类风格用该全市场指数；增强类风格含权益/转债暴露，"
                "没有合适的单一债券指数，标为“无合适基准”，对应 ETF 只作风格参考、不计算超额。"
                "本报告的“相对基准差”是正式榜同风格中位数减所选基准收益，只作研究对照；"
                "指数/ETF 与基金净值日可能不同步，表内给出日期。")
    else:
        note = ("没有可用的中债-新综合财富指数数据，退回到 ETF 复权净值代理；ETF 不是"
                "中债/中证总回报指数，也不是基金合同基准，且含二级市场价格、管理费与跟踪误差。")
    return {"type": "real_index+etf_reference" if real else "etf_proxy",
            "real_index": real, "rows": rows, "note": note,
            "as_of": as_of,
            "lag_days": _lag_days((real or {}).get("as_of") if real else None, as_of)}
