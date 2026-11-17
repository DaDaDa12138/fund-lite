"""P0-2 指标可比性：标记不可比净值曲线，避免污染分位与“低风险”叙事。

不可比不等于基金差，而是“这条净值曲线不能和其他债基按同一把尺子排序”：
* amortized_cost：摊余成本法定开债（名称含 87/88 个月、摊余），净值按实际利率法
  每日摊销，封闭期内几乎只涨不跌；与市值法基金的 MDD/Vol 不可比。
* zero_risk：1 年 MDD 与 Vol 同时为 0。真实市值法债基不会零波动，通常是估值/披露
  口径或数据缺失，不能当作“无风险”。
* sparse_nav：p75/中位采样间隔 > 3 天，周更/月更净值的日频风险指标不可比。
* stale_nav：最新净值距 as_of 超过 10 天，指标是旧窗口，不能代表当前风险。

标记只写 fund_metric_comparability，不改 fund_metric 原始指标；筛选层可据此把
这些基金排除出分位母体，但保留其记录与原因，便于研究视图单列。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from typing import Any

from .. import db
from .contract import FORMULA_CONTRACT

AMORTIZED_NAME_RE = re.compile(r"(?:87|88)\s*个月|摊余")
COMPARABILITY_FLAGS = ("amortized_cost", "zero_risk", "sparse_nav", "stale_nav")
MAX_NAV_AGE_DAYS = 10
SPARSE_GAP_DAYS = 3.0
AMORTIZED_VOL_PCT = 0.01  # vol_dec 单位是百分数；<0.01% 视为摊余成本式的近乎零波动


def _is_zero(value: Any) -> bool:
    try:
        return value is not None and abs(float(value)) < 1e-12
    except (TypeError, ValueError):
        return False


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _age_days(nav_last_date: str | None, as_of: str) -> int | None:
    if not nav_last_date:
        return None
    try:
        return (date.fromisoformat(as_of[:10]) - date.fromisoformat(nav_last_date[:10])).days
    except ValueError:
        return None


def classify(*, name: str | None, lockup_type: str | None,
             mdd_dec: Any, vol_dec: Any, median_gap_days: Any, p75_gap_days: Any,
             nav_last_date: str | None, as_of: str) -> list[str]:
    """返回该基金 1 年指标的不可比旗标（可能为空）。"""
    flags: list[str] = []
    name = name or ""
    lockup = (lockup_type or "").lower()
    mdd = _as_float(mdd_dec)
    vol = _as_float(vol_dec)
    med_gap = _as_float(median_gap_days)
    p75_gap = _as_float(p75_gap_days)

    # 名称是最强证据；periodic_open/closed 且“零回撤 + 近零波动”是兜底证据。
    if AMORTIZED_NAME_RE.search(name):
        flags.append("amortized_cost")
    elif (lockup in ("periodic_open", "closed") and _is_zero(mdd)
          and vol is not None and vol < AMORTIZED_VOL_PCT):
        flags.append("amortized_cost")

    if _is_zero(mdd) and _is_zero(vol):
        flags.append("zero_risk")

    if (med_gap is not None and med_gap > SPARSE_GAP_DAYS) or \
            (p75_gap is not None and p75_gap > SPARSE_GAP_DAYS):
        flags.append("sparse_nav")

    age = _age_days(nav_last_date, as_of)
    if age is not None and age > MAX_NAV_AGE_DAYS:
        flags.append("stale_nav")

    # 去重且按固定顺序输出，便于测试与前端稳定展示。
    return [f for f in COMPARABILITY_FLAGS if f in flags]


def refresh(conn: sqlite3.Connection, as_of: str,
            formula_contract: str = FORMULA_CONTRACT) -> dict[str, Any]:
    """重算 365 天指标的不可比旗标，写入快照表。返回计数摘要。"""
    rows = conn.execute(
        "SELECT m.fcode, m.mdd_dec, m.vol_dec, m.median_gap_days, m.p75_gap_days,"
        "       f.name, h.lockup_type, c.nav_last_date"
        " FROM fund_metric m"
        " JOIN fund f ON f.fcode=m.fcode"
        " LEFT JOIN fund_holding_policy h ON h.fcode=m.fcode"
        " LEFT JOIN fund_coverage c ON c.fcode=m.fcode"
        "   AND c.run_id=(SELECT MAX(run_id) FROM fund_coverage)"
        " WHERE m.period_days=365 AND m.formula_contract=?",
        (formula_contract,)).fetchall()
    now = db.now_iso()
    records: list[tuple[Any, ...]] = []
    counts = {flag: 0 for flag in COMPARABILITY_FLAGS}
    for r in rows:
        flags = classify(name=r["name"], lockup_type=r["lockup_type"],
                         mdd_dec=r["mdd_dec"], vol_dec=r["vol_dec"],
                         median_gap_days=r["median_gap_days"],
                         p75_gap_days=r["p75_gap_days"],
                         nav_last_date=r["nav_last_date"], as_of=as_of)
        for flag in flags:
            counts[flag] += 1
        records.append((r["fcode"], 365, formula_contract, as_of,
                        json.dumps(flags, ensure_ascii=False),
                        0 if flags else 1, now))
    conn.executemany(
        "INSERT OR REPLACE INTO fund_metric_comparability"
        "(fcode, period_days, formula_contract, as_of, flags_json, is_comparable, computed_at)"
        " VALUES(?,?,?,?,?,?,?)", records)
    conn.commit()
    return {"as_of": as_of, "formula_contract": formula_contract,
            "rows": len(records), "flagged": counts,
            "comparable": sum(1 for rec in records if rec[5] == 1)}


def load_latest(conn: sqlite3.Connection, as_of: str,
                period_days: int = 365) -> dict[str, list[str]]:
    """读取每只基金 <= as_of 的最近一次旗标；表不存在时返回空（兼容旧库）。"""
    try:
        rows = conn.execute(
            "WITH latest AS ("
            " SELECT fcode, flags_json, ROW_NUMBER() OVER ("
            "   PARTITION BY fcode ORDER BY as_of DESC) AS rn"
            " FROM fund_metric_comparability"
            " WHERE period_days=? AND as_of<=?)"
            " SELECT fcode, flags_json FROM latest WHERE rn=1",
            (period_days, as_of[:10])).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, list[str]] = {}
    for r in rows:
        try:
            parsed = json.loads(r["flags_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = []
        out[r["fcode"]] = [str(x) for x in parsed] if isinstance(parsed, list) else []
    return out
