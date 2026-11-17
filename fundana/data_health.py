"""数据健康快照：DB 完整性、迁移漂移、净值新鲜度、指标覆盖、采集健康、发布状态。

只读；不推断、不修复。口径：
- schema drift = 迁移文件 vs schema_migration.version；缺文件/多记录都算漂移。
- nav_age 以 China 今天（或显式 --business-date）为基准；stale = 单只基金最新净值落后基准 >10 天。
- fetch 成功率按 business_ok=1 / 窗口内调用；HTTP 200 但业务失败不算成功。
- source_redundancy 只告警、不失败：主链路单源域列出，便于后续补第二来源。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import db, freshness
from .metrics import contract as metric_contract

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
NAV_STALE_DAYS = 10
FETCH_WINDOW_DAYS = 7
# 筛选主链路必须至少有两个可切换来源的域；只有单源不是故障，但必须显式告警。
CRITICAL_DOMAINS = ("directory", "status", "profile", "nav", "scale", "manager", "holding", "fee")


def migration_files() -> list[str]:
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))


def schema_drift(applied: Iterable[str], files: Iterable[str] | None = None) -> dict[str, Any]:
    """比较已应用迁移与仓库迁移文件。纯函数，便于测试。"""
    applied_set = set(applied)
    file_list = sorted(files if files is not None else migration_files())
    file_set = set(file_list)
    missing = [f for f in file_list if f not in applied_set]
    unknown = sorted(applied_set - file_set)
    return {"ok": not missing and not unknown, "applied": len(applied_set),
            "files": len(file_list), "missing": missing, "unknown": unknown}


def source_health(conn, sources: dict[str, Any] | None, days: int = FETCH_WINDOW_DAYS,
                  now: datetime | None = None) -> dict[str, Any]:
    """按来源汇总近 N 天采集结果，并标出主链路中的单源域。

    sources 为 {source_id: SourceSpec 或含 domain 属性的对象}；传 None 时只返回
    fetch_log 中出现的来源，不推断注册表冗余。
    """
    now = now or datetime.now(db.CN)
    since = (now - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    stats: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
            "SELECT source_id, COUNT(*) AS calls, "
            "SUM(CASE WHEN business_ok=1 THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN business_ok=0 THEN 1 ELSE 0 END) AS failed, "
            "MAX(started_at) AS last_started "
            "FROM fetch_log WHERE started_at >= ? GROUP BY source_id", (since,)):
        stats[str(r[0])] = {"calls": int(r[1] or 0), "business_ok": int(r[2] or 0),
                            "business_failed": int(r[3] or 0), "last_started": r[4]}
    items: list[dict[str, Any]] = []
    for sid, spec in sorted((sources or {}).items()):
        domain = str(getattr(spec, "domain", "") or (spec.get("domain", "") if isinstance(spec, dict) else ""))
        s = stats.get(sid, {})
        calls = int(s.get("calls", 0))
        ok = int(s.get("business_ok", 0))
        items.append({"source_id": sid, "domain": domain, "calls": calls,
                      "business_ok": ok, "business_failed": int(s.get("business_failed", 0)),
                      "success_rate_pct": (round(100.0 * ok / calls, 2) if calls else None),
                      "last_started": s.get("last_started")})
    known = {x["source_id"] for x in items}
    for sid, s in stats.items():
        if sid not in known:
            items.append({"source_id": sid, "domain": None, "calls": s["calls"],
                          "business_ok": s["business_ok"], "business_failed": s["business_failed"],
                          "success_rate_pct": (round(100.0 * s["business_ok"] / s["calls"], 2)
                                               if s["calls"] else None),
                          "last_started": s.get("last_started")})
    domains: dict[str, list[str]] = {}
    for item in items:
        if item["domain"]:
            domains.setdefault(item["domain"], []).append(item["source_id"])
    single = ([d for d in CRITICAL_DOMAINS if len(domains.get(d, [])) < 2]
              if sources else [])
    return {"sources": items, "domains": domains, "single_source_domains": single,
            "warnings": ["single_source:%s" % d for d in single]}


# 源事实表 -> (日期列, 来源列)。raw_sha256 必须能回查到 raw_object，
# 否则“数据来自公开 API”只是口头承诺，无法复核。
PROVENANCE_TABLES = (
    ("fund_nav_daily", "nav_date", "nav_source"),
    ("src_directory", "business_date", None),
    ("src_snapshot_status", "business_date", None),
    ("src_pzd_profile", "business_date", None),
    ("src_jbgk_profile", "business_date", None),
    ("src_zqcc_holding", "business_date", None),
    ("src_manager_history", "business_date", None),
    ("src_rank_row", "business_date", None),
    ("market_index_daily", "trade_date", "source_id"),
    ("fund_fee_meta", "observed_at", "source"),
    ("fund_holding_bond", "report_period", None),
    ("fund_scale_history", "report_period", "src"),
)
# 落库别名：B1 快照在 fund_nav_daily.nav_source 里记 em_b1/em_b1_prev，
# 但注册表只有 em_nav_snapshot。别名不改变来源，只影响展示名。
SOURCE_ALIASES = {"em_b1": "em_nav_snapshot", "em_b1_prev": "em_nav_snapshot"}


def _table_exists(conn, name: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone() is not None
    except Exception:
        return False


def _columns(conn, name: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(%s)" % name)}
    except Exception:
        return set()


def provenance_audit(conn, nav_since: str | None = None,
                     allowed_sources: set[str] | None = None) -> dict[str, Any]:
    """逐表核对源事实的 raw_sha256 是否真实存在，并检查来源是否已注册。

    只读。旧库没有 raw_object 时返回 not_applicable，避免把测试夹具/早期库判死。
    这不是“数据正确”的证明，只证明每条源事实都能反查到一次真实 HTTP 响应。
    """
    if not _table_exists(conn, "raw_object"):
        return {"ok": True, "status": "not_applicable",
                "detail": "raw_object 不存在（旧库或测试夹具）",
                "tables": [], "missing_total": 0, "bad_format_total": 0,
                "unregistered_sources": []}

    aliases = dict(SOURCE_ALIASES)
    allowed = ({aliases.get(str(s), str(s)) for s in allowed_sources}
               if allowed_sources is not None else None)
    tables: list[dict[str, Any]] = []
    missing_total = 0
    bad_format_total = 0
    unregistered: list[dict[str, Any]] = []
    for table, date_col, source_col in PROVENANCE_TABLES:
        if not _table_exists(conn, table):
            tables.append({"table": table, "status": "missing_table", "ok": False,
                           "rows": 0, "missing_raw": 0, "bad_format": 0})
            continue
        cols = _columns(conn, table)
        if "raw_sha256" not in cols:
            tables.append({"table": table, "status": "missing_column", "ok": False,
                           "rows": 0, "missing_raw": 0, "bad_format": 0})
            continue
        where = ""
        params: tuple[Any, ...] = ()
        if nav_since and date_col and date_col in cols:
            where = " WHERE t.%s >= ?" % date_col
            params = (nav_since,)
        row = conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN t.raw_sha256 IS NULL OR length(t.raw_sha256)<>64"
            "          THEN 1 ELSE 0 END) AS bad_format,"
            " SUM(CASE WHEN t.raw_sha256 IS NOT NULL AND length(t.raw_sha256)=64"
            "          AND NOT EXISTS (SELECT 1 FROM raw_object ro"
            "                          WHERE ro.sha256=t.raw_sha256)"
            "          THEN 1 ELSE 0 END) AS missing_raw"
            " FROM %s t%s" % (table, where), params).fetchone()
        n = int(row[0] or 0) if row else 0
        bad_format = int(row[1] or 0) if row else 0
        missing_raw = int(row[2] or 0) if row else 0
        missing_total += missing_raw
        bad_format_total += bad_format
        entry = {"table": table, "rows": n, "missing_raw": missing_raw,
                 "bad_format": bad_format,
                 "ok": n == 0 or (missing_raw == 0 and bad_format == 0)}
        if source_col and source_col in cols and allowed is not None:
            seen = [str(r[0]) for r in conn.execute(
                "SELECT DISTINCT %s FROM %s WHERE %s IS NOT NULL"
                % (source_col, table, source_col))]
            unknown = sorted({s for s in seen if aliases.get(s, s) not in allowed})
            if unknown:
                entry["ok"] = False
                entry["unregistered_sources"] = unknown
                unregistered.append({"table": table, "column": source_col,
                                     "sources": unknown})
        tables.append(entry)
    status = "ok" if all(t["ok"] for t in tables) else "failed"
    return {"ok": status == "ok", "status": status, "tables": tables,
            "missing_total": missing_total, "bad_format_total": bad_format_total,
            "unregistered_sources": unregistered,
            "nav_since": nav_since}


def _one(conn, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None




def scheduler_health(state_dir, now: datetime | None = None,
                     grace_minutes: int = 120, conn=None) -> dict[str, Any]:
    """检查 scheduler 心跳是否过期；只告警，不改变 data-health 退出码。

    心跳由 scheduler 每轮写入 next_run_at / last_run_at / last_status。主机重启后
    若 systemd 没把进程拉起来，next_run_at 会一直停在过去；这里用宽限 120 分钟
    识别“该跑却没跑”，比只看 publication 更早。
    """
    if not state_dir:
        return {"ok": True, "status": "not_checked", "warn": False}
    path = Path(state_dir) / "scheduler_heartbeat.json"
    if not path.exists():
        return {"ok": True, "status": "missing", "warn": True,
                "detail": "no scheduler_heartbeat.json"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": True, "status": "invalid", "warn": True,
                "detail": str(exc)}
    now = now or datetime.now(db.CN)
    next_run = None
    try:
        if data.get("next_run_at"):
            next_run = datetime.fromisoformat(str(data["next_run_at"]))
    except ValueError:
        next_run = None
    overdue = bool(next_run and next_run < now - timedelta(minutes=max(0, int(grace_minutes))))
    last_status = data.get("last_status")
    last_run_at = data.get("last_run_at")
    if conn is not None and not last_status:
        try:
            row = conn.execute(
                "SELECT status, started_at FROM run WHERE mode='daily'"
                " ORDER BY started_at DESC LIMIT 1").fetchone()
        except Exception:
            row = None
        if row:
            last_status, last_run_at = row[0], row[1]
    failed = str(last_status or "").lower() in ("failed", "error")
    status = "overdue" if overdue else ("last_failed" if failed else "ok")
    return {"ok": True, "status": status, "warn": bool(overdue or failed),
            "last_run_at": last_run_at, "next_run_at": data.get("next_run_at"),
            "last_status": last_status, "grace_minutes": int(grace_minutes)}


def build(conn, days: int = FETCH_WINDOW_DAYS, business_date: str | None = None,
          integrity: bool = True, sources: dict[str, Any] | None = None,
          state_dir=None) -> dict[str, Any]:
    days = max(1, min(int(days), 90))
    quick = "skipped"
    if integrity:
        try:
            quick = str(_one(conn, "PRAGMA quick_check") or "unknown")
        except Exception as exc:  # pragma: no cover - 损坏库才会走到
            quick = "error: %s" % exc

    applied = [r[0] for r in conn.execute("SELECT version FROM schema_migration ORDER BY rowid")]
    drift = schema_drift(applied)

    latest_nav = _one(conn, "SELECT MAX(nav_date) FROM fund_nav_daily")
    ref = business_date or datetime.now(db.CN).date().isoformat()
    nav_cov = freshness.nav_coverage(conn, as_of=ref)
    effective_nav = nav_cov.get("effective_date")
    nav_age = None
    if effective_nav:
        try:
            nav_age = (date.fromisoformat(ref) - date.fromisoformat(str(effective_nav))).days
        except ValueError:
            nav_age = None
    max_nav_age = None
    if latest_nav:
        try:
            max_nav_age = (date.fromisoformat(ref) - date.fromisoformat(str(latest_nav))).days
        except ValueError:
            max_nav_age = None
    nav_funds = int(_one(conn, "SELECT COUNT(DISTINCT fcode) FROM fund_nav_daily") or 0)
    nav_rows = int(_one(conn, "SELECT COUNT(*) FROM fund_nav_daily") or 0)
    # 清盘基金净值停在清盘日，不计入“当前净值陈旧”告警；否则回填会把 stale 永久抬高。
    active = freshness._active_clause(conn)
    stale = int(_one(
        conn,
        "WITH latest AS (SELECT fcode, MAX(nav_date) AS d FROM fund_nav_daily GROUP BY fcode) "
        "SELECT COUNT(*) FROM latest"
        + (" JOIN fund f ON f.fcode=latest.fcode" if active else "")
        + " WHERE d < date(?, ?)" + active,
        (ref, "-%d day" % NAV_STALE_DAYS)) or 0)

    contract = metric_contract.FORMULA_CONTRACT
    metric_cov = (freshness.metric_coverage(conn, effective_nav or ref,
                                            formula_contract=contract)
                  if (effective_nav or ref) else {})
    metric_rows: list[dict[str, Any]] = []
    for r in conn.execute(
            "SELECT period_days, COUNT(*) AS n, "
            "SUM(CASE WHEN annual_dec IS NULL THEN 1 ELSE 0 END) AS annual_null, "
            "SUM(CASE WHEN mdd_dec IS NULL THEN 1 ELSE 0 END) AS mdd_null "
            "FROM fund_metric WHERE formula_contract=? GROUP BY period_days ORDER BY period_days",
            (contract,)):
        n = int(r[1] or 0)
        metric_rows.append({"period_days": r[0], "n": n,
                            "annual_null": int(r[2] or 0), "mdd_null": int(r[3] or 0),
                            "annual_null_pct": (round(100.0 * int(r[2] or 0) / n, 2) if n else 0.0)})

    since = (datetime.now(db.CN) - timedelta(days=days)).isoformat(timespec="seconds")
    fetch_sources: list[dict[str, Any]] = []
    for r in conn.execute(
            "SELECT source_id, COUNT(*) AS calls, "
            "SUM(CASE WHEN business_ok=1 THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN business_ok=0 THEN 1 ELSE 0 END) AS failed "
            "FROM fetch_log WHERE started_at >= ? GROUP BY source_id ORDER BY source_id", (since,)):
        calls = int(r[1] or 0)
        ok = int(r[2] or 0)
        fetch_sources.append({"source_id": r[0], "calls": calls, "business_ok": ok,
                              "business_failed": int(r[3] or 0),
                              "success_rate_pct": (round(100.0 * ok / calls, 2) if calls else 0.0)})
    fetch_calls = sum(x["calls"] for x in fetch_sources)
    fetch_ok = sum(x["business_ok"] for x in fetch_sources)
    src_health = source_health(conn, sources, days=days)
    try:
        prov_since = (date.fromisoformat(str(effective_nav or ref))
                      - timedelta(days=90)).isoformat()
    except ValueError:
        prov_since = None
    prov = provenance_audit(conn, nav_since=prov_since,
                            allowed_sources=set(sources or {}) if sources else None)

    pub = conn.execute(
        "SELECT publication_id, generation, screen_contract, formula_contract, status, created_at "
        "FROM publication ORDER BY generation DESC LIMIT 1").fetchone()
    publication = dict(pub) if pub else None
    gate = None
    if pub:
        gate = conn.execute(
            "SELECT status, warnings_json, blocks_json FROM publication_gate "
            "WHERE publication_id=? ORDER BY rowid DESC LIMIT 1",
            (publication["publication_id"],)).fetchone()
        if gate:
            gate = dict(gate)

    lifecycle_counts: dict[str, int] = {}
    try:
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM fund_lifecycle GROUP BY status"):
            lifecycle_counts[str(r["status"])] = int(r["n"])
        lifecycle_funds = int(conn.execute(
            "SELECT COUNT(DISTINCT fcode) FROM fund_lifecycle").fetchone()[0])
        lifecycle_with_nav = int(conn.execute(
            "SELECT COUNT(DISTINCT l.fcode) FROM fund_lifecycle l"
            " WHERE EXISTS (SELECT 1 FROM fund_nav_daily n WHERE n.fcode=l.fcode)"
        ).fetchone()[0])
    except Exception:
        lifecycle_funds = 0
        lifecycle_with_nav = 0

    def _distinct(table: str, column: str = "fcode") -> int:
        try:
            return int(conn.execute(
                f"SELECT COUNT(DISTINCT {column}) FROM {table}").fetchone()[0])
        except Exception:
            return 0

    try:
        funds_active = int(conn.execute(
            "SELECT COUNT(*) FROM fund WHERE COALESCE(fund_status,'') NOT IN ('liquidated','merged')"
        ).fetchone()[0])
        funds_liquidated = int(conn.execute(
            "SELECT COUNT(*) FROM fund WHERE fund_status IN ('liquidated','merged')"
        ).fetchone()[0])
    except Exception:
        funds_active = funds_liquidated = 0

    coverage = {
        "funds": _distinct("fund"),
        "funds_active": funds_active,
        "funds_liquidated": funds_liquidated,
        "profile": _distinct("fund_profile"),
        "nav": _distinct("fund_nav_daily"),
        "fee": _distinct("fund_fee_meta"),
        "scale": _distinct("fund_scale_history"),
        "manager_current": int(conn.execute(
            "SELECT COUNT(DISTINCT fcode) FROM fund_manager_tenure WHERE is_current=1").fetchone()[0]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_manager_tenure'").fetchone()
            else 0),
        "holding": _distinct("fund_holding_bond", "fcode"),
        "lifecycle": lifecycle_funds,
        "lifecycle_with_nav": lifecycle_with_nav,
    }
    try:
        row = conn.execute(
            "SELECT report_period, COUNT(DISTINCT fcode) AS n FROM fund_holding_bond"
            " GROUP BY report_period ORDER BY report_period DESC LIMIT 1").fetchone()
        coverage["holding_latest_period"] = row["report_period"] if row else None
        coverage["holding_latest_funds"] = int(row["n"]) if row else 0
    except Exception:
        coverage["holding_latest_period"] = None
        coverage["holding_latest_funds"] = 0

    nav_partial = bool(latest_nav and effective_nav and str(latest_nav) > str(effective_nav))
    metric_status = metric_cov.get("status")
    metric_ok = metric_status in ("ok", "unknown")
    checks = {
        "db_integrity": {"ok": quick in ("ok", "skipped"), "detail": quick},
        "schema": {"ok": drift["ok"], **drift},
        "nav_freshness": {"ok": effective_nav is not None and nav_age is not None
                                and nav_age <= NAV_STALE_DAYS,
                          "effective_nav": effective_nav, "latest_nav": latest_nav,
                          "max_nav": latest_nav, "business_date": ref,
                          "age_days": nav_age, "max_age_days": max_nav_age,
                          "coverage_pct": nav_cov.get("effective_coverage_pct"),
                          "max_coverage_pct": nav_cov.get("max_coverage_pct"),
                          "partial_latest": nav_partial, "stale_funds": stale},
        "metric_freshness": {"ok": metric_ok, "status": metric_status,
                             "metric_as_of": metric_cov.get("metric_as_of"),
                             "current_pct": metric_cov.get("current_pct"),
                             "fresh_pct": metric_cov.get("fresh_pct"),
                             "older_pct": metric_cov.get("older_pct"),
                             "missing_pct": metric_cov.get("missing_pct"),
                             "denominator": metric_cov.get("denominator"),
                             "warn": (not metric_ok) or metric_status != "ok"},
        "metrics": {"ok": bool(metric_rows), "contract": contract,
                    "periods": len(metric_rows), "rows": sum(x["n"] for x in metric_rows)},
        "fetch": {"ok": (fetch_calls == 0) or (fetch_ok / fetch_calls >= 0.9),
                  "window_days": days, "calls": fetch_calls, "business_ok": fetch_ok,
                  "success_rate_pct": (round(100.0 * fetch_ok / fetch_calls, 2)
                                       if fetch_calls else None)},
        "publication": {"ok": publication is not None,
                        "generation": publication.get("generation") if publication else None,
                        "screen_contract": publication.get("screen_contract") if publication else None,
                        "gate_status": gate.get("status") if gate else None},
        "source_redundancy": {"ok": True, "warn": bool(src_health["single_source_domains"]),
                              "single_source_domains": src_health["single_source_domains"]},
        "scheduler": scheduler_health(state_dir, conn=conn),
        "lifecycle": {"ok": True, "status": "informational",
                       "funds": lifecycle_funds, "with_nav": lifecycle_with_nav,
                       "counts": lifecycle_counts},
        "coverage": {"ok": True, "status": "informational", **coverage},
        "provenance": prov,
    }
    failed = [k for k, v in checks.items() if not v.get("ok")]
    warnings = list(src_health["warnings"])
    if nav_partial:
        warnings.append("nav_partial_latest:%s<max:%s" % (effective_nav, latest_nav))
    if metric_status and metric_status != "ok":
        warnings.append("metric_not_current:%s" % metric_status)
    for w in (nav_cov.get("warnings") or []) + (metric_cov.get("warnings") or []):
        if w not in warnings:
            warnings.append(w)
    if checks["scheduler"].get("warn"):
        warnings.append("scheduler:%s" % checks["scheduler"].get("status"))
    return {
        "ok": not failed,
        "failed_checks": failed,
        "warnings": warnings,
        "business_date": ref,
        "nav": {"effective": effective_nav, "latest": latest_nav, "max": latest_nav,
                "age_days": nav_age, "max_age_days": max_nav_age,
                "coverage_pct": nav_cov.get("effective_coverage_pct"),
                "max_coverage_pct": nav_cov.get("max_coverage_pct"),
                "partial_latest": nav_partial, "funds": nav_funds,
                "rows": nav_rows, "stale_funds": stale,
                "by_date": nav_cov.get("by_date")},
        "metrics": {"contract": contract, "periods": metric_rows,
                    "as_of": metric_cov.get("metric_as_of"),
                    "coverage": metric_cov},
        "fetch": {"window_days": days, "calls": fetch_calls, "business_ok": fetch_ok,
                  "success_rate_pct": (round(100.0 * fetch_ok / fetch_calls, 2)
                                       if fetch_calls else None),
                  "sources": fetch_sources},
        "publication": publication,
        "gate": gate,
        "lifecycle": {"funds": lifecycle_funds, "with_nav": lifecycle_with_nav,
                      "counts": lifecycle_counts},
        "coverage": coverage,
        "source_health": src_health,
        "provenance": prov,
        "checks": checks,
        "note": "只读快照；quick_check 跳过大库完整性扫描时 status=skipped。",
    }
