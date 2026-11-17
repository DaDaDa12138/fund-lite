"""身份层：fcode 为份额主键，A/C 不合并；family 仅在证据充分时建立。"""
from __future__ import annotations

import json
import re
from typing import Any

from .. import db
from ..collect import type_group

CLASS_SUFFIX = re.compile(r"([A-Z])$")


def latest_business_date(conn) -> str | None:
    row = conn.execute("SELECT MAX(business_date) AS d FROM src_directory").fetchone()
    return row["d"] if row else None


STALE_DAYS = 7


def refresh_status(conn, business_date: str, stale_days: int = STALE_DAYS,
                   types: set[str] | None = None) -> dict[str, int]:
    """维护 fund_status：当日出现在目录记 active，连续 stale_days 天未出现记 stale。

    目录只含存续基金，消失的基金不会再有新净值。标 stale 不能修复历史回测的
    幸存者偏差，但至少让"这只基金从某天起不再出现"在库里可查、可解释，而不是
    悄悄从 universe 消失。重新出现时自动回 active。
    """
    # 已由证监会 EID 确认清盘的基金保留 liquidated 标签：它们本来就不在目录里，
    # 若被刷新成 stale，survivorship 报告和人工排查会丢失“已清盘”这一事实。
    conn.execute("UPDATE fund SET fund_status='active' WHERE last_seen_date IS NOT NULL"
                 " AND last_seen_date >= date(?, ?)"
                 " AND COALESCE(fund_status, '')!='liquidated'",
                 (business_date, "-%d day" % int(stale_days)))
    sql = ("UPDATE fund SET fund_status='stale' WHERE last_seen_date IS NOT NULL"
           " AND last_seen_date < date(?, ?) AND COALESCE(fund_status, '')!='stale'"
           " AND COALESCE(fund_status, '')!='liquidated'")
    params: list[Any] = [business_date, "-%d day" % int(stale_days)]
    if types:
        marks = ",".join("?" * len(types))
        sql += " AND type_group IN (%s)" % marks
        params.extend(sorted(types))
    conn.execute(sql, params)
    rows = conn.execute("SELECT fund_status, COUNT(*) AS n FROM fund GROUP BY fund_status").fetchall()
    counts = {str(r["fund_status"]): int(r["n"]) for r in rows if r["fund_status"]}
    conn.commit()
    return counts


def run_identity(conn, run_id: str, business_date: str,
                 types: set[str] | None = None) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT code, name, type_text FROM src_directory WHERE business_date=?", (business_date,)).fetchall()
    if types:
        rows = [r for r in rows if type_group(r["type_text"]) in types]
    estab = {r["code"]: r["estab_date"] for r in conn.execute(
        "SELECT code, estab_date FROM src_jbgk_profile WHERE business_date=(", ())} if False else {}
    jbgk = {r["code"]: r for r in conn.execute(
        "SELECT code, estab_date, company_name FROM src_jbgk_profile WHERE business_date=("
        "SELECT MAX(business_date) FROM src_jbgk_profile)")}
    rank = {r["code"]: r for r in conn.execute(
        "SELECT code, estab_date, name FROM src_rank_row WHERE business_date=("
        "SELECT MAX(business_date) FROM src_rank_row)")}
    now = db.now_iso()
    stats: dict[str, int] = {"ok": 0, "quarantine": 0, "estab_missing": 0}
    payload = []
    for r in rows:
        code = r["code"]
        group = type_group(r["type_text"])
        jb = jbgk.get(code)
        rk = rank.get(code)
        estab_date = (jb["estab_date"] if jb else None) or (rk["estab_date"] if rk else None)
        company = jb["company_name"] if jb else None
        status = "ok"
        reason = None
        if group == "other" or not r["type_text"]:
            status = "quarantine"
            reason = "type_unclassified"
        elif not estab_date:
            # 成立日缺失不隔离（后续 jbgk/rank 可补），只计数；L1 会按 estab_date 判不通过
            stats["estab_missing"] += 1
        stats[status] += 1
        payload.append((code, r["name"], r["type_text"], group, estab_date, company, status, reason,
                        business_date, business_date, now))
    conn.executemany(
        "INSERT INTO fund(fcode, name, type_text, type_group, estab_date, company_name, identity_status,"
        " quarantine_reason, first_seen_date, last_seen_date, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode) DO UPDATE SET name=excluded.name, type_text=excluded.type_text,"
        " type_group=excluded.type_group, estab_date=COALESCE(excluded.estab_date, fund.estab_date),"
        " company_name=COALESCE(excluded.company_name, fund.company_name),"
        " identity_status=excluded.identity_status, quarantine_reason=excluded.quarantine_reason,"
        " last_seen_date=excluded.last_seen_date, updated_at=excluded.updated_at", payload)
    conn.commit()
    status_counts = refresh_status(conn, business_date, types=types)
    return {"funds": len(payload), "fund_status": status_counts, **stats}


L2_DEPENDENTS = ("fund_share_class", "fund_profile", "fund_fee_schedule", "fund_holding_policy",
                 "fund_style", "fund_buyable_daily", "fund_coverage", "fund_holding_bond",
                 "fund_manager_tenure", "fund_metric", "screen_decision", "screen_pick")


def purge_non_universe(conn, types: set[str]) -> dict[str, int]:
    """把不属于本期 universe 的 L2 行清掉，避免上一轮全类型数据混入债券榜。"""
    marks = ",".join("?" * len(types))
    codes = [r["fcode"] for r in conn.execute(
        f"SELECT fcode FROM fund WHERE type_group IS NULL OR type_group NOT IN ({marks})",
        tuple(sorted(types))).fetchall()]
    if not codes:
        return {"purged": 0}
    removed = {"purged": len(codes)}
    for table in L2_DEPENDENTS:
        n = 0
        for i in range(0, len(codes), 500):
            chunk = codes[i:i + 500]
            cur = conn.execute(f"DELETE FROM {table} WHERE fcode IN ({','.join('?' * len(chunk))})", chunk)
            n += cur.rowcount if cur.rowcount > 0 else 0
        if n:
            removed[table] = n
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        conn.execute(f"DELETE FROM fund WHERE fcode IN ({','.join('?' * len(chunk))})", chunk)
    conn.execute("DELETE FROM fund_family")
    conn.commit()
    return removed


def split_class(name: str) -> tuple[str, str | None]:
    """把 '易方达信用债债券A' 拆成 ('易方达信用债债券', 'A')。"""
    if not name:
        return "", None
    m = CLASS_SUFFIX.search(name.strip())
    if m:
        return name.strip()[:-1], m.group(1)
    return name.strip(), None


def run_share_classes(conn, business_date: str) -> dict[str, Any]:
    rows = conn.execute("SELECT fcode, name, company_name FROM fund WHERE last_seen_date=?",
                        (business_date,)).fetchall()
    classes = []
    families: dict[str, dict[str, Any]] = {}
    for r in rows:
        base, letter = split_class(r["name"] or "")
        family_id = f"{(r['company_name'] or 'UNKNOWN')}::{base}"
        classes.append((r["fcode"], family_id, letter, 1 if letter is None else 0))
        fam = families.setdefault(family_id, {"name": base, "company": r["company_name"], "members": []})
        fam["members"].append(r["fcode"])
    conn.executemany(
        "INSERT INTO fund_share_class(fcode, family_id, class_letter, is_primary) VALUES(?,?,?,?)"
        " ON CONFLICT(fcode) DO UPDATE SET family_id=excluded.family_id, class_letter=excluded.class_letter,"
        " is_primary=excluded.is_primary", classes)
    fam_rows = []
    for fid, fam in families.items():
        evidence = {"method": "company+name_without_class", "members": sorted(fam["members"])}
        fam_rows.append((fid, fam["name"], fam["company"], len(fam["members"]),
                         json.dumps(evidence, ensure_ascii=False), db.now_iso()))
    conn.executemany(
        "INSERT INTO fund_family(family_id, canonical_name, company_name, member_count, evidence_json,"
        " created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(family_id) DO UPDATE SET"
        " member_count=excluded.member_count, evidence_json=excluded.evidence_json", fam_rows)
    conn.commit()
    return {"share_classes": len(classes), "families": len(fam_rows),
            "multi_member_families": sum(1 for f in families.values() if len(f["members"]) > 1)}
