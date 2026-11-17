"""产品告警：对当前发布的正式/精选榜单做事件扫描。

告警不是新的筛选门槛，只回答“已经入选的基金，最近有没有发生需要复核的事”：
- 净值停更（数据断流）
- 近 90 天更换基金经理
- 规模跌破 2 亿 / 同比缩水超过 30%
- 近一年回撤超过榜单阈值
- 风格标签相对上一次发布发生漂移

用法：
    python3 -m fundana alerts
    python3 -m fundana alerts --board bond_core10 --as-of 2026-11-02
    python3 -m fundana alerts --out reports/analysis/alerts.json
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Sequence

from .audit import events as events_mod
from .screen import thresholds as T
from .metrics.window import NavRow, effective_returns

NAV_STALE_DAYS = 7
MANAGER_CHANGE_DAYS = 90
SCALE_SMALL_BILLION = 2.0
SCALE_SHRINK_PCT = -30.0
DRAWDOWN_LIMITS = {
    T.BOARD_CORE10: 2.5,
    T.BOARD_CORE: 2.5,
    T.BOARD_STABLE: 2.0,
    T.BOARD_ENHANCED: 5.0,
}
DEFAULT_BOARDS = (T.BOARD_CORE10, T.BOARD_CORE, T.BOARD_STABLE, T.BOARD_ENHANCED)


def _latest_publication(conn):
    return conn.execute(
        "SELECT p.publication_id, p.run_id, p.generation, r.business_date"
        " FROM publication p JOIN screen_run r ON r.run_id=p.run_id"
        " WHERE p.status='committed' ORDER BY p.generation DESC LIMIT 1").fetchone()


def _previous_run(conn, generation: int):
    row = conn.execute(
        "SELECT run_id FROM publication WHERE status='committed' AND generation<?"
        " ORDER BY generation DESC LIMIT 1", (generation,)).fetchone()
    return row["run_id"] if row else None


def _nav_metrics(conn, fcode: str, as_of: str) -> dict[str, Any] | None:
    start = (date.fromisoformat(as_of) - timedelta(days=365)).isoformat()
    rows = conn.execute(
        "SELECT nav_date, unit_nav, acc_nav, growth_pct FROM fund_nav_daily"
        " WHERE fcode=? AND nav_date<=? AND nav_date>=? ORDER BY nav_date",
        (fcode, as_of, start)).fetchall()
    if len(rows) < 2:
        return None
    nav_rows = [NavRow(r["nav_date"], r["unit_nav"], r["acc_nav"], r["growth_pct"])
                for r in rows]
    rets, _ = effective_returns(nav_rows)
    if len(rets) < 20:
        return None
    cum = peak = 1.0
    mdd = 0.0
    for r in rets:
        cum *= 1.0 + r
        peak = max(peak, cum)
        mdd = min(mdd, cum / peak - 1.0)
    last = date.fromisoformat(rows[-1]["nav_date"])
    return {"n_obs": len(rets), "mdd_pct": round(abs(mdd) * 100.0, 4),
            "freshness_days": (date.fromisoformat(as_of) - last).days,
            "last_nav_date": rows[-1]["nav_date"]}


def _manager_change(conn, fcode: str, as_of: str) -> dict[str, Any] | None:
    cutoff = (date.fromisoformat(as_of) - timedelta(days=MANAGER_CHANGE_DAYS)).isoformat()
    row = conn.execute(
        "SELECT mgr_name, start_date FROM fund_manager_tenure"
        " WHERE fcode=? AND start_date<=? ORDER BY start_date DESC LIMIT 1",
        (fcode, as_of)).fetchone()
    if row is None or not row["start_date"] or row["start_date"] <= cutoff:
        return None
    return {"mgr_name": row["mgr_name"], "start_date": row["start_date"]}


def _style_drift(conn, fcode: str, run_id: str, prev_run: str | None) -> dict[str, Any] | None:
    if not prev_run:
        return None
    cur = conn.execute("SELECT style_code FROM screen_decision WHERE run_id=? AND fcode=?",
                       (run_id, fcode)).fetchone()
    old = conn.execute("SELECT style_code FROM screen_decision WHERE run_id=? AND fcode=?",
                       (prev_run, fcode)).fetchone()
    if not cur or not old or not cur["style_code"] or not old["style_code"]:
        return None
    if cur["style_code"] == old["style_code"]:
        return None
    return {"from": old["style_code"], "to": cur["style_code"]}


def _picks(conn, run_id: str, boards: Sequence[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in boards)
    rows = conn.execute(
        f"SELECT p.fcode, p.board, p.rank, f.name FROM screen_pick p"
        f" LEFT JOIN fund f ON f.fcode=p.fcode"
        f" WHERE p.run_id=? AND p.board IN ({placeholders}) ORDER BY p.board, p.rank",
        (run_id, *boards)).fetchall()
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        item = seen.setdefault(r["fcode"], {"fcode": r["fcode"], "name": r["name"],
                                           "boards": [], "rank": r["rank"]})
        item["boards"].append(r["board"])
    return list(seen.values())


def scan(conn, as_of: str | None = None, boards: Sequence[str] = DEFAULT_BOARDS) -> dict[str, Any]:
    pub = _latest_publication(conn)
    if pub is None:
        return {"as_of": as_of, "publication_id": None, "alerts": [],
                "summary": {"funds": 0, "alerts": 0}}
    as_of = as_of or pub["business_date"]
    prev_run = _previous_run(conn, pub["generation"])
    picks = _picks(conn, pub["run_id"], boards)
    alerts: list[dict[str, Any]] = []
    for item in picks:
        fcode = item["fcode"]
        nav = _nav_metrics(conn, fcode, as_of)
        if nav is None:
            alerts.append({"fcode": fcode, "name": item["name"], "boards": item["boards"],
                           "code": "nav_missing", "severity": "high",
                           "message": "近一年净值不足，无法复核"})
        else:
            if nav["freshness_days"] > NAV_STALE_DAYS:
                alerts.append({"fcode": fcode, "name": item["name"], "boards": item["boards"],
                               "code": "nav_stale", "severity": "high",
                               "value": nav["freshness_days"],
                               "message": f"净值已 {nav['freshness_days']} 天未更新"})
            limit = max(DRAWDOWN_LIMITS.get(b, 2.5) for b in item["boards"])
            if nav["mdd_pct"] > limit:
                alerts.append({"fcode": fcode, "name": item["name"], "boards": item["boards"],
                               "code": "drawdown", "severity": "medium",
                               "value": nav["mdd_pct"],
                               "message": f"近一年最大回撤 {nav['mdd_pct']:.2f}% 超过 {limit:.1f}%"})
        mgr = _manager_change(conn, fcode, as_of)
        if mgr:
            alerts.append({"fcode": fcode, "name": item["name"], "boards": item["boards"],
                           "code": "manager_change", "severity": "medium",
                           "value": mgr["start_date"],
                           "message": f"{mgr['start_date']} 新任经理 {mgr['mgr_name'] or ''} 上任"})
        scale = events_mod.scale_features(conn, fcode, as_of)
        if scale.get("aum_billion") is not None and scale["aum_billion"] < SCALE_SMALL_BILLION:
            alerts.append({"fcode": fcode, "name": item["name"], "boards": item["boards"],
                           "code": "scale_small", "severity": "high",
                           "value": scale["aum_billion"],
                           "message": f"规模 {scale['aum_billion']:.2f} 亿元低于 {SCALE_SMALL_BILLION:.0f} 亿"})
        if scale.get("aum_yoy_pct") is not None and scale["aum_yoy_pct"] < SCALE_SHRINK_PCT:
            alerts.append({"fcode": fcode, "name": item["name"], "boards": item["boards"],
                           "code": "scale_shrink", "severity": "medium",
                           "value": scale["aum_yoy_pct"],
                           "message": f"规模同比 {scale['aum_yoy_pct']:.1f}% 低于 {SCALE_SHRINK_PCT:.0f}%"})
        drift = _style_drift(conn, fcode, pub["run_id"], prev_run)
        if drift:
            alerts.append({"fcode": fcode, "name": item["name"], "boards": item["boards"],
                           "code": "style_drift", "severity": "low",
                           "value": f"{drift['from']}->{drift['to']}",
                           "message": f"风格从 {drift['from']} 漂移到 {drift['to']}"})
    counts: dict[str, int] = {}
    for a in alerts:
        counts[a["code"]] = counts.get(a["code"], 0) + 1
    sev: dict[str, int] = {}
    for a in alerts:
        sev[a["severity"]] = sev.get(a["severity"], 0) + 1
    return {"as_of": as_of, "publication_id": pub["publication_id"],
            "generation": pub["generation"], "boards": list(boards),
            "funds": len(picks), "alerts": alerts,
            "summary": {"funds": len(picks), "alerts": len(alerts),
                        "by_code": counts, "by_severity": sev}}


def render(result: dict[str, Any]) -> str:
    if not result.get("publication_id"):
        return "没有已提交的发布，无法扫描告警"
    lines = [f"产品告警（generation {result['generation']}，as_of {result['as_of']}）",
             f"扫描 {result['funds']} 只，告警 {result['summary']['alerts']} 条"]
    for sev in ("high", "medium", "low"):
        group = [a for a in result["alerts"] if a["severity"] == sev]
        if not group:
            continue
        lines.append("")
        lines.append(f"[{sev}] {len(group)} 条")
        for a in group:
            lines.append(f"  {a['fcode']} {a['name'] or ''}  {a['message']}")
    return "\n".join(lines)
