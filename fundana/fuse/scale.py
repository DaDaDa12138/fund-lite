"""规模 as-of 视图：回测时只取报告期已公布的最近一期规模。

背景：fund_profile.asset_scale_billion 是最新一期；拿它去筛 2018–2020 年的基金
会把未来规模透进历史（规模因子在 L5 门禁和增强准入里都有权重）。gmbd 全历史
落库后，这里按 as_of 回看 45 天（季报披露滞后）取最近一期。

口径：gmbd 的 asset_scale_billion 是“份额级”规模，同一基金 A/C 份额各自一行。
L5 规模门槛看的是基金总规模，因此有 fund_share_class 时先按 family_id 汇总再回填
到每个份额；没有映射表时退回逐份额旧口径（仅测试/旧库）。
"""
from __future__ import annotations

from datetime import date, timedelta


def _has_share_class(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_share_class'").fetchone()
    return row is not None


def _family_map(conn) -> dict[str, str]:
    if not _has_share_class(conn):
        return {}
    return {r["fcode"]: r["family_id"] for r in conn.execute(
        "SELECT fcode, family_id FROM fund_share_class")}


def _latest_rows(conn, cutoff: str):
    return conn.execute(
        "SELECT h.fcode, h.report_period, h.asset_scale_billion, h.share_scale_billion"
        " FROM fund_scale_history h"
        " JOIN (SELECT fcode, MAX(report_period) AS m FROM fund_scale_history"
        "       WHERE report_period<=? AND asset_scale_billion IS NOT NULL"
        "       GROUP BY fcode) t"
        " ON t.fcode=h.fcode AND t.m=h.report_period", (cutoff,)).fetchall()


def scale_asof_map(conn, as_of_date: str, lag_days: int = 45) -> dict[str, dict]:
    cutoff = (date.fromisoformat(as_of_date) - timedelta(days=lag_days)).isoformat()
    rows = _latest_rows(conn, cutoff)
    fam = _family_map(conn)
    if not fam:
        return {r["fcode"]: {"report_period": r["report_period"],
                             "asset_scale_billion": r["asset_scale_billion"],
                             "share_scale_billion": r["share_scale_billion"]} for r in rows}
    # 每个 family 在 cutoff 前的最新报告期，再把该期所有份额规模相加。
    latest_by_fam: dict[str, str] = {}
    for r in rows:
        f = fam.get(r["fcode"])
        if f is None:
            continue
        if f not in latest_by_fam or r["report_period"] > latest_by_fam[f]:
            latest_by_fam[f] = r["report_period"]
    totals: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        f = fam.get(r["fcode"])
        if f is None or latest_by_fam.get(f) != r["report_period"]:
            continue
        key = (f, r["report_period"])
        bucket = totals.setdefault(key, {"asset": 0.0, "share": 0.0})
        bucket["asset"] += float(r["asset_scale_billion"] or 0.0)
        bucket["share"] += float(r["share_scale_billion"] or 0.0)
    out: dict[str, dict] = {}
    for r in rows:
        f = fam.get(r["fcode"])
        if f is None:
            out[r["fcode"]] = {"report_period": r["report_period"],
                               "asset_scale_billion": r["asset_scale_billion"],
                               "share_scale_billion": r["share_scale_billion"]}
            continue
        key = (f, latest_by_fam[f])
        t = totals.get(key)
        if t is None:
            continue
        out[r["fcode"]] = {"report_period": key[1],
                           "asset_scale_billion": round(t["asset"], 6),
                           "share_scale_billion": round(t["share"], 6)}
    return out


def share_scale_asof_map(conn, as_of_date: str, lag_days: int = 45) -> dict[str, dict]:
    """按份额返回 as-of 份额规模（不是 family 合计）。

    scale_asof_map 的 share_scale_billion 是 family 合计，不能拿来判单一份额的
    0.5 亿门槛；这里按 fcode 取 report_period<=cutoff 的最近一期，且只接受
    share_scale_billion 非空的行。缺失时由调用方回退 profile_current 并标源。
    """
    cutoff = (date.fromisoformat(as_of_date) - timedelta(days=lag_days)).isoformat()
    rows = conn.execute(
        "SELECT h.fcode, h.report_period, h.share_scale_billion"
        " FROM fund_scale_history h"
        " JOIN (SELECT fcode, MAX(report_period) AS m FROM fund_scale_history"
        "       WHERE report_period<=? AND share_scale_billion IS NOT NULL"
        "       GROUP BY fcode) t"
        " ON t.fcode=h.fcode AND t.m=h.report_period", (cutoff,)).fetchall()
    return {r["fcode"]: {"report_period": r["report_period"],
                         "share_scale_billion": r["share_scale_billion"]}
            for r in rows}


def family_scale_rows(conn, fcode: str, as_of_date: str,
                      lag_days: int = 45) -> list[dict]:
    """返回该基金所属 family 的逐报告期规模合计（升序）；无映射时退回流式逐份额。"""
    cutoff = (date.fromisoformat(as_of_date) - timedelta(days=lag_days)).isoformat()
    fam = _family_map(conn)
    family_id = fam.get(fcode)
    if family_id is None:
        return [dict(r) for r in conn.execute(
            "SELECT report_period, asset_scale_billion, share_scale_billion"
            " FROM fund_scale_history WHERE fcode=? AND report_period<=?"
            " AND asset_scale_billion IS NOT NULL ORDER BY report_period",
            (fcode, cutoff))]
    return [dict(r) for r in conn.execute(
        "SELECT h.report_period, ROUND(SUM(h.asset_scale_billion), 6) AS asset_scale_billion,"
        " ROUND(SUM(h.share_scale_billion), 6) AS share_scale_billion"
        " FROM fund_scale_history h JOIN fund_share_class s ON s.fcode=h.fcode"
        " WHERE s.family_id=? AND h.report_period<=? AND h.asset_scale_billion IS NOT NULL"
        " GROUP BY h.report_period ORDER BY h.report_period", (family_id, cutoff))]
