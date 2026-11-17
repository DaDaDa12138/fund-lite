"""风格因子归因：把「这只基金涨跌靠什么」拆成久期 / 信用 / 转债三个 beta（ADR-078）。

因子收益（日频，小数，已正交化）：
- duration    = 国债指数（000012）日收益
- credit      = 企债指数（000013）日收益对 duration 回归后的残差（剔除久期后的信用）
- convertible = 中证转债（000832）日收益对 (duration, credit) 回归后的残差

每只基金取最近 `window_days` 天的日收益，对三因子做带截距的 OLS：
    r_fund = α + β_d·duration + β_c·credit + β_v·convertible + ε
只保留与三个指数都有共同交易日的观测；样本不足 `min_obs` 不写行（不伪造 0 beta）。
结果按 as_of 留存，页面只读最新一行；历史 as_of 可用于回看暴露漂移。
"""
from __future__ import annotations

import math
import sqlite3
import statistics
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from . import db, fundtypes
from .metrics import window as window_mod

GOV_INDEX = "000012"
CORP_INDEX = "000013"
CONV_INDEX = "000832"
INDEX_LABELS = {GOV_INDEX: "国债指数", CORP_INDEX: "企债指数", CONV_INDEX: "中证转债"}

FACTOR_KEYS = ("duration", "credit", "convertible")
FACTOR_LABELS = {"duration": "久期暴露", "credit": "信用暴露", "convertible": "转债暴露"}
FACTOR_NOTES = {
    "duration": "与国债因子的标准化暴露（≈相关系数），越高越像长久期利率债。",
    "credit": "与「企债剔除国债后」信用因子的标准化暴露，越高越依赖信用下沉。",
    "convertible": "与中证转债因子的标准化暴露，越高越受权益/转债市场驱动。",
}
DEFAULT_WINDOW_DAYS = 365
DEFAULT_MIN_OBS = 60
MIN_R2 = 0.05          # 解释力低于 5% 时 beta 不可信，只留 r2/n_obs，不展示 beta
METHOD = "ols3z"   # 三因子 OLS，因子已正交化并标准化
ALPHA_METHOD = "ols3raw-hac5"  # 原始收益 OLS + Newey-West HAC(5)
DEFAULT_HAC_LAGS = 5
DEFAULT_ALPHA_MIN_OBS = 200


def _closes(conn, start: str, end: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {k: {} for k in INDEX_LABELS}
    for row in conn.execute(
            "SELECT index_code, trade_date, close FROM market_index_daily"
            " WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date", (start, end)):
        if row["index_code"] in out:
            out[row["index_code"]][row["trade_date"]] = float(row["close"])
    return out


def factor_returns(conn, as_of: str | None = None,
                   window_days: int = DEFAULT_WINDOW_DAYS) -> tuple[str, dict[str, tuple[float, float, float]]]:
    """返回 (as_of, {交易日: (duration, credit, convertible)})，单位小数。"""
    if as_of is None:
        row = conn.execute("SELECT MAX(trade_date) AS d FROM market_index_daily").fetchone()
        as_of = row["d"] if row else None
    if not as_of:
        raise ValueError("market_index_daily 为空：先跑 python3 -m fundana collect --domain index")
    try:
        end = date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError(f"as_of 不是 YYYY-MM-DD：{as_of}") from exc
    start = (end - timedelta(days=window_days + 20)).isoformat()
    closes = _closes(conn, start, as_of)
    if any(not closes[k] for k in (GOV_INDEX, CORP_INDEX, CONV_INDEX)):
        raise ValueError("指数日线不完整：需要国债/企债/中证转债三只都有数据")
    rets: dict[str, dict[str, float]] = {}
    for code, series in closes.items():
        prev = None
        cur: dict[str, float] = {}
        for day in sorted(series):
            close = series[day]
            if prev is not None and prev > 0:
                cur[day] = close / prev - 1.0
            prev = close
        rets[code] = cur
    days = sorted(set(rets[GOV_INDEX]) & set(rets[CORP_INDEX]) & set(rets[CONV_INDEX]))
    days = [d for d in days if start <= d <= as_of]
    if not days:
        raise ValueError("三个指数没有共同交易日")
    # 国债与企债日收益相关约 0.98，直接拿「企债 − 国债」当信用因子会和久期因子共线
    # （实测相关 −0.98，beta 会出现 ±3000 的伪值）。这里做两次正交化：
    #   credit = 企债对国债回归后的残差；convertible = 转债对 (国债, credit) 回归后的残差。
    # beta 的含义随之变成「在久期之外，对信用/转债的增量暴露」，仍是可解释的暴露，不是预测。
    gov = [rets[GOV_INDEX][d] for d in days]
    corp = [rets[CORP_INDEX][d] for d in days]
    conv = [rets[CONV_INDEX][d] for d in days]
    b_corp, _ = _ols_coef(corp, [gov])
    credit = [c - (b_corp[0] + b_corp[1] * g) for c, g in zip(corp, gov)]
    b_conv, _ = _ols_coef(conv, [gov, credit])
    conv_o = [v - (b_conv[0] + b_conv[1] * g + b_conv[2] * cr)
              for v, g, cr in zip(conv, gov, credit)]
    return as_of, {d: (g, cr, cv) for d, g, cr, cv in zip(days, gov, credit, conv_o)}


def _fund_returns(conn, fcode: str, start: str, end: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT nav_date, unit_nav, acc_nav, growth_pct FROM fund_nav_daily"
        " WHERE fcode=? AND nav_date BETWEEN ? AND ? ORDER BY nav_date", (fcode, start, end))
    out: dict[str, float] = {}
    prev = None
    for r in rows:
        cur = window_mod.NavRow(r["nav_date"], r["unit_nav"], r["acc_nav"], r["growth_pct"])
        g = r["growth_pct"]
        pseudo_zero = False
        if g is not None and abs(g) < 1e-12 and prev is not None and cur.unit_nav and prev.unit_nav:
            pseudo_zero = abs(cur.unit_nav / prev.unit_nav - 1.0) > 1e-6
        if g is None or pseudo_zero:
            ret = window_mod.reconstruct_return(cur, prev)
        else:
            ret = g / 100.0
        if ret is not None:
            out[r["nav_date"]] = ret
        prev = cur
    return out


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """高斯消元（部分主元），matrix 为 n×n，vector 为 n。"""
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("因子矩阵奇异（样本内某因子无波动）")
        a[col], a[pivot] = a[pivot], a[col]
        pv = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= pv
        for r in range(n):
            if r == col:
                continue
            factor = a[r][col]
            if abs(factor) < 1e-15:
                continue
            for j in range(col, n + 1):
                a[r][j] -= factor * a[col][j]
    return [a[i][n] for i in range(n)]


def _ols_coef(y: Sequence[float], columns: Sequence[Sequence[float]]) -> tuple[list[float], float | None]:
    """带截距的 OLS：columns 是若干等长因子列，返回 ([alpha, b1, b2, ...], r2)。"""
    n = len(y)
    k = len(columns)
    if n < k + 2:
        raise ValueError("样本不足")
    if any(len(col) != n for col in columns):
        raise ValueError("因子列长度不一致")
    design = [(1.0,) + tuple(col[i] for col in columns) for i in range(n)]
    size = k + 1
    # 正规方程 X'X β = X'y（(k+1)×(k+1)，纯 Python 足够快）
    xtx = [[sum(d[i] * d[j] for d in design) for j in range(size)] for i in range(size)]
    xty = [sum(d[i] * v for d, v in zip(design, y)) for i in range(size)]
    coef = _solve(xtx, xty)
    mean = sum(y) / n
    sse = 0.0
    sst = 0.0
    for d, v in zip(design, y):
        pred = sum(c * x for c, x in zip(coef, d))
        sse += (v - pred) ** 2
        sst += (v - mean) ** 2
    r2 = None if sst <= 0 else max(0.0, 1.0 - sse / sst)
    return coef, r2


def _ols(y: Sequence[float], xs: Sequence[tuple[float, float, float]]) -> tuple[list[float], float | None]:
    """三因子包装：返回 ([alpha, b_d, b_c, b_v], r2)。"""
    return _ols_coef(y, [[x[0] for x in xs], [x[1] for x in xs], [x[2] for x in xs]])


def _inverse(matrix: list[list[float]]) -> list[list[float]]:
    """小矩阵求逆（用 _solve 逐列解单位向量）；只用于 4×4 的 HAC 协方差。"""
    n = len(matrix)
    cols = []
    for j in range(n):
        unit = [0.0] * n
        unit[j] = 1.0
        cols.append(_solve(matrix, unit))
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def _ols_hac(y: Sequence[float], xs: Sequence[tuple[float, float, float]],
             lags: int = DEFAULT_HAC_LAGS) -> dict[str, Any]:
    """原始收益 OLS + Newey-West HAC 标准误。

    与标准化 OLS 的区别：不标准化基金收益/因子，因此 alpha 是“日收益截距”，
    可年化为 alpha_annual = alpha_daily × 252；t 值用 HAC(5) 修正自相关/异方差。
    返回的 beta 是原始因子 beta（因子日收益变动 1.0 时基金日收益的变动）。
    """
    n = len(y)
    if n < 4:
        raise ValueError("样本不足")
    columns = [[x[0] for x in xs], [x[1] for x in xs], [x[2] for x in xs]]
    coef, r2 = _ols_coef(y, columns)
    design = [(1.0,) + tuple(col[i] for col in columns) for i in range(n)]
    resid = [y[i] - sum(c * v for c, v in zip(coef, design[i])) for i in range(n)]
    xtx = [[sum(d[i] * d[j] for d in design) for j in range(4)] for i in range(4)]
    inv = _inverse(xtx)
    meat = [[0.0] * 4 for _ in range(4)]
    for i in range(n):
        score = [resid[i] * design[i][j] for j in range(4)]
        for a in range(4):
            for b in range(4):
                meat[a][b] += score[a] * score[b]
    # Bartlett 核：lag l 的权重 1 - l/(L+1)
    for lag in range(1, max(0, lags) + 1):
        weight = 1.0 - lag / (lags + 1.0)
        for i in range(lag, n):
            left = [resid[i] * design[i][j] for j in range(4)]
            right = [resid[i - lag] * design[i - lag][j] for j in range(4)]
            for a in range(4):
                for b in range(4):
                    meat[a][b] += weight * (left[a] * right[b] + right[a] * left[b])
    cov = [[0.0] * 4 for _ in range(4)]
    tmp = [[sum(inv[i][k] * meat[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
    for i in range(4):
        for j in range(4):
            cov[i][j] = sum(tmp[i][k] * inv[k][j] for k in range(4))
    se_alpha = math.sqrt(max(0.0, cov[0][0]))
    t_alpha = None if se_alpha <= 0 else coef[0] / se_alpha
    return {
        "alpha_daily": coef[0],
        "alpha_se": se_alpha,
        "alpha_t": t_alpha,
        "betas": (coef[1], coef[2], coef[3]),
        "r2": r2,
        "n_obs": n,
    }


def compute(conn, cfg=None, *, as_of: str | None = None, types: str | Iterable[str] | None = None,
            window_days: int = DEFAULT_WINDOW_DAYS, min_obs: int = DEFAULT_MIN_OBS,
            alpha_min_obs: int | None = None, hac_lags: int | None = None,
            commit: bool = True) -> dict[str, Any]:
    """计算并写入 fund_factor_exposure；返回计数与窗口。"""
    prof = cfg.get("quality_profile", default={}) if cfg is not None else {}
    prof = prof or {}
    alpha_min_obs = int(alpha_min_obs or prof.get("alpha_min_obs", DEFAULT_ALPHA_MIN_OBS))
    hac_lags = int(hac_lags or prof.get("alpha_hac_lags", DEFAULT_HAC_LAGS))
    trading_days = int(cfg.get("risk", "trading_days_per_year", default=252)) if cfg else 252
    as_of, factors = factor_returns(conn, as_of=as_of, window_days=window_days)
    start = (date.fromisoformat(as_of) - timedelta(days=window_days)).isoformat()
    clause, params = fundtypes.where(types, column="type_group", cfg=cfg)
    codes = [r["fcode"] for r in conn.execute(
        f"SELECT fcode FROM fund WHERE {clause} ORDER BY fcode", params)]
    computed_at = db.now_iso()
    # 三因子已正交化，但信用残差的日波动只有国债的约 1/4；不标准化会让 beta 量纲
    # 相差一个数量级（实测原始 beta 出现 ±3000 的伪值）。统一除以全样本标准差，
    # beta 的含义改为「因子每变动 1 个标准差，基金日收益变动多少个百分点」。
    factor_days = list(factors.values())
    means = [statistics.mean([x[j] for x in factor_days]) for j in range(3)]
    sds = [statistics.pstdev([x[j] for x in factor_days]) for j in range(3)]
    if any(sd <= 0 for sd in sds):
        raise ValueError("因子标准差为 0，无法标准化")
    rows: list[tuple[Any, ...]] = []
    skipped = 0
    low_r2 = 0
    alpha_ok = 0
    for code in codes:
        fr = _fund_returns(conn, code, start, as_of)
        days = [d for d in fr if d in factors]
        if len(days) < min_obs:
            skipped += 1
            continue
        y = [fr[d] for d in days]
        y_mean = statistics.mean(y)
        y_sd = statistics.pstdev(y)
        if y_sd <= 0:
            skipped += 1
            continue
        # 基金收益也标准化：beta 变成无量纲的「基金 1 个标准差对因子 1 个标准差的敏感度」，
        # 数值落在 -1..1 附近（三因子正交，单变量时等于相关系数），可跨基金/因子比较。
        yy = [(v - y_mean) / y_sd for v in y]
        xs = [tuple((factors[d][j] - means[j]) / sds[j] for j in range(3)) for d in days]
        try:
            coef, r2 = _ols(yy, xs)
        except ValueError:
            skipped += 1
            continue
        reliable = r2 is not None and r2 >= MIN_R2
        if not reliable:
            low_r2 += 1
        # 原始收益 OLS：alpha 可年化，beta 保留原始量纲；标准化 beta 继续供页面比较。
        alpha_annual = alpha_t = beta_d_raw = beta_c_raw = beta_v_raw = None
        alpha_reliable = 0
        try:
            raw = _ols_hac(y, [factors[d] for d in days], lags=hac_lags)
        except (ValueError, ZeroDivisionError):
            raw = None
        if raw is not None:
            alpha_annual = round(raw["alpha_daily"] * trading_days * 100.0, 4)
            alpha_t = None if raw["alpha_t"] is None else round(raw["alpha_t"], 3)
            beta_d_raw, beta_c_raw, beta_v_raw = (round(v, 6) for v in raw["betas"])
            alpha_reliable = int(bool(reliable and len(days) >= alpha_min_obs
                                      and alpha_t is not None))
            if alpha_reliable:
                alpha_ok += 1
        rows.append((as_of, code,
                     round(coef[1], 6) if reliable else None,
                     round(coef[2], 6) if reliable else None,
                     round(coef[3], 6) if reliable else None,
                     round(coef[0], 8), None if r2 is None else round(r2, 6),
                     len(days), days[0], days[-1], METHOD, computed_at,
                     alpha_annual, alpha_t, alpha_reliable,
                     beta_d_raw, beta_c_raw, beta_v_raw, ALPHA_METHOD))
    if commit:
        # 同一 as_of 只保留本次口径：换窗口/换方法重跑时，旧行（可能来自 730 天实验）
        # 若不在本次样本里会残留，看起来像「当前结果」。先清掉再整批写入。
        conn.execute("DELETE FROM fund_factor_exposure WHERE as_of=?", (as_of,))
        conn.executemany(
            "INSERT INTO fund_factor_exposure(as_of, fcode, beta_duration, beta_credit,"
            " beta_convertible, alpha, r2, n_obs, start_date, end_date, method, computed_at,"
            " alpha_annual_pct, alpha_t, alpha_reliable, beta_duration_raw, beta_credit_raw,"
            " beta_convertible_raw, alpha_method)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(as_of, fcode) DO UPDATE SET beta_duration=excluded.beta_duration,"
            " beta_credit=excluded.beta_credit, beta_convertible=excluded.beta_convertible,"
            " alpha=excluded.alpha, r2=excluded.r2, n_obs=excluded.n_obs,"
            " start_date=excluded.start_date, end_date=excluded.end_date,"
            " method=excluded.method, computed_at=excluded.computed_at,"
            " alpha_annual_pct=excluded.alpha_annual_pct, alpha_t=excluded.alpha_t,"
            " alpha_reliable=excluded.alpha_reliable,"
            " beta_duration_raw=excluded.beta_duration_raw,"
            " beta_credit_raw=excluded.beta_credit_raw,"
            " beta_convertible_raw=excluded.beta_convertible_raw,"
            " alpha_method=excluded.alpha_method", rows)
        conn.commit()
    return {"as_of": as_of, "window_days": window_days, "min_obs": min_obs,
            "min_r2": MIN_R2, "funds": len(codes), "computed": len(rows),
            "reliable": len(rows) - low_r2, "low_r2": low_r2, "skipped": skipped,
            "alpha_reliable": alpha_ok, "alpha_min_obs": alpha_min_obs,
            "hac_lags": hac_lags, "alpha_method": ALPHA_METHOD,
            "factors": {k: {"label": INDEX_LABELS[k], "n_days": len(factors)}
                        for k in (GOV_INDEX, CORP_INDEX, CONV_INDEX)}}


def latest(conn, fcode: str) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            "SELECT * FROM fund_factor_exposure WHERE fcode=? ORDER BY as_of DESC LIMIT 1",
            (fcode,)).fetchone()
    except sqlite3.OperationalError:
        # 旧库尚未跑 0018 迁移时不让详情页 500；迁移后自然有数据。
        return None
    if not row:
        return None
    as_of = row["as_of"]
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM fund_factor_exposure WHERE as_of=?"
        " AND beta_duration IS NOT NULL", (as_of,)).fetchone()["n"]
    percentiles = {}
    for key, col in (("duration", "beta_duration"), ("credit", "beta_credit"),
                     ("convertible", "beta_convertible")):
        value = row[col]
        if value is None or not total:
            percentiles[key] = None
            continue
        better = conn.execute(
            f"SELECT COUNT(*) AS n FROM fund_factor_exposure WHERE as_of=? AND {col} <= ?",
            (as_of, value)).fetchone()["n"]
        percentiles[key] = round(better / total, 4)
    return {
        "as_of": as_of,
        "window": {"start_date": row["start_date"], "end_date": row["end_date"],
                   "n_obs": row["n_obs"], "r2": row["r2"]},
        "betas": {k: row[f"beta_{k}"] for k in FACTOR_KEYS},
        "alpha": row["alpha"],
        "alpha_annual_pct": row["alpha_annual_pct"],
        "alpha_t": row["alpha_t"],
        "alpha_reliable": bool(row["alpha_reliable"]),
        "alpha_method": row["alpha_method"],
        "raw_betas": {"duration": row["beta_duration_raw"],
                      "credit": row["beta_credit_raw"],
                      "convertible": row["beta_convertible_raw"]},
        "percentiles": percentiles,
        "labels": FACTOR_LABELS,
        "notes": FACTOR_NOTES,
        "peer_n": total,
        "method": row["method"],
        "note": "三因子（国债 / 企债剔除国债 / 中证转债）已正交化；betas 是标准化暴露"
                "（≈相关系数，-1..1），raw_betas 是原始日收益 beta；alpha_annual_pct 为"
                "原始收益 OLS 年化截距（HAC t 值），R² 低于 5% 或样本不足 200 天时不可信。"
                "分位为同批基金内比较（越高代表该暴露越重）。",
    }
