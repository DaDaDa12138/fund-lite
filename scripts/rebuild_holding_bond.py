#!/usr/bin/env python3
"""把 src_zqcc_holding 的全部季度透传到派生表 fund_holding_bond。

日更 fuse 只写每只基金最新一期（风险门只需要最新持仓）；历史季度保留在源表，
用于审计/风格证据。本脚本用于一次性回填或重建派生表，不改变任何分类结果。

用法：
    python3 scripts/rebuild_holding_bond.py            # upsert，不删除已有行
    python3 scripts/rebuild_holding_bond.py --replace  # 先清空派生表再全量重建
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fundana import db
from fundana.config import load_config
from fundana.fuse.style import classify_bond_name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replace", action="store_true", help="先 DELETE 派生表再重建")
    ap.add_argument("--batch", type=int, default=5000)
    args = ap.parse_args()
    cfg = load_config()
    conn = db.connect(cfg)
    if args.replace:
        conn.execute("DELETE FROM fund_holding_bond")
        conn.commit()
    cur = conn.execute(
        "SELECT code, report_period, bond_code, bond_name, pct_nav, market_value_wan, raw_sha256"
        " FROM src_zqcc_holding ORDER BY code, report_period, bond_code")
    batch = []
    n = 0
    while True:
        rows = cur.fetchmany(args.batch)
        if not rows:
            break
        batch = [(r["code"], r["report_period"], r["bond_code"], r["bond_name"],
                  r["pct_nav"], r["market_value_wan"],
                  classify_bond_name(r["bond_name"], r["bond_code"]), r["raw_sha256"])
                 for r in rows]
        conn.executemany(
            "INSERT INTO fund_holding_bond(fcode, report_period, bond_code, bond_name, pct_nav,"
            " market_value_wan, bond_class, raw_sha256) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(fcode, report_period, bond_code) DO UPDATE SET"
            " bond_name=excluded.bond_name, pct_nav=excluded.pct_nav,"
            " market_value_wan=excluded.market_value_wan, bond_class=excluded.bond_class,"
            " raw_sha256=excluded.raw_sha256", batch)
        conn.commit()
        n += len(batch)
        if n % 50000 == 0:
            print(f"written {n:,}", flush=True)
    funds = conn.execute("SELECT COUNT(DISTINCT fcode) FROM fund_holding_bond").fetchone()[0]
    periods = conn.execute("SELECT COUNT(DISTINCT report_period) FROM fund_holding_bond").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM fund_holding_bond").fetchone()[0]
    print(f"done rows={total:,} funds={funds:,} periods={periods}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
