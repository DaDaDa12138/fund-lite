"""长期质量指标与风险标签（ADR-083）。

这里只回答“这只基金长期质量如何、有哪些需要人工复核的风险”，不直接改正式分：
- MRAR(2) 三年评级：36 个月月度有效收益，口径与晨星 3 年评级一致；
- 因子 alpha：来自 factors 表的原始收益 OLS + HAC t 值；
- 尾部/持仓/持有人/经理/规模/费用标签：只做观察清单，不参与 L1–L10。

所有缺失都返回 None/unknown，不用 0 或短期值冒充。
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from . import db, factors, fundtypes, risk_gate
from .fees.holding import holding_cost
from .metrics.tail import TAIL_CONTRACT, TAIL_WINDOW_DAYS

MRAR_METHOD = "mrar2-monthly"
MRAR_ANOMALY_METHOD = "mrar2-monthly:anomaly"
DEFAULT_MRAR_MONTHS = 36
DEFAULT_RISK_AVERSION = 2.0
DEFAULT_MAX_ABS_MONTHLY_RETURN = 0.60
DEFAULT_ALPHA_MIN_R2 = 0.30
DEFAULT_TAIL_MIN_OBS = 100


def _rf(cfg) -> float:
    return float(cfg.get("risk", "risk_free_rate", default=0.0164)) if cfg else 0.0164


def _prof(cfg) -> dict[str, Any]:
    if cfg is None:
        return {}
    return cfg.get("quality_profile", default={}) or {}


def _month_ends(conn, fcode: str, as_of: str, months: int) -> list[tuple[str, float]] | None:
    """返回最近 months+1 个月末的 (YYYY-MM, 复权净值)，用于算 months 个月度收益。

    同一个月取最后一个有效观测；acc_nav 与 unit_nav 必须至少有一种在全部月份可用，
    不允许中途切换口径（会把分红/拆分当成收益）。
    """
    rows = conn.execute(
        "SELECT nav_date, unit_nav, acc_nav FROM fund_nav_daily"
        " WHERE fcode=? AND nav_date<=? AND (unit_nav IS NOT NULL OR acc_nav IS NOT NULL)"
        " ORDER BY nav_date", (fcode, as_of)).fetchall()
    by_month: dict[str, tuple[float | None, float | None]] = {}
    for r in rows:
        by_month[r["nav_date"][:7]] = (r["unit_nav"], r["acc_nav"])
    keys = sorted(by_month)[-(months + 1):]
    if len(keys) < months + 1:
        return None
    unit = [by_month[k][0] for k in keys]
    acc = [by_month[k][1] for k in keys]
    if all(v is not None and v > 0 for v in acc):
        vals = [float(v) for v in acc]
    elif all(v is not None and v > 0 for v in unit):
        vals = [float(v) for v in unit]
    else:
        return None
    return list(zip(keys, vals))


def mrar(returns: Sequence[float], rf_annual: float = 0.0164,
         risk_aversion: float = DEFAULT_RISK_AVERSION) -> float | None:
    """MRAR(2)：月度有效收益序列 -> 年化小数；样本不足 36 个月返回 None。"""
    if len(returns) < 36:
        return None
    rf_m = (1.0 + rf_annual) ** (1.0 / 12.0) - 1.0
    total = 0.0
    for r in returns:
        g = (1.0 + r) / (1.0 + rf_m) - 1.0
        if 1.0 + g <= 0:
            return None
        total += (1.0 + g) ** (-risk_aversion)
    return (total / len(returns)) ** (-12.0 / risk_aversion) - 1.0


def compute_mrar(conn, cfg=None, *, as_of: str | None = None,
                 types: str | Iterable[str] | None = None, commit: bool = True) -> dict[str, Any]:
    """计算并写入 fund_quality_metric；只写有完整 36 个月观测的基金。"""
    prof = _prof(cfg)
    months = int(prof.get("mrar_months", DEFAULT_MRAR_MONTHS))
    min_months = int(prof.get("mrar_min_months", months))
    gamma = float(prof.get("mrar_risk_aversion", DEFAULT_RISK_AVERSION))
    max_abs_ret = float(prof.get("mrar_max_abs_monthly_return", DEFAULT_MAX_ABS_MONTHLY_RETURN))
    rf = _rf(cfg)
    if as_of is None:
        row = conn.execute("SELECT MAX(nav_date) AS d FROM fund_nav_daily").fetchone()
        as_of = row["d"] if row else None
    if not as_of:
        return {"as_of": None, "funds": 0, "computed": 0, "skipped": 0,
                "note": "fund_nav_daily 为空"}
    clause, params = fundtypes.where(types, column="type_group", cfg=cfg)
    codes = [r["fcode"] for r in conn.execute(
        f"SELECT fcode FROM fund WHERE {clause} ORDER BY fcode", params)]
    style = {r["fcode"]: r["style_code"] for r in conn.execute(
        "SELECT fcode, style_code FROM fund_style")}
    values: list[tuple[str, str, float, int, str, str]] = []
    anomalies: list[tuple[str, str, int, str, str]] = []
    skipped = 0
    for code in codes:
        obs = _month_ends(conn, code, as_of, months)
        if not obs:
            skipped += 1
            continue
        rets = [obs[i][1] / obs[i - 1][1] - 1.0 for i in range(1, len(obs))]
        if any(abs(x) > max_abs_ret for x in rets):
            # 债券基金月度 ±60% 基本只可能是拆分/净值口径跳变；宁可留空，也不能让 -99% 进入分位。
            anomalies.append((code, style.get(code) or "", len(rets), obs[0][0], obs[-1][0]))
            continue
        val = mrar(rets, rf_annual=rf, risk_aversion=gamma)
        if val is None or len(rets) < min_months:
            skipped += 1
            continue
        values.append((code, style.get(code) or "", val, len(rets), obs[0][0], obs[-1][0]))
    # 同风格样本 <10 时退回全样本分位，避免小风格里“人人都是前 10%”。
    by_style: dict[str, list[float]] = {}
    for _code, st, val, _n, _s, _e in values:
        by_style.setdefault(st, []).append(val)
    all_vals = [v[2] for v in values]
    rows = []
    computed_at = db.now_iso()
    for code, st, n, start, end in anomalies:
        rows.append((as_of, code, MRAR_ANOMALY_METHOD, 0, None, 0, start, end,
                     rf, 0, None, computed_at))
    for code, st, val, n, start, end in values:
        peers = by_style.get(st) or []
        if len(peers) < 10:
            peers = all_vals
        peer_n = len(peers)
        peer_pct = (sum(1 for x in peers if x <= val) / peer_n) if peer_n else None
        rows.append((as_of, code, MRAR_METHOD, n, round(val * 100.0, 4), n, start, end,
                     rf, peer_n, None if peer_pct is None else round(peer_pct, 4), computed_at))
    if commit:
        conn.execute("DELETE FROM fund_quality_metric WHERE as_of=?", (as_of,))
        conn.executemany(
            "INSERT INTO fund_quality_metric(as_of, fcode, method, window_months, mrar_pct,"
            " n_obs, start_date, end_date, rf_annual, peer_n, peer_pct, computed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(as_of, fcode) DO UPDATE SET method=excluded.method,"
            " window_months=excluded.window_months, mrar_pct=excluded.mrar_pct,"
            " n_obs=excluded.n_obs, start_date=excluded.start_date,"
            " end_date=excluded.end_date, rf_annual=excluded.rf_annual,"
            " peer_n=excluded.peer_n, peer_pct=excluded.peer_pct,"
            " computed_at=excluded.computed_at", rows)
        conn.commit()
    return {"as_of": as_of, "funds": len(codes), "computed": len(values),
            "anomalies": len(anomalies), "skipped": skipped,
            "months": months, "rf_annual": rf, "risk_aversion": gamma,
            "max_abs_monthly_return": max_abs_ret, "method": MRAR_METHOD}


def latest_mrar(conn, fcode: str, as_of: str | None = None) -> dict[str, Any] | None:
    if as_of:
        row = conn.execute(
            "SELECT * FROM fund_quality_metric WHERE fcode=? AND as_of<=?"
            " ORDER BY as_of DESC LIMIT 1", (fcode, as_of)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM fund_quality_metric WHERE fcode=? ORDER BY as_of DESC LIMIT 1",
            (fcode,)).fetchone()
    return dict(row) if row else None


def _last_series_value(raw: str | None, name: str) -> float | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    for series in data.get("series") or []:
        if series.get("name") != name:
            continue
        vals = [v for v in (series.get("data") or []) if v is not None]
        return float(vals[-1]) if vals else None
    return None


def _peer_pct(conn, sql: str, params: tuple[Any, ...], value: float | None) -> float | None:
    if value is None:
        return None
    rows = [r[0] for r in conn.execute(sql, params) if r[0] is not None]
    if not rows:
        return None
    return round(sum(1 for x in rows if x <= value) / len(rows), 4)


def _tail_peer(conn, style: str, value: float | None) -> tuple[float | None, int]:
    if value is None:
        return None, 0
    sql = ("SELECT t.cvar5_dec FROM fund_tail_metric t JOIN fund_style s ON s.fcode=t.fcode"
           " WHERE t.window_days=? AND t.formula_contract=? AND s.style_code=?")
    params = (TAIL_WINDOW_DAYS, TAIL_CONTRACT, style)
    rows = [r[0] for r in conn.execute(sql, params) if r[0] is not None]
    if len(rows) < 10:
        rows = [r[0] for r in conn.execute(
            "SELECT cvar5_dec FROM fund_tail_metric WHERE window_days=? AND formula_contract=?",
            (TAIL_WINDOW_DAYS, TAIL_CONTRACT)) if r[0] is not None]
    if not rows:
        return None, 0
    return round(sum(1 for x in rows if x <= value) / len(rows), 4), len(rows)


def _fee_peer(conn, style: str, value: float | None) -> tuple[float | None, int]:
    if value is None:
        return None, 0
    sql = ("SELECT p.recurring_fee_pct FROM fund_profile p JOIN fund_style s ON s.fcode=p.fcode"
           " WHERE s.style_code=? AND p.recurring_fee_pct IS NOT NULL")
    rows = [r[0] for r in conn.execute(sql, (style,))]
    if len(rows) < 10:
        rows = [r[0] for r in conn.execute(
            "SELECT recurring_fee_pct FROM fund_profile WHERE recurring_fee_pct IS NOT NULL")]
    if not rows:
        return None, 0
    return round(sum(1 for x in rows if x <= value) / len(rows), 4), len(rows)


def _scale_change(conn, fcode: str, as_of: str) -> tuple[float | None, float | None]:
    rows = conn.execute(
        "SELECT report_period, asset_scale_billion FROM fund_scale_history"
        " WHERE fcode=? AND report_period<=? AND asset_scale_billion IS NOT NULL"
        " ORDER BY report_period DESC LIMIT 5", (fcode, as_of)).fetchall()
    if not rows:
        return None, None
    latest = rows[0]["asset_scale_billion"]
    old = rows[4]["asset_scale_billion"] if len(rows) >= 5 else None
    if latest is None or not old or old <= 0:
        return latest, None
    return latest, round((latest / old - 1.0) * 100.0, 2)


def _manager_tenure(conn, fcode: str, as_of: str) -> int | None:
    rows = conn.execute(
        "SELECT start_date FROM fund_manager_tenure WHERE fcode=? AND is_current=1",
        (fcode,)).fetchall()
    if not rows:
        return None
    end = date.fromisoformat(as_of)
    days = []
    for r in rows:
        try:
            days.append((end - date.fromisoformat(r["start_date"])).days)
        except ValueError:
            continue
    return min(days) if days else None


def _flag(code: str, label: str, status: str, value: Any, threshold: str,
          detail: str) -> dict[str, Any]:
    return {"code": code, "label": label, "status": status, "value": value,
            "threshold": threshold, "detail": detail}


def profile(conn, cfg, fcode: str, *, as_of: str | None = None) -> dict[str, Any]:
    """单只基金的质量/风险观察卡；全部字段可为 None，不用 0 伪造。"""
    if as_of is None:
        row = conn.execute("SELECT MAX(nav_date) AS d FROM fund_nav_daily").fetchone()
        as_of = row["d"] if row else db.today_cn()
    style_row = conn.execute("SELECT style_code FROM fund_style WHERE fcode=?", (fcode,)).fetchone()
    style = style_row["style_code"] if style_row else ""
    prof_row = conn.execute("SELECT * FROM fund_profile WHERE fcode=?", (fcode,)).fetchone()
    meta = conn.execute("SELECT * FROM fund_fee_meta WHERE fcode=?", (fcode,)).fetchone()
    policy = conn.execute("SELECT effective_days FROM fund_holding_policy WHERE fcode=?",
                          (fcode,)).fetchone()
    pzd = conn.execute("SELECT holder_json, asset_alloc_json FROM src_pzd_profile"
                       " WHERE code=? ORDER BY business_date DESC LIMIT 1", (fcode,)).fetchone()
    inst = _last_series_value(pzd["holder_json"] if pzd else None, "机构持有比例")
    stock = _last_series_value(pzd["asset_alloc_json"] if pzd else None, "股票占净比")
    hold_period = conn.execute(
        "SELECT MAX(report_period) FROM fund_holding_bond WHERE fcode=?", (fcode,)).fetchone()[0]
    top10 = None
    if hold_period:
        top10 = conn.execute(
            "SELECT SUM(pct_nav) FROM (SELECT pct_nav FROM fund_holding_bond"
            " WHERE fcode=? AND report_period=? ORDER BY pct_nav DESC LIMIT 10)",
            (fcode, hold_period)).fetchone()[0]
    scale, scale_change = _scale_change(conn, fcode, as_of)
    tenure = _manager_tenure(conn, fcode, as_of)
    recurring = prof_row["recurring_fee_pct"] if prof_row else None
    max_sub = prof_row["max_sub_fee_pct"] if prof_row else None
    has_discount = meta["has_discount"] if meta else None
    cost_1y = None
    if recurring is not None:
        hc = holding_cost(recurring, max_sub, has_discount, 365)
        cost_1y = None if hc is None else hc["total_pct"]
    mrar_row = latest_mrar(conn, fcode, as_of)
    mrar_val = mrar_row["mrar_pct"] if mrar_row else None
    mrar_peer = mrar_row["peer_pct"] if mrar_row else None
    fac = factors.latest(conn, fcode)
    alpha_val = fac.get("alpha_annual_pct") if fac else None
    alpha_t = fac.get("alpha_t") if fac else None
    alpha_r2 = (fac.get("window") or {}).get("r2") if fac else None
    alpha_rel = bool(fac.get("alpha_reliable")) if fac else False
    tail = conn.execute(
        "SELECT cvar5_dec, worst_rolling180d_pct, max_underwater_obs, ongoing_drawdown,"
        " completeness FROM fund_tail_metric WHERE fcode=? AND window_days=?"
        " AND formula_contract=? ORDER BY computed_at DESC LIMIT 1",
        (fcode, TAIL_WINDOW_DAYS, TAIL_CONTRACT)).fetchone()
    cvar = tail["cvar5_dec"] if tail else None
    tail_peer, tail_n = _tail_peer(conn, style, cvar)
    fee_peer, fee_n = _fee_peer(conn, style, recurring)
    flags: list[dict[str, Any]] = []
    if mrar_row and mrar_row["mrar_pct"] is None:
        flags.append(_flag("mrar", "长期风险调整收益（MRAR）", "unknown", None,
                           "同风格中位", "月度收益异常（疑似拆分/净值口径跳变），不参与分位"))
    elif mrar_peer is None:
        flags.append(_flag("mrar", "长期风险调整收益（MRAR）", "unknown", mrar_val,
                           "同风格中位", "不足 36 个月或未计算"))
    else:
        flags.append(_flag("mrar", "长期风险调整收益（MRAR）", "ok" if mrar_peer >= 0.5 else "watch",
                           mrar_val, "同风格中位", "同风格分位 %.0f%%" % (100 * mrar_peer)))
    if not alpha_rel:
        flags.append(_flag("alpha", "因子 alpha", "unknown", alpha_val,
                           "R²≥0.30 且 HAC t≥0", "样本不足 200 天或 R² 过低"))
    else:
        ok = alpha_t is not None and alpha_t >= 0 and (alpha_r2 or 0) >= DEFAULT_ALPHA_MIN_R2
        flags.append(_flag("alpha", "因子 alpha", "ok" if ok else "watch", alpha_val,
                           "R²≥0.30 且 HAC t≥0",
                           "t=%.2f, R²=%.3f" % (alpha_t or 0, alpha_r2 or 0)))
    if cvar is None:
        flags.append(_flag("tail", "尾部风险", "unknown", None, "CVaR5 不在同风格最差 20%",
                           "尾部样本不足"))
    else:
        ok = (tail_peer is not None and tail_peer > 0.20
              and (tail["ongoing_drawdown"] or 0) <= 5)
        flags.append(_flag("tail", "尾部风险", "ok" if ok else "watch", cvar,
                           "CVaR5 不在同风格最差 20% 且当前回撤≤5%",
                           "CVaR5 分位 %.0f%%，当前回撤 %s" % (
                               100 * (tail_peer or 0), tail["ongoing_drawdown"])))
    if tenure is None:
        flags.append(_flag("manager", "经理稳定性", "unknown", None, "现任经理最短任职≥365 天",
                           "无现任经理任职记录"))
    else:
        flags.append(_flag("manager", "经理稳定性", "ok" if tenure >= 365 else "watch", tenure,
                           "现任经理最短任职≥365 天", "最短 %d 天" % tenure))
    if scale is None:
        flags.append(_flag("scale", "规模稳定性", "unknown", None, "规模≥2 亿且一年变化>-50%",
                           "无规模历史"))
    else:
        floor = 1.5 if style == "bond_short" else 2.0
        ok = scale >= floor and (scale_change is None or scale_change > -50)
        flags.append(_flag("scale", "规模稳定性", "ok" if ok else "watch", scale,
                           "规模≥%.1f 亿且一年变化>-50%%" % floor,
                           "一年变化 %s%%" % ("—" if scale_change is None else "%.1f" % scale_change)))
    if cost_1y is None or fee_peer is None:
        flags.append(_flag("cost", "持有成本", "unknown", cost_1y, "经常费率不高于同风格中位",
                           "费率数据缺失"))
    else:
        flags.append(_flag("cost", "持有成本", "ok" if fee_peer <= 0.5 else "watch", cost_1y,
                           "经常费率不高于同风格中位", "同风格分位 %.0f%%" % (100 * fee_peer)))
    if style in ("bond_secondary_enhanced", "bond_primary_enhanced", "bond_convertible"):
        if stock is None:
            flags.append(_flag("equity", "权益暴露", "unknown", None, "股票占净比≤5%", "无资产配置数据"))
        else:
            flags.append(_flag("equity", "权益暴露", "ok" if stock <= 5 else "watch", stock,
                               "股票占净比≤5%", "最新股票占净比 %.2f%%" % stock))
    if top10 is None:
        flags.append(_flag("concentration", "持仓集中度", "unknown", None, "前十大债券≤60%",
                           "无最新持仓数据"))
    else:
        flags.append(_flag("concentration", "持仓集中度", "ok" if top10 <= 60 else "watch", top10,
                           "前十大债券≤60%", "最新前十大合计 %.2f%%" % top10))
    counts = {k: sum(1 for f in flags if f["status"] == k) for k in ("ok", "watch", "unknown")}
    status = "watch" if counts["watch"] else ("unknown" if counts["unknown"] == len(flags) else "ok")
    return {
        "as_of": as_of,
        "mrar": {"value_pct": mrar_val, "n_obs": mrar_row["n_obs"] if mrar_row else None,
                 "peer_pct": mrar_peer, "peer_n": mrar_row["peer_n"] if mrar_row else None,
                 "start_date": mrar_row["start_date"] if mrar_row else None,
                 "end_date": mrar_row["end_date"] if mrar_row else None,
                 "method": mrar_row["method"] if mrar_row else None},
        "alpha": {"annual_pct": alpha_val, "t": alpha_t, "r2": alpha_r2,
                  "reliable": alpha_rel},
        "tail": {"cvar5_pct": None if cvar is None else round(cvar * 100, 4),
                 "cvar5_peer_pct": tail_peer, "peer_n": tail_n,
                 "worst180_pct": tail["worst_rolling180d_pct"] if tail else None,
                 "max_underwater_obs": tail["max_underwater_obs"] if tail else None,
                 "ongoing_drawdown": tail["ongoing_drawdown"] if tail else None},
        "risk": {"inst_ratio_pct": inst, "stock_pct": stock,
                 "top10_bond_pct": None if top10 is None else round(top10, 2),
                 "manager_tenure_days": tenure, "scale_billion": scale,
                 "scale_change_1y_pct": scale_change, "cost_1y_pct": cost_1y,
                 "recurring_fee_pct": recurring, "fee_peer_pct": fee_peer,
                 "fee_peer_n": fee_n, "holding_period": hold_period},
        "flags": flags,
        "risk_gate": risk_gate.assess(conn, fcode, style, as_of),
        "summary": {"status": status, **counts, "total": len(flags)},
        "note": "质量标签只做观察/报告，不进入 L1–L10 正式分；"
                "MRAR 为 36 个月月度有效收益的晨星式 MRAR(2)，不足 36 个月留空。"
                "risk_gate 是独立风险门明细，block 会在筛选 L8R 拦截。",
    }


def build_map(conn, cfg, codes: Sequence[str], *, as_of: str | None = None) -> dict[str, dict[str, Any]]:
    return {code: profile(conn, cfg, code, as_of=as_of) for code in codes}
