"""指标引擎：读 L2 净值 → 逐周期计算 → 写 fund_metric。"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Iterable

from .. import db
from . import contract, risk, window as window_mod


def load_series(conn, fcode: str, as_of: str) -> list[window_mod.NavRow]:
    rows = conn.execute(
        "SELECT nav_date, unit_nav, acc_nav, growth_pct FROM fund_nav_daily"
        " WHERE fcode=? AND nav_date<=? ORDER BY nav_date", (fcode, as_of)).fetchall()
    return [window_mod.NavRow(r["nav_date"], r["unit_nav"], r["acc_nav"], r["growth_pct"]) for r in rows]


def nav_signature(rows: Iterable[window_mod.NavRow]) -> str:
    import hashlib
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r.nav_date}|{r.unit_nav}|{r.growth_pct};".encode())
    return h.hexdigest()[:32]


def compute_fund(conn, cfg, fcode: str, as_of: str, *, only_missing: bool = False,
                 dry_run: bool = False) -> dict[str, Any]:
    series = load_series(conn, fcode, as_of)
    if not series:
        return {"fcode": fcode, "ok": False, "reason": "no_nav", "periods": 0, "dry": {}}
    sig = nav_signature(series)
    rf = float(cfg.get("risk", "risk_free_rate", default=0.0164))
    mar = float(cfg.get("risk", "mar", default=0.0))
    trading_days = int(cfg.get("risk", "trading_days_per_year", default=252))
    caps = cfg.get("risk", "caps", default=contract.CAPS) or contract.CAPS
    grace = int(cfg.get("risk", "coverage_grace_days", default=7))
    contract_id = cfg.get("risk", "formula_contract", default=contract.FORMULA_CONTRACT)

    written = 0
    skipped = 0
    dry: dict[int, dict[str, Any]] = {}
    undefined: list[str] = []
    for period_days in contract.PERIODS:
        if only_missing:
            hit = conn.execute(
                "SELECT 1 FROM fund_metric WHERE fcode=? AND period_days=? AND formula_contract=?",
                (fcode, period_days, contract_id)).fetchone()
            if hit:
                skipped += 1
                continue
        win = window_mod.build_window(series, period_days, as_of, grace)
        returns, basis, proxy_rows = window_mod.resolve_returns(win, win.anchor_row)
        # 无效窗口（空窗/点数不足/缺口/覆盖不足）不允许出数：短历史不得冒称长周期指标。
        fatal = [r for r in win.reasons
                 if r.startswith(("empty_window", "insufficient_points", "coverage_gap", "gap_exceeded"))]
        if not returns or fatal:
            metrics: dict[str, Any] = {
                "n_obs": win.n, "median_gap_days": win.median_gap_days, "p75_gap_days": win.p75_gap_days,
                "min_points_used": win.min_points_used,
                "undefined_reasons": win.reasons or ["no_returns"],
                "years": (round(win.span_days / 365.25, 4) if win.n > 1 else None),
                "total_return_pct": None, "annual_dec": None, "vol_dec": None,
                "positive_ratio_pct": None, "flat_ratio_pct": None,
                "non_negative_ratio_pct": None, "mdd_dec": None, "dd_duration_obs": None,
                "sharpe": None, "sharpe_t": None, "sortino": None, "calmar_dec": None,
                "var95_dec": None, "cvar5_dec": None, "raw_sharpe": None, "raw_sortino": None,
                "raw_calmar": None, "sharpe_cap_hit": 0, "sortino_cap_hit": 0, "calmar_cap_hit": 0,
            }
        else:
            metrics = risk.compute(returns, win.span_days, risk_free_rate=rf, mar=mar,
                                   period_days=period_days, trading_days=trading_days, caps=caps)
            metrics["median_gap_days"] = win.median_gap_days
            metrics["p75_gap_days"] = win.p75_gap_days
            metrics["min_points_used"] = win.min_points_used
            metrics["undefined_reasons"] = list(metrics.get("undefined_reasons") or []) + win.reasons
        reasons = metrics.get("undefined_reasons") or []
        if basis not in ("daily_growth_pct", "none"):
            reasons.append(f"return_basis:{basis}")
        if proxy_rows:
            reasons.append(f"proxy_rows:{proxy_rows}")
        if win.reconstructed_rows:
            reasons.append(f"growth_reconstructed:{win.reconstructed_rows}")
        undefined.extend(reasons)
        if dry_run:
            dry[period_days] = {**metrics, "undefined_reasons": reasons, "basis": basis,
                                "start_date": win.rows[0].nav_date if win.rows else None,
                                "end_date": win.rows[-1].nav_date if win.rows else None}
            continue
        conn.execute(
            "INSERT INTO fund_metric(fcode, period_days, formula_contract, nav_sig, start_date, end_date,"
            " n_obs, years, total_return_pct, annual_dec, vol_dec, positive_ratio_pct,"
            " flat_ratio_pct, non_negative_ratio_pct, mdd_dec,"
            " dd_duration_obs, sharpe, sharpe_t, sortino, calmar_dec, var95_dec, cvar5_dec, raw_sharpe,"
            " raw_sortino, raw_calmar, sharpe_cap_hit, sortino_cap_hit, calmar_cap_hit,"
            " undefined_reasons_json, computed_at, median_gap_days, min_points_used, p75_gap_days)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(fcode, period_days, formula_contract) DO UPDATE SET nav_sig=excluded.nav_sig,"
            " start_date=excluded.start_date, end_date=excluded.end_date, n_obs=excluded.n_obs,"
            " years=excluded.years, total_return_pct=excluded.total_return_pct,"
            " annual_dec=excluded.annual_dec, vol_dec=excluded.vol_dec,"
            " positive_ratio_pct=excluded.positive_ratio_pct,"
            " flat_ratio_pct=excluded.flat_ratio_pct,"
            " non_negative_ratio_pct=excluded.non_negative_ratio_pct, mdd_dec=excluded.mdd_dec,"
            " dd_duration_obs=excluded.dd_duration_obs, sharpe=excluded.sharpe, sharpe_t=excluded.sharpe_t,"
            " sortino=excluded.sortino, calmar_dec=excluded.calmar_dec, var95_dec=excluded.var95_dec,"
            " cvar5_dec=excluded.cvar5_dec, raw_sharpe=excluded.raw_sharpe,"
            " raw_sortino=excluded.raw_sortino, raw_calmar=excluded.raw_calmar,"
            " sharpe_cap_hit=excluded.sharpe_cap_hit, sortino_cap_hit=excluded.sortino_cap_hit,"
            " calmar_cap_hit=excluded.calmar_cap_hit,"
            " undefined_reasons_json=excluded.undefined_reasons_json, computed_at=excluded.computed_at,"
            " median_gap_days=excluded.median_gap_days, min_points_used=excluded.min_points_used,"
            " p75_gap_days=excluded.p75_gap_days",
            (fcode, period_days, contract_id, sig, win.rows[0].nav_date if win.rows else None,
             win.rows[-1].nav_date if win.rows else None, metrics.get("n_obs", 0), metrics.get("years"),
             metrics.get("total_return_pct"), metrics.get("annual_dec"), metrics.get("vol_dec"),
             metrics.get("positive_ratio_pct"), metrics.get("flat_ratio_pct"),
             metrics.get("non_negative_ratio_pct"), metrics.get("mdd_dec"), metrics.get("dd_duration_obs"),
             metrics.get("sharpe"), metrics.get("sharpe_t"), metrics.get("sortino"),
             metrics.get("calmar_dec"), metrics.get("var95_dec"), metrics.get("cvar5_dec"),
             metrics.get("raw_sharpe"), metrics.get("raw_sortino"), metrics.get("raw_calmar"),
             metrics.get("sharpe_cap_hit", 0), metrics.get("sortino_cap_hit", 0),
             metrics.get("calmar_cap_hit", 0),
             json.dumps(reasons, ensure_ascii=False), db.now_iso(),
             metrics.get("median_gap_days"), metrics.get("min_points_used"),
             metrics.get("p75_gap_days")))
        written += 1
    if dry_run:
        return {"fcode": fcode, "ok": True, "periods": len(dry), "dry": dry,
                "undefined": sorted(set(undefined)),
                "first": series[0].nav_date, "last": series[-1].nav_date, "nav_sig": sig}
    conn.commit()
    return {"fcode": fcode, "ok": True, "periods": written, "skipped": skipped,
            "undefined": sorted(set(undefined)),
            "first": series[0].nav_date, "last": series[-1].nav_date, "nav_sig": sig}


def market_as_of(conn, types=None) -> str | None:
    """全市场 as_of：本类型 universe 的净值日期最大值（避免用单只基金的日期当市场日）。

    types 走 fundtypes 注册表（ADR-077）；前缀表集中在一处，新增类型不会漏掉口径。
    """
    from .. import fundtypes
    prefixes = [p for code in fundtypes.resolve(types) for p in fundtypes.type_prefixes(code)]
    if not prefixes:
        return None
    clause = " OR ".join(["d2.type_text LIKE ?"] * len(prefixes))
    row = conn.execute(
        "SELECT MAX(n.nav_date) AS d FROM fund_nav_daily n JOIN src_directory d2 ON d2.code=n.fcode"
        " AND d2.business_date=(SELECT MAX(business_date) FROM src_directory)"
        f" WHERE {clause}", tuple(p + "%" for p in prefixes)).fetchone()
    return row["d"] if row and row["d"] else None