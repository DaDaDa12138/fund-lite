"""增强债准入的窗口敏感性：同一套阈值在 1/3/5 年窗口下分别能过多少只。

用途：判断 bond_enhanced 为空是"窗口太短"还是"市场事实"，不改任何正式规则。
输出：reports/analysis/enhanced_sensitivity.csv + 终端表格。
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fundana.config import load_config  # noqa: E402

# 与 docs/screening-rules.md「增强债补充准入」一致
RULES = {
    "bond_primary_enhanced": dict(annual=4.5, sharpe=1.1, calmar=2.6, mdd=0.08, positive=55.0, fee=1.10),
    "bond_secondary_enhanced": dict(annual=5.5, sharpe=1.1, calmar=2.6, mdd=0.08, positive=55.0, fee=1.25),
    "bond_convertible": dict(annual=4.0, sharpe=1.1, calmar=3.2, mdd=0.03, positive=54.0, fee=0.70),
}
WINDOWS = (365, 730, 1095, 1825)


def main() -> int:
    cfg = load_config()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    out = []
    for style, rule in RULES.items():
        for days in WINDOWS:
            rows = conn.execute(
                "SELECT m.annual_dec, m.sharpe, m.calmar_dec, m.mdd_dec, m.positive_ratio_pct,"
                " p.recurring_fee_pct, p.asset_scale_billion"
                " FROM fund_metric m JOIN fund_style s ON s.fcode=m.fcode"
                " JOIN fund_profile p ON p.fcode=m.fcode"
                " WHERE s.style_code=? AND m.period_days=?", (style, days)).fetchall()
            n = len(rows)
            if not n:
                out.append(dict(style=style, window_days=days, n=0, pass_all=0,
                                annual=0, sharpe=0, calmar=0, mdd=0, positive=0, fee=0, scale=0))
                continue
            ok = dict(annual=0, sharpe=0, calmar=0, mdd=0, positive=0, fee=0, scale=0)
            passed = 0
            for r in rows:
                checks = {
                    "annual": r["annual_dec"] is not None and r["annual_dec"] * 100 >= rule["annual"],
                    "sharpe": r["sharpe"] is not None and r["sharpe"] >= rule["sharpe"],
                    "calmar": r["calmar_dec"] is not None and r["calmar_dec"] >= rule["calmar"],
                    "mdd": r["mdd_dec"] is not None and r["mdd_dec"] <= rule["mdd"],
                    "positive": r["positive_ratio_pct"] is not None and r["positive_ratio_pct"] >= rule["positive"],
                    "fee": r["recurring_fee_pct"] is not None and r["recurring_fee_pct"] <= rule["fee"],
                    "scale": r["asset_scale_billion"] is not None and r["asset_scale_billion"] >= 2.0,
                }
                for k, v in checks.items():
                    ok[k] += int(v)
                passed += int(all(checks.values()))
            out.append(dict(style=style, window_days=days, n=n, pass_all=passed, **ok))
    target = Path(cfg.report_root) / "analysis"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "enhanced_sensitivity.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(out[0]))
        writer.writeheader()
        writer.writerows(out)
    print(f"{'style':26}{'win':>6}{'n':>6}{'pass':>6}{'annual':>8}{'sharpe':>8}{'calmar':>8}"
          f"{'mdd':>6}{'posit':>7}{'fee':>6}{'scale':>7}")
    for r in out:
        print(f"{r['style']:26}{r['window_days']:>6}{r['n']:>6}{r['pass_all']:>6}{r['annual']:>8}"
              f"{r['sharpe']:>8}{r['calmar']:>8}{r['mdd']:>6}{r['positive']:>7}{r['fee']:>6}{r['scale']:>7}")
    print(f"\n写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
