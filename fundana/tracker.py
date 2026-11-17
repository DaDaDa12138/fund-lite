"""长期跟踪榜（Tracker）。

规格书 §23.2 的四条硬约束，这里是逐条落地的：
1) 以**交易日**为单位累积：一个业务日只记一份快照，同一天重算指标后重新发布以最后一次为准
   （见 ADR-054）；幂等键仍是 publication_id（= revision_id），重复发布同一份快照直接 skipped；
2) 新增/退出/回流按**相邻交易日**比对，而不是按日期字符串猜；
3) 冻结的 prior rows 才是筛选输入，这里只做「发布后记账」，不参与打分；
4) 从空库开始只能从第一个真实发布累积，不伪造历史次数。
"""
from __future__ import annotations

import json
from typing import Any

from . import db

FORMAL_BOARDS = ("bond_stable", "bond_enhanced")


def _prev_snapshot(conn):
    """上一份快照。调用点在"写入本份快照之前"，所以直接取最新一条即可——
    不能用 created_at < 本次时间过滤：同一秒内连跑两次会取不到上一条，
    导致 streak 归零、score_delta 丢失（真实 bug，2026-11-01 单测抓到）。"""
    return conn.execute(
        "SELECT * FROM screening_snapshot ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()


def _snapshot_picks(conn, publication_id: str) -> list[dict[str, Any]]:
    """从事件表还原某份快照的入选集合（change='exited' 的 fcode 不在榜上）。"""
    out = []
    for r in conn.execute(
            "SELECT fcode, board, rank, formal_score, confidence, detail_json"
            " FROM screening_tracker_event WHERE publication_id=? AND change != 'exited'"
            " ORDER BY fcode", (publication_id,)):
        try:
            style = json.loads(r["detail_json"] or "{}").get("style_code")
        except (TypeError, ValueError):
            style = None
        out.append({"fcode": r["fcode"], "board": r["board"], "rank": r["rank"],
                    "formal_score": r["formal_score"], "confidence": r["confidence"],
                    "style_code": style})
    return out


def _apply_snapshot(conn, snap, picks, prev, prev_picks, snapshots_total: int,
                    now: str) -> dict[str, int]:
    """把一份快照记进跟踪表：新增 / 在榜 / 回流 / 退出。prev 是上一个交易日的快照。"""
    prev_id = prev["publication_id"] if prev else None
    business_date = snap["business_date"]
    run_id = snap["run_id"]
    current = {p["fcode"]: p for p in picks if p.get("board") in FORMAL_BOARDS}
    entered = held = returned = exited = 0
    for fcode, p in current.items():
        row = conn.execute("SELECT * FROM screening_tracker WHERE fcode=?", (fcode,)).fetchone()
        prev_score = None
        if prev_id:
            ev = prev_picks.get(fcode)
            if ev is not None:
                prev_score = ev.get("formal_score")
        delta = None
        if prev_score is not None and p.get("formal_score") is not None:
            delta = round(float(p["formal_score"]) - float(prev_score), 4)
        if row is None:
            change = "entered"
            entered += 1
            conn.execute(
                "INSERT INTO screening_tracker(fcode, first_seen_date, first_publication_id,"
                " last_seen_date, last_publication_id, appearances, snapshots_total, streak, status,"
                " last_board, last_rank, last_formal_score, prev_formal_score, score_delta,"
                " best_rank, best_board, best_formal_score, style_code, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fcode, business_date, snap["publication_id"], business_date,
                 snap["publication_id"], 1, snapshots_total, 1, "active", p.get("board"),
                 p.get("rank"), p.get("formal_score"), prev_score, delta, p.get("rank"),
                 p.get("board"), p.get("formal_score"), p.get("style_code"), now))
        else:
            in_prev = prev_id is not None and fcode in prev_picks
            change = "held" if in_prev else ("returned" if row["appearances"] > 0 else "entered")
            if change == "held":
                held += 1
            elif change == "returned":
                returned += 1
            else:
                entered += 1
            streak = (row["streak"] or 0) + 1 if in_prev else 1
            best_rank = min([x for x in (row["best_rank"], p.get("rank")) if x is not None],
                            default=None)
            best_score = max([x for x in (row["best_formal_score"], p.get("formal_score"))
                              if x is not None], default=None)
            conn.execute(
                "UPDATE screening_tracker SET last_seen_date=?, last_publication_id=?,"
                " appearances=appearances+1, snapshots_total=?, streak=?, status='active',"
                " last_board=?, last_rank=?, prev_formal_score=?, last_formal_score=?,"
                " score_delta=?, best_rank=?, best_board=?, best_formal_score=?,"
                " style_code=COALESCE(?, style_code), updated_at=? WHERE fcode=?",
                (business_date, snap["publication_id"], snapshots_total, streak, p.get("board"),
                 p.get("rank"), prev_score, p.get("formal_score"), delta, best_rank,
                 p.get("board") if best_score == p.get("formal_score") else row["best_board"],
                 best_score, p.get("style_code"), now, fcode))
        conn.execute(
            "INSERT INTO screening_tracker_event(publication_id, business_date, run_id, fcode,"
            " change, board, rank, formal_score, confidence, prev_publication_id, score_delta,"
            " detail_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snap["publication_id"], business_date, run_id, fcode, change, p.get("board"),
             p.get("rank"), p.get("formal_score"), p.get("confidence"), prev_id, delta,
             json.dumps({"style_code": p.get("style_code")}, ensure_ascii=False), now))

    if prev_id:
        for fcode in prev_picks:
            if fcode in current:
                continue
            exited += 1
            conn.execute(
                "UPDATE screening_tracker SET status='exited', streak=0, snapshots_total=?,"
                " updated_at=? WHERE fcode=?", (snapshots_total, now, fcode))
            conn.execute(
                "INSERT OR IGNORE INTO screening_tracker_event(publication_id, business_date, run_id,"
                " fcode, change, board, rank, formal_score, confidence, prev_publication_id,"
                " score_delta, detail_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (snap["publication_id"], business_date, run_id, fcode, "exited", None, None, None,
                 None, prev_id, None, json.dumps({"reason": "absent_in_this_snapshot"}), now))
        # 未在上一快照但仍在跟踪表里的（此前已退出）本次仍未入选 → 保持 exited，不加次数
    return {"entered": entered, "held": held, "returned": returned, "exited": exited}


def _rebuild(conn, now: str, extra_picks: dict[str, list[dict[str, Any]]] | None = None) -> None:
    """按业务日重放全部快照，重算跟踪表与事件表。

    同一交易日重发（重算指标、改配置后重跑 screen + publish）只保留最后一次：
    否则「出现次数」会把同一天的重跑算成多次（ADR-045 决策三要防的正是这个）。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM screening_snapshot ORDER BY created_at, rowid")]
    by_date: dict[str, dict[str, Any]] = {}
    for snap in rows:
        by_date[snap["business_date"]] = snap          # 后写覆盖前写 = 保留最后一次
    kept = [by_date[d] for d in sorted(by_date)]
    # 事件表里存着历史快照的入选集合，重建前先读出来（下面会整表删掉）
    picks_by_pub: dict[str, list[dict[str, Any]]] = {}
    for snap in kept:
        pub = snap["publication_id"]
        picks_by_pub[pub] = (extra_picks or {}).get(pub) or _snapshot_picks(conn, pub)
    conn.execute("DELETE FROM screening_tracker_event")
    conn.execute("DELETE FROM screening_tracker")
    conn.execute("DELETE FROM screening_snapshot")
    prev = None
    for i, snap in enumerate(kept, 1):
        picks = picks_by_pub[snap["publication_id"]]
        conn.execute(
            "INSERT INTO screening_snapshot(publication_id, business_date, run_id, generation,"
            " formal_count, created_at) VALUES(?,?,?,?,?,?)",
            (snap["publication_id"], snap["business_date"], snap["run_id"], snap["generation"],
             len([p for p in picks if p.get("board") in FORMAL_BOARDS]), snap["created_at"]))
        prev_picks = ({p["fcode"]: p for p in picks_by_pub[prev["publication_id"]]}
                      if prev else {})
        _apply_snapshot(conn, snap, picks, prev, prev_picks, i, now)
        prev = snap


def rebuild(conn, now: str | None = None) -> dict[str, Any]:
    """按业务日重算跟踪表（修数据/补历史用）：同日多份快照只保留最后一份。"""
    before = snapshots_total(conn)
    _rebuild(conn, now or db.now_iso())
    conn.commit()
    out = summary(conn)
    out["snapshots_before"] = before
    return out


def update_after_publish(conn, publication_id: str, business_date: str, run_id: str | None,
                         picks: list[dict[str, Any]], generation: int | None = None,
                         created_at: str | None = None) -> dict[str, Any]:
    """发布成功后记账：一个交易日只记一份快照，同日重发以最后一次为准。"""
    now = created_at or db.now_iso()
    done = conn.execute("SELECT 1 FROM screening_snapshot WHERE publication_id=?",
                        (publication_id,)).fetchone()
    if done:
        return {"skipped": True, "reason": "snapshot_already_counted",
                "publication_id": publication_id}
    # 重跑旧净值日不能把跟踪榜倒回去：例如 11-05 已按 11-04 净值发布，之后又用
    # 11-03 的旧 screen_run 重发，若照插会排到 11-04 前面，令最新一期的“进入/退出”
    # 相对上一旧快照计算，产生假换手。只允许同日覆盖或向前推进。
    latest = conn.execute(
        "SELECT business_date FROM screening_snapshot ORDER BY business_date DESC LIMIT 1"
    ).fetchone()
    if latest and business_date < latest["business_date"]:
        return {"skipped": True, "reason": "older_business_date",
                "publication_id": publication_id, "business_date": business_date,
                "latest_business_date": latest["business_date"]}
    superseded = [r["publication_id"] for r in conn.execute(
        "SELECT publication_id FROM screening_snapshot WHERE business_date=?", (business_date,))]
    formal = [p for p in picks if p.get("board") in FORMAL_BOARDS]
    conn.execute(
        "INSERT INTO screening_snapshot(publication_id, business_date, run_id, generation,"
        " formal_count, created_at) VALUES(?,?,?,?,?,?)",
        (publication_id, business_date, run_id, generation, len(formal), now))
    _rebuild(conn, now, {publication_id: picks})
    conn.commit()
    counts = {r["change"]: r["n"] for r in conn.execute(
        "SELECT change, COUNT(*) AS n FROM screening_tracker_event WHERE publication_id=?"
        " GROUP BY 1", (publication_id,))}
    prev_row = conn.execute(
        "SELECT prev_publication_id FROM screening_tracker_event WHERE publication_id=?"
        " AND prev_publication_id IS NOT NULL LIMIT 1", (publication_id,)).fetchone()
    return {"skipped": False, "publication_id": publication_id, "business_date": business_date,
            "prev_publication_id": prev_row[0] if prev_row else None,
            "snapshots_total": snapshots_total(conn), "formal_count": len(formal),
            "superseded": superseded,
            "entered": counts.get("entered", 0), "held": counts.get("held", 0),
            "returned": counts.get("returned", 0), "exited": counts.get("exited", 0)}


def _latest_contract(conn) -> str | None:
    """最新一轮筛选使用的公式契约：跟踪榜默认锁它，避免多版本指标 JOIN 出重复行。"""
    row = conn.execute("SELECT formula_contract FROM screen_run"
                       " ORDER BY created_at DESC LIMIT 1").fetchone()
    return row[0] if row else None


def current(conn, board: str | None = None, limit: int = 50, offset: int = 0,
            active_only: bool = True,
            formula_contract: str | None = None) -> dict[str, Any]:
    where = []
    args: list[Any] = []
    if active_only:
        where.append("t.status='active'")
    if board:
        where.append("t.last_board=?")
        args.append(board)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT COUNT(*) FROM screening_tracker t" + clause, args).fetchone()[0]
    # fund_metric 按 (fcode, period_days, formula_contract) 多版本共存：不锁契约时
    # 同一只基金会 JOIN 出两行（跟踪榜每只基金出现两次的根因）。
    contract = formula_contract or _latest_contract(conn)
    metric_join = " LEFT JOIN fund_metric m ON m.fcode=t.fcode AND m.period_days=365"
    join_args: list[Any] = []
    if contract:
        metric_join += " AND m.formula_contract=?"
        join_args.append(contract)
    rows = conn.execute(
        "SELECT t.*, f.name, sc.family_id, fp.asset_scale_billion, fp.recurring_fee_pct,"
        " m.annual_dec AS annual_1y, m.sharpe AS sharpe_1y, m.mdd_dec AS mdd_1y,"
        " n.nav_date, n.growth_pct AS day_pct,"
        " CASE WHEN t.snapshots_total > 0 THEN ROUND(100.0 * t.appearances / t.snapshots_total, 2)"
        "      ELSE NULL END AS frequency_pct"
        " FROM screening_tracker t"
        " LEFT JOIN fund f ON f.fcode=t.fcode"
        " LEFT JOIN fund_share_class sc ON sc.fcode=t.fcode"
        " LEFT JOIN fund_profile fp ON fp.fcode=t.fcode"
        + metric_join +
        " LEFT JOIN fund_nav_daily n ON n.fcode=t.fcode"
        "   AND n.nav_date=(SELECT MAX(nav_date) FROM fund_nav_daily x WHERE x.fcode=t.fcode)"
        + clause +
        " ORDER BY t.appearances DESC, t.last_formal_score DESC NULLS LAST, t.fcode"
        " LIMIT ? OFFSET ?", join_args + args + [limit, offset]).fetchall()
    snap = conn.execute("SELECT * FROM screening_snapshot ORDER BY created_at DESC LIMIT 1").fetchone()
    return {"rows": [dict(r) for r in rows], "total": total,
            "snapshots_total": snapshots_total(conn),
            "latest_snapshot": dict(snap) if snap else None}


def snapshots_total(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM screening_snapshot").fetchone()[0])


def history(conn, fcode: str, limit: int = 50) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM screening_tracker_event WHERE fcode=? ORDER BY created_at DESC, event_id DESC"
        " LIMIT ?", (fcode, limit)).fetchall()
    tracker = conn.execute("SELECT * FROM screening_tracker WHERE fcode=?", (fcode,)).fetchone()
    return {"tracker": dict(tracker) if tracker else None,
            "events": [dict(r) for r in rows],
            "snapshots_total": snapshots_total(conn)}


def summary(conn) -> dict[str, Any]:
    total = snapshots_total(conn)
    active = conn.execute("SELECT COUNT(*) FROM screening_tracker WHERE status='active'").fetchone()[0]
    exited = conn.execute("SELECT COUNT(*) FROM screening_tracker WHERE status='exited'").fetchone()[0]
    pers = conn.execute(
        "SELECT COUNT(*) FROM screening_tracker WHERE appearances >= 3").fetchone()[0]
    return {"snapshots_total": total, "active": active, "exited": exited,
            "appearances_ge_3": pers}
