"""报告 v4：把一次筛选 run 写成十四节、可解释、带反面证据的报告。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import (benchmarks, capacity, db, factor_buckets as fbm, forward, fundtypes,
                lifecycle, portfolio_diagnostics as diag, survivorship)
from ..screen import objective, oos, style_shadow
from . import report_schema
from ..audit import portfolio as port
from ..labels import label_layer, label_style
from ..screen import thresholds as T

SCHEMA_VERSION = 3


def _fmt(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _corr_text(value: Any) -> str:
    if isinstance(value, dict):
        return "%s vs %s：%s（共同 %s 日）" % (
            value.get("a", "—"), value.get("b", "—"),
            _fmt(value.get("corr"), 3), value.get("common_obs", "—"))
    return _fmt(value, 3)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join("" if x is None else str(x) for x in row) + " |")
    return "\n".join(out)


def _funnel(conn, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT layer_reached, COUNT(*) AS c FROM screen_decision WHERE run_id=?"
        " GROUP BY 1 ORDER BY 1", (run_id,)).fetchall()
    total = sum(r["c"] for r in rows) or 1
    return [{"layer": r["layer_reached"], "label": label_layer(r["layer_reached"]),
             "count": r["c"], "share_pct": round(100.0 * r["c"] / total, 2)} for r in rows]


def _reject_reasons(conn, run_id: str, limit: int = 8) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for r in conn.execute("SELECT reason_codes_json FROM screen_decision WHERE run_id=?",
                          (run_id,)):
        for code in json.loads(r["reason_codes_json"] or "[]"):
            key = str(code).split(":")[0]
            counts[key] = counts.get(key, 0) + 1
    return [{"code": k, "count": v} for k, v in
            sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def _coverage(conn, run_id: str) -> dict[str, Any]:
    formula = conn.execute("SELECT formula_contract FROM screen_run WHERE run_id=?",
                           (run_id,)).fetchone()
    formula = formula["formula_contract"] if formula else None

    def count(period: int) -> int:
        return conn.execute(
            "SELECT COUNT(DISTINCT fcode) FROM fund_metric WHERE period_days=?"
            " AND annual_dec IS NOT NULL AND formula_contract=?", (period, formula)).fetchone()[0]

    return {"funds": conn.execute("SELECT COUNT(DISTINCT fcode) FROM fund_metric WHERE formula_contract=?",
                                  (formula,)).fetchone()[0],
            "with_1y": count(365), "with_3y": count(1095), "with_5y": count(1825),
            "formula_contract": formula}


def _formal_counts(run: dict[str, Any], formal: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Return (L9 passed, published formal rows, folded rows).

    旧 screen_run 只有 formal_count（L9 口径）。新表拆出 l9_pass_count /
    published_count，报告必须按发布数写，不能再用 len(formal) 冒充。
    """
    def _get(key):
        try:
            return run[key]
        except (KeyError, IndexError):
            return None

    raw = _get("l9_pass_count")
    if raw is None:
        raw = _get("formal_count")
    l9_pass = int(raw or 0)
    pub_raw = _get("published_count")
    published = int(pub_raw) if pub_raw is not None else len(formal)
    return l9_pass, published, max(0, l9_pass - published)


def _evidence_match(run, evidence: dict[str, Any]) -> bool:
    """Whether the registered IC/DSR evidence is for this run's NAV date.

    The evidence block is a point-in-time research result; reusing it on a newer
    NAV date without saying so would make a historical DSR look current.
    """
    try:
        nav = run["nav_as_of"]
    except (KeyError, IndexError):
        nav = None
    asof = evidence.get("as_of") if evidence else None
    return bool(nav and asof and str(nav) == str(asof))


def _evidence_label(run, evidence: dict[str, Any]) -> str:
    if _evidence_match(run, evidence):
        return "本次证据（as_of %s）" % evidence.get("as_of")
    try:
        nav = run["nav_as_of"]
    except (KeyError, IndexError):
        nav = None
    if not evidence or not evidence.get("as_of"):
        return "未登记 as_of 的历史证据"
    return ("历史证据（as_of %s，与本次净值日 %s 不匹配，仅作方法参考）"
            % (evidence.get("as_of"), nav or "—"))


def _survivorship_note(evidence: dict[str, Any]) -> str:
    """清盘基金纳入/排除对照；没有登记时返回空串，不猜数字。"""
    sv = (evidence or {}).get("survivorship") or {}
    if not sv:
        return ""
    return ("**幸存者偏差对照（%s 个半年队列，纳入 %s 只已确认清盘基金）**："
            "rank_score 年化 IC %s→%s（Δ%s）；composite %s→%s。"
            "样本仍不完整（EID 清盘 %s 条中，%s 只债券型已回填、%s 只有公开净值，合并 0），"
            "只能算部分校正，不能据此宣称无偏。"
            % (_fmt(sv.get("cohorts"), 0), _fmt(sv.get("liquidated_funds"), 0),
               _fmt(sv.get("rank_ic_annual_ex_survivors"), 4),
               _fmt(sv.get("rank_ic_annual_with_liquidated"), 4),
               _fmt(sv.get("rank_ic_annual_delta"), 4),
               _fmt(sv.get("composite_ic_ex_survivors"), 4),
               _fmt(sv.get("composite_ic_with_liquidated"), 4),
               _fmt(sv.get("lifecycle_liquidated_total"), 0),
               _fmt(sv.get("liquidated_funds"), 0),
               _fmt(sv.get("lifecycle_with_nav"), 0)))


def _provenance(conn, cfg, gate_result=None) -> dict[str, Any]:
    """源事实溯源摘要：每条原始记录都必须能回查到一次真实 HTTP 响应。

    优先复用发布门禁已算过的 provenance_integrity，避免对 1,900 万行源表重复扫描。
    """
    if gate_result:
        for chk in gate_result.get("checks") or []:
            if chk.get("code") == "provenance_integrity" and chk.get("evidence"):
                p = chk["evidence"]
                break
        else:
            p = None
    else:
        p = None
    from .. import data_health
    if p is None:
        allowed = set(cfg.sources) if getattr(cfg, "sources", None) else None
        p = data_health.provenance_audit(conn, allowed_sources=allowed)
    total_rows = 0
    for table, _date_col, _source_col in data_health.PROVENANCE_TABLES:
        try:
            total_rows += int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] or 0)
        except Exception:                                    # 表不存在时由审计结果标 missing_table
            pass
    return {
        "status": p.get("status"),
        "tables": len(p.get("tables") or []),
        "checked_rows": sum(int(t.get("rows") or 0) for t in (p.get("tables") or [])),
        "total_rows": total_rows,
        "nav_since": p.get("nav_since"),
        "missing_raw": p.get("missing_total", 0),
        "bad_format": p.get("bad_format_total", 0),
        "unregistered": len(p.get("unregistered_sources") or []),
    }


def _provenance_text(prov: dict[str, Any]) -> str:
    checked = "{:,}".format(int(prov.get("checked_rows") or 0))
    total = "{:,}".format(int(prov.get("total_rows") or 0))
    scope = ("本次门禁核对 %s 行（NAV 取 %s 起近 90 天，其余源表全量）"
             % (checked, prov.get("nav_since"))
             if prov.get("nav_since") else "本次逐条核对 %s 行" % checked)
    return ("**数据来源与真实性**：基金原始数据只来自公开 API；%s 张源表共 %s 行，%s，"
            "raw_sha256 缺失 %s、格式错 %s、未注册来源 %s。缺失值保持 NULL，不用 0 或模拟值填充；"
            "ETF 只作风格参考，同风格 peer median 只作代理基准。"
            % (_fmt(prov.get("tables"), 0), total, scope,
               _fmt(prov.get("missing_raw"), 0), _fmt(prov.get("bad_format"), 0),
               _fmt(prov.get("unregistered"), 0)))


def _data_asof(conn) -> dict[str, Any]:
    """Return the latest disclosure/observation dates used by this report."""
    def one(sql: str):
        try:
            row = conn.execute(sql).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    return {
        "holding_report_period": one("SELECT MAX(report_period) FROM fund_holding_bond"),
        "fee_observed_at": one("SELECT MAX(observed_at) FROM fund_fee_meta"),
        "scale_report_period": one("SELECT MAX(report_period) FROM fund_scale_history"),
        "manager_start_date": one("SELECT MAX(start_date) FROM fund_manager_tenure WHERE is_current=1"),
        "profile_updated_at": one("SELECT MAX(updated_at) FROM fund_profile"),
    }


def _period_end(value: Any) -> str | None:
    """Convert 2026Q3 to 2026-09-30; pass ISO dates through."""
    if not value:
        return None
    text = str(value)
    try:
        if "Q" in text:
            year, quarter = text.split("Q", 1)
            month = int(quarter) * 3
            day = {3: 31, 6: 30, 9: 30, 12: 31}[month]
            return date(int(year), month, day).isoformat()
        return date.fromisoformat(text[:10]).isoformat()
    except (TypeError, ValueError, KeyError):
        return None


def _age_days(value: Any, as_of: str) -> str:
    if not value:
        return "—"
    try:
        return str(max(0, (date.fromisoformat(as_of[:10]) - date.fromisoformat(str(value)[:10])).days))
    except (TypeError, ValueError):
        return "—"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    n = len(values)
    mid = n // 2
    if n % 2:
        return round(values[mid], 6)
    return round((values[mid - 1] + values[mid]) / 2.0, 6)


def _style_benchmarks(conn, run: dict, rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """同风格等权中位数基准；只用于报告对照，不进入筛选分。"""
    styles = sorted({r.get("style_code") for r in rows if r.get("style_code")})
    if not styles:
        return {}
    placeholders = ",".join("?" for _ in styles)
    out: dict[str, dict[str, float]] = {s: {} for s in styles}
    for period in (365, 1095, 1825):
        q = (
            "SELECT s.style_code, m.annual_dec FROM fund_metric m"
            " JOIN fund_style s ON s.fcode=m.fcode"
            " WHERE m.period_days=? AND m.formula_contract=? AND m.annual_dec IS NOT NULL"
            "   AND m.end_date<=? AND s.style_code IN (" + placeholders + ")"
        )
        args = [period, run["formula_contract"], run["nav_as_of"], *styles]
        buckets: dict[str, list[float]] = {s: [] for s in styles}
        for r in conn.execute(q, args):
            buckets.setdefault(r["style_code"], []).append(float(r["annual_dec"]))
        for s in styles:
            med = _median(buckets.get(s, []))
            # annual_dec 是小数（0.03 = 3%），报告列是百分点；不转换会差 100 倍。
            out[s][str(period)] = round(med * 100.0, 4) if med is not None else None
    return out


def _excess(value: Any, bench: Any) -> float | None:
    if value is None or bench is None:
        return None
    return round(float(value) - float(bench), 4)


def _portfolio(conn, run: dict, rows: list[dict[str, Any]],
               formal_rows: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    if not rows:
        return None
    as_of = str(run["business_date"])
    start = (date.fromisoformat(as_of) - timedelta(days=365)).isoformat()
    series: dict[str, list[tuple[str, float]]] = {}
    meta: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = row["fcode"]
        s = port._load_series(conn, code, start, as_of)
        if s:
            series[code] = s
        meta[code] = {"company": row.get("company_name"), "family": row.get("family_id"),
                      "style": row.get("style_code")}
    if len(series) < 2:
        return {"requested": len(rows), "series_used": len(series),
                "note": "共同净值不足，无法做组合诊断"}
    result = diag.diagnose(series, meta, min_pair_obs=40)
    codes = list(series)
    betas = []
    for code in codes:
        r = conn.execute(
            "SELECT beta_duration_raw FROM fund_factor_exposure WHERE fcode=? AND as_of<=?"
            " ORDER BY as_of DESC LIMIT 1", (code, as_of)).fetchone()
        if r and r["beta_duration_raw"] is not None:
            betas.append(float(r["beta_duration_raw"]))
    duration = None
    if betas:
        betas.sort()

        def q(p: float) -> float:
            idx = min(len(betas) - 1, max(0, int(round((len(betas) - 1) * p))))
            return round(betas[idx], 4)

        duration = {"n": len(betas), "p10": q(0.10), "p50": q(0.50), "p90": q(0.90),
                    "spread": round(q(0.90) - q(0.10), 4)}
    bucket_map = fbm.load_map(conn, as_of)
    core_buckets = {"short": 0, "mid": 0, "long": 0, "unknown": 0}
    credit_buckets = {"low": 0, "mid": 0, "high": 0, "unknown": 0}
    for row in rows:
        b = bucket_map.get(row["fcode"]) or {}
        core_buckets[b.get("duration", "unknown")] = core_buckets.get(
            b.get("duration", "unknown"), 0) + 1
        credit_buckets[b.get("credit", "unknown")] = credit_buckets.get(
            b.get("credit", "unknown"), 0) + 1
    style_matrix = fbm.style_duration_matrix(formal_rows or rows, bucket_map)
    eff = result.get("effective_bets")
    target = float(T.CORE10_TARGET_EFFECTIVE_BETS)
    budget = {
        "target": target,
        "effective_bets": eff,
        "status": ("meets_target" if eff is not None and eff >= target else
                   ("downgraded" if eff is not None else "unavailable")),
        "persisted": rows[0].get("core10_risk_budget") if rows else None,
        "note": ("达到目标，但仍低于 10 个独立 bet" if eff is not None and eff >= target
                 else "低于目标：core10 只能作为集中观察组合，不能宣称分散化"),
    }
    return {
        "requested": len(rows), "series_used": len(series),
        "avg_pairwise_corr": result.get("avg_pairwise_corr"),
        "max_pairwise_corr": result.get("max_pairwise_corr"),
        "effective_bets": result.get("effective_bets"),
        "diversification_ratio": result.get("diversification_ratio"),
        "styles": result.get("styles"), "companies": result.get("companies"),
        "families": result.get("families"), "flags": result.get("flags"),
        "duration": duration,
        "factor_buckets": {"duration": core_buckets, "credit": credit_buckets,
                           "unknown": sum(1 for row in rows
                                          if (bucket_map.get(row["fcode"]) or {}).get("duration") == "unknown")},
        "style_duration": style_matrix,
        "mdd_contribution": _mdd_contribution(series),
        "risk_budget": budget,
        "note": result.get("note"),
    }


def _mdd_contribution(series: dict[str, list[tuple[str, float]]]) -> list[dict[str, Any]]:
    """等权组合从峰值到谷底的回撤贡献（近似，按共同披露日对齐）。

    series 是日收益小数（0.0004 表示 0.04%），不是净值；先各自复利成 1.0 起的
    财富曲线，再等权平均。
    """
    common = sorted(set.intersection(*[set(d for d, _ in s) for s in series.values()]))
    if len(common) < 3:
        return []
    maps = {code: dict(s) for code, s in series.items()}
    curves = {}
    for code in maps:
        wealth = [1.0]
        for d in common:
            wealth.append(wealth[-1] * (1.0 + float(maps[code][d])))
        curves[code] = wealth
    port_curve = [sum(curves[c][i] for c in curves) / len(curves) for i in range(len(common) + 1)]
    peak_i = 0
    max_dd = 0.0
    trough_i = 0
    for i, v in enumerate(port_curve):
        if v > port_curve[peak_i]:
            peak_i = i
        dd = v / port_curve[peak_i] - 1.0
        if dd < max_dd:
            max_dd = dd
            trough_i = i
    if trough_i <= peak_i:
        return []
    contrib = []
    for code in curves:
        r = curves[code][trough_i] / curves[code][peak_i] - 1.0
        contrib.append({"fcode": code, "contribution_pct": round(100.0 * r / len(curves), 4)})
    contrib.sort(key=lambda x: x["contribution_pct"])
    return contrib[:5]


def _cross_board(boards: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    names = ["bond_stable", "bond_enhanced", "bond_core", "bond_core10", "bond_long"]
    rows = [r for b in names for r in boards.get(b, [])]
    fams = [r.get("family_id") for r in rows if r.get("family_id")]
    counts: dict[str, int] = {}
    for f in fams:
        counts[f] = counts.get(f, 0) + 1
    dup = [{"family_id": k, "count": v} for k, v in counts.items() if v > 1]
    return {"rows": len(rows), "unique_codes": len({r["fcode"] for r in rows}),
            "unique_families": len(counts), "duplicate_families": len(dup),
            "max_family_rows": max(counts.values()) if counts else 0,
            "duplicate_examples": sorted(dup, key=lambda x: (-x["count"], x["family_id"]))[:5]}


def _top(rows: list[dict[str, Any]], n: int = 10) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (r.get("rank") is None, r.get("rank") or 0))[:n]


def _clip_text(text: Any, limit: int) -> str:
    """按标点截断解释文本，避免把“0.46%”切成“0.”。"""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    head = value[:limit]
    for sep in ("；", "。", "，", "、", " "):
        pos = head.rfind(sep)
        if pos >= max(8, limit // 3):
            return head[:pos + 1].rstrip() + "…"
    return head.rstrip() + "…"


def _long_table(rows: list[dict[str, Any]], bucket_map=None) -> str:
    body = []
    for r in _top(rows, 20):
        b = (bucket_map or {}).get(r["fcode"]) or {}
        body.append([r.get("rank"), r["fcode"], r.get("name"), label_style(r.get("style_code")),
                     _DURATION_LABEL.get(b.get("duration") or "unknown", "未知"),
                     _fmt(r.get("long_score"), 2), _fmt(r.get("annual_3y_pct"), 2, "%"),
                     _fmt(r.get("mdd_3y_pct"), 2, "%"), _fmt(r.get("annual_5y_pct"), 2, "%"),
                     _fmt((r.get("coverage_ratio_5y") or 0) * 100, 1, "%"),
                     _fmt(r.get("manager_tenure_min_days"), 0), _fmt(r.get("share_scale_billion"), 2),
                     _fmt(r.get("cost_3y_pct"), 2, "%"), _clip_text(r.get("why_selected"), 120),
                     _clip_text(r.get("caveat"), 120)])
    return _table(["排名", "代码", "名称", "风格", "久期桶", "长期分", "3年年化", "3年MDD", "5年年化",
                   "5年覆盖", "经理天数", "份额(亿)", "3年成本", "为什么选它", "主要短板"], body)


def _metrics_table(rows: list[dict[str, Any]], n: int = 10,
                   bench: dict[str, dict[str, float]] | None = None) -> str:
    body = []
    for r in _top(rows, n):
        b = (bench or {}).get(r.get("style_code"), {})
        body.append([r["fcode"], r.get("name"),
                     _fmt(r.get("annual_3y_pct"), 2, "%"),
                     _fmt(r.get("mdd_3y_pct"), 2, "%"),
                     _fmt(r.get("sharpe_3y"), 2),
                     _fmt(r.get("calmar_3y"), 2),
                     _fmt(r.get("annual_5y_pct"), 2, "%"),
                     _fmt(r.get("mdd_5y_pct"), 2, "%"),
                     _fmt(r.get("sharpe_5y"), 2),
                     _fmt(r.get("calmar_5y"), 2),
                     _fmt((r.get("coverage_ratio_5y") or 0) * 100, 1, "%"),
                     _fmt(r.get("cost_3y_pct"), 2, "%"),
                     _fmt(r.get("cost_5y_pct"), 2, "%"),
                     _fmt(b.get("1095"), 2, "%"),
                     _fmt(_excess(r.get("annual_3y_pct"), b.get("1095")), 2, "%"),
                     _fmt(b.get("1825"), 2, "%"),
                     _fmt(_excess(r.get("annual_5y_pct"), b.get("1825")), 2, "%"),
                     _fmt(r.get("alpha_annual_pct"), 2, "%"),
                     _fmt(r.get("alpha_t"), 2)])
    return _table(["代码", "名称", "3年年化", "3年MDD", "3年Sharpe", "3年Calmar",
                   "5年年化", "5年MDD", "5年Sharpe", "5年Calmar", "5年覆盖",
                   "3年成本", "5年成本", "同风格3年", "超额3年", "同风格5年", "超额5年",
                   "因子alpha", "alpha t"], body)


def _formal_table(rows: list[dict[str, Any]], n: int = 10,
                  bench: dict[str, dict[str, float]] | None = None) -> str:
    body = []
    for r in _top(rows, n):
        b = (bench or {}).get(r.get("style_code"), {})
        body.append([r.get("rank"), r["fcode"], r.get("name"), label_style(r.get("style_code")),
                     _fmt(r.get("rank_score"), 2), _fmt(r.get("formal_score"), 2),
                     _fmt(r.get("annual_1y_pct"), 2, "%"),
                     _fmt(b.get("365"), 2, "%"),
                     _fmt(_excess(r.get("annual_1y_pct"), b.get("365")), 2, "%"),
                     _fmt(r.get("annual_3y_pct"), 2, "%"),
                     _fmt(r.get("mdd_3y_pct"), 2, "%"), _fmt(r.get("annual_5y_pct"), 2, "%"),
                     _fmt((r.get("coverage_ratio_5y") or 0) * 100, 1, "%"),
                     _clip_text(r.get("why_selected"), 100), _clip_text(r.get("caveat"), 100)])
    return _table(["排名", "代码", "名称", "风格", "排序分", "正式分", "1年年化", "peer中位1年",
                   "peer超额1年", "3年年化", "3年MDD", "5年年化", "5年覆盖",
                   "为什么选它", "主要短板"], body)


_DURATION_LABEL = {"short": "短", "mid": "中", "long": "长", "unknown": "未知"}
_CREDIT_LABEL = {"low": "低", "mid": "中", "high": "高", "unknown": "未知"}


def _core10_section(core10, stable, bucket_map, portfolio):
    """核心精选 10 只：明细 + 公司集中度 + 与稳健榜重叠。

    core10 不是独立评级，只是同一正式候选池的跨风格再集中；这里把“哪 10 只、
    来自哪家公司、久期/信用桶、与稳健榜重复多少”写清楚，避免被当成 10 个独立推荐。
    """
    stable_codes = {r["fcode"] for r in stable}
    overlap = [r for r in core10 if r["fcode"] in stable_codes]
    companies = {}
    for r in core10:
        name = r.get("company_name") or "未知"
        companies[name] = companies.get(name, 0) + 1
    top_companies = sorted(companies.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    families = {}
    for r in core10:
        fam = r.get("family_id")
        if fam:
            families[fam] = families.get(fam, 0) + 1
    dup_families = {k: v for k, v in families.items() if v > 1}
    if not core10:
        return ("本轮 core10 为空：正式候选池不足，或跨风格集中时没有满足风格上限与"
                "相关性约束的组合。空榜不是数据故障，也不要用稳健榜前十替代。",
                {"rows": [], "overlap_stable": [], "companies": {}})
    body = []
    for r in core10:
        b = bucket_map.get(r["fcode"]) or {}
        body.append([
            r.get("rank"), r["fcode"], r.get("name"), r.get("company_name") or "—",
            label_style(r.get("style_code")),
            _DURATION_LABEL.get(b.get("duration"), "未知"),
            _CREDIT_LABEL.get(b.get("credit"), "未知"),
            _fmt(r.get("rank_score"), 2), _fmt(r.get("formal_score"), 2),
            _fmt(r.get("annual_1y_pct"), 2, "%"), _fmt(r.get("annual_3y_pct"), 2, "%"),
            _fmt(r.get("mdd_3y_pct"), 2, "%"), _fmt(r.get("annual_5y_pct"), 2, "%"),
            _fmt((r.get("coverage_ratio_5y") or 0) * 100, 1, "%"),
            _fmt(r.get("cost_3y_pct"), 2, "%"), _clip_text(r.get("why_selected"), 90),
        ])
    md = _table(["排名", "代码", "名称", "公司", "风格", "久期桶", "信用桶", "排序分",
                 "正式分", "1年年化", "3年年化", "3年MDD", "5年年化", "5年覆盖",
                 "3年成本", "为什么选它"], body)
    comp_txt = "；".join("%s %s 只（%s）" % (n, c, _fmt(100.0 * c / len(core10), 1, "%"))
                         for n, c in top_companies) or "—"
    overlap_txt = ("与稳健榜重叠 %s/%s 只（%s）" % (
        len(overlap), len(core10),
        "、".join("%s %s" % (r["fcode"], r.get("name") or "") for r in overlap[:10]))
        if overlap else "与稳健榜无重叠")
    dup_txt = ("；同 family 重复 %s 个（%s）" % (
        len(dup_families), "、".join("%s×%s" % (k, v) for k, v in sorted(dup_families.items())))
        if dup_families else "；同 family 无重复")
    eff = (portfolio or {}).get("effective_bets")
    target = float(T.CORE10_TARGET_EFFECTIVE_BETS)
    if eff is None:
        eff_txt = "有效 bet 不可计算，不能把 10 只当成 10 个独立 bet。"
    elif eff >= target:
        eff_txt = ("有效 bet %s（目标 %s），达到目标但仍低于 10，不能宣称完全分散。"
                   % (_fmt(eff, 3), _fmt(target, 1)))
    else:
        eff_txt = ("有效 bet %s（目标 %s），低于目标，core10 只能作为集中观察组合，"
                   "不能据此做等权配置。" % (_fmt(eff, 3), _fmt(target, 1)))
    md = ("core10 从**正式候选池**（稳健榜口径，不含增强债/可转债）按排序分跨风格再取 10 只，"
          "不占正式榜名额，也不是另一套质量等级；它会与稳健榜重叠。风格上限与相关性惩罚只控制"
          "集中度，不保证 10 个独立风险源。\n\n" + md
          + "\n\n**公司集中度**：前三大 " + comp_txt + "。"
          + "\n\n**跨榜关系**：" + overlap_txt + dup_txt + "。"
          + "\n\n**组合风险**：" + eff_txt + "详见第 8 节组合体检。")
    return md, {"rows": core10, "overlap_stable": [r["fcode"] for r in overlap],
                "companies": companies, "duplicate_families": dup_families,
                "effective_bets": eff}


def _share_table(rows: list[dict[str, Any]], n: int = 10) -> str:
    body = []
    for r in _top(rows, n):
        sc = r.get("share_class_compare") or {}
        rec = sc.get("recommended") or {}
        cur = next((m for m in (sc.get("members") or []) if m.get("fcode") == r["fcode"]), {})
        body.append([r["fcode"], r.get("name"), sc.get("class_letter"),
                     _fmt(cur.get("cost_1y_pct"), 2, "%"),
                     _fmt(cur.get("cost_3y_pct"), 2, "%"),
                     _fmt(cur.get("cost_5y_pct"), 2, "%"), rec.get("class_letter"),
                     _fmt(rec.get("cost_3y_pct"), 2, "%"),
                     _fmt(sc.get("advantage_3y_pct"), 3), _fmt(sc.get("break_even_years"), 2)])
    return _table(["代码", "名称", "当前份额", "当前1年", "当前3年", "当前5年",
                   "推荐份额", "推荐3年", "3年节省(pp)", "盈亏平衡(年)"], body)


def _benchmark_table(bmark: dict[str, Any]) -> str:
    rows = []
    for r in bmark.get("rows") or []:
        b = r.get("benchmark") or {}
        m = r.get("benchmark_metrics") or {}
        etf = r.get("etf") or {}
        etf_m = r.get("etf_metrics") or {}
        lag = r.get("benchmark_lag_days")
        scope = {"broad_market": "全市场指数", "style_proxy": "风格ETF代理",
                 "not_applicable": "无合适基准"}.get(r.get("benchmark_scope"), "—")
        bcell = "—" if not b else "%s %s" % (b.get("code") or "—", b.get("name") or "")
        ecell = "—" if not etf else "%s %s" % (etf.get("code") or "—", etf.get("name") or "")
        rows.append([label_style(r.get("style_code")), r.get("members"), scope,
                     bcell,
                     _fmt(m.get("annual_1y"), 2, "%"),
                     _fmt(r.get("peer_median_1y_pct"), 2, "%"),
                     _fmt(r.get("excess_1y_pct"), 2, "pp"),
                     ecell,
                     _fmt(etf_m.get("annual_1y"), 2, "%"),
                     "%s%s" % (m.get("as_of") or "—",
                                "" if lag is None else "（滞后%d天）" % lag)])
    if not rows:
        return "本轮没有可用的真实指数或 ETF 代理基准数据。"
    return _table(["风格", "正式榜只数", "基准类型", "基准", "基准1年", "peer中位1年",
                   "相对基准差1年", "参考ETF", "ETF1年", "基准日"], rows)

def _capacity_table(cap: dict[str, Any], field: str) -> str:
    buckets = [b["bucket"] for b in (cap.get("overall") or [])]
    if not buckets:
        return "本轮没有可用的规模/业绩交集。"
    body = []
    overall = {b["bucket"]: b for b in (cap.get("overall") or [])}
    body.append(["全部"] + [_fmt((overall.get(b) or {}).get(field), 2, "%")
                            + "（%s）" % (overall.get(b) or {}).get("n", 0)
                            for b in buckets])
    for s in cap.get("styles") or []:
        cells = {b["bucket"]: b for b in s.get("buckets") or []}
        body.append([s.get("label")] + [_fmt((cells.get(b) or {}).get(field), 2, "%")
                                        + "（%s）" % (cells.get(b) or {}).get("n", 0)
                                        for b in buckets])
    return _table(["风格"] + buckets, body)


def _uncovered_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "没有登记未实现的类型。"
    body = [[r["label"], r["funds"], "；".join(r.get("gaps") or [])] for r in rows]
    return _table(["类型", "最新目录数量", "未纳入原因（缺口）"], body)


def _objective_table(obj: dict[str, Any]) -> str:
    return _table(["维度", "判定", "证据", "目标"], objective.table_rows(obj))


def _shadow_table(sh: dict[str, Any]) -> str:
    fixed = sh.get("fixed_style") or {}
    q70 = sh.get("dynamic_q70_k5") or {}
    q60 = sh.get("dynamic_q60_k4") or {}
    rows = [
        ["固定线风格化", fixed.get("actual_count"), fixed.get("shadow_count"),
         len(fixed.get("entered") or []), len(fixed.get("exited") or []),
         "只把 L6C 换成 STYLE_FLOORS，保留 composite≥62 与 3 年完整"],
        ["动态层 q70 / k5", q70.get("actual"), q70.get("shadow"),
         q70.get("entered"), q70.get("exited"),
         "生产口径的近似复现；母体用 L6C–L9，差 8 只"],
        ["动态层 q60 / k4", q60.get("actual"), q60.get("shadow"),
         q60.get("entered"), q60.get("exited"),
         "更松分位 + 少一维，新增 27 只，未验证"],
    ]
    return _table(["方案", "实际通过", "影子通过", "新增", "退出", "说明"], rows)


def build(conn, cfg, run_id: str, run: dict, boards: dict[str, list[dict[str, Any]]],
          gate_result: dict[str, Any] | None = None) -> dict[str, Any]:
    stable = boards.get("bond_stable", [])
    enhanced = boards.get("bond_enhanced", [])
    long_rows = boards.get("bond_long", [])
    core10 = boards.get("bond_core10", [])
    formal = stable + enhanced
    l9_pass, published, folded = _formal_counts(run, formal)
    counts = {b: len(v) for b, v in boards.items()}
    uncovered = fundtypes.uncovered(conn)
    data_asof = _data_asof(conn)
    prov = _provenance(conn, cfg, gate_result)
    funnel = _funnel(conn, run_id)
    reasons = _reject_reasons(conn, run_id)
    cov = _coverage(conn, run_id)
    fwd = forward.summary(conn)
    oos_res = oos.load_cached(cfg, run["formula_contract"])
    evidence = cfg.get("evidence") or {}
    ev_match = _evidence_match(run, evidence)
    ev_label = _evidence_label(run, evidence)
    gate_result = gate_result or {}
    portfolio = _portfolio(conn, run, core10, formal_rows=formal)
    bench = _style_benchmarks(conn, run, formal)
    bmark = benchmarks.summary(conn, run["formula_contract"], formal,
                                   as_of=run["metric_as_of"])
    try:
        cap = capacity.summary(conn, cfg, as_of=run["metric_as_of"],
                               contract=run["formula_contract"])
    except Exception:
        cap = None
    try:
        shadow = style_shadow.report_summary(conn, run_id)
    except Exception:
        shadow = None
    # DSR 是历史证据（as_of 可能早于本次净值日），仍应显示真实值；
    # 是否“本次证据”由 ev_match/ev_label 单独标注，不能用 None 掩盖已知的历史 DSR。
    objective_result = objective.evaluate(
        formal, bmark, fwd, evidence.get("dsr"),
        float(cfg.get("evidence", "dsr_threshold", default=0.95) or 0.95))
    cross = _cross_board(boards)
    risk_counts: dict[str, int] = {}
    for r in formal:
        key = r.get("risk_status") or "none"
        risk_counts[key] = risk_counts.get(key, 0) + 1

    sections: list[dict[str, Any]] = []

    def add(sid: str, title: str, md: str, data: Any = None) -> None:
        sections.append({"id": sid, "title": title, "markdown": md, "data": data})

    conclusion = [
        "本轮 %s 只债券型/指数型固收份额中，%s 只通过基础门禁；L9 通过 %s 只，"
        "再经 family/company、增强风格/总量配额与双榜目标/比例截断后 %s 只进入正式榜"
        "（稳健 %s、增强 %s；折叠 %s），长期核心观察榜 %s 只。"
        % (run["universe_count"], run["base_pass_count"], l9_pass, published,
           len(stable), len(enhanced), folded, len(long_rows)),
        "**这是候选池，不是评级、不是单一指标排名，也不是买入建议。** 通过筛选只说明它满足了"
        "可投性、规模、费用、风险与历史业绩的规则条件，不说明未来收益。",
        "证据等级 %s：前瞻 cohort %s 个、成熟正式观测 %s 条。"
        "%s：%s 队列排序分对前瞻年化的 IC %s、对最大回撤 IC %s；%s"
        % (fwd["evidence_label"], fwd["cohorts"], fwd["matured_formal_observations"],
           ev_label, evidence.get("cohorts", "—"),
           evidence.get("rank_ic_annual", "—"), evidence.get("rank_ic_mdd", "—"),
           ("DSR 未估计（队列数不足以估计，不能用旧值代替）。"
            if evidence.get("dsr") is None else
            "DSR %s（n_trials=%s）；DSR<0.95，不能拒绝“无选基技能”的原假设。"
            % (evidence.get("dsr"), evidence.get("n_trials", "—")))),
    ]
    if str(evidence.get("status") or "") == "stale_recompute_pending":
        conclusion.append("**旧 12 队列 IC/DSR 待重算**：walkforward 的 metric_end_date 前视 bug "
                          "于 2026-11-06 修复；上段数字是修复前口径，不能当作当前技能证据。")
    _sv_note = _survivorship_note(evidence)
    if _sv_note:
        conclusion.append(_sv_note)
    if not long_rows:
        conclusion.append("本轮长期核心观察榜为空：该榜要求 3 年完整业绩、5 年覆盖≥60% 且 5 年年化非空、"
                          "经理任职≥730 天且覆盖 3 年窗口≥80%、份额≥2 亿元；为空说明严格条件没有同时满足的标的，"
                          "不是数据故障。")
    conclusion.append(
        "**目标函数（%s）**：%s\n\n" % (objective.VERSION, objective.DEFINITION)
        + _objective_table(objective_result)
        + "\n\n**判定：%s**" % objective_result["headline"])
    add("conclusion", "1. 一页结论", "\n\n".join(conclusion),
        {"counts": counts, "evidence_level": fwd["evidence_label"],
         "evidence_asof": evidence.get("as_of"), "evidence_match": ev_match,
         "rank_ic_annual": evidence.get("rank_ic_annual"),
         "rank_ic_mdd": evidence.get("rank_ic_mdd"), "dsr": evidence.get("dsr")})

    run_meta = _table(["字段", "值"], [
        ["run_id", run_id], ["business_date", run["business_date"]],
        ["nav_as_of", run["nav_as_of"]], ["metric_as_of", run["metric_as_of"]],
        ["profile", run["profile"]], ["screen_contract", run["screen_contract"]],
        ["formula_contract", run["formula_contract"]], ["universe", run["universe_count"]],
        ["base_pass", run["base_pass_count"]], ["L9 通过", l9_pass],
        ["family/company 折叠", folded], ["正式榜发布", published],
    ])
    cov_md = _table(["指标", "数量"], [
        ["有指标基金", cov["funds"]], ["1 年可用", cov["with_1y"]],
        ["3 年可用", cov["with_3y"]], ["5 年可用", cov["with_5y"]],
    ])
    holding_period = data_asof["holding_report_period"]
    holding_end = _period_end(holding_period)
    holding_label = ("%s（%s）" % (holding_period, holding_end)
                     if holding_period and holding_end and holding_period != holding_end
                     else holding_period)
    asof_md = _table(["数据", "最新日期", "距 metric_as_of（天）"], [
        ["持仓报告期", holding_label,
         _age_days(holding_end, run["metric_as_of"])],
        ["费率观测日", data_asof["fee_observed_at"],
         _age_days(data_asof["fee_observed_at"], run["metric_as_of"])],
        ["规模报告期", data_asof["scale_report_period"],
         _age_days(data_asof["scale_report_period"], run["metric_as_of"])],
        ["现任经理起始日（最新）", data_asof["manager_start_date"],
         _age_days(data_asof["manager_start_date"], run["metric_as_of"])],
        ["profile 更新时间", data_asof["profile_updated_at"],
         _age_days(data_asof["profile_updated_at"], run["metric_as_of"])],
    ])
    add("scope", "2. 数据与范围",
        "本轮日期、契约与候选范围如下；3/5 年覆盖决定长期证据能覆盖多少只基金。\n\n"
        + run_meta + "\n\n" + cov_md +
        "\n\n**数据时点与陈旧度**（持仓、费率、规模、经理均为季度/事件披露，不是每日更新）：\n\n"
        + asof_md +
        "\n\n" + _provenance_text(prov) +
        "\n\n正式榜要求 3 年窗口有效年数≥2.7 年（is_full_3y）；1–3 年基金只进入新锐榜，"
        "不靠综合分上限被动淘汰。"
        "\n\n**范围外类型（只登记、不筛选）**：下表类型不在本轮债券 universe 内，"
        "数量是最新公开目录快照，不是候选；它们的净值口径/风格/阈值/榜单语义尚未标定，"
        "不能混入债券榜，也不能用债券固定线评价。\n\n"
        + _uncovered_table(uncovered), {"run": {k: run[k] for k in run.keys()}, "coverage": cov,
                                        "data_asof": data_asof, "uncovered": uncovered})

    funnel_rows = [[f["label"], f["layer"], f["count"], "%.2f%%" % f["share_pct"]] for f in funnel]
    reason_rows = [[r["code"], r["count"]] for r in reasons]
    add("funnel", "3. 漏斗与淘汰原因",
        "按决策层级统计的终态；同一只基金只记最终层。\n\n"
        + _table(["层级", "代码", "数量", "占候选"], funnel_rows)
        + "\n\n最常见拒绝原因（可多重）：\n\n"
        + _table(["原因", "次数"], reason_rows), {"funnel": funnel, "reasons": reasons})

    if long_rows:
        long_buckets = fbm.load_map(conn, run["business_date"])
        long_md = (_long_table(long_rows, long_buckets)
                   + "\n\n### 长期指标明细（3年/5年）\n\n"
                   + _metrics_table(long_rows, 20, bench=bench))
    else:
        long_md = ("本轮没有基金同时满足长期核心观察榜条件。该榜是影子观察榜，"
                   "不替换稳健榜；为空时不要用稳健榜前十替代长期结论。")
    add("long", "4. 长期核心观察榜（bond_long）",
        "评分 = 0.6×长期正式分 + 0.4×长期核心分；资格：3 年完整、5 年覆盖≥60% 且 5 年年化非空、"
        "经理任职≥730 天且覆盖 3 年窗口≥80%、份额≥2 亿元、核心风格。该榜不进入 FORMAL_BOARDS。"
        "核心风格包含短债；短债入榜只代表低波动与证据长度，不代表久期匹配长期目标，"
        "久期桶列用于识别这一点。\n\n" + long_md,
        {"rows": long_rows})

    add("formal", "5. 稳健榜与增强榜",
        "排名按排序分降序；正式分用于准入/质量，不是排序主键。"
        "两列并列显示，避免“排名 1 的分数低于排名 2”。\n\n"
        "**两条准入路径不同，不能共用“固定线”表述：**稳健榜走 L6C 九项固定线"
        "（综合分≥62、1 年年化≥2%、Sharpe≥0.8、Calmar≥2.8、非负日≥70%、"
        "1 年 MDD≤2%、1 年 Vol≤6%、持有 1 年成本≤0.90%、3 年完整业绩）；"
        "增强榜走增强债相对准入（同风格分位+地板+严格口径 7 项至少 5 项+正式分≥55），"
        "不要求通过 L6C 的 70%/2%/0.90% 绝对线，因此增强榜的“为什么选它”不能写成"
        "“通过正式榜固定线”。\n\n"
        "同风格基准是同一 style_code 下所有有指标基金的等权中位数，只作 peer 对照，"
        "不是可交易债券指数；真实指数对照见第 6 节，报告不宣称“跑赢基准”。\n\n### 稳健榜前十\n\n"
        + _formal_table(stable, bench=bench) + "\n\n### 稳健榜前10 长期指标\n\n"
        + _metrics_table(stable, 10, bench=bench)
        + "\n\n### 增强榜前十\n\n" + _formal_table(enhanced, bench=bench)
        + "\n\n### 增强榜前10 长期指标\n\n" + _metrics_table(enhanced, 10, bench=bench),
        {"stable": stable[:10], "enhanced": enhanced[:10], "style_bench": bench})

    add("benchmark", "6. 基准对照（中债-新综合财富 + 参考ETF）",
        "稳健类风格使用中国债券信息网公开接口的**中债-新综合财富（总值）指数**（CBC_CFZS_00）"
        "作为全市场债券总回报参照；它覆盖银行间与交易所债券，不是任何一只基金的合同业绩比较基准，"
        "也不是单风格指数。增强类风格（可转债/一级/二级）含权益或转债暴露，纯债指数不是合适对照，"
        "也没有可替代的单一债券指数，因此标为“无合适基准”；对应 ETF 只列作风格参考，不计算超额。"
        "表中“相对基准差”= 正式榜同风格中位数 − 该行所选基准收益，只作研究对照，"
        "不构成业绩承诺；指数/ETF 日与基金净值日不同步时以表内日期为准。\n\n"
        + _benchmark_table(bmark)
        + "\n\n" + bmark.get("note", ""),
        {"benchmark": bmark})

    if shadow:
        shadow_md = (
            "这一节回答“如果换阈值会怎样”，不是生产结果。影子只读 screen_decision，"
            "不写 screen_pick；q60/k4 的新增基金没有经过前瞻验证，不能直接当作推荐。\n\n"
            + _shadow_table(shadow)
            + "\n\n" + shadow.get("note", ""))
        add("shadow", "7. 影子对照（研究，不改变榜单）", shadow_md,
            {"shadow": shadow})

    if portfolio:
        p = portfolio
        dur = p.get("duration") or {}
        pmd = _table(["指标", "值"], [
            ["参与基金", "%s/%s" % (p.get("series_used"), p.get("requested"))],
            ["平均两两相关", _fmt(p.get("avg_pairwise_corr"), 3)],
            ["最大两两相关", _corr_text(p.get("max_pairwise_corr"))],
            ["有效 bet", _fmt(p.get("effective_bets"), 3)],
            ["风险预算", ("达标" if (p.get("risk_budget") or {}).get("status") == "meets_target"
                         else ("降级为集中观察" if (p.get("risk_budget") or {}).get("status") == "downgraded"
                               else "不可计算"))
             + "（目标 %s）" % _fmt(T.CORE10_TARGET_EFFECTIVE_BETS, 1)],
            ["分散化比率", _fmt(p.get("diversification_ratio"), 3)],
            ["久期 beta p10/p50/p90", "%s / %s / %s" % (_fmt(dur.get("p10"), 3),
                                                   _fmt(dur.get("p50"), 3), _fmt(dur.get("p90"), 3))],
            ["久期 beta 跨度", _fmt(dur.get("spread"), 3)],
            ["core10 久期桶 short/mid/long/unknown",
             "%s / %s / %s / %s" % tuple((p.get("factor_buckets") or {}).get("duration", {}).get(k, 0)
                                          for k in ("short", "mid", "long", "unknown"))],
            ["core10 信用桶 low/mid/high/unknown",
             "%s / %s / %s / %s" % tuple((p.get("factor_buckets") or {}).get("credit", {}).get(k, 0)
                                          for k in ("low", "mid", "high", "unknown"))],
        ])
        sd = p.get("style_duration") or {}
        matrix = sd.get("matrix") or {}
        disp = sd.get("dispersion") or {}
        sd_rows = []
        for style in sorted(matrix):
            m = matrix[style]
            d = disp.get(style) or {}
            sd_rows.append([label_style(style), m.get("short", 0), m.get("mid", 0),
                            m.get("long", 0), m.get("unknown", 0),
                            _fmt(d.get("p10"), 3), _fmt(d.get("p50"), 3),
                            _fmt(d.get("p90"), 3), _fmt(d.get("spread"), 3)])
        if sd_rows:
            pmd += ("\n\n### 正式榜风格 × 久期桶\n\n"
                    + _table(["风格", "短久期", "中久期", "长久期", "未知",
                              "beta p10", "beta p50", "beta p90", "跨度"], sd_rows)
                    + "\n\n桶定义：raw beta_duration <0.25 短、0.25–0.75 中、≥0.75 长；"
                      "覆盖<60% 或 R²<0.05 记未知。风格标签没有隔离久期，同一风格内"
                      "仍可能同时有短久期与长久期基金。")
        mdd_rows = [[x["fcode"], _fmt(x["contribution_pct"], 3, "%")]
                    for x in (p.get("mdd_contribution") or [])]
        port_md = pmd
        if mdd_rows:
            port_md += ("\n\n最大回撤贡献（等权组合，负值越大拖累越大）：\n\n"
                        + _table(["代码", "贡献"], mdd_rows))
        eff = p.get("effective_bets")
        target = T.CORE10_TARGET_EFFECTIVE_BETS
        if eff is None:
            eff_txt = "有效 bet 无法计算（共同净值不足）"
        elif eff >= target:
            eff_txt = ("有效 bet %s，达到目标 %s；仍低于 10，不能当成 10 个独立 bet"
                       % (_fmt(eff, 3), _fmt(target, 1)))
        else:
            eff_txt = ("有效 bet %s，低于目标 %s；core10 只能作为集中观察组合，"
                       "不能宣称已实现分散化，也不应据此做等权配置"
                       % (_fmt(eff, 3), _fmt(target, 1)))
        port_md += ("\n\n**组合诊断结论**：core10 是集中观察组合，不是 10 个独立 bet；"
                    + eff_txt + "。风格/久期暴露高度重叠时，榜单名次不等于组合风险预算。")
    else:
        port_md = "本轮 core10 为空或共同净值不足，无法做组合诊断。"
    cross_md = ("跨榜 family 去重：5 个榜单共 %s 行；唯一代码 %s 只；唯一 family %s 个；"
                "重复 family %s 个（最多 %s 行）。core/core10 与 stable 重叠是视图重复，"
                "不能把多榜重复当独立推荐。"
                % (cross.get("rows"), cross.get("unique_codes"),
                   cross.get("unique_families"), cross.get("duplicate_families"),
                   cross.get("max_family_rows")))
    add("portfolio", "8. 组合体检",
        port_md + "\n\n" + cross_md,
        {"portfolio": portfolio, "cross_board": cross})

    core10_md, core10_data = _core10_section(
        core10, stable, fbm.load_map(conn, run["business_date"]), portfolio)
    add("core10", "9. 核心精选 10 只（bond_core10）", core10_md, core10_data)

    if cap and (cap.get("styles") or cap.get("overall")):
        cap_md = (
            "按风格×规模桶看 1/3 年业绩中位数；每格括号内是该桶只数，少于 %s 只时中位数为 null。"
            "规模按 family 汇总、业绩按份额，风格用 as-of 快照（命中历史 %s 只）。"
            "**这是研究区间，不是规模上限**：只有 OOS 与前瞻同时支持时才可设硬上限；"
            "当前全市场大桶并未系统性更差，不能据此硬砍大基金。\n\n### 1 年业绩中位数\n\n"
            % (cap.get("min_bucket_count"), cap.get("style_history_hits"))
            + _capacity_table(cap, "annual_1y_pct")
            + "\n\n### 3 年业绩中位数\n\n"
            + _capacity_table(cap, "annual_3y_pct")
            + "\n\n" + cap.get("note", ""))
        add("capacity", "10. 规模与容量（研究）", cap_md, {"capacity": cap})
    else:
        add("capacity", "10. 规模与容量（研究）",
            "本轮没有可用的规模/业绩交集，无法画容量曲线。", {"capacity": cap})

    add("fees", "11. 费用与持有期",
        "下表按 3 年持有期成本推荐同 family 份额；1/3/5 年各自最便宜见 API 的 "
        "recommended_by_horizon。赎回费未计入，实际选择还要看赎回条款与个人持有期。\n\n"
        + _share_table(stable, 10), {"share_class": [r.get("share_class_compare") for r in stable[:10]]})

    gate_lines = []
    if gate_result:
        gate_lines.append("发布门禁状态：**%s**" % gate_result.get("status", "—"))
        for c in gate_result.get("checks", []) or []:
            if c.get("status") == "warn":
                gate_lines.append("- warning %s：%s" % (c.get("code"), c.get("detail")))
            elif c.get("status") == "block":
                gate_lines.append("- block %s：%s" % (c.get("code"), c.get("detail")))
    risk_md = "\n".join(gate_lines) or "本轮没有门禁结果。"
    risk_md += ("\n\n**反面证据必须与榜单一起读：**\n\n"
                "- 前瞻证据 %s，成熟正式观测 %s 条；LEVEL_1 表示还没有足够成熟样本验证排序。\n"
                "- 证据口径：%s。\n"
                "- 12 队列排序分 IC（旧口径，待重算）：年化 %s、最大回撤 %s；DSR %s。\n"
                "- 排序分 = 0.85×综合分 + 0.15×6 个月收益分位；综合分含 3/5 年权重，"
                "但 1 年指标仍是重要组成，不能当作完整周期能力。\n"
                "- 历史净值有幸存者偏差；已用 EID 公告回填 445 只清盘债券基金（1,819 条中的一部分），"
                "合并样本仍为 0，只能算部分校正，不能宣称无偏。\n"
                "- 风格分组主要来自名称/类型与转债持仓，未完全隔离久期与信用暴露。"
                % (fwd["evidence_label"], fwd["matured_formal_observations"], ev_label,
                   evidence.get("rank_ic_annual", "—"), evidence.get("rank_ic_mdd", "—"),
                   evidence.get("dsr", "—")))
    lc = lifecycle.counts(conn)
    if lc.get("funds", 0) == 0:
        risk_md += ("\n- 生命周期表 0 行：清盘/合并样本尚未导入，回测与前瞻的幸存者偏差"
                    "没有校正；当前库里只有存续基金。")
    else:
        risk_md += ("\n- 生命周期样本 %s 只（清盘 %s、合并 %s；其中 %s 只有历史净值）已入库，"
                    "用于可投性判断与披露；`walkforward` 已可纳入回填的 445 只清盘债券基金，"
                    "但 1,819 条 EID 清盘中只有 458 只有公开净值、合并样本 0，历史 IC/DSR 的幸存者偏差"
                    "只做了部分校正，不能宣称无偏。"
                    % (lc.get("funds"), lc.get("liquidated", 0), lc.get("merged", 0),
                       lc.get("with_nav", 0)))
    sv = survivorship.summary(conn, lookback_days=30, limit=10)
    if sv.get("status") == "ok":
        risk_md += ("\n\n**幸存者偏差诊断（公开目录快照）**：目录快照 %s 天（%s 至 %s），"
                    "基线 %s 只、最新 %s 只；最新目录中不再出现的债券代码 %s 只（候选，"
                    "不等于清盘/合并）。候选不进入筛选宇宙，也不改变任何榜单。"
                    % (sv["snapshots"], sv["first_date"], sv["latest_date"],
                       sv["baseline_count"], sv["latest_count"], sv["bond_missing_count"]))
        if sv.get("candidates"):
            sv_rows = [[c["fcode"], c["name"], c["last_seen"], _fmt(c["days_missing"], 0),
                        c["nav_last_date"] or "—",
                        _fmt(c["nav_stale_days"], 0) if c.get("nav_stale_days") is not None else "—"]
                       for c in sv["candidates"]]
            risk_md += ("\n\n" + _table(
                ["代码", "名称", "最后出现在目录", "缺失天数", "最后净值日", "净值停更天数"],
                sv_rows))
        risk_md += ("\n\n" + sv["note"])
    else:
        risk_md += ("\n\n**幸存者偏差诊断**：%s" % sv.get("note", "目录历史不足。"))
    add("risk", "12. 风险与反面证据", risk_md,
        {"gate": gate_result, "risk_counts": risk_counts, "forward": fwd,
         "survivorship": sv})

    fwd_rows = []
    for x in (fwd.get("rows") or []):
        fwd_rows.append([x.get("business_date"), x.get("horizon_days"),
                         _fmt((x.get("formal") or {}).get("n"), 0),
                         _fmt((x.get("formal") or {}).get("median"), 3),
                         _fmt(x.get("excess_median_pct"), 3),
                         _fmt(x.get("excess_win_rate_pct"), 2)])
    if oos_res and oos_res.get("status") == "ok":
        oos_md = ("\n\n**季度 OOS 研究模型（research_only，不改变榜单）**："
                  "训练 %s 季、测试 %s 季（%s 至 %s）；样本外 mean IC %s（t=%s），"
                  "多空组合季度均值 %s，DSR %s（n_trials=%s）。"
                  "这只是风格内去均值后的季度截面检验，不是久期/信用/转债因子中性化；"
                  "样本少、DSR 未达 0.95，不能据此宣称有选基技能，也不切换生产阈值。"
                  % (oos_res.get("train_quarters"), oos_res.get("test_quarters"),
                     (oos_res.get("test_period") or ["—", "—"])[0],
                     (oos_res.get("test_period") or ["—", "—"])[1],
                     _fmt(oos_res.get("mean_ic"), 3), _fmt(oos_res.get("ic_t"), 2),
                     _fmt(oos_res.get("long_short_mean"), 4), _fmt(oos_res.get("dsr"), 3),
                     len(oos_res.get("features") or [])))
    else:
        oos_md = ("\n\n**季度 OOS 研究模型**：尚未运行；"
                  "运行 python3 -m fundana oos 后，报告会引用最近一次缓存结果。"
                  "该模型是研究项，不改变筛选与榜单。")

    add("evidence", "13. 证据等级与统计显著性",
        "证据等级 **%s**；cohorts %s，成熟正式观测 %s，不同决策月份 %s。"
        "证据口径：%s。"
        "最近到期 %s（还有 %s 天，%s 天观察期）。下表列出全部 cohort×期限，未到期不写收益。\n\n"
        % (fwd["evidence_label"], fwd["cohorts"], fwd["matured_formal_observations"],
           fwd["distinct_decision_months"], ev_label, fwd.get("next_maturity_date") or "—",
           fwd.get("next_maturity_days") if fwd.get("next_maturity_days") is not None else "—",
           fwd.get("next_maturity_horizon_days") or "—")
        + _table(["决策日", "期限(天)", "正式样本", "正式中位数", "超额中位数", "胜率"],
                 fwd_rows)
        + "\n\nLEVEL_3 需要≥12 个不同决策月份且≥100 个成熟观测；LEVEL_4 需要费后净收益验证，"
          "本阶段不宣称。" + oos_md, {"forward": fwd, "config": evidence,
                                     "evidence_match": ev_match, "oos": oos_res})

    add("method", "14. 方法与附录",
        "筛选契约 %s，公式契约 %s。完整审计见 docs/audit-2026-11-review4.md；"
        "复现命令：python3 -m fundana screen --profile bond_v1、python3 -m fundana publish。\n\n"
        "固定线（仅稳健榜 L6C）：综合分≥62、1 年非负日占比≥70%%、3 年完整业绩、"
        "1 年成本≤0.90%%、Sharpe≥0.8、Calmar≥2.8、MDD≤2%%、Vol≤6%%。"
        "L5.5 另设 1 年成本硬天花板≤1.30%%，不是 L6C 固定线。增强榜不执行这组固定线，"
        "走增强债相对准入。动态层在可投域（L1–L5.5）上取无条件 q70，再 max(62,q70)，"
        "并要求 5/6 个动态维度通过；动态阈值不可用时按失败处理。"
        % (run["screen_contract"], run["formula_contract"]),
        {"screen_contract": run["screen_contract"], "formula_contract": run["formula_contract"]})

    facts = {
        "l9_pass": l9_pass,
        "published": published,
        "folded": folded,
        "formal_by_board": {"bond_stable": len(stable),
                            "bond_enhanced": len(enhanced)},
        "board_counts": counts,
        "cohorts": fwd["cohorts"],
        "matured_formal_observations": fwd["matured_formal_observations"],
        "data_asof": data_asof,
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": db.now_iso(),
        "run": {k: run[k] for k in run.keys()},
        "counts": counts,
        "facts": facts,
        "benchmark": bmark,
        "objective": objective_result,
        "evidence_level": fwd["evidence_label"],
        "sections": sections,
    }
    md = ["# 债券型基金筛选报告 v4", "",
          "run %s · 业务日 %s · 净值日 %s · 指标日 %s · 契约 %s"
          % (run_id, run["business_date"], run["nav_as_of"], run["metric_as_of"],
             run["screen_contract"]), "",
          "> 这是候选池与证据报告，不是买入建议。历史业绩不代表未来。", ""]
    for s in sections:
        md.append("## " + s["title"])
        md.append("")
        md.append(s["markdown"])
        md.append("")
    report["markdown"] = "\n".join(md)
    return report


def write(rev: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    errors = report_schema.validate(report)
    if errors:
        raise ValueError("报告结构不符合契约：" + "；".join(errors))
    md_path = rev / "report.md"
    json_path = rev / "report.json"
    md_path.write_text(report["markdown"], encoding="utf-8")
    json_path.write_text(json.dumps({k: v for k, v in report.items() if k != "markdown"},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path
