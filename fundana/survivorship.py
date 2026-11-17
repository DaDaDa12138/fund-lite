"""幸存者偏差诊断（P1-7）：公开目录快照里的“消失候选”。

数据边界：
* 基金目录是公开 API 的逐日快照（src_directory），只能说明某代码在某天
  出现在目录、某天不再出现；**不能**据此认定清盘/合并。
* 真正的清盘/合并必须走 fund_lifecycle：优先证监会 EID 公告采集
  （`fundana lifecycle-collect`），也支持外部 CSV 导入（公告核对后的名单）。
* 本模块只输出 candidate，不写 fund_lifecycle，不参与筛选宇宙，也不把
  “目录消失”当作负收益或失败样本。
* 当前目录历史只有数天，无法校正长期幸存者偏差；报告必须同时披露覆盖天数。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import db

MAX_CANDIDATES = 50


def _d(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def directory_dates(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT business_date FROM src_directory ORDER BY business_date")]


def summary(conn, *, lookback_days: int = 30, limit: int = MAX_CANDIDATES) -> dict[str, Any]:
    """最新目录相对 lookback 窗口内历史快照的消失候选。

    只比较“最新快照里不再出现”的代码；中间短暂缺失但最新又出现的，不算候选。
    """
    dates = directory_dates(conn)
    if len(dates) < 2:
        return {
            "status": "insufficient_history",
            "snapshots": len(dates),
            "first_date": dates[0] if dates else None,
            "latest_date": dates[-1] if dates else None,
            "coverage_days": 1 if dates else 0,
            "lookback_days": lookback_days,
            "baseline_count": 0,
            "latest_count": 0,
            "missing_count": 0,
            "bond_missing_count": 0,
            "candidates": [],
            "lifecycle_funds": _lifecycle_funds(conn),
            "note": "目录快照不足 2 天，无法判断消失候选；这不等于没有幸存者偏差。",
        }

    latest = dates[-1]
    latest_d = _d(latest)
    cutoff = latest_d - timedelta(days=max(1, lookback_days))
    history = [d for d in dates if d < latest and (_d(d) or latest_d) >= cutoff]
    if not history:
        history = dates[:-1]

    rows = conn.execute(
        "SELECT business_date, code, name, type_text FROM src_directory"
        " WHERE business_date>=? ORDER BY business_date",
        (history[0],)).fetchall()
    seen: dict[str, dict[str, Any]] = {}
    latest_codes: set[str] = set()
    for r in rows:
        code = r["code"]
        if r["business_date"] == latest:
            latest_codes.add(code)
        cur = seen.get(code)
        if cur is None or r["business_date"] > cur["last_seen"]:
            seen[code] = {"last_seen": r["business_date"], "name": r["name"],
                          "type_text": r["type_text"]}

    missing = sorted(set(seen) - latest_codes)
    funds = {r["fcode"]: r for r in conn.execute(
        "SELECT fcode, name, type_text, type_group, estab_date, fund_status, last_seen_date FROM fund")}

    candidates = []
    for code in missing:
        info = seen[code]
        last_seen = info["last_seen"]
        fund = funds.get(code)
        type_text = (fund["type_text"] if fund is not None and "type_text" in fund.keys()
                     else info["type_text"])
        if fund is not None:
            is_bond = (fund["type_group"] == "bond")
        else:
            is_bond = str(type_text or "").startswith("债券")
        if not is_bond:
            continue
        nav_last = conn.execute(
            "SELECT MAX(nav_date) FROM fund_nav_daily WHERE fcode=?", (code,)).fetchone()[0]
        days_missing = (latest_d - (_d(last_seen) or latest_d)).days
        nav_stale_days = None
        if nav_last:
            nav_d = _d(nav_last)
            if nav_d:
                nav_stale_days = max(0, (latest_d - nav_d).days)
        candidates.append({
            "fcode": code,
            "name": info["name"] or (fund["name"] if fund is not None else None),
            "type_text": type_text,
            "last_seen": last_seen,
            "days_missing": days_missing,
            "in_fund_table": fund is not None,
            "fund_status": fund["fund_status"] if fund is not None else None,
            "estab_date": fund["estab_date"] if fund is not None else None,
            "nav_last_date": nav_last,
            "nav_stale_days": nav_stale_days,
            "status": "directory_missing_candidate",
        })

    candidates.sort(key=lambda x: (-x["days_missing"], x["last_seen"] or "", x["fcode"]))
    first_d = _d(history[0])
    coverage_days = (latest_d - first_d).days + 1 if first_d else 1
    note = ("目录消失只表示最新公开目录不再出现，不等于清盘/合并；"
            "候选不进入筛选宇宙，也不能用来校正长期幸存者偏差。"
            f"当前目录历史仅 {coverage_days} 天；长期清盘样本以 fund_lifecycle 的 EID 公告为准。")
    return {
        "status": "ok",
        "snapshots": len(history) + 1,
        "first_date": history[0],
        "latest_date": latest,
        "coverage_days": coverage_days,
        "lookback_days": lookback_days,
        "baseline_count": len(seen),
        "latest_count": len(latest_codes),
        "missing_count": len(missing),
        "bond_missing_count": len(candidates),
        "candidates": candidates[:max(1, limit)],
        "candidates_total": len(candidates),
        "lifecycle_funds": _lifecycle_funds(conn),
        "note": note,
    }


def _lifecycle_funds(conn) -> int:
    try:
        return int(conn.execute(
            "SELECT COUNT(DISTINCT fcode) FROM fund_lifecycle").fetchone()[0] or 0)
    except Exception:
        return 0
