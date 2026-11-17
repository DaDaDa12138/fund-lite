"""风格判定：否定优先 + 正向证据 + 置信等级。

优先级（证据优先于类型标签）：
  转债证据(名称/zqcc 可转债占比>=20%) > 债券型-混合二级 > 债券型-混合一级 >
  中短债 > 固收指数 > 利率债 > 信用债 > 现金增强 > 综合纯债
理由：东财把可转债基金统一归入「债券型-混合二级」（实测 86 只名称含"转债"全在该类型），
     若先按类型判二级增强，会把转债基金错并入二级增强组。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .. import db

CLASSIFIER_VERSION = "style-2026.11.2"

CONVERTIBLE_CODE_PREFIXES = ("110", "111", "113", "118", "123", "127", "128", "132")
CONVERTIBLE_RATIO_THRESHOLD = 0.20

RATE_NAME_KEYS = ("国债", "国开", "进出口", "农发", "口行", "政策性金融", "地方政府债", "政金债",
                  "央行票据", "利率债")
CREDIT_NAME_KEYS = ("信用债", "产业债", "城投", "二级资本债", "永续债", "中期票据", "短期融资券",
                    "超短期融资券", "公司债", "企业债", "金融债")
SHORT_NAME_KEYS = ("中短债", "短债", "超短债")
INDEX_NAME_KEYS = ("债券指数", "政金债指数", "国债指数", "信用债指数", "综合债指数")
CASH_NAME_KEYS = ("货币增强", "现金增利", "现金增强")
CONV_NAME_KEYS = ("转债", "可转换")


def classify_bond_name(bond_name: str | None, bond_code: str | None) -> str:
    """按名称关键词优先、代码前缀兜底，把单只债券归入 convertible|rate|credit|other。"""
    name = bond_name or ""
    code = (bond_code or "").strip()
    if any(k in name for k in ("转债", "可转换", "可交换")):
        return "convertible"
    if code[:3] in CONVERTIBLE_CODE_PREFIXES:
        return "convertible"
    if any(k in name for k in RATE_NAME_KEYS):
        return "rate"
    if any(k in name for k in CREDIT_NAME_KEYS):
        return "credit"
    return "other"


BENCHMARK_SHORT_KEYS = ("1年以下", "0-1年", "0至1年", "中短债", "短债")
BENCHMARK_CONV_KEYS = ("可转债", "可交换债")
BENCHMARK_CREDIT_KEYS = ("信用债", "企业债", "公司债", "城投", "中票", "短融", "金融债",
                         "二级资本债", "永续债")
BENCHMARK_RATE_KEYS = ("国债", "国开", "政策性金融", "利率债", "地方政府债", "政金债")


def classify_benchmark(benchmark_text: str | None) -> str | None:
    """按业绩比较基准的权重/关键词判断合同风格，只返回 short/convertible/credit/rate。"""
    text = benchmark_text or ""
    if not text:
        return None
    weights: dict[str, float] = {}
    for part in re.split(r"[+＋]", text):
        part = part.strip()
        if not part:
            continue
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", part)
        weight = float(m.group(1)) if m else 0.0
        cat = None
        if any(k in part for k in BENCHMARK_SHORT_KEYS):
            cat = "short"
        elif any(k in part for k in BENCHMARK_CONV_KEYS):
            cat = "convertible"
        elif any(k in part for k in BENCHMARK_CREDIT_KEYS):
            cat = "credit"
        elif any(k in part for k in BENCHMARK_RATE_KEYS):
            cat = "rate"
        if cat:
            weights[cat] = weights.get(cat, 0.0) + weight
    if weights:
        cat, weight = max(weights.items(), key=lambda kv: kv[1])
        if weight >= 50.0:
            return cat
    # 无权重或权重不明确时，只在出现强合同词时给标签；综合指数不强行归类。
    if any(k in text for k in BENCHMARK_SHORT_KEYS):
        return "short"
    if "可转债" in text and "信用债" not in text and "国债" not in text:
        return "convertible"
    if any(k in text for k in BENCHMARK_CREDIT_KEYS) and "综合" not in text:
        return "credit"
    if any(k in text for k in BENCHMARK_RATE_KEYS) and "综合" not in text:
        return "rate"
    return None


def classify(name: str, type_text: str | None, holdings: list[dict[str, Any]],
             benchmark_text: str | None = None) -> dict[str, Any]:
    name = name or ""
    type_text = type_text or ""
    total_pct = sum(h["pct_nav"] or 0.0 for h in holdings)
    conv_pct = sum(h["pct_nav"] or 0.0 for h in holdings if h["bond_class"] == "convertible")
    rate_pct = sum(h["pct_nav"] or 0.0 for h in holdings if h["bond_class"] == "rate")
    credit_pct = sum(h["pct_nav"] or 0.0 for h in holdings if h["bond_class"] == "credit")
    conv_ratio = (conv_pct / total_pct) if total_pct > 0 else None
    bench_style = classify_benchmark(benchmark_text)
    evidence: dict[str, Any] = {
        "type_text": type_text,
        "name_convertible": any(k in name for k in CONV_NAME_KEYS),
        "benchmark_text": benchmark_text,
        "benchmark_style": bench_style,
        "holding_total_pct": round(total_pct, 4),
        "holding_convertible_pct": round(conv_pct, 4),
        "holding_rate_pct": round(rate_pct, 4),
        "holding_credit_pct": round(credit_pct, 4),
        "convertible_ratio": round(conv_ratio, 4) if conv_ratio is not None else None,
        "threshold": CONVERTIBLE_RATIO_THRESHOLD,
        "classifier_version": CLASSIFIER_VERSION,
    }
    conv_by_holding = conv_ratio is not None and conv_ratio >= CONVERTIBLE_RATIO_THRESHOLD
    conv_by_name = evidence["name_convertible"]

    # 1) 名称明确是转债基金：最高优先级（东财把转债基金归入混合二级）
    if conv_by_name:
        evidence["rule"] = "convertible_by_name"
        return {"style_code": "bond_convertible", "confidence": "text_heuristic", "evidence": evidence}

    # 2) 固收指数：指数基金的合同基准通常指向标的指数，不能把“1年以下指数”误判成短债主动基金
    if type_text.startswith("指数型") or any(k in name for k in INDEX_NAME_KEYS):
        evidence["rule"] = "index"
        return {"style_code": "bond_index", "confidence": "structured_confirmed", "evidence": evidence}

    # 3) 合同基准明确是短端（如“中债综合财富(1年以下)指数*90%”）：优先于混合一级/二级类型标签
    if bench_style == "short":
        evidence["rule"] = "benchmark_short"
        return {"style_code": "bond_short", "confidence": "structured_confirmed", "evidence": evidence}

    # 4) 持仓转债证据：强证据（转债占净值>=30% 或占债券持仓>=50%）可直接定转债；
    #    只有弱证据（20%-50%）且合同基准没有给出更明确风格时，才按转债处理。
    if conv_by_holding and (conv_pct >= 30.0 or conv_ratio >= 0.50 or bench_style in (None, "convertible")):
        conf = "multi_field_consistent" if (conv_by_holding and conv_by_name) else "structured_confirmed"
        evidence["rule"] = "convertible_by_holding"
        return {"style_code": "bond_convertible", "confidence": conf, "evidence": evidence}

    # 5) 合同基准明确是转债指数
    if bench_style == "convertible":
        evidence["rule"] = "benchmark_convertible"
        return {"style_code": "bond_convertible", "confidence": "structured_confirmed", "evidence": evidence}

    # 6) 合同基准明确是信用/利率：覆盖“混合一级”等法律类型标签（如 80% 高等级信用债 + 5% 转债）
    if bench_style in ("credit", "rate"):
        evidence["rule"] = "benchmark_" + bench_style
        return {"style_code": "bond_" + bench_style, "confidence": "structured_confirmed",
                "evidence": evidence}

    # 7) 名称中短债
    if any(k in name for k in SHORT_NAME_KEYS):
        evidence["rule"] = "name_short"
        return {"style_code": "bond_short", "confidence": "text_heuristic", "evidence": evidence}

    # 8) 类型标签：混合二级 / 混合一级
    if "混合二级" in type_text or "二级债" in name:
        evidence["rule"] = "type_secondary"
        return {"style_code": "bond_secondary_enhanced", "confidence": "structured_confirmed",
                "evidence": evidence}
    if "混合一级" in type_text or "一级债" in name:
        evidence["rule"] = "type_primary"
        return {"style_code": "bond_primary_enhanced", "confidence": "structured_confirmed",
                "evidence": evidence}

    # 9) 剩余弱转债持仓（无合同基准时）
    if conv_by_holding:
        evidence["rule"] = "convertible_by_holding"
        return {"style_code": "bond_convertible", "confidence": "structured_confirmed",
                "evidence": evidence}

    # 5) 利率债
    if any(k in name for k in RATE_NAME_KEYS):
        conf = "multi_field_consistent" if rate_pct > credit_pct else "text_heuristic"
        evidence["rule"] = "name_rate"
        return {"style_code": "bond_rate", "confidence": conf, "evidence": evidence}

    # 6) 信用债
    if any(k in name for k in CREDIT_NAME_KEYS):
        conf = "multi_field_consistent" if credit_pct >= rate_pct else "text_heuristic"
        evidence["rule"] = "name_credit"
        return {"style_code": "bond_credit", "confidence": conf, "evidence": evidence}

    # 7) 现金增强
    if any(k in name for k in CASH_NAME_KEYS):
        evidence["rule"] = "name_cash"
        return {"style_code": "bond_cash_enhanced", "confidence": "text_heuristic",
                "evidence": evidence}

    # 8) 兜底：综合纯债
    evidence["rule"] = "default_pure"
    conf = "multi_field_consistent" if type_text.startswith("债券型") else "unknown"
    return {"style_code": "bond_pure", "confidence": conf, "evidence": evidence}


def run_style(conn, business_date: str, holdings_min_coverage: float = 0.80) -> dict[str, Any]:
    funds = conn.execute("SELECT fcode, name, type_text FROM fund WHERE last_seen_date=? AND"
                         " identity_status='ok'", (business_date,)).fetchall()
    benchmarks = {r["fcode"]: r["benchmark_text"] for r in conn.execute(
        "SELECT fcode, benchmark_text FROM fund_profile")}
    periods = {r["code"]: r["report_period"] for r in conn.execute(
        "SELECT code, MAX(report_period) AS report_period FROM src_zqcc_holding GROUP BY code")}
    # 覆盖率闸门：半覆盖时“同一类基金一半按持仓、一半按名称”判定，标签不可比。
    # 低于阈值就整体不用持仓证据，让所有基金处在同一证据层级（宁可不升级，也不混用）。
    # 分子/分母必须是同一期同一域：分子只算最新报告期且有持仓的当日可申购基金，
    # 分母是当轮采集范围。旧实现用全历史持仓数除以当日可申购数，覆盖率可以 >100%，
    # 80% 闸门等于永远打开（实测 7928/6065=131%）。
    latest_period = conn.execute(
        "SELECT MAX(report_period) AS p FROM src_zqcc_holding").fetchone()["p"]
    scope_codes = {r["fcode"] for r in conn.execute(
        "SELECT fcode FROM fund_buyable_daily WHERE buyable_state='confirmed_yes'"
        " AND business_date=(SELECT MAX(business_date) FROM fund_buyable_daily)")}
    fund_codes = {f["fcode"] for f in funds}
    scope_codes = scope_codes & fund_codes if scope_codes else fund_codes
    covered = {code for code, period in periods.items()
               if period == latest_period and code in scope_codes}
    denominator = len(scope_codes) or len(funds)
    coverage = (len(covered) / denominator) if denominator else 0.0
    use_holdings = coverage >= holdings_min_coverage
    if not use_holdings:
        periods = {}
    holdings: dict[str, list[dict[str, Any]]] = {}
    for code, period in periods.items():
        rows = conn.execute("SELECT bond_code, bond_name, pct_nav, market_value_wan, raw_sha256"
                            " FROM src_zqcc_holding"
                            " WHERE code=? AND report_period=?", (code, period)).fetchall()
        holdings[code] = [{"bond_code": r["bond_code"], "bond_name": r["bond_name"],
                           "pct_nav": r["pct_nav"],
                           "market_value_wan": r["market_value_wan"],
                           "raw_sha256": r["raw_sha256"],
                           "bond_class": classify_bond_name(r["bond_name"], r["bond_code"])}
                          for r in rows]
        conn.executemany(
            "INSERT INTO fund_holding_bond(fcode, report_period, bond_code, bond_name, pct_nav,"
            " market_value_wan, bond_class, raw_sha256) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(fcode, report_period, bond_code) DO UPDATE SET bond_name=excluded.bond_name,"
            " pct_nav=excluded.pct_nav, market_value_wan=excluded.market_value_wan,"
            " bond_class=excluded.bond_class",
            [(code, period, h["bond_code"], h["bond_name"], h["pct_nav"],
              h["market_value_wan"], h["bond_class"], h["raw_sha256"])
             for h in holdings[code]])
    now = db.now_iso()
    out = []
    dist: dict[str, int] = {}
    conf_dist: dict[str, int] = {}
    for f in funds:
        res = classify(f["name"], f["type_text"], holdings.get(f["fcode"], []),
                       benchmarks.get(f["fcode"]))
        dist[res["style_code"]] = dist.get(res["style_code"], 0) + 1
        conf_dist[res["confidence"]] = conf_dist.get(res["confidence"], 0) + 1
        out.append((f["fcode"], res["style_code"], res["confidence"],
                    json.dumps(res["evidence"], ensure_ascii=False), CLASSIFIER_VERSION, now))
    conn.executemany(
        "INSERT INTO fund_style(fcode, style_code, confidence, evidence_json, classifier_version,"
        " updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(fcode) DO UPDATE SET"
        " style_code=excluded.style_code, confidence=excluded.confidence,"
        " evidence_json=excluded.evidence_json, classifier_version=excluded.classifier_version,"
        " updated_at=excluded.updated_at", out)
    # 同日快照：回测要的是“当时怎么打标签”，不是“今天怎么打标签”（ADR-065）。
    conn.executemany(
        "INSERT INTO fund_style_history(fcode, as_of_date, style_code, confidence,"
        " classifier_version, created_at) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(fcode, as_of_date) DO UPDATE SET style_code=excluded.style_code,"
        " confidence=excluded.confidence, classifier_version=excluded.classifier_version,"
        " created_at=excluded.created_at",
        [(r[0], business_date, r[1], r[2], r[4], r[5]) for r in out])
    conn.commit()
    return {"styled": len(out), "style_dist": dist, "confidence_dist": conf_dist,
            "with_holdings": len(holdings), "holdings_coverage": round(coverage, 4),
            "holdings_used": use_holdings,
            "holdings_min_coverage": holdings_min_coverage}


def style_asof_map(conn, as_of_date: str) -> tuple[dict[str, str], int]:
    """fcode→style_code 的 as-of 视图：优先取 as_of_date 当日或之前最近快照，缺失回退当前标签。

    返回 (映射, 命中历史快照的只数)。历史表从 2026-11 起才有数据（ADR-065），
    因此 2026 年之前的回测仍以当前标签为主，命中率会明确打印出来，避免把前视当无偏。
    """
    hist = {r["fcode"]: r["style_code"] for r in conn.execute(
        "SELECT h.fcode, h.style_code FROM fund_style_history h"
        " JOIN (SELECT fcode, MAX(as_of_date) AS m FROM fund_style_history"
        "       WHERE as_of_date<=? GROUP BY fcode) t"
        " ON t.fcode=h.fcode AND t.m=h.as_of_date", (as_of_date,))}
    current = {r["fcode"]: r["style_code"] for r in conn.execute(
        "SELECT fcode, style_code FROM fund_style")}
    current.update(hist)
    return current, len(hist)

