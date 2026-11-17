"""增强债影子榜：用同一套准入阈值换到更长窗口复算，只做研究，不进正式发布。

为什么需要它：正式增强榜用 1 年窗口，2026 年债市 1 年收益中枢只有 2–4%，
含权益仓位的二级/转债基金在"年化 5.5% + 日胜率 55%"上几乎不可达（ADR-017）。
影子榜不改阈值、不改正式榜，只回答"若用 3 年窗口，这套准入会留下谁"。
产物：reports/analysis/enhanced_shadow_<window>d.csv + .json（research_only）。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .. import db
from .. import fundtypes
from . import engine, thresholds as T

WINDOW_CHOICES = (365, 730, 1095, 1825)
CSV_FIELDS = ["fcode", "name", "style_code", "checks_passed", "checks_total", "failed",
              "annual_pct", "sharpe", "calmar", "mdd_pct", "positive_pct", "fee_pct",
              "scale_billion", "manager_career_min_days"]


def run_shadow(conn, cfg, formula_contract: str, window_days: int = 1095,
               limit: int = 60) -> dict[str, Any]:
    if window_days not in WINDOW_CHOICES:
        raise ValueError("window_days 必须是 %s 之一" % (WINDOW_CHOICES,))
    cands = engine._load_candidates(conn, db.today_cn(), formula_contract,
                                    fundtypes.resolve(cfg=cfg))
    rows: list[dict[str, Any]] = []
    for c in cands:
        style = c["style_code"]
        if style not in T.ENHANCED_STYLES:
            continue
        checks = engine.enhanced_checks(c, style, window_days)
        passed = sum(1 for v in checks.values() if v["pass"])
        failed = [k for k, v in checks.items() if not v["pass"]]
        rows.append({
            "fcode": c["fcode"], "name": c["name"], "style_code": style,
            "checks_passed": passed, "checks_total": len(checks),
            "failed": "|".join(failed),
            "annual_pct": _r(checks["annual_1y"]["value"]),
            "sharpe": _r(checks["sharpe"]["value"]),
            "calmar": _r(checks["calmar"]["value"]),
            "mdd_pct": _r(None if checks["mdd"]["value"] is None else checks["mdd"]["value"] * 100),
            "positive_pct": _r(checks["positive"]["value"]),
            "fee_pct": _r(checks["fee"]["value"]),
            "scale_billion": _r(checks["scale"]["value"]),
            "manager_career_min_days": c.get("manager_career_min_days"),
        })
    rows.sort(key=lambda r: (-r["checks_passed"], -(r["annual_pct"] or -999), r["fcode"]))
    out_dir = Path(cfg.report_root) / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / ("enhanced_shadow_%dd.csv" % window_days)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    full = [r for r in rows if r["checks_passed"] == r["checks_total"]]
    by_style: dict[str, dict[str, int]] = {}
    for r in rows:
        b = by_style.setdefault(r["style_code"], {"entered": 0, "full_pass": 0, "one_short": 0})
        b["entered"] += 1
        if r["checks_passed"] == r["checks_total"]:
            b["full_pass"] += 1
        if r["checks_passed"] == r["checks_total"] - 1:
            b["one_short"] += 1
    meta = {
        "research_only": True, "window_days": window_days, "generated_at": db.now_iso(),
        "formula_contract": formula_contract, "entered": len(rows), "full_pass": len(full),
        "one_short": sum(1 for r in rows if r["checks_passed"] == r["checks_total"] - 1),
        "by_style": by_style, "csv": str(csv_path),
        "note": "同一套增强准入阈值换窗口复算；不写库、不进 screen_pick、不影响正式发布。",
    }
    (out_dir / ("enhanced_shadow_%dd.json" % window_days)).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"meta": meta, "rows": rows[:max(1, min(limit, 500))]}


def _r(v: float | None, nd: int = 6) -> float | None:
    return None if v is None else round(float(v), nd)
