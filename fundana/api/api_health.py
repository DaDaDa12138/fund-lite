"""API 健康页数据层：把 source_registry 与 fetch_log 摊平成可核对的一页。

口径：
- 成功率 = business_ok=1 的调用 / 窗口内调用；HTTP 200 但业务失败不算成功。
- p50/p95 延迟只统计窗口内真实调用；慢请求 = 超过该源 domain 的延迟预算。
- 新鲜度 = 最近一次 business_ok=1 距现在的小时数，与 source_registry 的 ttl_days 比较。
- 健康度 = 60% 成功率 + 25% 延迟 + 15% 新鲜度；不额外发请求、不做推断性打分。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from .. import db, labels as labels_mod

CN_TZ = db.CN
DEFAULT_DAYS = 7
MAX_DAYS = 90

# 按 domain 给延迟预算：批量快照天然比单基金档案慢，不能用一个固定阈值。
LATENCY_BUDGET_MS = {
    "directory": 2500,
    "status": 5000,
    "profile": 2500,
    "nav": 2500,
    "rank": 6000,
    "fee": 2500,
    "validation": 3000,
    "scale": 2500,
    "manager": 2500,
    "holding": 2500,
}
DEFAULT_BUDGET_MS = 3000

ROLE_LABELS = {
    "primary": "主链路",
    "validation": "跨源校验",
    "cross": "交叉校验",
    "unknown": "未注册",
}
HEALTH_LABELS = {
    "healthy": "健康",
    "degraded": "降级",
    "down": "异常",
    "unknown": "未验证",
    "unregistered": "未注册",
}
FRESHNESS_LABELS = {
    "fresh": "新鲜",
    "stale": "过期",
    "unknown": "无成功记录",
}


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def _percentile(values: list[int], q: float) -> float | None:
    """线性插值分位数；空列表返回 None，避免把“没有调用”写成 0ms。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 1)


def _parse_ts(value: str | None) -> datetime | None:
    return db.parse_ts(value)


def _age_hours(last_success_at: str | None, now: datetime) -> float | None:
    dt = _parse_ts(last_success_at)
    if not dt:
        return None
    return round(max(0.0, (now - dt).total_seconds() / 3600.0), 1)


def _role(spec) -> str:
    if spec is None:
        return "unknown"
    if spec.domain == "validation":
        return "validation"
    if spec.domain == "rank":
        return "cross"
    return "primary"


def _budget(spec) -> int:
    if spec is None:
        return DEFAULT_BUDGET_MS
    return int(LATENCY_BUDGET_MS.get(spec.domain, DEFAULT_BUDGET_MS))


def _latency_score(p95_ms: float | None, budget_ms: int) -> float:
    if p95_ms is None:
        return 0.0
    if p95_ms <= budget_ms:
        return 100.0
    if p95_ms >= budget_ms * 2:
        return 0.0
    return round(100.0 * (budget_ms * 2 - p95_ms) / budget_ms, 1)


def _freshness_score(age_hours: float | None, ttl_days: float) -> float:
    if age_hours is None:
        return 0.0
    ttl_hours = max(float(ttl_days) * 24.0, 1.0)
    if age_hours <= ttl_hours:
        return 100.0
    if age_hours >= ttl_hours * 2:
        return 0.0
    return round(100.0 * (ttl_hours * 2 - age_hours) / ttl_hours, 1)


def _evaluate(calls: int, ok: int, success_rate: float, p95_ms: float | None,
              budget_ms: int, age_hours: float | None, ttl_days: float) -> tuple[str, float, dict, str]:
    latency = _latency_score(p95_ms, budget_ms)
    freshness = _freshness_score(age_hours, ttl_days)
    score = round(0.60 * success_rate + 0.25 * latency + 0.15 * freshness, 1)
    breakdown = {"success": round(success_rate, 2), "latency": latency, "freshness": freshness}
    if calls == 0:
        return "unknown", 0.0, breakdown, "窗口内无调用"
    if ok == 0:
        return "down", 0.0, breakdown, "窗口内无成功调用"
    if success_rate < 90 or score < 60:
        return "down", score, breakdown, "成功率低于 90%"
    ttl_hours = max(float(ttl_days) * 24.0, 1.0)
    if success_rate < 99:
        return "degraded", score, breakdown, "成功率低于 99%"
    if p95_ms is not None and p95_ms > budget_ms * 2:
        return "degraded", score, breakdown, "p95 延迟高于预算 2 倍"
    if ttl_days > 0 and age_hours is not None and age_hours > ttl_hours:
        return "degraded", score, breakdown, "最近成功超过 TTL"
    if age_hours is not None and age_hours > ttl_hours * 2:
        return "degraded", score, breakdown, "最近成功已超过 2 个 TTL"
    if score < 80:
        return "degraded", score, breakdown, "综合健康度低于 80"
    return "healthy", score, breakdown, "正常"


def build(conn, cfg, days: int = DEFAULT_DAYS, now: datetime | None = None) -> dict[str, Any]:
    days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    now = now or datetime.now(db.CN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=db.CN)
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")

    rows = [dict(r) for r in conn.execute(
        "SELECT source_id, started_at, duration_ms, business_ok, status, error_msg"
        " FROM fetch_log WHERE started_at >= ? ORDER BY source_id, started_at", (since,))]
    by_source: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_source[row["source_id"]].append(row)

    state: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT source_id, MAX(consecutive_failures) AS consecutive_failures,"
            " MAX(cooldown_until) AS cooldown_until FROM source_state GROUP BY source_id"):
        state[r["source_id"]] = dict(r)

    specs = dict(getattr(cfg, "sources", {}) or {})
    ordered_ids = list(specs.keys())
    # fetch_log 里出现但注册表没有的源也要列出来，否则“全部接口”会漏掉漂移。
    ordered_ids += sorted(set(by_source) - set(specs))

    sources: list[dict[str, Any]] = []
    all_durations: list[int] = []
    slow_total = 0
    for sid in ordered_ids:
        spec = specs.get(sid)
        items = by_source.get(sid, [])
        durations = [int(x["duration_ms"] or 0) for x in items]
        all_durations.extend(durations)
        calls = len(items)
        ok = sum(1 for x in items if x["business_ok"])
        success_rate = _pct(ok, calls)
        p50 = _percentile(durations, 0.50)
        p95 = _percentile(durations, 0.95)
        max_ms = max(durations) if durations else None
        budget = _budget(spec)
        slow = sum(1 for d in durations if d > budget)
        slow_total += slow
        last = items[-1] if items else None
        successes = [x for x in items if x["business_ok"]]
        last_success = successes[-1]["started_at"] if successes else None
        last_error = next((x["error_msg"] for x in reversed(items)
                           if not x["business_ok"] and x["error_msg"]), None)
        age = _age_hours(last_success, now)
        ttl = float(spec.ttl_days) if spec is not None else 0.0
        if last_success is None:
            freshness = "unknown"
        elif ttl > 0 and (age or 0.0) <= ttl * 24.0:
            freshness = "fresh"
        else:
            freshness = "stale"
        health, score, breakdown, note = _evaluate(
            calls, ok, success_rate, p95, budget, age, ttl)
        if spec is None:
            health, note, score = "unregistered", "未在 source_registry.yaml 注册", None
        st = state.get(sid, {})
        sources.append({
            "source_id": sid,
            "label": labels_mod.label_source(sid),
            "domain": spec.domain if spec is not None else "",
            "role": _role(spec),
            "role_label": ROLE_LABELS.get(_role(spec), ROLE_LABELS["unknown"]),
            "method": spec.method if spec is not None else "GET",
            "url": spec.url if spec is not None else "",
            "parser": spec.parser if spec is not None else "",
            "ttl_days": spec.ttl_days if spec is not None else 0,
            "rate_qps": spec.rate_qps if spec is not None else 0,
            "note": spec.note if spec is not None else "未在 source_registry.yaml 注册",
            "calls": calls,
            "business_ok": ok,
            "business_failed": calls - ok,
            "success_rate_pct": success_rate,
            "avg_ms": round(sum(durations) / len(durations), 1) if durations else None,
            "p50_ms": p50,
            "p95_ms": p95,
            "max_ms": max_ms,
            "latency_budget_ms": budget,
            "slow_calls": slow,
            "slow_rate_pct": _pct(slow, calls),
            "last_at": last["started_at"] if last else None,
            "last_success_at": last_success,
            "age_hours": age,
            "freshness": freshness,
            "freshness_label": FRESHNESS_LABELS[freshness],
            "last_status": last["status"] if last else None,
            "last_error": last_error,
            "consecutive_failures": int(st.get("consecutive_failures") or 0),
            "cooldown_until": st.get("cooldown_until"),
            "health": health,
            "health_label": HEALTH_LABELS[health],
            "health_score": score,
            "score_breakdown": breakdown,
            "status_note": note,
        })

    counts = {k: sum(1 for s in sources if s["health"] == k)
              for k in ("healthy", "degraded", "down", "unknown", "unregistered")}
    total_calls = len(rows)
    total_ok = sum(1 for r in rows if r["business_ok"])
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_days": days,
        "since": since,
        "registry": {
            "version": getattr(cfg, "registry_version", "") or "",
            "count": len(specs),
            "unregistered": len(set(by_source) - set(specs)),
        },
        "summary": {
            "sources": len(sources),
            **counts,
            "calls": total_calls,
            "business_ok": total_ok,
            "success_rate_pct": _pct(total_ok, total_calls),
            "avg_ms": round(sum(all_durations) / len(all_durations), 1) if all_durations else None,
            "p95_ms": _percentile(all_durations, 0.95),
            "slow_calls": slow_total,
            "slow_rate_pct": _pct(slow_total, total_calls),
            "last_at": max((r["started_at"] for r in rows), default=None),
        },
        "sources": sources,
    }
