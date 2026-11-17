"""从 raw_object 重放 pingzhongdata，重建 fund_nav_daily（一次性修正用）。

背景（ADR-019）：_ms_to_date 曾用 UTC 解释东财"业务日零点（北京时间）"的毫秒时间戳，
导致 901 万行 nav_date 整体左移一天。解析器已改为固定 +08:00，本脚本用**已落库的原始
响应**重放，把历史行按正确日期重写——不重新联网，因此可离线执行、可审计。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fundana import config, db
from fundana.fetch import parsers


def main(limit: int | None = None) -> int:
    cfg = config.load_config()
    conn = db.connect(cfg)
    rows = conn.execute(
        "SELECT key, MAX(started_at) AS ts FROM fetch_log"
        " WHERE source_id='em_pzd' AND business_ok=1 AND raw_sha256 IS NOT NULL"
        " GROUP BY key ORDER BY key").fetchall()
    codes = [r["key"] for r in rows if r["key"] and len(r["key"]) == 6]
    if limit:
        codes = codes[:limit]
    print(f"重放 {len(codes)} 只基金的 pzd 原始响应", flush=True)
    t0 = time.monotonic()
    ok = fail = written = dropped = rev_dropped = 0
    for i, code in enumerate(codes, 1):
        sha = conn.execute(
            "SELECT raw_sha256 FROM fetch_log WHERE source_id='em_pzd' AND key=? AND business_ok=1"
            " AND raw_sha256 IS NOT NULL ORDER BY started_at DESC LIMIT 1", (code,)).fetchone()
        if not sha:
            fail += 1
            continue
        try:
            body = db.get_raw(conn, sha["raw_sha256"])
            text = body.decode("utf-8", errors="replace")
            parsed = parsers.parse_pzd(text)
            nav_rows = parsers.pzd_nav_rows(parsed["nav"], parsed["acc"])
        except Exception as exc:                      # noqa: BLE001
            fail += 1
            print(f"  ! {code} {type(exc).__name__}: {exc}", flush=True)
            continue
        if not nav_rows:
            fail += 1
            continue
        old = conn.execute("SELECT COUNT(*) FROM fund_nav_daily WHERE fcode=?", (code,)).fetchone()[0]
        dropped += old
        rev_dropped += conn.execute("DELETE FROM fund_nav_revision WHERE fcode=?", (code,)).rowcount
        conn.execute("DELETE FROM fund_nav_daily WHERE fcode=?", (code,))
        conn.executemany(
            "INSERT INTO fund_nav_daily(fcode, nav_date, unit_nav, acc_nav, growth_pct, nav_source,"
            " raw_sha256, revision_no) VALUES(?,?,?,?,?,?,?,0)",
            [(code, r["nav_date"], r["unit_nav"], r["acc_nav"], r["growth_pct"], "em_pzd",
              sha["raw_sha256"]) for r in nav_rows])
        written += len(nav_rows)
        ok += 1
        if i % 200 == 0:
            conn.commit()
            print(f"  [{i}/{len(codes)}] ok={ok} fail={fail} rows={written}"
                  f" {time.monotonic() - t0:.0f}s", flush=True)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    out = {"funds_ok": ok, "funds_failed": fail, "rows_written": written,
           "rows_replaced": dropped, "revisions_dropped": rev_dropped,
           "elapsed_s": round(time.monotonic() - t0, 1)}
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else None))
