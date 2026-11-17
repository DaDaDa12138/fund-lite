"""费用阶梯回放：从已落库的 jjfl raw_object 重解析，不重新联网。

用途：解析器修正后（例如“不小于7天/小于30天/T<7日”），把 8,000 只基金的
fund_fee_tier / fund_fee_schedule / fund_fee_meta 一次性重算，避免再跑 1 小时网络。
"""
from __future__ import annotations

from typing import Any

from .. import db
from ..fetch import parsers
from . import store


def rebuild_from_raw(conn, cfg, *, limit: int | None = None) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT fcode, raw_sha256, observed_at FROM fund_fee_meta"
        " WHERE raw_sha256 IS NOT NULL ORDER BY fcode").fetchall()
    if limit:
        rows = rows[:int(limit)]
    ok = fail = 0
    tiers = schedule = 0
    failures: dict[str, int] = {}
    for i, row in enumerate(rows, 1):
        try:
            body = db.get_raw(conn, row["raw_sha256"])
            text = body.decode("utf-8", "replace")
            parsed = parsers.parse_jjfl(text)
            observed = row["observed_at"] or db.now_iso()
            written = store.write_fee_tiers(conn, row["fcode"], parsed, observed,
                                            row["raw_sha256"])
            store.write_fee_meta(conn, row["fcode"], parsed, observed, row["raw_sha256"])
            tiers += written["tiers"]
            schedule += written["schedule"]
            ok += 1
        except Exception as exc:  # 单只坏页不阻断全量回放
            fail += 1
            key = type(exc).__name__
            failures[key] = failures.get(key, 0) + 1
        if i % 200 == 0:
            conn.commit()
    conn.commit()
    return {"ok": ok, "fail": fail, "tiers": tiers, "schedule": schedule,
            "failures": failures, "total": len(rows)}
