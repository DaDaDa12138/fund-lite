"""只读洞察接口：全局搜索、市场概览、同类分位、回撤曲线、基金对比。

设计约束与其余 API 一致：
- 只读，不写库；所有指标查询锁定 formula_contract（否则同一只基金出多行）。
- 缺失即 None，不填 0；分位在样本 < MIN_PEERS 时返回 None（不给虚假排名）。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from .. import fundtypes
from ..fees.holding import cost_profile
from ..metrics.tail import TAIL_CONTRACT, TAIL_WINDOW_DAYS

MIN_PEERS = 20
MAX_COMPARE = 3
COMPARE_DAYS = 1095

# 同风格分位：key -> (中文名, 是否越大越好, 单位)
PEER_METRICS = (
    ("annual_dec", "1 年年化", True, "pct"),
    ("sharpe", "夏普比率", True, "num"),
    ("calmar_dec", "卡玛比率", True, "num"),
    ("mdd_dec", "1 年最大回撤", False, "pct"),
    ("non_negative_ratio_pct", "非负日占比", True, "pctpoint"),
)


def _cost_profile(profile) -> dict | None:
    """持有期总成本（ADR-059）：{'365': {...}, '1095': {...}}，口径同 fundana.fees.holding。"""
    if not profile:
        return None
    prof = cost_profile(profile["recurring_fee_pct"], profile["max_sub_fee_pct"],
                        profile["has_discount"])
    return {str(k): v for k, v in prof.items()} or None


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def search(conn, q: str, limit: int = 8) -> list[dict]:
    """按代码前缀 / 名称片段搜索，代码精确命中排最前。"""
    q = (q or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    rows = conn.execute(
        "SELECT f.fcode, f.name, f.type_text, s.style_code FROM fund f"
        " LEFT JOIN fund_style s ON s.fcode=f.fcode"
        " WHERE f.fcode LIKE ? OR f.name LIKE ?"
        " ORDER BY CASE WHEN f.fcode=? THEN 0 WHEN f.fcode LIKE ? THEN 1 ELSE 2 END, f.fcode"
        " LIMIT ?", (like, like, q, f"{q}%", limit)).fetchall()
    from ..labels import label_style
    return [{"fcode": r["fcode"], "name": r["name"], "type_text": r["type_text"],
             "style_code": r["style_code"], "style_label": label_style(r["style_code"])}
            for r in rows]


def market_summary(conn, contract: str, cfg=None) -> dict:
    """全市场债基横截面：1 年期收益分布、风格中位数、可申购比例。"""
    clause, tparams = fundtypes.where(cfg=cfg, column="f.type_group")
    rows = conn.execute(
        "SELECT s.style_code, m.annual_dec, m.mdd_dec, m.sharpe, m.calmar_dec,"
        " m.positive_ratio_pct"
        " FROM fund_metric m JOIN fund f ON f.fcode=m.fcode"
        " LEFT JOIN fund_style s ON s.fcode=m.fcode"
        f" WHERE m.period_days=365 AND m.formula_contract=? AND {clause}",
        (contract, *tparams)).fetchall()
    annual = [r["annual_dec"] for r in rows]
    known = [v for v in annual if v is not None]
    n = len(known)
    positive = sum(1 for v in known if v > 0)
    buckets = [("< -2%", -99.0, -0.02), ("-2% ~ 0", -0.02, 0.0), ("0 ~ 2%", 0.0, 0.02),
               ("2% ~ 4%", 0.02, 0.04), ("4% ~ 6%", 0.04, 0.06), ("> 6%", 0.06, 99.0)]
    dist = []
    for label, lo, hi in buckets:
        dist.append({"label": label, "count": sum(
            1 for v in annual if v is not None and lo <= v < hi)})
    by_style: dict = {}
    for r in rows:
        by_style.setdefault(r["style_code"] or "unknown", []).append(r)
    styles = []
    from ..labels import label_style
    for code, items in by_style.items():
        ann = [x["annual_dec"] for x in items]
        pos = sum(1 for v in ann if v is not None and v > 0)
        known = sum(1 for v in ann if v is not None)
        styles.append({
            "style_code": code, "style_label": label_style(code), "n": len(items),
            "annual_median": _median(ann), "annual_mean": _mean(ann),
            "positive_share": (pos / known) if known else None,
            "mdd_median": _median([x["mdd_dec"] for x in items]),
            "sharpe_median": _median([x["sharpe"] for x in items]),
        })
    styles.sort(key=lambda s: (s["annual_median"] is None, -(s["annual_median"] or 0)))
    buyable = dict(conn.execute(
        "SELECT buyable_state, COUNT(*) AS c FROM fund_buyable_daily"
        " WHERE business_date=(SELECT MAX(business_date) FROM fund_buyable_daily)"
        " GROUP BY 1").fetchall())
    return {
        "n_metric": n,
        "annual_median": _median(annual),
        "annual_mean": _mean(annual),
        "annual_p25": _quantile(annual, 0.25),
        "annual_p75": _quantile(annual, 0.75),
        "positive_share": (positive / n) if n else None,
        "mdd_median": _median([r["mdd_dec"] for r in rows]),
        "sharpe_median": _median([r["sharpe"] for r in rows]),
        "distribution": dist,
        "styles": styles,
        "buyable": buyable,
    }


def _quantile(values, q: float):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    idx = max(0, min(len(vals) - 1, int(round((len(vals) - 1) * q))))
    return vals[idx]


def peer_rank(conn, fcode: str, contract: str) -> dict:
    """同风格分位：返回每项指标“好于同风格多少比例”，样本不足不给分位。"""
    style = conn.execute("SELECT style_code FROM fund_style WHERE fcode=?", (fcode,)).fetchone()
    style_code = style["style_code"] if style else None
    mine = conn.execute(
        "SELECT annual_dec, sharpe, calmar_dec, mdd_dec, non_negative_ratio_pct"
        " FROM fund_metric WHERE fcode=? AND period_days=365 AND formula_contract=?",
        (fcode, contract)).fetchone()
    if not mine or not style_code:
        return {"style_code": style_code, "n_peers": 0, "items": []}
    peers = conn.execute(
        "SELECT m.annual_dec, m.sharpe, m.calmar_dec, m.mdd_dec, m.non_negative_ratio_pct"
        " FROM fund_metric m JOIN fund_style s ON s.fcode=m.fcode"
        " WHERE s.style_code=? AND m.period_days=365 AND m.formula_contract=?",
        (style_code, contract)).fetchall()
    items = []
    for key, label, higher_better, unit in PEER_METRICS:
        value = mine[key]
        vals = [r[key] for r in peers if r[key] is not None]
        pct = None
        if value is not None and len(vals) >= MIN_PEERS:
            if higher_better:
                better = sum(1 for v in vals if v < value)
            else:
                better = sum(1 for v in vals if v > value)
            pct = better / len(vals)
        items.append({"key": key, "label": label, "value": value, "percentile": pct,
                      "higher_better": higher_better, "unit": unit, "n": len(vals)})
    return {"style_code": style_code, "n_peers": len(peers), "items": items}


def drawdown_series(conn, fcode: str, days: int = COMPARE_DAYS, max_points: int = 600) -> dict:
    """回撤曲线：以区间内历史最高净值为基准的水下深度（百分点，负数）。"""
    end_row = conn.execute("SELECT MAX(nav_date) AS d FROM fund_nav_daily WHERE fcode=?",
                           (fcode,)).fetchone()
    end = end_row["d"] if end_row else None
    if not end:
        return {"points": [], "n_total": 0, "n_points": 0, "start": None, "end": None,
                "max_dd_pct": None, "max_dd_date": None, "current_dd_pct": None}
    start = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT nav_date, unit_nav FROM fund_nav_daily"
        " WHERE fcode=? AND nav_date>? AND nav_date<=? AND unit_nav IS NOT NULL"
        " ORDER BY nav_date", (fcode, start, end)).fetchall()
    peak = None
    points = []
    max_dd = None
    max_dd_date = None
    for r in rows:
        nav = float(r["unit_nav"])
        peak = nav if peak is None else max(peak, nav)
        dd = (nav / peak - 1.0) * 100.0 if peak else 0.0
        points.append({"date": r["nav_date"], "dd_pct": round(dd, 4)})
        if max_dd is None or dd < max_dd:
            max_dd, max_dd_date = dd, r["nav_date"]
    n_total = len(points)
    if n_total > max_points:
        step = math.ceil(n_total / max_points)
        sampled = points[::step]
        if sampled[-1] != points[-1]:
            sampled.append(points[-1])
        points = sampled
    return {"points": points, "n_total": n_total, "n_points": len(points),
            "start": rows[0]["nav_date"] if rows else None, "end": end,
            "max_dd_pct": round(max_dd, 4) if max_dd is not None else None,
            "max_dd_date": max_dd_date,
            "current_dd_pct": points[-1]["dd_pct"] if points else None}


def compare(conn, codes: list[str], contract: str, days: int = COMPARE_DAYS) -> dict:
    """并排对比：档案 + 1 年期指标 + 归一化净值（同一窗口、起点=100）+ 年度收益。"""
    from ..labels import label_board, label_confidence, label_style
    from . import periods as periods_mod

    items = []
    end_row = conn.execute("SELECT MAX(nav_date) AS d FROM fund_nav_daily").fetchone()
    end = end_row["d"] if end_row else None
    start = (date.fromisoformat(end) - timedelta(days=days)).isoformat() if end else None
    for code in codes[:MAX_COMPARE]:
        fund = conn.execute("SELECT fcode, name, type_text, estab_date, company_name"
                            " FROM fund WHERE fcode=?", (code,)).fetchone()
        if not fund:
            items.append({"fcode": code, "missing": True})
            continue
        profile = conn.execute("SELECT asset_scale_billion, recurring_fee_pct,"
                               " max_sub_fee_pct, mgr_fee_pct, trust_fee_pct, sales_fee_pct,"
                               " benchmark_text,"
                               " (SELECT has_discount FROM fund_fee_meta WHERE fcode=?) AS has_discount"
                               " FROM fund_profile WHERE fcode=?", (code, code)).fetchone()
        style = conn.execute("SELECT style_code, confidence FROM fund_style WHERE fcode=?",
                             (code,)).fetchone()
        metric = conn.execute(
            "SELECT annual_dec, total_return_pct, sharpe, sortino, calmar_dec, mdd_dec,"
            " vol_dec, positive_ratio_pct, n_obs FROM fund_metric"
            " WHERE fcode=? AND period_days=365 AND formula_contract=?",
            (code, contract)).fetchone()
        tail = conn.execute("SELECT mdd_dec FROM fund_tail_metric WHERE fcode=?"
                            " AND window_days=? AND formula_contract=?"
                            " ORDER BY computed_at DESC LIMIT 1",
                            (code, TAIL_WINDOW_DAYS, TAIL_CONTRACT)).fetchone()
        pick = conn.execute("SELECT board, rank, formal_score FROM screen_pick WHERE fcode=?"
                            " ORDER BY run_id DESC LIMIT 1", (code,)).fetchone()
        nav = []
        if start:
            rows = conn.execute(
                "SELECT nav_date, unit_nav FROM fund_nav_daily"
                " WHERE fcode=? AND nav_date>=? AND nav_date<=? AND unit_nav IS NOT NULL"
                " ORDER BY nav_date", (code, start, end)).fetchall()
            if rows:
                base = float(rows[0]["unit_nav"])
                raw = [{"date": r["nav_date"], "value": round(float(r["unit_nav"]) / base * 100.0, 4)}
                       for r in rows]
                step = max(1, math.ceil(len(raw) / 260))
                nav = raw[::step]
                if nav[-1] != raw[-1]:
                    nav.append(raw[-1])
        annual = []
        try:
            for row in reversed(periods_mod.period_rows(conn, code, "year", 6)["rows"]):
                annual.append({"period": row["period"], "chain_return_pct": row["chain_return_pct"],
                               "partial": row["partial"]})
        except Exception:                     # noqa: BLE001 —— 聚合失败不应拖垮对比
            annual = []
        items.append({
            "fcode": fund["fcode"], "name": fund["name"], "type_text": fund["type_text"],
            "company_name": fund["company_name"], "estab_date": fund["estab_date"],
            "style_code": style["style_code"] if style else None,
            "style_label": label_style(style["style_code"] if style else None),
            "confidence_label": label_confidence(style["confidence"] if style else None),
            "scale_billion": profile["asset_scale_billion"] if profile else None,
            "recurring_fee_pct": profile["recurring_fee_pct"] if profile else None,
            "cost": _cost_profile(profile),
            "benchmark_text": profile["benchmark_text"] if profile else None,
            "board_label": label_board(pick["board"]) if pick else None,
            "rank": pick["rank"] if pick else None,
            "formal_score": pick["formal_score"] if pick else None,
            "metrics": dict(metric) if metric else None,
            "mdd_3y": tail["mdd_dec"] if tail else None,
            "nav": nav, "annual": annual,
        })
    return {"items": items, "start": start, "end": end, "days": days}
