#!/usr/bin/env python3
"""幸存者偏差对照：同一套 walkforward 代码，分别排除/纳入已确认清盘基金。

用途：量化“只从当前存续基金构造历史宇宙”对 IC/命中率的抬升幅度。
只读 walkforward，不写生产榜；清盘样本必须来自 fund_lifecycle 的 EID 公告，
不能用目录消失或净值停更推断。

用法：
  python3 scripts/audit_survivorship.py --years 2020,2021,2022,2023,2024,2025
  python3 scripts/audit_survivorship.py --out reports/analysis/audit_2026_11
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fundana import db  # noqa: E402
from fundana.audit import walkforward as wf  # noqa: E402
from fundana.config import load_config  # noqa: E402

KEYS = ("rank_score", "composite", "formal_score", "sharpe", "return_6m", "return_1y")


def _compare(base: dict, live: dict) -> dict:
    out = {}
    for key in KEYS:
        b = (base.get("summary") or {}).get(key) or {}
        a = (live.get("summary") or {}).get(key) or {}
        out[key] = {
            "ic_annual_ex_survivors": b.get("ic_annual"),
            "ic_annual_with_liquidated": a.get("ic_annual"),
            "ic_annual_delta": (None if b.get("ic_annual") is None or a.get("ic_annual") is None
                                else round(a["ic_annual"] - b["ic_annual"], 4)),
            "ic_sharpe_ex_survivors": b.get("ic_sharpe"),
            "ic_sharpe_with_liquidated": a.get("ic_sharpe"),
            "ic_sharpe_delta": (None if b.get("ic_sharpe") is None or a.get("ic_sharpe") is None
                                else round(a["ic_sharpe"] - b["ic_sharpe"], 4)),
            "hit_rate_ex_survivors": b.get("hit_rate"),
            "hit_rate_with_liquidated": a.get("hit_rate"),
        }
    return out


def _render(result: dict) -> str:
    lines = ["# 幸存者偏差对照（walkforward）", ""]
    lines.append(f"- 生成时间：{result['generated_at']}")
    lines.append(f"- 队列：{', '.join(str(y) for y in result['years'])}；top={result['top']}；"
                 f"horizon={result['horizon_days']}d")
    lines.append(f"- 清盘样本：{result['liquidated_universe_note']}")
    lines.append("")
    lines.append("| 队列 | 宇宙(排除) | 宇宙(纳入) | 过基础门(排除) | 过基础门(纳入) | 过固定线(排除) | 过固定线(纳入) |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for c in result.get("cohorts", []):
        lines.append("| {d} | {a} | {b} | {c} | {e} | {f} | {g} |".format(
            d=c.get("as_of"),
            a=c.get("universe_ex_survivors"), b=c.get("universe_with_liquidated"),
            c=c.get("gate_pass_ex_survivors"), e=c.get("gate_pass_with_liquidated"),
            f=c.get("fixed_pass_ex_survivors"), g=c.get("fixed_pass_with_liquidated")))
    lines.append("")
    lines.append("| 排序键 | IC(年化) 排除清盘 | IC(年化) 纳入清盘 | ΔIC | IC(夏普) 排除 | IC(夏普) 纳入 | ΔIC |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for key in KEYS:
        c = result["comparison"].get(key) or {}
        lines.append("| {k} | {a} | {b} | {d} | {e} | {f} | {g} |".format(
            k=key,
            a=_fmt(c.get("ic_annual_ex_survivors"), 4),
            b=_fmt(c.get("ic_annual_with_liquidated"), 4),
            d=_fmt(c.get("ic_annual_delta"), 4),
            e=_fmt(c.get("ic_sharpe_ex_survivors"), 4),
            f=_fmt(c.get("ic_sharpe_with_liquidated"), 4),
            g=_fmt(c.get("ic_sharpe_delta"), 4)))
    lines.append("")
    lines.append("> 读法：排除清盘=旧口径（只含当前存续基金），纳入清盘=校正后口径。"
                 "ΔIC 为负说明旧口径高估了排序能力。清盘样本仍不完整（仅 EID 已确认、"
                 "且公开 API 有净值的部分），不能宣称完全无偏。")
    return "\n".join(lines) + "\n"


def _fmt(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2020,2021,2022,2023,2024,2025")
    ap.add_argument("--top", type=int, default=118)
    ap.add_argument("--horizon", type=int, default=365)
    ap.add_argument("--out", default="reports/analysis/audit_2026_11")
    args = ap.parse_args()

    years = [int(y) for y in args.years.split(",") if y.strip()]
    cfg = load_config()
    conn = db.connect(cfg)
    base = wf.run(conn, cfg, years, top=args.top, horizon_days=args.horizon,
                  include_liquidated=False)
    live = wf.run(conn, cfg, years, top=args.top, horizon_days=args.horizon,
                  include_liquidated=True)
    n_liq = conn.execute(
        "SELECT COUNT(DISTINCT fcode) FROM fund_lifecycle"
        " WHERE status IN ('liquidated','merged')").fetchone()[0]
    in_fund = conn.execute(
        "SELECT COUNT(*) FROM fund WHERE fund_status IN ('liquidated','merged')").fetchone()[0]
    cohorts = []
    for b, a in zip(base.get("cohorts", []), live.get("cohorts", [])):
        cohorts.append({
            "as_of": b.get("as_of"),
            "universe_ex_survivors": b.get("universe"),
            "universe_with_liquidated": a.get("universe"),
            "gate_pass_ex_survivors": b.get("gate_pass"),
            "gate_pass_with_liquidated": a.get("gate_pass"),
            "fixed_pass_ex_survivors": b.get("fixed_pass"),
            "fixed_pass_with_liquidated": a.get("fixed_pass"),
        })
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "years": years, "top": args.top, "horizon_days": args.horizon,
        "liquidated_universe_note": (f"fund_lifecycle 去重 {n_liq} 只，"
                                     f"其中已补入 fund 表 {in_fund} 只；"
                                     "只纳入公开 API 有净值/档案的样本"),
        "cohorts": cohorts,
        "baseline_ex_survivors": base,
        "with_liquidated": live,
        "comparison": _compare(base, live),
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    jp = out / f"walkforward-survivorship-{stamp}.json"
    mp = out / f"walkforward-survivorship-{stamp}.md"
    jp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    mp.write_text(_render(result), encoding="utf-8")
    print(_render(result))
    print(f"明细：{jp}")
    print(f"摘要：{mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
