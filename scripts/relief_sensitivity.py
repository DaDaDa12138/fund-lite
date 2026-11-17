#!/usr/bin/env python3
"""单一原因缓释的敏感性分析（ADR-052）。

目的：证明"当前缓释规则在本轮筛选里一次都没放行"不是程序缺陷，而是规则与分数分布不匹配：
缓释要求综合分 >= 80，而综合分 >= 80 的基金几乎不会只差一项硬比较（它们本来就全过）。
本脚本用已落库的 relief_json 做离线反事实，不重跑筛选、不改阈值。

用法：python3 scripts/relief_sensitivity.py
输出：reports/analysis/relief_sensitivity.csv + 终端摘要
"""
from __future__ import annotations

import csv
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fundana import db  # noqa: E402
from fundana.config import load_config  # noqa: E402

COMPOSITE_GRID = (80.0, 78.0, 75.0, 72.0)
GAP_GRID = (0.15, 0.10, 0.05)


def main() -> int:
    cfg = load_config(ROOT)
    conn = db.connect(cfg)
    run = conn.execute("SELECT run_id, business_date, formula_contract FROM screen_run"
                       " ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    if not run:
        print("没有 screen_run，先跑 screen")
        return 1
    rows = []
    for r in conn.execute(
            "SELECT d.fcode, d.relief_json, p.formal_score, p.board"
            " FROM screen_decision d LEFT JOIN screen_pick p"
            "  ON p.run_id = d.run_id AND p.fcode = d.fcode"
            " WHERE d.run_id = ? AND d.relief_json IS NOT NULL", (run["run_id"],)):
        det = (json.loads(r["relief_json"]) or {}).get("detail") or {}
        if not det.get("type"):
            continue
        rows.append({"fcode": r["fcode"], "type": det["type"],
                     "value": det.get("value"), "threshold": det.get("threshold"),
                     "gap": det.get("gap"), "composite": det.get("composite"),
                     "formal_score": r["formal_score"], "board": r["board"]})
    out_dir = ROOT / "reports" / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_rows = []
    for comp_min in COMPOSITE_GRID:
        for gap_cap in GAP_GRID:
            for t in ("annual_1y", "positive", "scale"):
                hits = [x for x in rows
                        if x["type"] == t and (x["composite"] or 0) >= comp_min
                        and (x["gap"] is None or x["gap"] <= gap_cap)]
                out_rows.append({"run_id": run["run_id"], "business_date": run["business_date"],
                                 "type": t, "composite_min": comp_min, "gap_cap": gap_cap,
                                 "allowed": len(hits),
                                 "codes": ",".join(x["fcode"] for x in hits[:8])})
    path = out_dir / "relief_sensitivity.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print("run", run["run_id"], "contract", run["formula_contract"])
    print("单原因候选", len(rows), "按类型:",
          {t: sum(1 for x in rows if x["type"] == t) for t in ("annual_1y", "positive", "scale")})
    print("综合分分布: max %.2f / median %.2f" % (
        max((x["composite"] or 0) for x in rows),
        sorted(x["composite"] or 0 for x in rows)[len(rows) // 2] if rows else 0))
    for comp_min in COMPOSITE_GRID:
        line = []
        for gap_cap in GAP_GRID:
            n = sum(1 for x in out_rows if x["composite_min"] == comp_min
                    and x["gap_cap"] == gap_cap for _ in range(x["allowed"]))
            line.append("gap<=%.2f:%d" % (gap_cap, n))
        print("  综合>=%.0f ->" % comp_min, " ".join(line))
    print("CSV:", path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
