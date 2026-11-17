"""Phase 2 质量增强：合同基准映射、因子 alpha、组合独立性（只读影子研究）。

边界：
* 只读 screen_run/screen_run_input/fund_factor_exposure/fund_profile/
  src_pzd_profile/fund_nav_daily/market_index_daily；
* 不写 screen_pick，不改生产权重，不参与发布；
* 合同基准只映射到项目已采集的四个指数：CBC_CFZS_00（中债-新综合财富，
  总回报）、000012（国债，价格）、000013（企债，价格）、000832（中证转债，
  价格）。没有短端/中期/信用分层指数，期限或券种不匹配时只能标“代理”，
  不把它包装成精确合同基准；
* 因子 alpha 来自 fund_factor_exposure 的 730 天三因子回归，是“扣除久期/
  信用/转债暴露后的残差收益”，仍包含票息 carry 与选券能力，不能单独称为
  经理技能；只作同风格参考线（alpha_t 与 r2 同时达标），不构成质量通过率或发布门禁；
* 组合独立性用等权、共同披露日、未插补的相关性诊断，不进入筛选门禁。
"""
from __future__ import annotations

import calendar
import re
from collections import Counter
from bisect import bisect_right
from datetime import date, timedelta
from itertools import combinations
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

from ..daily_returns import load_daily_returns
from ..portfolio_diagnostics import diagnose, effective_bets, pair_corr
from ..screen import thresholds as T

INDEX_META: dict[str, dict[str, Any]] = {
    "CBC_CFZS_00": {
        "name": "中债-新综合财富(总值)指数",
        "return_type": "total_return",
        "note": "全市场债券财富/总值指数，含票息；不是单只基金合同基准。",
    },
    "000012": {
        "name": "国债指数",
        "return_type": "price_return",
        "note": "交易所国债价格指数，不含票息；只能作利率债方向代理。",
    },
    "000013": {
        "name": "企债指数",
        "return_type": "price_return",
        "note": "交易所企债价格指数，不含票息；只能作信用债方向代理。",
    },
    "000832": {
        "name": "中证转债",
        "return_type": "price_return",
        "note": "中证可转换债券价格指数，不含票息；只能作转债方向代理。",
    },
}

CLASS_INDEX: dict[str, str | None] = {
    "composite": "CBC_CFZS_00",
    "rate": "000012",
    "credit": "000013",
    "convertible": "000832",
    "cash": None,
    "equity": None,
    "other": None,
}
CLASS_LABELS = {
    "composite": "综合债", "rate": "利率债", "credit": "信用债",
    "convertible": "转债", "cash": "存款/现金", "equity": "权益", "other": "其他",
}
MATURITY_LABELS = {
    "short": "1年以下", "medium": "1-3年", "midlong": "1-5年",
    "long": "5年以上", "broad": "未限定", "mixed": "多期限",
}

# 绝对质量分层（影子研究阈值，不是生产发布门禁）。
# A 只给“精确合同基准 + 正超额 + 滚动 1 年胜率 + 回撤上限”同时通过的样本；
# proxy 基准只能进 B/C，不能因为方向对得上就称为精确优质。
QUALITY_TIER_VERSION = "quality-tier-v1"
QUALITY_ROLLING_WINDOW_DAYS = 365
QUALITY_ROLLING_MIN_WINDOWS = 8
QUALITY_A_EXCESS_MIN = 0.0
QUALITY_A_WIN_RATE_MIN = 0.60
QUALITY_A_MDD_MAX = 3.0
QUALITY_A_MDD_MAX_ENHANCED = 5.0
QUALITY_B_EXCESS_MIN = -0.5
QUALITY_B_WIN_RATE_MIN = 0.50
QUALITY_B_MDD_MAX = 5.0
QUALITY_B_MDD_MAX_ENHANCED = 8.0
QUALITY_B_PROXY_EXCESS_MIN = 0.5
# 债券基金单日净值涨跌超过 10% 通常只能是份额折算、数据源错误、未调整的
# 公司行为，或大额赎回费一次性计入基金财产；在绝对质量分层里直接标“未评级”，
# 避免把一次性净值异动当成可持续超额。
QUALITY_DATA_ANOMALY_MOVE_PCT = 10.0

_PERCENT_RE = re.compile(r"[*×xX]\s*(\d+(?:\.\d+)?)\s*%")
_NUMERIC_SEG_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")


def _segments(text: str) -> list[str]:
    raw = (text or "").replace("＋", "+").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split("+") if p.strip()]
    # “一年期定期存款利率(税后)+1.2%” 里的 1.2% 是加法利差，不是资产权重；
    # 把纯数字段并回前一段，避免把基准拆成两个资产。
    merged: list[str] = []
    for part in parts:
        if merged and _NUMERIC_SEG_RE.match(part):
            merged[-1] = merged[-1] + "+" + part
        else:
            merged.append(part)
    return merged


def _weight(segment: str) -> float | None:
    m = _PERCENT_RE.search(segment)
    return None if not m else float(m.group(1)) / 100.0


def _maturity(segment: str) -> str:
    s = segment.replace(" ", "")
    if any(k in s for k in ("1年以下", "0-1年", "0－1年")):
        return "short"
    if any(k in s for k in ("0-3年", "0－3年", "1-3年", "1－3年", "1-2年", "2-3年")):
        return "medium"
    if any(k in s for k in ("1-5年", "1－5年", "3-5年", "3－5年", "2-5年")):
        return "midlong"
    if any(k in s for k in ("5年以上", "5-10年", "7-10年", "10年以上")):
        return "long"
    return "broad"


def _classify(segment: str) -> str:
    s = segment
    if any(k in s for k in ("可转换", "转债", "可交换")):
        return "convertible"
    if any(k in s for k in ("同业存单", "活期存款", "定期存款", "银行存款",
                            "货币市场", "现金")):
        return "cash"
    if any(k in s for k in ("国债", "国开", "进出口", "农发", "口行",
                            "政策性金融", "地方政府债", "政金债", "利率债")):
        return "rate"
    if any(k in s for k in ("信用债", "信用类", "高信用等级", "投资级", "产业债",
                            "城投", "二级资本债", "永续债", "中期票据", "短期融资券",
                            "公司债", "企业债", "中高等级")):
        return "credit"
    if any(k in s for k in ("沪深300", "中证500", "股票", "权益")):
        return "equity"
    if any(k in s for k in ("综合债", "全债", "债券总指数", "中债综合", "中证综合",
                            "中债总财富", "新综合", "综合财富", "综合全价", "全价")):
        return "composite"
    return "other"


def parse_contract_benchmark(text: str | None) -> dict[str, Any]:
    """把业绩比较基准拆成资产段、期限与权重。

    返回 status：
    * mapped：全部权重都能映射到指数；
    * mapped_cash_zero：现金/存款权重按 0 收益处理，其余全部可映射；
    * cash_only：只有存款/现金，无法与债券指数比较；
    * partial：存在无法映射的非现金资产（如股票、海外、商品）；
    * unknown：没有基准文本或无法识别。
    """
    raw = (text or "").strip()
    if not raw:
        return {"text": raw, "status": "unknown", "quality": "unmapped",
                "components": [], "classes": [],
                "primary_class": "unknown", "maturity": "broad",
                "mapped_weight": 0.0, "cash_weight": 0.0, "unknown_weight": 1.0,
                "note": "没有业绩比较基准文本"}
    parts = _segments(raw)
    parsed = []
    for part in parts:
        cls = _classify(part)
        parsed.append({
            "text": part,
            "class": cls,
            "class_label": CLASS_LABELS.get(cls, cls),
            "maturity": _maturity(part),
            "weight": _weight(part),
            "index_code": CLASS_INDEX.get(cls),
        })
    # 有权重的段按给定权重；没有权重的段均分剩余权重。
    known = [x for x in parsed if x["weight"] is not None]
    unknown = [x for x in parsed if x["weight"] is None]
    if known:
        used = sum(float(x["weight"]) for x in known)
        remain = max(0.0, 1.0 - used)
        for x in unknown:
            x["weight"] = remain / len(unknown) if unknown else 0.0
    else:
        for x in parsed:
            x["weight"] = 1.0 / len(parsed) if parsed else 0.0
    total = sum(float(x["weight"]) for x in parsed) or 1.0
    for x in parsed:
        x["weight"] = round(float(x["weight"]) / total, 6)

    mapped = sum(x["weight"] for x in parsed if x["index_code"])
    cash = sum(x["weight"] for x in parsed if x["class"] == "cash")
    unknown_w = sum(x["weight"] for x in parsed if not x["index_code"] and x["class"] != "cash")
    if not parsed:
        status = "unknown"
    elif mapped <= 0 and cash >= 0.999:
        status = "cash_only"
    elif mapped >= 0.999:
        status = "mapped"
    elif mapped + cash >= 0.999 and unknown_w <= 1e-9:
        status = "mapped_cash_zero"
    else:
        status = "partial"
    # 主类按最高非现金权重；全是现金时取 cash。
    non_cash = [x for x in parsed if x["class"] != "cash"] or parsed
    primary = max(non_cash, key=lambda x: x["weight"])["class"] if non_cash else "unknown"
    maturities = {x["maturity"] for x in parsed if x["maturity"] != "broad"}
    maturity = "mixed" if len(maturities) > 1 else (next(iter(maturities)) if maturities else "broad")
    # 质量等级：只有“综合债 + 未限定期限 + CBC 总回报”可称 exact；
    # 其余即使能映射，也只是方向代理，不能当精确合同基准。
    if status == "mapped" and primary == "composite" and maturity == "broad":
        quality = "exact"
    elif status in ("mapped", "mapped_cash_zero"):
        quality = "proxy"
    elif status == "cash_only":
        quality = "cash_only"
    elif status == "partial":
        quality = "partial"
    else:
        quality = "unmapped"
    return {
        "text": raw, "status": status, "quality": quality,
        "components": parsed,
        "classes": list(dict.fromkeys(x["class"] for x in parsed)),
        "primary_class": primary,
        "maturity": maturity,
        "mapped_weight": round(mapped, 4),
        "cash_weight": round(cash, 4),
        "unknown_weight": round(unknown_w, 4),
        "note": "只有四个已采集指数；短端/中期/信用分层合同基准没有对应指数，只能标代理。",
    }


def _index_window(conn, code: str, as_of: str, days: int) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT trade_date, close FROM market_index_daily"
        " WHERE index_code=? AND trade_date<=? ORDER BY trade_date", (code, as_of)).fetchall()
    if len(rows) < 2:
        return None
    end_day = date.fromisoformat(str(rows[-1]["trade_date"]))
    end_close = float(rows[-1]["close"])
    target = end_day - timedelta(days=days)
    anchor = None
    for row in rows:
        day = date.fromisoformat(str(row["trade_date"]))
        if day > target:
            break
        anchor = (day, float(row["close"]))
    if anchor is None or anchor[1] <= 0 or end_close <= 0:
        return None
    years = (end_day - anchor[0]).days / 365.25
    need = days / 365.25
    coverage = years / need if need else 0.0
    if coverage < 0.80:
        return None
    try:
        annual = (end_close / anchor[1]) ** (1.0 / years) - 1.0
    except (OverflowError, ValueError, ZeroDivisionError):
        return None
    return {
        "code": code, "annual_pct": round(annual * 100.0, 4),
        "start": anchor[0].isoformat(), "end": end_day.isoformat(),
        "years": round(years, 3), "coverage": round(min(1.0, coverage), 4),
        "return_type": INDEX_META.get(code, {}).get("return_type", "unknown"),
    }


def index_cache(conn, as_of: str, codes: Iterable[str] = tuple(INDEX_META)) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for code in codes:
        out[code] = {days: _index_window(conn, code, as_of, days)
                     for days in (365, 1095, 1825)}
    return out


def contract_metrics(conn, text: str | None, as_of: str,
                     cache: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """按合同基准文本计算可映射指数的 1/3/5 年年化。

    只有 exact（综合债、未限定期限、CBC 总回报）给出 excess 资格；
    其余返回指数收益但标 proxy，不把价格指数/短端错配包装成超额。
    """
    parsed = parse_contract_benchmark(text)
    cache = cache or index_cache(conn, as_of)
    components = parsed.get("components") or []
    annual: dict[str, float | None] = {}
    detail: list[dict[str, Any]] = []
    for days in (365, 1095, 1825):
        total = 0.0
        ok = True
        for comp in components:
            code = comp.get("index_code")
            weight = float(comp.get("weight") or 0.0)
            if not code:
                # 现金/存款按 0 收益；无法映射的非现金段使该期限不可算。
                if comp.get("class") == "cash":
                    continue
                ok = False
                break
            item = (cache.get(code) or {}).get(days)
            if not item:
                ok = False
                break
            total += weight * float(item["annual_pct"])
        annual[str(days)] = (round(total, 4)
                              if ok and parsed.get("status") not in ("unknown", "cash_only")
                              else None)
    for comp in components:
        code = comp.get("index_code")
        item = (cache.get(code) or {}).get(1095) if code else None
        detail.append({
            "text": comp["text"], "class": comp["class"], "weight": comp["weight"],
            "maturity": comp["maturity"], "index_code": code,
            "index_name": INDEX_META.get(code, {}).get("name") if code else None,
            "annual_3y_pct": item["annual_pct"] if item else None,
        })
    total_weight = sum(float(c.get("weight") or 0.0) for c in components if c.get("index_code"))
    price_weight = sum(float(c.get("weight") or 0.0) for c in components
                       if c.get("index_code") and INDEX_META.get(c["index_code"], {}).get("return_type") == "price_return")
    return {
        **parsed,
        "annual_1y_pct": annual["365"],
        "annual_3y_pct": annual["1095"],
        "annual_5y_pct": annual["1825"],
        "mapped_index_weight": round(total_weight, 4),
        "price_index_weight": round(price_weight, 4),
        "detail": detail,
        "note": ("exact：综合债、未限定期限，CBC 为总回报；proxy：利率/信用/转债或期限受限，"
                 "指数只是方向代理；cash_only/partial/unmapped 不计算合同超额。"),
    }


def alpha_metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """按榜单汇总因子 alpha 参考线；alpha 单位是年化百分点，含 carry。"""
    vals = [(x.get("alpha_annual_pct"), x.get("alpha_t"), x.get("factor_r2"),
             bool(x.get("alpha_reliable")))
            for x in items]
    reliable = [(a, t, r2) for a, t, r2, ok in vals if ok and a is not None]
    return {
        "alpha_covered": sum(1 for a, _t, _r, _ok in vals if a is not None),
        "alpha_reliable": len(reliable),
        "alpha_median_pct": _median([a for a, _t, _r in reliable]),
        "alpha_positive_share": _share(sum(1 for a, _t, _r in reliable if a > 0), len(reliable)),
        "alpha_significant_share": _share(sum(1 for _a, t, _r in reliable if (t or 0) >= 2.0), len(reliable)),
        "alpha_r2_median": _median([r for _a, _t, r in reliable if r is not None]),
        "alpha_reference_pass": sum(1 for a, t, r in reliable
                                    if a >= 1.0 and (t or 0) >= 2.0 and (r or 0) >= 0.15),
        "alpha_reference_eligible": len(reliable),
    }


def _median(values: Iterable[float | None]) -> float | None:
    xs = [float(v) for v in values if v is not None]
    return round(median(xs), 4) if xs else None


def _share(num: int, denom: int) -> float | None:
    return round(num / denom, 4) if denom else None


def _pct(values: Iterable[float | None], q: float) -> float | None:
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo, hi = int(pos), min(len(xs) - 1, int(pos) + 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo), 4)


def benchmark_metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    infos = [x.get("benchmark_contract") or {} for x in items]
    qualities = [str(i.get("quality") or "unmapped") for i in infos]
    maturities = [str(i.get("maturity") or "broad") for i in infos]
    return {
        "benchmark_mapped": sum(1 for q in qualities if q in ("exact", "proxy")),
        "benchmark_exact": sum(1 for q in qualities if q == "exact"),
        "benchmark_proxy": sum(1 for q in qualities if q == "proxy"),
        "benchmark_cash_only": sum(1 for q in qualities if q == "cash_only"),
        "benchmark_partial": sum(1 for q in qualities if q == "partial"),
        "benchmark_unmapped": sum(1 for q in qualities if q in ("unmapped", "unknown")),
        "benchmark_maturity": {k: maturities.count(k) for k in
                               ("short", "medium", "midlong", "long", "broad", "mixed")},
    }


def exposure_metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    durations = [x.get("beta_duration") for x in items]
    credits = [x.get("beta_credit") for x in items]
    convs = [x.get("beta_convertible") for x in items]
    r2s = [x.get("factor_r2") for x in items]
    n = len(items)
    return {
        "duration_beta_p10": _pct(durations, 0.10),
        "duration_beta_p90": _pct(durations, 0.90),
        "credit_beta_p10": _pct(credits, 0.10),
        "credit_beta_p90": _pct(credits, 0.90),
        "max_convertible_beta": _median([max((v for v in convs if v is not None), default=None)]),
        "high_convertible": [x["fcode"] for x in items
                             if x.get("beta_convertible") is not None and x["beta_convertible"] >= 0.30],
        "factor_r2_median": _median(r2s),
        "leveraged_share": _share(sum(1 for x in items if x.get("leverage")), n),
        **alpha_metrics(items),
        **benchmark_metrics(items),
    }


def enhanced_risk_metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    mdd = [x.get("mdd_3y") for x in items]
    vol = [x.get("vol_3y") for x in items]
    return {
        "enhanced_3y_covered": sum(1 for v in mdd if v is not None),
        "mdd_3y_median_pct": _median(mdd),
        "mdd_3y_p90_pct": _pct(mdd, 0.90),
        "vol_3y_median_pct": _median(vol),
        "vol_3y_p90_pct": _pct(vol, 0.90),
        "mdd_3y_over_2pct": [x["fcode"] for x in items
                             if x.get("mdd_3y") is not None and x["mdd_3y"] > 2.0],
        "vol_3y_over_3pct": [x["fcode"] for x in items
                             if x.get("vol_3y") is not None and x["vol_3y"] > 3.0],
    }


def _load_series(conn, codes: Sequence[str], as_of: str, days: int = 365) -> dict[str, list[tuple[str, float]]]:
    start = (date.fromisoformat(as_of) - timedelta(days=days)).isoformat()
    out = {}
    for code in codes:
        series = load_daily_returns(conn, code, start, as_of)
        if series:
            out[code] = series
    return out


def _series_eff(series: Mapping[str, Sequence[tuple[str, float]]], codes: Sequence[str]) -> tuple[float | None, float | None, int]:
    rhos = []
    for i, a in enumerate(codes):
        for b in codes[i + 1:]:
            rho, _ = pair_corr(series.get(a) or [], series.get(b) or [])
            if rho is not None:
                rhos.append(float(rho))
    if len(codes) < 2:
        return None, None, len(rhos)
    eff = effective_bets(len(codes), sum(rhos) / len(rhos)) if rhos else None
    return (None if eff is None else round(eff, 3),
            None if not rhos else round(max(rhos), 4), len(rhos))


def core10_independence(conn, items: Sequence[Mapping[str, Any]], as_of: str) -> dict[str, Any]:
    codes = [x["fcode"] for x in items]
    series = _load_series(conn, codes, as_of)
    meta = {x["fcode"]: {"company": x.get("company_name"), "family": x.get("family_id"),
                         "style": x.get("style_code")} for x in items}
    diag = diagnose({c: series[c] for c in codes if c in series}, meta)
    top_company = diag.get("top_company") or {}
    checks = {
        "effective_bets": {
            "value": diag.get("effective_bets"),
            "target": T.CORE10_TARGET_EFFECTIVE_BETS,
            "pass": (diag.get("effective_bets") is not None
                     and diag["effective_bets"] >= T.CORE10_TARGET_EFFECTIVE_BETS),
        },
        "max_pair": {
            "value": (diag.get("max_pairwise_corr") or {}).get("corr"),
            "target": 0.75,
            "pass": ((diag.get("max_pairwise_corr") or {}).get("corr") is not None
                     and diag["max_pairwise_corr"]["corr"] <= 0.75),
        },
        "top_company_share": {
            "value": top_company.get("share"),
            "target": 0.30,
            "pass": (top_company.get("share") is not None and top_company["share"] <= 0.30),
        },
        "leveraged_share": {
            "value": _share(sum(1 for x in items if x.get("leverage")), len(items)),
            "target": 0.60,
            "pass": (_share(sum(1 for x in items if x.get("leverage")), len(items)) or 0) <= 0.60,
        },
    }
    failed = [k for k, v in checks.items() if not v["pass"]]
    return {
        "n": len(items),
        "series_covered": len(series),
        "effective_bets": diag.get("effective_bets"),
        "avg_pairwise_corr": diag.get("avg_pairwise_corr"),
        "max_pair": diag.get("max_pairwise_corr"),
        "top_company": top_company,
        "companies": diag.get("companies"),
        "families": diag.get("families"),
        "styles": diag.get("styles"),
        "flags": diag.get("flags"),
        "checks": checks,
        "failed_checks": failed,
        "status": "meets_target" if not failed else "downgraded",
        "alpha": alpha_metrics(items),
        "exposure": exposure_metrics(items),
        "note": "等权、共同披露日、未插补；有效 bet 目标 1.8/最大相关 0.75 是影子门槛，未进生产。",
    }


def best_effort_core10(items: Sequence[Mapping[str, Any]], as_of: str,
                       conn) -> dict[str, Any]:
    """在核心 50 里做有限贪心，估计现有候选池能达到的有效 bet 上限。

    不是生产选样，也不是全局最优；只回答“把约束放松一点，1.8 是否可达”。
    """
    eligible = [x for x in items if x.get("series")]
    if len(eligible) < 2:
        return {"status": "unavailable", "reason": "no_series"}
    style_caps_default = T.CORE10_STYLE_CAPS
    configs = [
        ("default_caps", style_caps_default, T.CORE10_MAX_PAIR_CORR),
        ("relaxed_caps", {"bond_short": 4, "bond_credit": 4, "bond_rate": 2, "bond_index": 2},
         T.CORE10_MAX_PAIR_CORR),
        ("no_style_caps", {}, T.CORE10_MAX_PAIR_CORR),
        ("default_caps_loose_pair", style_caps_default, 0.90),
    ]
    best: dict[str, Any] | None = None
    for name, caps, max_pair in configs:
        for lam in (30.0, 50.0, 80.0, 120.0, 200.0):
            picked = []
            for _ in range(int(T.CORE10_QUOTA)):
                cand = None
                for x in eligible:
                    if x["fcode"] in {p["fcode"] for p in picked}:
                        continue
                    style = x.get("style_code") or ""
                    cap = caps.get(style)
                    if cap is not None and sum(1 for p in picked if p.get("style_code") == style) >= cap:
                        continue
                    if sum(1 for p in picked if p.get("company_name") == x.get("company_name")) >= T.COMPANY_CAP:
                        continue
                    if any(p.get("family_id") == x.get("family_id") for p in picked):
                        continue
                    mx = 0.0
                    ok = True
                    for p in picked:
                        rho, _ = pair_corr(x.get("series") or [], p.get("series") or [])
                        if rho is None:
                            continue
                        if rho > max_pair:
                            ok = False
                            break
                        mx = max(mx, float(rho))
                    if not ok:
                        continue
                    score = float((x.get("scores") or {}).get("rank_score")
                                  or x.get("rank_score") or 0.0)
                    value = score - lam * mx
                    if cand is None or value > cand[0]:
                        cand = (value, x)
                if cand is None:
                    break
                picked.append(cand[1])
            if len(picked) < 2:
                continue
            codes = [p["fcode"] for p in picked]
            eff, maxp, _ = _series_eff({p["fcode"]: p["series"] for p in picked}, codes)
            if eff is None:
                continue
            score = sum(float((p.get("scores") or {}).get("rank_score")
                              or p.get("rank_score") or 0.0) for p in picked)
            row = {"config": name, "lambda": lam, "n": len(codes), "effective_bets": eff,
                   "max_pair": maxp, "rank_score_sum": round(score, 2), "codes": codes}
            if best is None or (row["effective_bets"], row["n"], -row["max_pair"]) > (
                    best["effective_bets"], best["n"], -best["max_pair"]):
                best = row
    if not best:
        return {"status": "unavailable", "reason": "no_selection"}
    best["status"] = "above_target" if best["effective_bets"] >= T.CORE10_TARGET_EFFECTIVE_BETS else "below_target"
    best["target"] = T.CORE10_TARGET_EFFECTIVE_BETS
    best["note"] = "有限贪心估计，不是全局最优，也不是上界；只能说明这些参数下找到的最好组合，不能据此断言目标不可达。"
    return best


def core10_shadow_frontier(items: Sequence[Mapping[str, Any]],
                            production_codes: Sequence[str],
                            max_rounds: int = 40) -> dict[str, Any]:
    """在 core10 合格池上做相关性约束的单换局部搜索（影子，不写生产）。

    这是“前沿披露”，不是生产选样：目标是回答“同样约束下，是否存在比
    当前 core10 更独立的组合，代价是多少排序分”。不是全局最优，也不
    自动替换 screen_pick。
    """
    by = {x["fcode"]: x for x in items if x.get("series")}
    codes = sorted(by)
    if len(codes) < int(T.CORE10_QUOTA):
        return {"status": "unavailable", "reason": "pool_too_small",
                "pool_size": len(codes)}

    n = len(codes)
    idx = {c: i for i, c in enumerate(codes)}
    matrix: list[list[float | None]] = [[None] * n for _ in range(n)]
    for i, a in enumerate(codes):
        for j in range(i + 1, n):
            rho, _ = pair_corr(by[a]["series"], by[codes[j]]["series"],
                               min_obs=T.CORE10_MIN_COMMON_OBS)
            if rho is not None:
                matrix[i][j] = matrix[j][i] = float(rho)

    def score_of(code: str) -> float:
        x = by[code]
        return float((x.get("scores") or {}).get("rank_score")
                     or x.get("rank_score") or 0.0)

    def metrics(selection: Sequence[str]) -> tuple[float | None, float | None, float | None, int]:
        rhos = [matrix[idx[a]][idx[b]] for a, b in combinations(selection, 2)
                if matrix[idx[a]][idx[b]] is not None]
        if not rhos:
            return None, None, None, 0
        avg = sum(rhos) / len(rhos)
        eff = effective_bets(len(selection), avg)
        return (None if eff is None else round(eff, 4),
                round(max(rhos), 4), round(avg, 4), len(rhos))

    def company_share(selection: Sequence[str]) -> float | None:
        counts = {}
        for c in selection:
            company = by[c].get("company_name") or ""
            counts[company] = counts.get(company, 0) + 1
        return _share(max(counts.values()), len(selection)) if counts else None

    def valid(selection: Sequence[str], partial: bool = False) -> bool:
        if not selection:
            return True
        if len(set(selection)) != len(selection):
            return False
        if partial:
            if len(selection) > int(T.CORE10_QUOTA):
                return False
        elif len(selection) != int(T.CORE10_QUOTA):
            return False
        if len({by[c].get("family_id") for c in selection}) != len(selection):
            return False
        companies = {}
        styles = {}
        leveraged = 0
        for c in selection:
            company = by[c].get("company_name") or ""
            companies[company] = companies.get(company, 0) + 1
            style = by[c].get("style_code") or ""
            styles[style] = styles.get(style, 0) + 1
            cap = T.CORE10_STYLE_CAPS.get(style)
            if cap is not None and styles[style] > cap:
                return False
            if by[c].get("leverage"):
                leveraged += 1
        if any(v > T.COMPANY_CAP for v in companies.values()):
            return False
        # 部分组合也按“最多 6/10 只杠杆代理”约束，避免在只有 1-2 只时
        # 用比例误杀所有杠杆基金，导致贪心种子凑不满 10 只。
        return leveraged <= int(0.60 * int(T.CORE10_QUOTA) + 1e-9)

    def value(selection: Sequence[str], mode: str) -> tuple[float, ...]:
        eff, maxp, _avg, _pairs = metrics(selection)
        score = sum(score_of(c) for c in selection)
        if eff is None or maxp is None:
            return (-1.0, -score, 0.0, 0.0)
        if mode == "pair":
            return (-maxp, eff, score, 0.0)
        if mode == "eff":
            return (eff, -maxp, score, 0.0)
        feasible = eff >= T.CORE10_TARGET_EFFECTIVE_BETS and maxp <= 0.75
        return (1.0 if feasible else 0.0,
                score if feasible else -score,
                eff, -maxp)

    def greedy_seed() -> list[str]:
        picked: list[str] = []
        for c in sorted(codes, key=lambda x: (-score_of(x), x)):
            cand = picked + [c]
            if valid(cand, partial=True):
                picked = cand
            if len(picked) == int(T.CORE10_QUOTA):
                break
        return picked

    def improve(start: Sequence[str], mode: str) -> list[str]:
        current = list(start)
        if not valid(current):
            current = greedy_seed()
        if not valid(current):
            return current
        current_value = value(current, mode)
        for _ in range(max_rounds):
            best_value = current_value
            best_selection = None
            current_set = set(current)
            for out in sorted(current):
                base = [c for c in current if c != out]
                for inc in codes:
                    if inc in current_set:
                        continue
                    cand = base + [inc]
                    if not valid(cand):
                        continue
                    cand_value = value(cand, mode)
                    if cand_value > best_value:
                        best_value = cand_value
                        best_selection = cand
            if best_selection is None:
                break
            current, current_value = best_selection, best_value
        return current

    production = [c for c in production_codes if c in by]
    production_valid = valid(production)
    if not production_valid:
        production = greedy_seed()

    def row(mode: str, label: str, selection: Sequence[str]) -> dict[str, Any]:
        eff, maxp, avg, pairs = metrics(selection)
        score = round(sum(score_of(c) for c in selection), 2)
        lev = _share(sum(1 for c in selection if by[c].get("leverage")), len(selection))
        top = company_share(selection)
        checks = {
            "effective_bets": {"value": eff, "target": T.CORE10_TARGET_EFFECTIVE_BETS,
                               "pass": eff is not None and eff >= T.CORE10_TARGET_EFFECTIVE_BETS},
            "max_pair": {"value": maxp, "target": 0.75,
                         "pass": maxp is not None and maxp <= 0.75},
            "top_company_share": {"value": top, "target": 0.30,
                                  "pass": top is not None and top <= 0.30},
            "leveraged_share": {"value": lev, "target": 0.60,
                                "pass": (lev or 0) <= 0.60},
        }
        return {
            "mode": mode, "label": label,
            "codes": list(selection),
            "names": ["%s %s" % (c, by[c].get("name") or "") for c in selection],
            "n": len(selection),
            "effective_bets": eff, "max_pair": maxp, "avg_pairwise_corr": avg,
            "pair_count": pairs,
            "rank_score_sum": score,
            "leveraged_share": lev, "top_company_share": top,
            "checks": checks,
            "failed_checks": [k for k, v in checks.items() if not v["pass"]],
        }

    prod_row = row("production", "当前生产", production)
    prod_score = prod_row["rank_score_sum"]
    pair_codes = improve(production, "pair")
    eff_codes = improve(production, "eff")
    score_codes = improve(pair_codes, "score")
    rows = [prod_row,
            row("pair", "最小最大相关", pair_codes),
            row("eff", "最高有效 bet", eff_codes),
            row("score", "满足双目标最高分", score_codes)]
    for r in rows:
        r["score_delta_vs_production"] = round(r["rank_score_sum"] - prod_score, 2)
        r["added"] = sorted(set(r["codes"]) - set(production))
        r["removed"] = sorted(set(production) - set(r["codes"]))

    recommended = None
    for r in rows[1:]:
        if not r["failed_checks"]:
            if recommended is None or r["rank_score_sum"] > recommended["rank_score_sum"]:
                recommended = r
    return {
        "status": "ok",
        "pool_size": len(codes),
        "targets": {"effective_bets": T.CORE10_TARGET_EFFECTIVE_BETS,
                    "max_pair": 0.75, "leveraged_share": 0.60},
        "production_valid": production_valid,
        "name_map": {c: (by[c].get("name") or "") for c in codes},
        "production": prod_row,
        "frontier": rows[1:],
        "recommended": (recommended or {}).get("mode"),
        "note": ("确定性单换局部搜索，最多 %d 轮；不是全局最优，也不自动替换生产。"
                 "recommended 仅在有效 bet、最大相关、公司占比、杠杆占比全部通过时给出。"
                 % max_rounds),
    }


def quality_flags(item: Mapping[str, Any]) -> list[str]:
    flags = []
    if item.get("alpha_reliable") and (item.get("alpha_t") or 0) < 2.0:
        flags.append("alpha_not_significant")
    if item.get("alpha_annual_pct") is not None and item["alpha_annual_pct"] < 1.0:
        flags.append("alpha_below_1pct")
    if item.get("factor_r2") is not None and item["factor_r2"] < 0.15:
        flags.append("factor_r2_low")
    if (item.get("benchmark_contract") or {}).get("quality") in ("partial", "unmapped"):
        flags.append("benchmark_unmapped")
    return flags


def _month_ends(start: date, end: date) -> list[date]:
    """[start, end] 内的日历月末；用于滚动 12 个月窗口。"""
    out: list[date] = []
    y, m = start.year, start.month
    while True:
        last = calendar.monthrange(y, m)[1]
        d = date(y, m, last)
        if d > end:
            break
        if d >= start:
            out.append(d)
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out


def _index_series(conn, code: str, start: str, as_of: str) -> list[tuple[date, float]]:
    rows = conn.execute(
        "SELECT trade_date, close FROM market_index_daily"
        " WHERE index_code=? AND trade_date>=? AND trade_date<=?"
        " ORDER BY trade_date", (code, start, as_of)).fetchall()
    out = []
    for row in rows:
        try:
            out.append((date.fromisoformat(str(row["trade_date"])), float(row["close"])))
        except (TypeError, ValueError):
            continue
    return out


def rolling_excess_stats(conn, fcode: str, index_code: str, as_of: str,
                         years: int = 3,
                         window_days: int = QUALITY_ROLLING_WINDOW_DAYS) -> dict[str, Any]:
    """月度滚动 12 个月超额胜率（只读，不插补）。

    对最近 ``years`` 年的每个月末，取该日之前 365 天的基金日收益与指数收盘，
    计算基金累计收益减指数累计收益。窗口内基金收益少于 180 个观测、或指数
    缺锚点时跳过；至少 8 个有效窗口才给出胜率。该函数只回答“滚动一年里
    有多少个窗口跑赢”，不是统计显著性或未来收益保证。
    """
    end = date.fromisoformat(as_of)
    load_start = end - timedelta(days=years * 366 + window_days + 45)
    empty = {
        "status": "insufficient", "index_code": index_code,
        "n_windows": 0, "positive_windows": 0, "win_rate": None,
        "median_excess_pct": None, "worst_excess_pct": None,
        "best_excess_pct": None, "first_window_end": None, "last_window_end": None,
        "max_daily_move_pct": None, "max_daily_move_date": None,
    }
    # 3 年净值的新基金也应能算“最近可得的滚动窗口”，不能因为加载起点早于
    # 成立日就被 load_daily_returns 的 start_tolerance 整段拒绝。
    first = conn.execute(
        "SELECT MIN(nav_date) AS d FROM fund_nav_daily WHERE fcode=? AND nav_date<=?",
        (fcode, as_of)).fetchone()
    if not first or not first["d"]:
        return empty
    first_nav = date.fromisoformat(str(first["d"]))
    start = max(load_start, first_nav - timedelta(days=1))
    series = load_daily_returns(conn, fcode, start.isoformat(), as_of)
    idx = _index_series(conn, index_code, load_start.isoformat(), as_of)
    if not series or len(idx) < 2:
        return empty
    fund_dates = [date.fromisoformat(str(d)) for d, _r in series]
    fund_rets = [float(r) for _d, r in series]
    # 只检查最近 3 年的单日收益，避免把更早的份额折算/分红调整带进来。
    anomaly_start = end - timedelta(days=years * 365)
    recent = [(d, r) for d, r in zip(fund_dates, fund_rets) if d >= anomaly_start]
    max_move = max(recent, key=lambda x: abs(x[1])) if recent else None
    idx_dates = [d for d, _c in idx]
    idx_closes = [c for _d, c in idx]
    wins: list[float] = []
    window_ends: list[date] = []
    for month_end in _month_ends(end - timedelta(days=years * 365), end):
        pos = bisect_right(fund_dates, month_end) - 1
        if pos < 0:
            continue
        window_end = fund_dates[pos]
        window_start = window_end - timedelta(days=window_days)
        lo = bisect_right(fund_dates, window_start)
        rets = fund_rets[lo:pos + 1]
        if len(rets) < 180:
            continue
        cum = 1.0
        bad = False
        for r in rets:
            if r <= -1.0:
                bad = True
                break
            cum *= 1.0 + r
        if bad:
            continue
        i0 = bisect_right(idx_dates, window_start) - 1
        i1 = bisect_right(idx_dates, window_end) - 1
        if i0 < 0 or i1 <= i0 or idx_closes[i0] <= 0 or idx_closes[i1] <= 0:
            continue
        bench = idx_closes[i1] / idx_closes[i0] - 1.0
        wins.append(round((cum - 1.0 - bench) * 100.0, 4))
        window_ends.append(window_end)
    if not wins:
        return empty
    positive = sum(1 for x in wins if x > 0)
    return {
        "status": "ok" if len(wins) >= QUALITY_ROLLING_MIN_WINDOWS else "insufficient",
        "index_code": index_code,
        "n_windows": len(wins),
        "positive_windows": positive,
        "win_rate": round(positive / len(wins), 4),
        "median_excess_pct": _median(wins),
        "worst_excess_pct": round(min(wins), 4),
        "best_excess_pct": round(max(wins), 4),
        "first_window_end": window_ends[0].isoformat(),
        "last_window_end": window_ends[-1].isoformat(),
        "max_daily_move_pct": round(max_move[1] * 100.0, 4) if max_move else None,
        "max_daily_move_date": max_move[0].isoformat() if max_move else None,
    }


def classify_quality_tier(item: Mapping[str, Any], rolling: Mapping[str, Any] | None = None,
                          board: str | None = None) -> dict[str, Any]:
    """把单只基金分成 A/B/C/未评级（影子研究，不写生产）。

    * A：exact 合同基准、3 年净超额 >= 0、滚动 1 年胜率 >= 60%、
      MDD <= 3%（增强债 <= 5%）；
    * B：exact 3 年净超额 >= -0.5、滚动胜率 >= 50%、MDD <= 5%（增强 <= 8%）；
      proxy 基准只能在代理超额 >= 0.5 且滚动胜率 >= 50% 时进 B；
    * C：有足够基准但未达到 B；
    * 未评级：没有可映射基准、没有 3 年净值或无法计算。
    """
    contract = item.get("benchmark_contract") or {}
    quality = str(contract.get("quality") or "unmapped")
    enhanced = str(board or "") == T.BOARD_ENHANCED
    mdd_a = QUALITY_A_MDD_MAX_ENHANCED if enhanced else QUALITY_A_MDD_MAX
    mdd_b = QUALITY_B_MDD_MAX_ENHANCED if enhanced else QUALITY_B_MDD_MAX
    annual = item.get("annual_3y")
    if annual is None:
        return {"tier": "unrated", "reasons": ["no_3y_nav"],
                "excess_pct": None, "excess_kind": quality,
                "rolling_win_rate": None, "rolling_windows": 0, "mdd_3y": None}
    if quality == "exact":
        excess = item.get("contract_excess_3y_pct")
        excess_kind = "exact"
    elif quality == "proxy":
        excess = item.get("proxy_excess_3y_pct")
        excess_kind = "proxy"
    else:
        return {"tier": "unrated", "reasons": ["benchmark_not_exact_or_proxy"],
                "excess_pct": None, "excess_kind": quality,
                "rolling_win_rate": None, "rolling_windows": 0,
                "mdd_3y": item.get("mdd_3y")}
    if excess is None:
        return {"tier": "unrated", "reasons": ["benchmark_excess_missing"],
                "excess_pct": None, "excess_kind": excess_kind,
                "rolling_win_rate": None, "rolling_windows": 0,
                "mdd_3y": item.get("mdd_3y")}

    rolling = rolling or {}
    move = rolling.get("max_daily_move_pct")
    if move is not None and abs(float(move)) >= QUALITY_DATA_ANOMALY_MOVE_PCT:
        return {
            "tier": "unrated",
            "reasons": ["nav_jump_ge_%spct" % int(QUALITY_DATA_ANOMALY_MOVE_PCT)],
            "excess_pct": round(float(excess), 4),
            "excess_kind": excess_kind,
            "rolling_win_rate": rolling.get("win_rate"),
            "rolling_windows": int(rolling.get("n_windows") or 0),
            "mdd_3y": item.get("mdd_3y"),
            "max_daily_move_pct": round(float(move), 4),
            "max_daily_move_date": rolling.get("max_daily_move_date"),
        }
    nwin = int(rolling.get("n_windows") or 0)
    win = rolling.get("win_rate")
    mdd = item.get("mdd_3y")
    reasons: list[str] = []
    if nwin < QUALITY_ROLLING_MIN_WINDOWS:
        reasons.append("rolling_windows_insufficient")
    elif win is None:
        reasons.append("rolling_win_rate_missing")

    if excess_kind == "exact":
        if float(excess) < QUALITY_A_EXCESS_MIN:
            reasons.append("exact_excess_below_zero")
        if win is not None and nwin >= QUALITY_ROLLING_MIN_WINDOWS and float(win) < QUALITY_A_WIN_RATE_MIN:
            reasons.append("rolling_win_rate_below_a")
        if mdd is None:
            reasons.append("mdd_missing")
        elif float(mdd) > mdd_a:
            reasons.append("mdd_above_a_limit")
        if not reasons:
            tier = "A"
        elif (float(excess) >= QUALITY_B_EXCESS_MIN
              and nwin >= QUALITY_ROLLING_MIN_WINDOWS
              and win is not None and float(win) >= QUALITY_B_WIN_RATE_MIN
              and mdd is not None and float(mdd) <= mdd_b):
            tier = "B"
        else:
            tier = "C"
    else:
        if float(excess) < QUALITY_B_PROXY_EXCESS_MIN:
            reasons.append("proxy_excess_below_0.5")
        if win is not None and nwin >= QUALITY_ROLLING_MIN_WINDOWS and float(win) < QUALITY_B_WIN_RATE_MIN:
            reasons.append("rolling_win_rate_below_b")
        if mdd is None:
            reasons.append("mdd_missing")
        elif float(mdd) > mdd_b:
            reasons.append("mdd_above_b_limit")
        tier = "B" if not reasons else "C"
    return {
        "tier": tier,
        "reasons": list(dict.fromkeys(reasons)),
        "excess_pct": round(float(excess), 4),
        "excess_kind": excess_kind,
        "rolling_win_rate": win,
        "rolling_windows": nwin,
        "mdd_3y": mdd,
        "max_daily_move_pct": rolling.get("max_daily_move_pct"),
        "max_daily_move_date": rolling.get("max_daily_move_date"),
    }


def absolute_quality_tiers(conn, items: Sequence[Mapping[str, Any]], as_of: str) -> dict[str, Any]:
    """对去重后的基金做绝对质量分层，并汇总到榜单。

    只读；不修改 screen_pick。同一基金若出现在多个榜单，只计算一次滚动序列，
    但按榜单分别计数。返回的 items 按 A/B/C/未评级排序。
    """
    unique: dict[str, Mapping[str, Any]] = {}
    boards_by_code: dict[str, set[str]] = {}
    ranks_by_code: dict[str, dict[str, Any]] = {}
    for item in items:
        code = str(item.get("fcode") or "")
        if not code:
            continue
        unique.setdefault(code, item)
        board = str(item.get("board") or "")
        if board:
            boards_by_code.setdefault(code, set()).add(board)
            ranks_by_code.setdefault(code, {})[board] = item.get("rank")

    # 份额类别不是独立基金：同一 family 的 A/C/D 份额共享组合，只应算一个
    # “基金”口径。这里只做汇总口径，不改变逐份额分类结果。
    families: dict[str, str] = {}
    if conn is not None and unique:
        placeholders = ",".join("?" for _ in unique)
        try:
            for fcode, family_id in conn.execute(
                    "SELECT fcode, family_id FROM fund_share_class WHERE fcode IN (%s)" % placeholders,
                    tuple(unique)):
                families[str(fcode)] = str(family_id or fcode)
        except Exception:
            families = {}

    rows: list[dict[str, Any]] = []
    for code, item in unique.items():
        contract = item.get("benchmark_contract") or {}
        quality = str(contract.get("quality") or "unmapped")
        index_code = None
        if quality == "exact":
            index_code = "CBC_CFZS_00"
        elif quality == "proxy":
            comps = [c for c in (contract.get("components") or []) if c.get("index_code")]
            if comps:
                index_code = max(comps, key=lambda c: float(c.get("weight") or 0.0))["index_code"]
        rolling = (rolling_excess_stats(conn, code, index_code, as_of)
                   if index_code else {
                       "status": "unavailable", "index_code": None, "n_windows": 0,
                       "positive_windows": 0, "win_rate": None,
                       "median_excess_pct": None, "worst_excess_pct": None,
                       "best_excess_pct": None, "first_window_end": None,
                       "last_window_end": None})
        boards = sorted(boards_by_code.get(code) or [])
        # 同一基金同时在稳健/增强时，按更宽的增强阈值分类，避免把增强债误判为稳健。
        board = T.BOARD_ENHANCED if T.BOARD_ENHANCED in boards else (boards[0] if boards else "")
        tier = classify_quality_tier(item, rolling, board=board)
        rows.append({
            "fcode": code,
            "family_id": families.get(code, code),
            "name": item.get("name"),
            "rank": item.get("rank"),
            "ranks_by_board": ranks_by_code.get(code, {}),
            "boards": sorted(boards_by_code.get(code) or []),
            "tier": tier["tier"],
            "reasons": tier["reasons"],
            "benchmark_quality": quality,
            "excess_pct": tier["excess_pct"],
            "excess_kind": tier["excess_kind"],
            "rolling_win_rate": tier["rolling_win_rate"],
            "rolling_windows": tier["rolling_windows"],
            "mdd_3y": tier["mdd_3y"],
            "max_daily_move_pct": tier.get("max_daily_move_pct"),
            "max_daily_move_date": tier.get("max_daily_move_date"),
            "rolling": rolling,
        })

    order = {"A": 0, "B": 1, "C": 2, "unrated": 3}
    rows.sort(key=lambda x: (order.get(x["tier"], 9), -(x.get("excess_pct") or -999), x["fcode"]))
    by_code = {r["fcode"]: r for r in rows}
    by_board: dict[str, dict[str, Any]] = {}
    for item in items:
        board = str(item.get("board") or "")
        code = str(item.get("fcode") or "")
        if not board or not code:
            continue
        b = by_board.setdefault(board, {
            "n": 0, "A": 0, "B": 0, "C": 0, "unrated": 0,
            "exact_n": 0, "proxy_n": 0, "rolling_n": 0,
            "excess": [],
        })
        b["n"] += 1
        row = by_code.get(code)
        if not row:
            b["unrated"] += 1
            continue
        b[row["tier"]] = b.get(row["tier"], 0) + 1
        if row["benchmark_quality"] == "exact":
            b["exact_n"] += 1
        elif row["benchmark_quality"] == "proxy":
            b["proxy_n"] += 1
        if row["rolling_windows"] >= QUALITY_ROLLING_MIN_WINDOWS:
            b["rolling_n"] += 1
        if row["excess_pct"] is not None:
            b["excess"].append(row["excess_pct"])
    for b in by_board.values():
        b["median_excess_pct"] = _median(b.pop("excess"))

    counts = Counter(r["tier"] for r in rows)
    data_anomaly_n = sum(1 for r in rows if any(str(x).startswith("nav_jump_") for x in r["reasons"]))
    # family 口径：同一基金的不同份额只保留“最好”的层级；A 级不会因为
    # A/C 两个份额同时入榜而被重复计数。
    family_best: dict[str, dict[str, Any]] = {}
    for row in rows:
        fam = row["family_id"]
        prev = family_best.get(fam)
        key = (order.get(row["tier"], 9), -(row.get("excess_pct") if row.get("excess_pct") is not None else -999))
        if prev is None:
            family_best[fam] = row
            continue
        prev_key = (order.get(prev["tier"], 9),
                    -(prev.get("excess_pct") if prev.get("excess_pct") is not None else -999))
        if key < prev_key:
            family_best[fam] = row
    family_counts = Counter(r["tier"] for r in family_best.values())
    return {
        "version": QUALITY_TIER_VERSION,
        "as_of": as_of,
        "thresholds": {
            "a_excess_min_pct": QUALITY_A_EXCESS_MIN,
            "a_win_rate_min": QUALITY_A_WIN_RATE_MIN,
            "a_mdd_max_pct": QUALITY_A_MDD_MAX,
            "a_mdd_max_enhanced_pct": QUALITY_A_MDD_MAX_ENHANCED,
            "b_excess_min_pct": QUALITY_B_EXCESS_MIN,
            "b_win_rate_min": QUALITY_B_WIN_RATE_MIN,
            "b_mdd_max_pct": QUALITY_B_MDD_MAX,
            "b_mdd_max_enhanced_pct": QUALITY_B_MDD_MAX_ENHANCED,
            "b_proxy_excess_min_pct": QUALITY_B_PROXY_EXCESS_MIN,
            "rolling_min_windows": QUALITY_ROLLING_MIN_WINDOWS,
            "rolling_window_days": QUALITY_ROLLING_WINDOW_DAYS,
        },
        "summary": {
            "n": len(rows), "A": counts.get("A", 0), "B": counts.get("B", 0),
            "C": counts.get("C", 0), "unrated": counts.get("unrated", 0),
            "family_n": len(family_best),
            "family_A": family_counts.get("A", 0),
            "family_B": family_counts.get("B", 0),
            "family_C": family_counts.get("C", 0),
            "family_unrated": family_counts.get("unrated", 0),
            "data_anomaly_n": data_anomaly_n,
            "exact_n": sum(1 for r in rows if r["benchmark_quality"] == "exact"),
            "proxy_n": sum(1 for r in rows if r["benchmark_quality"] == "proxy"),
            "rolling_n": sum(1 for r in rows if r["rolling_windows"] >= QUALITY_ROLLING_MIN_WINDOWS),
        },
        "by_board": by_board,
        "items": rows,
    }


def render_quality_tier_section(result: Mapping[str, Any]) -> list[str]:
    """报告 6.1 节：绝对质量分层。只读披露，不改变生产榜单。"""
    tiers = result.get("quality_tiers") or {}
    if not tiers:
        return []
    lines = ["## 6.1 绝对质量分层（只读影子，不改变榜单）", ""]
    lines.append("分层回答的是“在当前数据里有没有绝对超额和一致性”，不是“未来会不会继续涨”。"
                 "A 级要求 exact 合同基准、3 年净超额≥0、月度滚动 1 年胜率≥60%（至少 8 个窗口）、"
                 "MDD≤3%（增强债≤5%）；B 级要求 exact 净超额≥-0.5、胜率≥50%、MDD≤5%（增强≤8%）。"
                 "proxy 基准只能进 B/C；没有可映射基准或 3 年净值的标“未评级”；"
                 "最近 3 年单日净值涨跌≥10% 的样本按一次性净值异动标“未评级”，不把份额折算、"
                 "数据源错误、未调整公司行为或大额赎回费一次性计入当成可持续超额。")
    lines.append("")
    lines.append("| 榜单 | 只数 | A | B | C | 未评级 | exact | proxy | 滚动窗口覆盖 | 超额中位% |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    board_labels = {T.BOARD_STABLE: "稳健", T.BOARD_ENHANCED: "增强", T.BOARD_CORE: "核心50",
                    T.BOARD_CORE10: "核心10", T.BOARD_LONG: "长期20",
                    T.BOARD_WATCH: "观察", T.BOARD_LOCKED: "锁定", T.BOARD_ROOKIE: "新锐"}
    for board, label in board_labels.items():
        b = (tiers.get("by_board") or {}).get(board) or {}
        if not b:
            continue
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                     % (label, b.get("n", 0), b.get("A", 0), b.get("B", 0),
                        b.get("C", 0), b.get("unrated", 0), b.get("exact_n", 0),
                        b.get("proxy_n", 0), b.get("rolling_n", 0),
                        _fmt(b.get("median_excess_pct"), 2)))
    lines.append("")
    a_rows = [x for x in (tiers.get("items") or []) if x.get("tier") == "A"]
    b_rows = [x for x in (tiers.get("items") or []) if x.get("tier") == "B"]
    a_families = {x.get("family_id") or x["fcode"] for x in a_rows}
    if a_rows:
        lines.append("A 级样本（份额类别 %s 只 / family %s 个）：%s。"
                     % (len(a_rows), len(a_families), "；".join(
                         "%s %s（%s，超额 %s%%，滚动胜率 %s）"
                         % (x["fcode"], x.get("name") or "",
                            "/".join(board_labels.get(b, b) for b in (x.get("boards") or [])), _fmt(x.get("excess_pct"), 2),
                            _fmt(100 * (x.get("rolling_win_rate") or 0), 0))
                         for x in a_rows[:12])))
        if len(a_families) < len(a_rows):
            lines.append("同一基金的不同份额（A/C/D）只算一个 family；A 级名单按份额列出，"
                         "不能把同一基金的多个份额当成多只独立产品。")
    else:
        lines.append("本次没有 A 级样本：在当前 exact 合同样本里，没有同时满足正超额、滚动胜率和回撤上限的基金。")
    lines.append("")
    if b_rows:
        lines.append("B 级 %s 只；其中 proxy 基准 %s 只。B 级只代表“未达到 A，但比 C 更接近绝对质量”，"
                     "不能当作买入建议。"
                     % (len(b_rows), sum(1 for x in b_rows if x.get("excess_kind") == "proxy")))
        lines.append("")
    summary = tiers.get("summary") or {}
    pick_n = sum(int((b or {}).get("n") or 0) for b in (tiers.get("by_board") or {}).values())
    lines.append("上表按 pick 计（8 个榜合计 %s 条）；按代码去重后 %s 只（含观察/锁定/新锐；"
                 "按 family 去重 %s 个）：C 级 %s 只、未评级 %s 只。"
                 "C 级不自动剔除，未评级也不等于差；它们只说明当前证据不足以支持“绝对优质”的判断。"
                 "该分层不写 screen_pick、不参与 gate；若要升级为发布门禁，必须先做前瞻 cohort 与 OOS 验证。"
                 % (pick_n, summary.get("n", 0), summary.get("family_n", summary.get("n", 0)),
                    summary.get("C", 0), summary.get("unrated", 0)))
    if summary.get("data_anomaly_n"):
        lines.append("未评级中有 %s 只最近 3 年出现单日净值涨跌≥10%%：这通常是份额折算、"
                     "数据源错误、未调整的公司行为，或大额赎回费一次性计入基金财产；"
                     "不能把由此产生的高收益当成可持续超额。"
                     % summary["data_anomaly_n"])
        anomaly_rows = [x for x in (tiers.get("items") or [])
                        if any(str(r).startswith("nav_jump_") for r in (x.get("reasons") or []))]
        if anomaly_rows:
            lines.append("触发样本：%s。" % "；".join(
                "%s %s（%s，单日 %s%%，%s）"
                % (x["fcode"], x.get("name") or "",
                   "/".join(board_labels.get(b, b) for b in (x.get("boards") or [])),
                   _fmt(x.get("max_daily_move_pct"), 2), x.get("max_daily_move_date") or "日期未知")
                for x in anomaly_rows[:8]))
            live = [x for x in anomaly_rows if set(x.get("boards") or []) &
                    {T.BOARD_STABLE, T.BOARD_ENHANCED, T.BOARD_CORE, T.BOARD_LONG}]
            if live:
                lines.append("其中 %s 只仍出现在发布榜单：%s；说明生产筛选尚未对一次性净值异动做调整，"
                             "本影子分层不会自动改榜，生产门禁需另行审批。"
                             % (len(live), "；".join(
                                 "%s %s（%s）"
                                 % (x["fcode"], x.get("name") or "",
                                    "、".join(
                                        "%s第%s" % (board_labels.get(b, b),
                                                     (x.get("ranks_by_board") or {}).get(b) or "?")
                                        for b in (x.get("boards") or [])
                                        if b in {T.BOARD_STABLE, T.BOARD_ENHANCED,
                                                 T.BOARD_CORE, T.BOARD_LONG}))
                                 for x in live[:8])))
    lines.append("")
    return lines


def render_quality_sections(result: Mapping[str, Any]) -> list[str]:
    q = result.get("quality") or {}
    if not q:
        return []
    lines: list[str] = []
    lines.append("## 6. 合同基准映射：能算什么、不能算什么")
    lines.append("")
    lines.append("项目只采集了 4 个指数：CBC_CFZS_00（中债-新综合财富，总回报）、"
                 "000012（国债，价格）、000013（企债，价格）、000832（中证转债，价格）。"
                 "短端、1-3 年、1-5 年、信用分层等合同基准没有对应指数，不能伪造精确超额。")
    lines.append("")
    lines.append("| 榜单 | 可映射 | 精确 | 代理 | 仅存款 | 部分 | 未映射 | 期限分布 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    board_labels = {T.BOARD_STABLE: "稳健", T.BOARD_ENHANCED: "增强", T.BOARD_CORE: "核心50",
                    T.BOARD_CORE10: "核心10", T.BOARD_LONG: "长期20",
                    T.BOARD_WATCH: "观察", T.BOARD_LOCKED: "锁定", T.BOARD_ROOKIE: "新锐"}
    for board, label in board_labels.items():
        b = (result.get("boards") or {}).get(board) or {}
        if not b:
            continue
        mat = b.get("benchmark_maturity") or {}
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |"
                     % (label, b.get("benchmark_mapped", 0), b.get("benchmark_exact", 0),
                        b.get("benchmark_proxy", 0), b.get("benchmark_cash_only", 0),
                        b.get("benchmark_partial", 0), b.get("benchmark_unmapped", 0),
                        "、".join("%s=%s" % (MATURITY_LABELS.get(k, k), v)
                                 for k, v in mat.items() if v)))
    lines.append("")
    exact_bits = []
    for board, label in board_labels.items():
        b = (result.get("boards") or {}).get(board) or {}
        n = b.get("contract_excess_3y_n") or 0
        if not n:
            continue
        gross_n = b.get("contract_excess_3y_gross_n") or 0
        if gross_n:
            exact_bits.append(
                "%s 扣费后 %s%%（正 %s%%，n=%s）/加回持续费率后 %s%%（正 %s%%，n=%s）"
                % (label, _fmt(b.get("contract_excess_3y_median_pct"), 2),
                   _fmt(100 * (b.get("contract_excess_3y_positive_share") or 0), 1), n,
                   _fmt(b.get("contract_excess_3y_gross_median_pct"), 2),
                   _fmt(100 * (b.get("contract_excess_3y_gross_positive_share") or 0), 1),
                   gross_n))
        else:
            exact_bits.append("%s %s%%（n=%s，正超额 %s%%）"
                              % (label, _fmt(b.get("contract_excess_3y_median_pct"), 2), n,
                                 _fmt(100 * (b.get("contract_excess_3y_positive_share") or 0), 1)))
    if exact_bits:
        lines.append("精确合同样本的 3 年年化超额（基金净值−CBC 总回报）中位：" + "；".join(exact_bits) + "。"
                     "加回口径只加回管理+托管+销售服务费（持续计提），不含申购/赎回等一次性费用；"
                     "只覆盖 exact 子样本，不能外推到全榜。")
        lines.append("")
        fees = [(label, (result.get("boards") or {}).get(board) or {})
                for board, label in board_labels.items()]
        fee_txt = "；".join("%s %s%%" % (label, _fmt(b.get("median_recurring_fee_pct"), 2))
                            for label, b in fees if b.get("median_recurring_fee_pct") is not None)
        if fee_txt:
            lines.append("各榜持续计提费率中位：" + fee_txt + "。费率只能解释一部分负超额；"
                         "加回后仍为负的榜，不能把“扣费前看起来不错”当作已实现超额。")
            lines.append("")
    lines.append("结论：在当前 API 数据范围内，只有“综合债、未限定期限”的合同基准可以精确对照 CBC；"
                 "利率/信用/转债及带期限的基准只能做方向代理。任何“合同超额”列必须同时标注 exact/proxy，"
                 "否则会拿价格指数或短端错配当业绩证据。精确样本的 3 年超额中位为负，"
                 "说明候选池在扣费后没有稳定跑赢 CBC 总回报；不能把“入榜”读成“已实现绝对超额”。")
    lines.append("")
    lines.extend(render_quality_tier_section(result))
    lines.append("## 7. 因子 alpha：扣除 beta 后的残差（含 carry，仅参考）")
    lines.append("")
    lines.append("alpha 是 730 天三因子（国债/企债/转债）回归的年化截距，已扣除久期、信用、转债暴露。"
                 "注意：三个因子都是价格指数，基金净值含票息，因此 alpha 主要反映 carry 与选券，"
                 "不能当作纯经理技能或绝对质量通过率。参考线：alpha ≥ 1.0%、t ≥ 2.0、r2 ≥ 0.15；"
                 "只用于同风格内分层，不用于发布门禁。")
    lines.append("")
    lines.append("| 榜单 | alpha覆盖 | 可信 | alpha中位% | 正值占比 | t≥2占比 | r2中位 | 过参考线 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for board, label in board_labels.items():
        b = (result.get("boards") or {}).get(board) or {}
        if not b:
            continue
        lines.append("| %s | %s/%s | %s | %s | %s%% | %s%% | %s | %s/%s |"
                     % (label, b.get("alpha_covered", 0), b.get("n", 0), b.get("alpha_reliable", 0),
                        _fmt(b.get("alpha_median_pct"), 2),
                        _fmt(100 * (b.get("alpha_positive_share") or 0), 1),
                        _fmt(100 * (b.get("alpha_significant_share") or 0), 1),
                        _fmt(b.get("alpha_r2_median"), 3),
                        b.get("alpha_reference_pass", 0), b.get("alpha_reference_eligible", 0)))
    lines.append("")
    cq = q.get("core10") or {}
    bad = [x for x in (cq.get("rows") or []) if x.get("flags")]
    if bad:
        lines.append("核心10 中未过 alpha 参考线或基准不可映射的：" +
                     "；".join("%s %s（%s）" % (x["fcode"], x.get("name") or "",
                                                "、".join(x["flags"])) for x in bad))
        lines.append("")
    lines.append("## 8. 组合独立性：core10 离 1.8 有多远")
    lines.append("")
    lines.append("当前核心10：有效 bet %s（目标 %s），平均相关 %s，最大相关对 %s，"
                 "单一公司最高 %s（%s），杠杆代理占比 %s。"
                 % (_fmt(cq.get("effective_bets"), 3), _fmt((cq.get("checks") or {}).get("effective_bets", {}).get("target"), 2),
                    _fmt(cq.get("avg_pairwise_corr"), 3),
                    _fmt((cq.get("max_pair") or {}).get("corr"), 3),
                    ((cq.get("top_company") or {}).get("name") or "—"),
                    _fmt((cq.get("top_company") or {}).get("share"), 2),
                    _fmt((cq.get("checks") or {}).get("leveraged_share", {}).get("value"), 2)))
    lines.append("")
    lines.append("| 检查 | 当前值 | 影子目标 | 结果 |")
    lines.append("|---|---:|---:|---|")
    for key, label in (("effective_bets", "有效独立 bet"), ("max_pair", "最大相关对"),
                       ("top_company_share", "单公司最高占比"), ("leveraged_share", "杠杆代理占比")):
        c = (cq.get("checks") or {}).get(key) or {}
        lines.append("| %s | %s | %s | %s |"
                     % (label, _fmt(c.get("value"), 3), _fmt(c.get("target"), 3),
                        "通过" if c.get("pass") else "未通过"))
    lines.append("")
    ceiling = q.get("core10_ceiling") or {}
    greedy = ceiling.get("effective_bets")
    current_eff = cq.get("effective_bets")
    if greedy is not None:
        if current_eff is not None and current_eff >= greedy:
            relation = ("当前 core10 的有效 bet 为 %s，高于这次贪心找到的 %s；"
                        "贪心不是上界，不能据此断言 1.8 不可达。" %
                        (_fmt(current_eff, 3), _fmt(greedy, 3)))
        else:
            relation = ("当前 core10 的有效 bet 为 %s，低于这次贪心找到的 %s；"
                        "候选池可能还有提升空间，但贪心不是全局最优。" %
                        (_fmt(current_eff, 3), _fmt(greedy, 3)))
        failed = cq.get("failed_checks") or []
        labels = {"effective_bets": "有效独立 bet", "max_pair": "最大相关对",
                  "top_company_share": "单公司最高占比", "leveraged_share": "杠杆代理占比"}
        failed_text = "、".join(labels.get(x, x) for x in failed) or "无"
        lines.append("有限贪心探测：在核心 50 内用 20 组参数搜索，最高找到有效 bet %s"
                     "（最大相关 %s，%s 只）。%s当前未通过的检查：%s。"
                     "影子目标 1.8 只能作为披露/分级目标，不能单独硬判。"
                     % (_fmt(greedy, 3), _fmt(ceiling.get("max_pair"), 3),
                        ceiling.get("n"), relation, failed_text))
        lines.append("")
    frontier = q.get("core10_frontier") or {}
    if frontier.get("status") == "ok":
        rows = [frontier.get("production") or {}] + list(frontier.get("frontier") or [])
        names = frontier.get("name_map") or {}
        lines.append("### 8.1 相关性约束影子前沿（不写生产）")
        lines.append("")
        lines.append("在 core10 合格池（%s 只）上做确定性单换局部搜索；约束为 family 唯一、"
                     "公司≤%s、风格限额、杠杆代理≤60%%，目标为有效 bet≥%s 且最大相关≤0.75。"
                     % (frontier.get("pool_size"), T.COMPANY_CAP,
                        _fmt((frontier.get("targets") or {}).get("effective_bets"), 2)))
        lines.append("")
        lines.append("| 方案 | 有效bet | 最大相关 | 排序分 | 相对生产 | 杠杆 | 未过检查 | 增/删 |")
        lines.append("|---|---:|---:|---:|---:|---:|---|---|")
        for r in rows:
            failed = r.get("failed_checks") or []
            labels = {"effective_bets": "有效bet", "max_pair": "最大相关",
                      "top_company_share": "公司占比", "leveraged_share": "杠杆"}
            failed_txt = "、".join(labels.get(x, x) for x in failed) or "无"
            delta = r.get("score_delta_vs_production")
            delta_txt = "—" if r.get("mode") == "production" else ("%+.2f" % delta if delta is not None else "—")
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s/%s |"
                         % (r.get("label"), _fmt(r.get("effective_bets"), 3),
                            _fmt(r.get("max_pair"), 3), _fmt(r.get("rank_score_sum"), 2),
                            delta_txt, _fmt(r.get("leveraged_share"), 2), failed_txt,
                            len(r.get("added") or []), len(r.get("removed") or [])))
        lines.append("")
        rec_mode = frontier.get("recommended")
        rec = next((r for r in rows if r.get("mode") == rec_mode), None)
        if rec:
            add = "、".join("%s %s" % (c, names.get(c, "")) for c in (rec.get("added") or [])) or "无"
            rem = "、".join("%s %s" % (c, names.get(c, "")) for c in (rec.get("removed") or [])) or "无"
            lines.append("满足全部影子检查且排序分最高的方案是“%s”：有效 bet %s、最大相关 %s、"
                         "排序分 %s（相对生产 %+.2f）。新增：%s；剔除：%s。"
                         % (rec.get("label"), _fmt(rec.get("effective_bets"), 3),
                            _fmt(rec.get("max_pair"), 3), _fmt(rec.get("rank_score_sum"), 2),
                            rec.get("score_delta_vs_production") or 0.0, add, rem))
            lines.append("")
            lines.append("这只是“同样约束下可达到的前沿”，不是全局最优，也不自动替换生产 core10；"
                         "排序分下降说明独立性提升有机会成本，是否接受要由产品规则决定。")
        else:
            lines.append("在本次单换搜索中没有找到同时满足全部影子检查的方案；"
                         "前沿行只用于说明取舍，不能据此宣称目标可达或不可达。")
        lines.append("")
    lines.append("## 9. 质量结论（基于第 2–8 节）")
    lines.append("")
    lines.append("增强债 3 年风险的覆盖、P90 与尾部样本见第 3 节，此处不重复。")
    lines.append("")
    failed = cq.get("failed_checks") or []
    labels = {"effective_bets": "有效独立 bet", "max_pair": "最大相关对",
              "top_company_share": "单公司最高占比", "leveraged_share": "杠杆代理占比"}
    failed_text = "、".join(labels.get(x, x) for x in failed) or "无"
    lines.append("质量结论：alpha 参考线在稳健/核心50 上覆盖良好，但 alpha 含 carry，不能读成质量通过率；"
                 "增强债约一半 t<2，残差稳定性更弱。core10 个体 alpha 大多过参考线，"
                 "组合独立性当前未通过的是 %s。1.8 只能作为披露/分级目标，不能单独硬判。"
                 "这些结果只用于报告披露，不改变生产榜单。" % failed_text)
    lines.append("")
    return lines


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return ("%%.%df" % digits) % float(value)
    except (TypeError, ValueError):
        return str(value)
