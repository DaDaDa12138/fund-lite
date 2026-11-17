"""费用阶梯落库：把 parse_jjfl 的结果写成事实表 + 规范化阶梯。

为什么单独一层：采集器（联网）与回放器（从 raw_object 重解析）必须写同一套行，
否则修了解析器还要重新联网 8,000 次。这里只做纯写入，不发请求。
"""
from __future__ import annotations

from typing import Any

from .model import Tier, pick_redemption


def _tiers(rows: list[dict[str, Any]]) -> list[Tier]:
    return [Tier(**{k: r.get(k) for k in Tier.__dataclass_fields__}) for r in rows]


def redemption_rate_at(parsed: dict[str, Any], horizon_days: int) -> float | None:
    """按持有天数取赎回费率（百分点）；没有匹配档位返回 None。"""
    tier = pick_redemption(_tiers(parsed.get("redemption", [])), int(horizon_days))
    return None if tier is None else tier.rate_pct


def write_fee_tiers(conn, code: str, parsed: dict[str, Any], observed: str,
                    raw_sha: str) -> dict[str, int]:
    """替换某只基金的全部费用阶梯，并同步 fund_fee_schedule。"""
    conn.execute("DELETE FROM fund_fee_tier WHERE fcode=?", (code,))
    conn.execute("DELETE FROM fund_fee_schedule WHERE fcode=?", (code,))
    tier_rows = []
    for kind in ("purchase", "subscription", "redemption"):
        for i, t in enumerate(parsed.get(kind, []), 1):
            tier_rows.append((code, kind, i, t.get("amount_lo_cny"), t.get("amount_hi_cny"),
                              t.get("days_lo"), t.get("days_hi"), t.get("rate_pct"),
                              t.get("discount_pct"), t.get("fixed_cny"), observed, raw_sha,
                              t.get("label")))
    if tier_rows:
        conn.executemany(
            "INSERT INTO fund_fee_tier(fcode, kind, tier_no, amount_lo_cny, amount_hi_cny,"
            " days_lo, days_hi, rate_pct, discount_pct, fixed_cny, observed_at, raw_sha256,"
            " raw_label) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", tier_rows)

    schedule_rows = []
    for i, t in enumerate(parsed.get("purchase", []), 1):
        rate = t.get("discount_pct") if t.get("discount_pct") is not None else t.get("rate_pct")
        schedule_rows.append((code, "sub", i, t.get("amount_lo_cny"), t.get("amount_hi_cny"),
                              None, None, rate, "em_jjfl", t.get("label")))
    for i, t in enumerate(parsed.get("redemption", []), 1):
        schedule_rows.append((code, "red", i, None, None, t.get("days_lo"), t.get("days_hi"),
                              t.get("rate_pct"), "em_jjfl", t.get("label")))
    if schedule_rows:
        conn.executemany(
            "INSERT INTO fund_fee_schedule(fcode, kind, tier_no, amount_lo, amount_hi, days_lo,"
            " days_hi, rate_pct, src, raw_label) VALUES(?,?,?,?,?,?,?,?,?,?)", schedule_rows)
    return {"tiers": len(tier_rows), "schedule": len(schedule_rows)}


def write_fee_meta(conn, code: str, parsed: dict[str, Any], observed: str,
                   raw_sha: str) -> dict[str, Any]:
    """写基金级费率快照；赎回费 1/3 年档位只做展示与审计，不改变筛选门槛。"""
    op = parsed["operating"]
    has_discount = 1 if any(t.get("discount_pct") is not None for t in parsed["purchase"]) else 0
    red_1y = redemption_rate_at(parsed, 365)
    red_3y = redemption_rate_at(parsed, 1095)
    conn.execute(
        "INSERT INTO fund_fee_meta(fcode, mgr_fee_pct, trust_fee_pct, sales_fee_pct, has_discount,"
        " source, observed_at, raw_sha256, redemption_1y_pct, redemption_3y_pct)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode) DO UPDATE SET mgr_fee_pct=excluded.mgr_fee_pct,"
        " trust_fee_pct=excluded.trust_fee_pct, sales_fee_pct=excluded.sales_fee_pct,"
        " has_discount=excluded.has_discount, source=excluded.source,"
        " observed_at=excluded.observed_at, raw_sha256=excluded.raw_sha256,"
        " redemption_1y_pct=excluded.redemption_1y_pct,"
        " redemption_3y_pct=excluded.redemption_3y_pct",
        (code, op["mgr_fee_pct"], op["trust_fee_pct"], op["sales_fee_pct"], has_discount,
         "em_jjfl", observed, raw_sha, red_1y, red_3y))
    return {"has_discount": has_discount, "redemption_1y_pct": red_1y,
            "redemption_3y_pct": red_3y}
