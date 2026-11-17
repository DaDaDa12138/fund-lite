"""基金生命周期（P0-1）：清盘/合并/暂停的状态表与 as-of 宇宙。

数据边界：
* 库内 fund 表只有存续基金（identity.refresh_status 的注释已说明），不能用
  “今天不在表里”反推清盘，也不能用 orphan NAV 冒充清盘样本。
* 清盘/合并名单优先从证监会 EID 公开公告采集（`lifecycle_csrc`），也支持
  外部 CSV 导入。导入只接受显式 status/effective_date，缺失字段直接报错，不猜。
* NAV 停止更新只能标记 nav_stale，不能自动等同清盘；采集不全时会误杀。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from . import db

VALID_STATUS = {"liquidated", "merged", "suspended", "active"}


def import_csv(conn, path: str | Path) -> dict:
    """导入生命周期 CSV：fcode,status,effective_date[,last_nav_date,reason,source]。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    rows, errors = [], []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for i, raw in enumerate(csv.DictReader(fh), start=2):
            fcode = (raw.get("fcode") or "").strip()
            status = (raw.get("status") or "").strip().lower()
            eff = (raw.get("effective_date") or "").strip()[:10]
            if not fcode or status not in VALID_STATUS or len(eff) != 10:
                errors.append({"line": i, "error": "fcode/status/effective_date 无效",
                               "row": raw})
                continue
            rows.append((fcode, status, eff, (raw.get("last_nav_date") or "").strip()[:10] or None,
                         (raw.get("reason") or "").strip() or None,
                         (raw.get("source") or "").strip() or "csv",
                         json.dumps(raw, ensure_ascii=False), db.now_iso()))
    if rows:
        conn.executemany(
            "INSERT INTO fund_lifecycle(fcode,status,effective_date,last_nav_date,reason,source,"
            " raw_json,created_at) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(fcode,status,effective_date) DO UPDATE SET"
            " last_nav_date=excluded.last_nav_date, reason=excluded.reason,"
            " source=excluded.source, raw_json=excluded.raw_json", rows)
        conn.commit()
    return {"path": str(path), "imported": len(rows), "errors": errors}


def status_as_of(conn, fcode: str, as_of: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM fund_lifecycle WHERE fcode=? AND effective_date<=?"
        " ORDER BY effective_date DESC, CASE status WHEN 'liquidated' THEN 0"
        " WHEN 'merged' THEN 1 WHEN 'suspended' THEN 2 ELSE 3 END LIMIT 1",
        (fcode, as_of[:10])).fetchone()
    return row["status"] if row else None


def is_investable(conn, fcode: str, as_of: str) -> bool:
    return status_as_of(conn, fcode, as_of) not in ("liquidated", "merged")


def universe_as_of(conn, as_of: str) -> list[str]:
    """回测宇宙：当时已成立的存续基金 + 当时尚未清盘、且已有净值的历史基金。"""
    as_of = as_of[:10]
    # fund 表只保留存续基金；历史清盘基金靠 fund_lifecycle 的 UNION 补回。
    # 若库里有净值表，用“as_of 前已有净值”作为“当时已成立”的可验证代理，
    # 避免把尚未成立的清盘基金错误纳入早期宇宙；没有净值表的测试库保持旧行为。
    has_nav = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_nav_daily'"
    ).fetchone() is not None
    nav_clause = (" AND EXISTS (SELECT 1 FROM fund_nav_daily n"
                  " WHERE n.fcode=fund_lifecycle.fcode AND n.nav_date<=?)") if has_nav else ""
    # EID 公告没有基金类型字段；清盘样本里混有混合/股票/货币基金。
    # 回测只纳入“当前仍在 fund 表（债券池）”或公告名称明确含“债”且不含
    # 混合/股票/货币的样本；宁可少纳入，也不把非债基金塞进债券宇宙。
    bond_clause = (
        " AND (EXISTS (SELECT 1 FROM fund f2 WHERE f2.fcode=fund_lifecycle.fcode)"
        "      OR (COALESCE(reason,'') LIKE '%债%'"
        "          AND COALESCE(reason,'') NOT LIKE '%混合%'"
        "          AND COALESCE(reason,'') NOT LIKE '%股票%'"
        "          AND COALESCE(reason,'') NOT LIKE '%货币%'))")
    sql = (
        "SELECT fcode FROM fund WHERE estab_date IS NOT NULL AND estab_date<=?"
        "   AND NOT EXISTS (SELECT 1 FROM fund_lifecycle l WHERE l.fcode=fund.fcode"
        "                   AND l.status IN ('liquidated','merged') AND l.effective_date<=?)"
        " UNION SELECT fcode FROM fund_lifecycle WHERE effective_date>?"
        "   AND status IN ('liquidated','merged','suspended','active')" + nav_clause + bond_clause)
    params = [as_of, as_of, as_of] + ([as_of] if has_nav else [])
    rows = conn.execute(sql, params)
    return sorted({r[0] for r in rows})


def counts(conn) -> dict:
    out = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM fund_lifecycle GROUP BY status")}
    out["funds"] = conn.execute("SELECT COUNT(DISTINCT fcode) FROM fund_lifecycle").fetchone()[0]
    # 只有拿到历史净值的清盘样本才可能用于回测；其余只能做生命周期披露。
    try:
        out["with_nav"] = conn.execute(
            "SELECT COUNT(DISTINCT l.fcode) FROM fund_lifecycle l"
            " WHERE EXISTS (SELECT 1 FROM fund_nav_daily n WHERE n.fcode=l.fcode)"
        ).fetchone()[0]
    except Exception:
        out["with_nav"] = 0
    return out
