#!/usr/bin/env python3
"""按年补齐债券基金历史持仓（公开东财接口，可断点续跑）。

只调用 source_registry 中已注册的 em_zqcc_year：
  https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=zqcc&code={code}&topline=10&year={year}
每个成功响应都会写 raw_object/fetch_log；本脚本不生成任何模拟持仓。

用法：
  python3 scripts/backfill_holdings.py --years 2016-2026
  python3 scripts/backfill_holdings.py --years 2024-2026 --limit 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fundana import collect, db  # noqa: E402
from fundana.config import load_config  # noqa: E402
from fundana.fetch.http import Fetcher  # noqa: E402

STATE = ROOT / "var" / "state" / "holdings_backfill.json"


def parse_years(raw: str) -> list[int]:
    years: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            years.extend(range(int(lo), int(hi) + 1))
        else:
            years.append(int(part))
    return sorted(set(years))


def write_state(payload: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2016-2026")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    years = parse_years(args.years)
    if not years:
        print("no years", file=sys.stderr)
        return 2

    funds = conn.execute(
        "SELECT fcode, estab_date FROM fund ORDER BY fcode"
    ).fetchall()
    if args.limit:
        funds = funds[: args.limit]

    # 已有成功 fetch_log 的 (code,year) 直接跳过；force 时重拉。
    done: set[str] = set()
    if not args.force:
        done = {
            r["key"]
            for r in conn.execute(
                "SELECT key FROM fetch_log WHERE source_id='em_zqcc_year' AND business_ok=1"
            )
        }

    run_id = db.start_run(
        conn, "backfill_holdings", cfg.config_hash, cfg.code_version,
        notes=f"years={years[0]}-{years[-1]} funds={len(funds)}",
    )
    fetcher = Fetcher(cfg, conn, run_id)
    started = time.monotonic()
    counters = {"ok": 0, "fail": 0, "skip": 0, "rows": 0, "periods": 0, "empty": 0}
    failures: dict[str, int] = {}
    total = 0
    for f in funds:
        fy = int((f["estab_date"] or "2016")[:4])
        total += sum(1 for y in years if y >= fy)
    print(f"run={run_id} funds={len(funds)} years={years[0]}-{years[-1]} target_calls={total}")

    try:
        for i, f in enumerate(funds, 1):
            code = f["fcode"]
            fy = int((f["estab_date"] or "2016")[:4])
            for year in years:
                if year < fy:
                    continue
                key = f"{code}:{year}"
                if key in done:
                    counters["skip"] += 1
                    continue
                try:
                    out = collect.collect_zqcc_year(fetcher, code, year, force=args.force)
                except Exception as exc:  # 单只失败不阻断全量；错误写进 failures
                    counters["fail"] += 1
                    failures[type(exc).__name__] = failures.get(type(exc).__name__, 0) + 1
                    continue
                if out.get("ok"):
                    counters["ok"] += 1
                    counters["rows"] += int(out.get("rows") or 0)
                    counters["periods"] += int(out.get("periods") or 0)
                    if not out.get("rows"):
                        counters["empty"] += 1
                else:
                    counters["fail"] += 1
                    status = out.get("status", "?")
                    failures[status] = failures.get(status, 0) + 1

            if i % 25 == 0 or i == len(funds):
                elapsed = max(time.monotonic() - started, 0.001)
                rate = (counters["ok"] + counters["fail"]) / elapsed
                remain = max(total - counters["skip"] - counters["ok"] - counters["fail"], 0)
                state = {
                    "run_id": run_id,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "funds_total": len(funds),
                    "funds_done": i,
                    "target_calls": total,
                    "counters": counters,
                    "failures": failures,
                    "calls_per_sec": round(rate, 3),
                    "eta_minutes": round(remain / rate / 60, 1) if rate else None,
                }
                write_state(state)
                print(json.dumps(state, ensure_ascii=False), flush=True)

        db.finish_run(conn, run_id, "success")
        print(json.dumps({"done": True, **counters, "failures": failures}, ensure_ascii=False))
        return 0 if counters["fail"] == 0 else 1
    except BaseException as exc:
        db.finish_run(conn, run_id, "failed", notes=f"{type(exc).__name__}: {exc}"[:500])
        raise


if __name__ == "__main__":
    raise SystemExit(main())
