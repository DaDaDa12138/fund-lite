#!/usr/bin/env python3
"""把证监会 EID 已确认清盘的债券基金补进历史宇宙（公开 API，可断点续跑）。

目的：walkforward 目前只从当前存续基金构造宇宙，历史 IC/DSR 有幸存者偏差。
本脚本只做“数据补齐”，不改变生产筛选：
- 用 em_jbgk 判断基金类型（只收债券型/指数型-固收）；
- 用 em_pzd 拉成立以来净值（东财对部分清盘基金仍有完整历史）；
- 用 em_gmbd / em_jjjl / em_jjfl 补规模、经理任职、费率；
- 插入 fund（last_seen_date=清盘日）、fund_profile、fund_share_class、fund_style；
- 不生成任何模拟净值/规模/费率，失败就跳过并记录。

清盘基金在 as_of 早于 effective_date 时仍会被 screen._load_candidates 纳入，
as_of 晚于清盘日则被 lifecycle_clause 排除——这是回测需要的 point-in-time 语义。

用法：
  python3 scripts/backfill_liquidated.py --limit 5
  python3 scripts/backfill_liquidated.py
  python3 scripts/backfill_liquidated.py --force
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fundana import collect, db, fundtypes  # noqa: E402
from fundana.config import load_config  # noqa: E402
from fundana.fetch import parsers  # noqa: E402
from fundana.fetch.http import Fetcher  # noqa: E402
from fundana.fuse import style as style_mod  # noqa: E402

STATE = ROOT / "var" / "state" / "liquidated_backfill.json"


def _num_yi(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*亿", text)
    return float(m.group(1)) if m else None


def _write_state(payload: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)


def _upsert_fund(conn, code: str, parsed: dict, effective: str, today: str) -> None:
    name = parsed.get("full_name") or code
    # 成立日必须是公开档案里的字段；缺失就留空，不能拿清盘日代替（那会编造 PIT 信息）。
    estab = parsed.get("estab_date")
    conn.execute(
        "INSERT INTO fund(fcode, name, type_text, type_group, estab_date, company_name,"
        " fund_status, identity_status, first_seen_date, last_seen_date, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode) DO UPDATE SET name=excluded.name, type_text=excluded.type_text,"
        " type_group=excluded.type_group, estab_date=excluded.estab_date,"
        " company_name=excluded.company_name, fund_status=excluded.fund_status,"
        " identity_status=excluded.identity_status, last_seen_date=excluded.last_seen_date,"
        " updated_at=excluded.updated_at",
        (code, name, parsed.get("type_text"), "bond", estab, parsed.get("company_name"),
         "liquidated", "ok", estab, effective, today))
    mgr = parsed.get("mgr_fee_pct")
    trust = parsed.get("trust_fee_pct")
    sales = parsed.get("sales_fee_pct")
    recurring = (round((mgr or 0.0) + (trust or 0.0) + (sales or 0.0), 4)
                 if any(v is not None for v in (mgr, trust, sales)) else None)
    conn.execute(
        "INSERT INTO fund_profile(fcode, full_name, asset_scale_billion, asset_scale_src,"
        " asset_scale_obs, share_scale_billion, share_scale_src, mgr_fee_pct, trust_fee_pct,"
        " sales_fee_pct, recurring_fee_pct, max_sub_fee_pct, max_red_fee_pct, benchmark_text,"
        " tracking_text, field_src_json, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode) DO UPDATE SET full_name=excluded.full_name,"
        " asset_scale_billion=excluded.asset_scale_billion, asset_scale_src=excluded.asset_scale_src,"
        " asset_scale_obs=excluded.asset_scale_obs, share_scale_billion=excluded.share_scale_billion,"
        " share_scale_src=excluded.share_scale_src, mgr_fee_pct=excluded.mgr_fee_pct,"
        " trust_fee_pct=excluded.trust_fee_pct, sales_fee_pct=excluded.sales_fee_pct,"
        " recurring_fee_pct=excluded.recurring_fee_pct, max_sub_fee_pct=excluded.max_sub_fee_pct,"
        " max_red_fee_pct=excluded.max_red_fee_pct, benchmark_text=excluded.benchmark_text,"
        " tracking_text=excluded.tracking_text, field_src_json=excluded.field_src_json,"
        " updated_at=excluded.updated_at",
        (code, name, parsed.get("asset_scale"), "em_jbgk", today,
         _num_yi(parsed.get("share_scale_text")), "em_jbgk", mgr, trust, sales, recurring,
         parsed.get("max_sub_fee_pct"), parsed.get("max_red_fee_pct"),
         parsed.get("benchmark_text"), parsed.get("tracking_text"),
         json.dumps({"source": "em_jbgk", "backfill": "liquidated"}, ensure_ascii=False), today))
    conn.execute(
        "INSERT OR IGNORE INTO fund_share_class(fcode, family_id, class_letter, is_primary)"
        " VALUES(?,?,?,?)", (code, code, code[-1] if code else None, 1))
    st = style_mod.classify(name, parsed.get("type_text"), [])
    conn.execute(
        "INSERT OR IGNORE INTO fund_style(fcode, style_code, confidence, evidence_json,"
        " classifier_version, updated_at) VALUES(?,?,?,?,?,?)",
        (code, st["style_code"], st["confidence"],
         json.dumps(st["evidence"], ensure_ascii=False), "liquidated-type-text-backfill", today))
    # 定开/封闭只能从公开基金名称识别，不能凭空编造持有天数；名称已足以触发 L3
    # 的 lockup_type 拦截，避免把“五年定开”清盘基金误当随时可申赎样本。
    lockup = None
    if "定期开放" in name:
        lockup = "periodic_open"
    elif "封闭" in name:
        lockup = "closed"
    if lockup:
        conn.execute(
            "INSERT INTO fund_holding_policy(fcode, structured_days, name_inferred_days,"
            " effective_days, lockup_type, evidence_json, updated_at)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(fcode) DO UPDATE SET lockup_type=excluded.lockup_type,"
            " evidence_json=excluded.evidence_json, updated_at=excluded.updated_at",
            (code, None, None, None, lockup,
             json.dumps({"source": "em_jbgk_name", "pattern": lockup}, ensure_ascii=False), today))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    rows = conn.execute(
        "SELECT l.fcode, l.effective_date FROM fund_lifecycle l"
        " LEFT JOIN fund f ON f.fcode=l.fcode"
        " WHERE l.status='liquidated' AND f.fcode IS NULL"
        " AND l.fcode GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'"
        " ORDER BY l.fcode").fetchall()
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("no missing liquidated funds")
        return 0

    run_id = db.start_run(conn, "backfill_liquidated", cfg.config_hash, cfg.code_version,
                          notes=f"funds={len(rows)}")
    fetcher = Fetcher(cfg, conn, run_id)
    started = time.monotonic()
    counters = {"ok": 0, "skip": 0, "fail": 0, "non_bond": 0, "nav_rows": 0}
    failures: dict[str, int] = {}
    today = db.today_cn()
    print(f"run={run_id} funds={len(rows)}")

    try:
        for i, r in enumerate(rows, 1):
            code = r["fcode"]
            effective = r["effective_date"] or today
            try:
                jb = collect.collect_jbgk_one(fetcher, code, today)
                if not jb.get("ok"):
                    counters["fail"] += 1
                    failures[jb.get("status", "jbgk_failed")] = failures.get(jb.get("status", "jbgk_failed"), 0) + 1
                    continue
                res = fetcher.get("em_jbgk", key=code)
                parsed = parsers.parse_jbgk(res.text)
                if fundtypes.type_group(parsed.get("type_text")) != "bond":
                    counters["non_bond"] += 1
                    continue
                nav = collect.collect_pzd_one(fetcher, code, today, force=args.force)
                if not nav.get("ok") or not nav.get("nav_rows"):
                    counters["fail"] += 1
                    failures[nav.get("status", "nav_empty")] = failures.get(nav.get("status", "nav_empty"), 0) + 1
                    continue
                _upsert_fund(conn, code, parsed, effective, today)
                counters["nav_rows"] += int(nav.get("nav_rows") or 0)
                for fn, key in ((lambda: collect.collect_scale_one(fetcher, code, force=args.force), "scale"),
                                (lambda: collect.collect_manager_one(fetcher, code, effective, force=args.force), "manager"),
                                (lambda: collect.collect_fee_one(fetcher, code), "fee")):
                    out = fn()
                    if not out.get("ok"):
                        failures[f"{key}:{out.get('status', '?')}"] = failures.get(f"{key}:{out.get('status', '?')}", 0) + 1
                conn.commit()
                counters["ok"] += 1
            except Exception as exc:  # 单只失败不阻断全量
                conn.rollback()
                counters["fail"] += 1
                failures[type(exc).__name__] = failures.get(type(exc).__name__, 0) + 1
                continue

            if i % 10 == 0 or i == len(rows):
                elapsed = max(time.monotonic() - started, 0.001)
                rate = (counters["ok"] + counters["fail"] + counters["non_bond"]) / elapsed
                state = {
                    "run_id": run_id,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "funds_total": len(rows), "funds_done": i,
                    "counters": counters, "failures": failures,
                    "calls_per_sec": round(rate, 3),
                    "eta_minutes": round((len(rows) - i) / rate / 60, 1) if rate else None,
                }
                _write_state(state)
                print(json.dumps(state, ensure_ascii=False), flush=True)

        db.finish_run(conn, run_id, "success")
        print(json.dumps({"done": True, **counters, "failures": failures}, ensure_ascii=False))
        return 0 if counters["fail"] == 0 else 1
    except BaseException as exc:
        db.finish_run(conn, run_id, "failed", notes=f"{type(exc).__name__}: {exc}"[:500])
        raise


if __name__ == "__main__":
    raise SystemExit(main())
