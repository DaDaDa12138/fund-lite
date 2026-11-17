"""回填 fund_holding_bond 的 market_value_wan / raw_sha256。

背景：ADR-034 之前 fuse/style.py 的 INSERT 把 market_value_wan 写成 NULL、raw_sha256 写成 NULL，
导致网页持仓表“市值(万元)”整列为“—”。L1（src_zqcc_holding）一直有值，属于融合层漏传。
本脚本只做一次性回填；之后的 fuse 运行由 style.py 直接透传。

用法：python3 scripts/backfill_holding_market_value.py [--db var/db/fundana.db]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回填持仓市值与来源 sha")
    ap.add_argument("--db", default=str(ROOT / "var/db/fundana.db"))
    args = ap.parse_args(argv)
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout=15000")
    before = conn.execute(
        "SELECT COUNT(*) FROM fund_holding_bond WHERE market_value_wan IS NULL").fetchone()[0]
    cur = conn.execute(
        "UPDATE fund_holding_bond SET"
        " market_value_wan=(SELECT s.market_value_wan FROM src_zqcc_holding s"
        "   WHERE s.code=fund_holding_bond.fcode AND s.report_period=fund_holding_bond.report_period"
        "     AND s.bond_code=fund_holding_bond.bond_code),"
        " raw_sha256=(SELECT s.raw_sha256 FROM src_zqcc_holding s"
        "   WHERE s.code=fund_holding_bond.fcode AND s.report_period=fund_holding_bond.report_period"
        "     AND s.bond_code=fund_holding_bond.bond_code)"
        " WHERE market_value_wan IS NULL")
    conn.commit()
    after = conn.execute(
        "SELECT COUNT(*) FROM fund_holding_bond WHERE market_value_wan IS NULL").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM fund_holding_bond").fetchone()[0]
    print(f"fund_holding_bond: total={total} null_before={before} null_after={after}"
          f" updated={cur.rowcount}")
    conn.close()
    return 0 if after == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
