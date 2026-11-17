"""跨端点校验（§16.1）：每日用第二来源复核 NAV，抽样深检历史，比对阶段收益。

纪律（照抄架构书，不得放宽）：
- NAV 容差 abs<=0.001 OR rel<=1%；收益容差 abs<=0.5pp OR rel<=5%（基数 <1pp 时只用绝对差）；
- 日期差 <=5 天记 soft_lag，但阶段收益必须日期精确一致，否则 mismatch；
- mismatch 双值保留，**不静默择优**：canonical 仍取主源，证据写 detail_json；
- 第二源全部不可用 -> degraded=True，由发布门禁降级为 warning（不伪造通过）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Iterable

from .. import db, fundtypes

DEFAULT_NAV_ABS = 0.001
DEFAULT_NAV_REL = 0.01
DEFAULT_RET_ABS_PP = 0.5
DEFAULT_RET_REL = 0.05
DEFAULT_SOFT_LAG_DAYS = 5


def _tol(cfg) -> dict[str, float]:
    v = (cfg.get("validation", default={}) or {}) if cfg is not None else {}
    return {
        "nav_abs": float(v.get("nav_abs", DEFAULT_NAV_ABS)),
        "nav_rel": float(v.get("nav_rel", DEFAULT_NAV_REL)),
        "ret_abs_pp": float(v.get("return_abs_pp", DEFAULT_RET_ABS_PP)),
        "ret_rel": float(v.get("return_rel", DEFAULT_RET_REL)),
        "soft_lag": int(v.get("nav_date_soft_lag_days", DEFAULT_SOFT_LAG_DAYS)),
    }


def compare(a: float | None, b: float | None, abs_tol: float, rel_tol: float) -> tuple[str, float | None, float | None]:
    """返回 (status, delta_abs, delta_rel)。missing 与 real_zero 必须分开。"""
    if a is None and b is None:
        return "missing", None, None
    if a is None:
        return "missing_a", None, None
    if b is None:
        return "missing_b", None, None
    delta = abs(float(a) - float(b))
    rel = (delta / abs(float(b))) if float(b) else None
    ok = delta <= abs_tol or (rel is not None and rel <= rel_tol)
    return ("pass" if ok else "mismatch"), delta, rel


def _check_id(fcode: str, field: str, business_date: str, source_a: str, source_b: str) -> str:
    raw = "|".join([fcode, field, business_date, source_a, source_b])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _persist(conn, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO nav_cross_check(check_id, business_date, fcode, source_a, source_b,"
        " field, value_a, value_b, delta_abs, delta_rel, tolerance, status, detail_json, checked_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["check_id"], r["business_date"], r["fcode"], r["source_a"], r["source_b"], r["field"],
          r["value_a"], r["value_b"], r["delta_abs"], r["delta_rel"], r["tolerance"], r["status"],
          r.get("detail_json"), r["checked_at"]) for r in rows])
    conn.commit()
    return len(rows)


def _snap_date(md: str | None, business_date: str):
    """把快照里的 MM-DD 还原成完整日期（跨年时回退一年）。"""
    if not md:
        return None
    parts = str(md).split("-")
    if len(parts) < 2:
        return None
    try:
        bd = date.fromisoformat(business_date[:10])
        cand = date(bd.year, int(parts[-2]), int(parts[-1]))
    except (ValueError, TypeError):
        return None
    if cand > bd:
        cand = date(bd.year - 1, cand.month, cand.day)
    return cand


def check_daily(conn, business_date: str, cfg=None) -> dict[str, Any]:
    """E3 申赎状态快照 vs B1 净值快照：同一业务日的单位净值与净值日期。

    两个源都是单请求全市场，比对不需要额外网络请求。比对范围限定在**本期
    universe（债券型）**：拿 27,929 只全市场去比 7,365 只债基，只会产出 2 万条
    无意义的 missing_b，把真正的口径分歧淹没掉。
    """
    tol = _tol(cfg)
    now = db.now_iso()
    clause, tparams = fundtypes.where(cfg=cfg, column="f.type_group")
    rows = conn.execute(
        "SELECT s.code, s.unit_nav AS a_nav, s.nav_date_md"
        " FROM src_snapshot_status s"
        f" JOIN fund f ON f.fcode = s.code AND {clause}"
        " WHERE s.business_date = ?", (*tparams, business_date)).fetchall()
    # E3 只给 MM-DD，必须还原成完整日期再与 B1 的 nav_date 精确 JOIN。
    # 旧实现用“最新净值日的月日”做相等判断：停牌多年、最新净值日恰好同月同日的
    # 基金会得到 lag=0 的假同日通过（实测 16,130 条里 soft_lag 14,956，同日仅 513）。
    snap_dates = {r["code"]: _snap_date(r["nav_date_md"], business_date) for r in rows}
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _cross_snap(fcode TEXT PRIMARY KEY, nav_date TEXT)")
    conn.execute("DELETE FROM _cross_snap")
    # 显式存 ISO 字符串：Python 3.12 起 sqlite3 的 date adapter 已弃用，
    # 且 TEXT 与 fund_nav_daily.nav_date 的 ISO 文本直接可比。
    conn.executemany("INSERT OR REPLACE INTO _cross_snap(fcode, nav_date) VALUES(?,?)",
                     [(c, d.isoformat()) for c, d in snap_dates.items() if d])
    exact_nav = {r["fcode"]: r["unit_nav"] for r in conn.execute(
        "SELECT s.fcode, n.unit_nav FROM _cross_snap s"
        " JOIN fund_nav_daily n ON n.fcode=s.fcode AND n.nav_date=s.nav_date")}
    latest = {r["fcode"]: r["nav_date"] for r in conn.execute(
        "SELECT fcode, MAX(nav_date) AS nav_date FROM fund_nav_daily"
        " WHERE fcode IN (SELECT fcode FROM _cross_snap) GROUP BY fcode")}
    # 最新净值日对应的净值：用来识别“值已经走到新交易日、日期列还停在旧交易日”的上游中间态
    latest_nav = {r["fcode"]: r["unit_nav"] for r in conn.execute(
        "SELECT n.fcode, n.unit_nav FROM fund_nav_daily n"
        " JOIN (SELECT fcode, MAX(nav_date) AS d FROM fund_nav_daily"
        "       WHERE fcode IN (SELECT fcode FROM _cross_snap) GROUP BY fcode) m"
        " ON m.fcode=n.fcode AND m.d=n.nav_date")}
    out: list[dict[str, Any]] = []
    same_day = 0
    for r in rows:
        code = r["code"]
        sd = snap_dates.get(code)
        has_exact = code in exact_nav
        b_nav = exact_nav.get(code)
        nd = sd if has_exact else (date.fromisoformat(latest[code]) if latest.get(code) else None)
        if r["a_nav"] is not None and b_nav is not None:
            status, delta, rel = compare(r["a_nav"], b_nav, tol["nav_abs"], tol["nav_rel"])
            if status == "pass":
                same_day += 1
        elif r["a_nav"] is None:
            status, delta, rel = compare(r["a_nav"], b_nav, tol["nav_abs"], tol["nav_rel"])
        elif latest.get(code):
            # 快照日期在库里没有对应净值：值不可比，记 date_lag，不冒充 mismatch
            status, delta, rel = "date_lag", None, None
        else:
            status, delta, rel = compare(r["a_nav"], b_nav, tol["nav_abs"], tol["nav_rel"])
        lag_nav_date = None
        if status == "mismatch" and sd is not None and latest.get(code):
            # 上游状态列表刷新的中间态：净值列已经更新到新的交易日，净值日期列还停在旧交易日。
            # 这种行 A 的值与它自己声明的旧日期本来就不该相等（实测 2026-11-17 下午 379 只
            # 债基如此：0.9180 是 11-16 的净值，日期列却写 11-13），判 mismatch 是假阳性。
            # 改记 date_lag —— 仍在汇总里可见、仍进 detail，但不再触发发布门禁。
            lnd = latest[code]
            lnav = latest_nav.get(code)
            if lnd > sd.isoformat() and lnav is not None:
                if compare(r["a_nav"], lnav, tol["nav_abs"], tol["nav_rel"])[0] == "pass":
                    status, delta, rel = "date_lag", None, None
                    lag_nav_date = lnd
        detail = None
        if status != "pass" and r["nav_date_md"]:
            # MM-DD vs YYYY-MM-DD：日期不一致时给出门禁可读的证据
            det = {"status_snapshot_date": r["nav_date_md"],
                   "snap_date": sd.isoformat() if sd else None,
                   "nav_daily_date": nd.isoformat() if nd else None}
            if lag_nav_date:
                det["status_value_matches_nav_date"] = lag_nav_date
            detail = json.dumps(det, ensure_ascii=False)
        out.append({"check_id": _check_id(code, "unit_nav", business_date,
                                          "em_status_snapshot", "em_nav_snapshot"),
                    "business_date": business_date, "fcode": code,
                    "source_a": "em_status_snapshot", "source_b": "em_nav_snapshot",
                    "field": "unit_nav", "value_a": r["a_nav"], "value_b": b_nav,
                    "delta_abs": delta, "delta_rel": rel,
                    "tolerance": "nav_abs %.3f|nav_rel %.3f" % (tol["nav_abs"], tol["nav_rel"]),
                    "status": status, "detail_json": detail, "checked_at": now})
        if r["nav_date_md"]:
            out.append({"check_id": _check_id(code, "nav_date", business_date,
                                              "em_status_snapshot", "em_nav_snapshot"),
                        "business_date": business_date, "fcode": code,
                        "source_a": "em_status_snapshot", "source_b": "em_nav_snapshot",
                        "field": "nav_date", "value_a": None, "value_b": None,
                        "delta_abs": None, "delta_rel": None,
                        "tolerance": "exact",
                        "status": "pass" if has_exact else "soft_lag",
                        # 日期行没有数值，两侧原始日期必须落 detail，否则前端只能显示“—”
                        "detail_json": json.dumps(
                            {"status_snapshot_date": r["nav_date_md"],
                             "snap_date": sd.isoformat() if sd else None,
                             "nav_daily_date": nd.isoformat() if nd else None},
                            ensure_ascii=False),
                        "checked_at": now})
    # 每日重跑必须先清掉本日旧结论，否则口径收窄后旧行会以"幽灵 missing_b"残留
    conn.execute("DELETE FROM nav_cross_check WHERE business_date=? AND source_a=? AND source_b=?",
                 (business_date, "em_status_snapshot", "em_nav_snapshot"))
    written = _persist(conn, out)
    return {"business_date": business_date, "written": written,
            "funds": len(rows), "pass": sum(1 for r in out if r["status"] == "pass"),
            "mismatch": sum(1 for r in out if r["status"] == "mismatch"),
            "soft_lag": sum(1 for r in out if r["status"] == "soft_lag"),
            "date_lag": sum(1 for r in out if r["status"] == "date_lag"),
            "same_day": same_day,
            "same_day_ratio": (same_day / len(rows)) if rows else None,
            "missing": sum(1 for r in out if r["status"].startswith("missing"))}


def _parse_dj(payload: str) -> dict[str, tuple[float | None, float | None]]:
    doc = json.loads(payload)
    if int(doc.get("result_code", -1)) != 0:
        raise ValueError("dj result_code=%r" % doc.get("result_code"))
    items = ((doc.get("data") or {}).get("items") or [])
    out: dict[str, tuple[float | None, float | None]] = {}
    for it in items:
        d = str(it.get("date") or "")[:10]
        if not d:
            continue
        # 蛋卷同一接口有两种字段名：文档写 unit_nav/daily_growth，线上实际返回 nav/percentage
        nav = it.get("unit_nav")
        if nav in (None, ""):
            nav = it.get("nav")
        if nav in (None, ""):
            nav = it.get("value")
        growth = it.get("daily_growth")
        if growth in (None, ""):
            growth = it.get("percentage")
        try:
            out[d] = (float(nav),
                      None if growth in (None, "") else float(growth))
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        raise ValueError("dj 空数据集")
    return out


def _parse_lsjz(payload: str) -> dict[str, tuple[float | None, float | None]]:
    doc = json.loads(payload)
    data = doc.get("Data")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("LSJZList") or []
    else:
        items = []
    out: dict[str, tuple[float | None, float | None]] = {}
    for it in items:
        d = str(it.get("FSRQ") or "")[:10]
        if not d:
            continue
        try:
            g = it.get("JZZZL")
            out[d] = (float(it["DWJZ"]), float(g) if g not in (None, "") else None)
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        raise ValueError("lsjz 空数据集")
    return out


def check_sample(conn, cfg, codes: Iterable[str], run_id: str, days: int = 250) -> dict[str, Any]:
    """抽样深检：第二源（蛋卷 -> 东财 lsjz 降级）比对最近 N 个业务日的单位净值与日增长率。"""
    from ..fetch.http import Fetcher

    tol = _tol(cfg)
    fetcher = Fetcher(cfg, conn, run_id)
    now = db.now_iso()
    rows: list[dict[str, Any]] = []
    degraded = 0
    used: dict[str, int] = {}
    for code in codes:
        series: dict[str, tuple[float | None, float | None]] | None = None
        source_b = None
        for sid in ("dj_nav", "em_lsjz"):
            if sid not in cfg.sources:
                continue
            res = fetcher.get(sid, code)
            if not res.business_ok:
                continue
            try:
                series = _parse_dj(res.text) if sid == "dj_nav" else _parse_lsjz(res.text)
            except (ValueError, json.JSONDecodeError):
                continue
            source_b = sid
            break
        if series is None:
            degraded += 1
            continue
        used[source_b] = used.get(source_b, 0) + 1
        local = conn.execute(
            "SELECT nav_date, unit_nav, growth_pct FROM fund_nav_daily WHERE fcode=?"
            " ORDER BY nav_date DESC LIMIT ?", (code, days)).fetchall()
        for r in local:
            d = r["nav_date"]
            if d not in series:
                continue
            b_nav, b_growth = series[d]
            st, delta, rel = compare(r["unit_nav"], b_nav, tol["nav_abs"], tol["nav_rel"])
            rows.append({"check_id": _check_id(code, "unit_nav", d, "em_pzd", source_b),
                         "business_date": d, "fcode": code, "source_a": "em_pzd", "source_b": source_b,
                         "field": "unit_nav", "value_a": r["unit_nav"], "value_b": b_nav,
                         "delta_abs": delta, "delta_rel": rel,
                         "tolerance": "nav_abs %.3f|nav_rel %.3f" % (tol["nav_abs"], tol["nav_rel"]),
                         "status": st, "detail_json": None, "checked_at": now})
            if b_growth is not None and r["growth_pct"] is not None:
                st2, d2, r2 = compare(r["growth_pct"], b_growth, tol["ret_abs_pp"], tol["ret_rel"])
                rows.append({"check_id": _check_id(code, "growth_pct", d, "em_pzd", source_b),
                             "business_date": d, "fcode": code, "source_a": "em_pzd", "source_b": source_b,
                             "field": "growth_pct", "value_a": r["growth_pct"], "value_b": b_growth,
                             "delta_abs": d2, "delta_rel": r2,
                             "tolerance": "ret_abs %.2fpp|ret_rel %.3f" % (tol["ret_abs_pp"], tol["ret_rel"]),
                             "status": st2, "detail_json": None, "checked_at": now})
    written = _persist(conn, rows)
    return {"written": written, "codes": len(list(codes)) if not isinstance(codes, list) else len(codes),
            "degraded": degraded, "sources": used,
            "mismatch": sum(1 for r in rows if r["status"] == "mismatch"),
            "pass": sum(1 for r in rows if r["status"] == "pass")}


def check_period_returns(conn, run_id: str, cfg=None) -> dict[str, Any]:
    """上游阶段收益（pingzhongdata syl_1n）vs 本地链式复算的 365 天总回报。

    两个口径不完全同窗口，所以用 abs<=0.5pp OR rel<=5% 的宽容差，只用来发现量级错误。
    """
    tol = _tol(cfg)
    now = db.now_iso()
    # run_id 是本次 validate 的 run（只用于 check_id 命名空间）；候选母体必须取最近一次
    # screen_run —— 否则 WHERE d.run_id=validate_run 永远 0 行（首版就是这个错）。
    run = conn.execute(
        "SELECT run_id, formula_contract, business_date FROM screen_run"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    if not run:
        return {"run_id": run_id, "written": 0, "funds": 0, "pass": 0, "mismatch": 0}
    screen_run_id, screen_contract = run["run_id"], run["formula_contract"]
    # 行的 business_date 用该轮筛选的业务日（否则会写成 run_id，页面与按日汇总都读不懂）
    screen_bd = run["business_date"] or screen_run_id
    # 必须锁定该轮筛选使用的公式契约：fund_metric 按 (fcode, period, contract) 多版本共存，
    # 不限定会一条基金命中两版指标（实测 written 13,222 = 2 × 6,611）。
    rows = conn.execute(
        "SELECT d.fcode, p.syl_1n_pct, m.total_return_pct"
        " FROM screen_decision d"
        " JOIN src_pzd_profile p ON p.code=d.fcode"
        " JOIN fund_metric m ON m.fcode=d.fcode AND m.period_days=365"
        "   AND m.formula_contract=?"
        " WHERE d.run_id=? AND p.syl_1n_pct IS NOT NULL AND m.total_return_pct IS NOT NULL",
        (screen_contract, screen_run_id)).fetchall()
    # 阶段收益核对只对"最近一轮筛选"有意义：旧轮次的残留行会让汇总数虚高（实测 363 = 6 + 357）
    conn.execute(
        "DELETE FROM nav_cross_check WHERE field='return_1y'"
        " AND source_a='em_pzd_syl' AND source_b='fundana_metric'")
    out = []
    for r in rows:
        st, delta, rel = compare(r["syl_1n_pct"], r["total_return_pct"], tol["ret_abs_pp"], tol["ret_rel"])
        out.append({"check_id": _check_id(r["fcode"], "return_1y", screen_run_id, "em_pzd_syl", "fundana_metric"),
                    "business_date": screen_bd, "fcode": r["fcode"],  # 该轮筛选的业务日
                    "source_a": "em_pzd_syl", "source_b": "fundana_metric",
                    "field": "return_1y", "value_a": r["syl_1n_pct"], "value_b": r["total_return_pct"],
                    "delta_abs": delta, "delta_rel": rel,
                    "tolerance": "ret_abs %.2fpp|ret_rel %.3f" % (tol["ret_abs_pp"], tol["ret_rel"]),
                    "status": st,
                    "detail_json": '{"screen_run_id": "%s"}' % screen_run_id,
                    "checked_at": now})
    written = _persist(conn, out)
    return {"run_id": run_id, "screen_run_id": screen_run_id, "written": written, "funds": len(rows),
            "pass": sum(1 for r in out if r["status"] == "pass"),
            "mismatch": sum(1 for r in out if r["status"] == "mismatch")}


def period_summary(conn) -> dict[str, Any]:
    """阶段收益核对汇总（只反映当前 nav_cross_check 里的 return_1y 行）。"""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM nav_cross_check WHERE field='return_1y' GROUP BY 1").fetchall()
    total = sum(r["n"] for r in rows)
    by_status = {r["status"]: r["n"] for r in rows}
    mismatch = by_status.get("mismatch", 0)
    return {"total": total, "by_status": by_status,
            "mismatch_ratio": (mismatch / total) if total else None}


def summarize(conn, business_date: str | None = None) -> dict[str, Any]:
    """按业务日汇总；mismatch_ratio 是发布门禁的唯一输入。"""
    if business_date is None:
        row = conn.execute("SELECT MAX(business_date) AS d FROM nav_cross_check"
                           " WHERE field IN ('unit_nav','growth_pct')").fetchone()
        business_date = row["d"] if row else None
    if not business_date:
        return {"business_date": None, "total": 0, "by_status": {}, "by_field": {},
                "mismatch_ratio": None, "degraded": True}
    rows = conn.execute(
        # 阶段收益核对（field='return_1y'）与当日净值核对不是同一件事，不进 mismatch_ratio
        "SELECT field, status, COUNT(*) AS n FROM nav_cross_check WHERE business_date=?"
        " AND field != 'return_1y' GROUP BY 1,2", (business_date,)).fetchall()
    total = sum(r["n"] for r in rows)
    by_status: dict[str, int] = {}
    by_field: dict[str, dict[str, int]] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
        by_field.setdefault(r["field"], {})[r["status"]] = r["n"]
    mismatch = by_status.get("mismatch", 0)
    # 同日口径：nav_date 行的 pass 数 = 两侧完整日期完全一致且都有值的基金数。
    # 这是“真的拿同一天净值在比”的分母，必须和 total 一起披露；只看 mismatch_ratio
    # 会把大量 date_lag 当成通过。
    date_field = by_field.get("nav_date") or {}
    same_day = int(date_field.get("pass", 0))
    funds = sum(date_field.values())
    return {"business_date": business_date, "total": total, "by_status": by_status,
            "by_field": by_field,
            "mismatch_ratio": (mismatch / total) if total else None,
            "same_day": same_day,
            "same_day_ratio": (same_day / funds) if funds else None,
            "degraded": total == 0}


def _days_between(a: str, b: str) -> int:
    return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
