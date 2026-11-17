"""P2-1：把因子暴露映射成久期/信用桶，用于报告与组合诊断。

边界：
* 只用 `fund_factor_exposure` 的 raw beta（对指数收益回归的原始敏感度），
  不用 standardized beta；后者是组内标准化值，不能跨风格比较。
* 覆盖不足（n_obs/252 < 0.60）或解释力不足（R² < 0.05）一律标 unknown，
  不猜桶、不用风格标签替代。
* 桶只用于报告分层与诊断，不改变正式榜准入；等 12 队列证据足够后再讨论
  是否进入分位分组（审计 §13.2 P2-1）。
"""
from __future__ import annotations

from typing import Any

DURATION_EDGES = (0.25, 0.75)
CREDIT_EDGES = (0.10, 0.60)
MIN_COVERAGE = 0.60
MIN_R2 = 0.05
TRADING_DAYS = 252


def _bucket(value: float | None, edges: tuple[float, float]) -> str:
    if value is None:
        return "unknown"
    lo, hi = edges
    if value < lo:
        return "low"
    if value < hi:
        return "mid"
    return "high"


def classify(beta_duration: float | None, beta_credit: float | None,
             r2: float | None, n_obs: int | None) -> dict[str, Any]:
    coverage = (float(n_obs) / TRADING_DAYS) if n_obs else 0.0
    reliable = (coverage >= MIN_COVERAGE and r2 is not None and float(r2) >= MIN_R2)
    if not reliable:
        return {"duration": "unknown", "credit": "unknown", "coverage": round(coverage, 4),
                "r2": r2, "beta_duration": beta_duration, "beta_credit": beta_credit,
                "reliable": False}
    dur = _bucket(beta_duration, DURATION_EDGES)
    # 对外统一用 short/mid/long；low/high 只用于信用桶，避免报告层再映射一次。
    dur = {"low": "short", "mid": "mid", "high": "long"}.get(dur, "unknown")
    return {"duration": dur,
            "credit": _bucket(beta_credit, CREDIT_EDGES),
            "coverage": round(coverage, 4), "r2": r2,
            "beta_duration": beta_duration, "beta_credit": beta_credit, "reliable": True}


def load_map(conn, as_of: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "WITH latest AS (SELECT fcode, MAX(as_of) AS as_of FROM fund_factor_exposure"
        "                WHERE as_of<=? GROUP BY fcode)"
        " SELECT f.fcode, f.beta_duration_raw, f.beta_credit_raw, f.r2, f.n_obs, f.as_of,"
        "        s.style_code"
        " FROM fund_factor_exposure f JOIN latest l"
        "   ON l.fcode=f.fcode AND l.as_of=f.as_of"
        " LEFT JOIN fund_style s ON s.fcode=f.fcode",
        (as_of,)).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        item = classify(r["beta_duration_raw"], r["beta_credit_raw"], r["r2"], r["n_obs"])
        item["style"] = r["style_code"]
        item["factor_as_of"] = r["as_of"]
        out[r["fcode"]] = item
    return out


def style_duration_matrix(rows: list[dict[str, Any]],
                          buckets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """返回风格 × 久期桶计数与久期 beta 分位；unknown 单独一列，不丢弃。"""
    styles = sorted({(r.get("style_code") or "unknown") for r in rows})
    matrix = {s: {"short": 0, "mid": 0, "long": 0, "unknown": 0} for s in styles}
    betas: dict[str, list[float]] = {s: [] for s in styles}
    for r in rows:
        style = r.get("style_code") or "unknown"
        b = buckets.get(r["fcode"]) or {}
        matrix.setdefault(style, {"short": 0, "mid": 0, "long": 0, "unknown": 0})
        matrix[style][b.get("duration", "unknown")] = (
            matrix[style].get(b.get("duration", "unknown"), 0) + 1)
        if b.get("reliable") and b.get("beta_duration") is not None:
            betas.setdefault(style, []).append(float(b["beta_duration"]))

    def q(values: list[float], p: float) -> float | None:
        if not values:
            return None
        values = sorted(values)
        idx = min(len(values) - 1, max(0, int(round((len(values) - 1) * p))))
        return round(values[idx], 4)

    dispersion = {s: {"n": len(v), "p10": q(v, 0.10), "p50": q(v, 0.50),
                      "p90": q(v, 0.90),
                      "spread": (round(q(v, 0.90) - q(v, 0.10), 4)
                                 if q(v, 0.10) is not None else None)}
                  for s, v in betas.items()}
    return {"styles": styles, "matrix": matrix, "dispersion": dispersion,
            "n": len(rows), "unknown": sum(1 for r in rows
                                           if (buckets.get(r["fcode"]) or {}).get("duration") == "unknown")}
