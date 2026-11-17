"""规模容量曲线（P2-11，研究）：按风格看业绩与规模的关系。

回答的问题：同一风格里，规模更大的组是否系统性更差？如果 OOS 没有支持，
就不能设硬性规模上限；本模块只给研究区间，不改变筛选。

口径：
- 规模用 fund_scale_history 的 as-of 值（季报口径，回看 45 天），并按 family 汇总；
- 业绩用当前公式契约的 365/1095 天指标（annual_dec、sharpe、mdd_dec）；
- 风格用 fund_style_history 的 as-of 快照，缺失回退当前标签；
- 每个风格×规模桶至少 MIN_BUCKET_COUNT 只才给中位数，否则返回 null，避免
  用 1–2 只基金编造“规模效应”。
"""
from __future__ import annotations

from datetime import date
from statistics import median
from typing import Any

from .fuse.scale import scale_asof_map
from .fuse.style import style_asof_map
from .labels import label_style
from .screen import thresholds as T

MIN_BUCKET_COUNT = 5
BUCKETS: tuple[tuple[str, float, float | None], ...] = (
    ("<2 亿", 0.0, 2.0),
    ("2–5 亿", 2.0, 5.0),
    ("5–10 亿", 5.0, 10.0),
    ("10–20 亿", 10.0, 20.0),
    ("20–50 亿", 20.0, 50.0),
    ("≥50 亿", 50.0, None),
)


def _bucket(scale: float) -> str:
    for label, lo, hi in BUCKETS:
        if scale >= lo and (hi is None or scale < hi):
            return label
    return BUCKETS[-1][0]


def _median(values: list[float]) -> float | None:
    vals = [float(v) for v in values if v is not None]
    if len(vals) < MIN_BUCKET_COUNT:
        return None
    return round(median(vals), 6)


def _metrics(conn, contract: str) -> dict[str, dict[int, dict[str, Any]]]:
    out: dict[str, dict[int, dict[str, Any]]] = {}
    for r in conn.execute(
            "SELECT fcode, period_days, annual_dec, sharpe, mdd_dec"
            " FROM fund_metric WHERE formula_contract=? AND period_days IN (365,1095)",
            (contract,)):
        out.setdefault(r["fcode"], {})[int(r["period_days"])] = dict(r)
    return out


def summary(conn, cfg, as_of: str | None = None,
            contract: str | None = None) -> dict[str, Any]:
    """按风格×规模桶返回 1/3 年业绩中位数；只读，不写库。"""
    contract = contract or cfg.get("risk", "formula_contract",
                                  default="fundana-risk-2026.11.5")
    if as_of is None:
        row = conn.execute(
            "SELECT MAX(end_date) AS d FROM fund_metric"
            " WHERE formula_contract=? AND period_days=365", (contract,)).fetchone()
        as_of = row["d"] if row and row["d"] else date.today().isoformat()
    styles, hist_hits = style_asof_map(conn, as_of)
    scales = scale_asof_map(conn, as_of)
    metrics = _metrics(conn, contract)
    rows: list[dict[str, Any]] = []
    for fcode, style in styles.items():
        if style not in T.STYLE_ORDER:
            continue
        sc = scales.get(fcode) or {}
        scale = sc.get("asset_scale_billion")
        if scale is None:
            continue
        m = metrics.get(fcode) or {}
        m1 = m.get(365) or {}
        m3 = m.get(1095) or {}
        rows.append({
            "fcode": fcode, "style_code": style, "scale": float(scale),
            "bucket": _bucket(float(scale)),
            "annual_1y": m1.get("annual_dec"), "annual_3y": m3.get("annual_dec"),
            "sharpe_1y": m1.get("sharpe"), "mdd_1y": m1.get("mdd_dec"),
        })
    styles_out = []
    for style in T.STYLE_ORDER:
        subset = [r for r in rows if r["style_code"] == style]
        if not subset:
            continue
        buckets = []
        for label, _lo, _hi in BUCKETS:
            cell = [r for r in subset if r["bucket"] == label]
            if not cell:
                continue
            buckets.append({
                "bucket": label, "n": len(cell),
                "annual_1y_pct": _median([r["annual_1y"] * 100 for r in cell
                                          if r["annual_1y"] is not None]),
                "annual_3y_pct": _median([r["annual_3y"] * 100 for r in cell
                                          if r["annual_3y"] is not None]),
                "sharpe_1y": _median([r["sharpe_1y"] for r in cell
                                      if r["sharpe_1y"] is not None]),
                "mdd_1y_pct": _median([r["mdd_1y"] * 100 for r in cell
                                       if r["mdd_1y"] is not None]),
            })
        styles_out.append({
            "style_code": style, "label": label_style(style),
            "funds": len(subset), "buckets": buckets,
        })
    overall = []
    for label, _lo, _hi in BUCKETS:
        cell = [r for r in rows if r["bucket"] == label]
        if not cell:
            continue
        overall.append({
            "bucket": label, "n": len(cell),
            "annual_1y_pct": _median([r["annual_1y"] * 100 for r in cell
                                      if r["annual_1y"] is not None]),
            "annual_3y_pct": _median([r["annual_3y"] * 100 for r in cell
                                      if r["annual_3y"] is not None]),
            "sharpe_1y": _median([r["sharpe_1y"] for r in cell
                                  if r["sharpe_1y"] is not None]),
            "mdd_1y_pct": _median([r["mdd_1y"] * 100 for r in cell
                                   if r["mdd_1y"] is not None]),
        })
    return {
        "as_of": as_of, "formula_contract": contract,
        "funds": len(rows), "styles": styles_out, "overall": overall,
        "style_history_hits": hist_hits, "min_bucket_count": MIN_BUCKET_COUNT,
        "note": ("这是研究区间，不是规模上限；只有 OOS 与前瞻同时支持时才可设硬上限。"
                 "规模按 family 汇总，业绩按份额；同一基金多份额可能重复计入风格桶，"
                 "但每只份额只出现一次。桶内少于 %d 只时中位数为 null。" % MIN_BUCKET_COUNT),
    }
