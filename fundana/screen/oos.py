"""P2-15：季度 OOS 研究模型（research_only，不改变筛选）。

回答的问题：把 365/1095 天指标在季度截面上做风格内中性化后，能否样本外预测
下一季度收益？这是"规则池之上"的研究，不是生产榜单；只有 OOS IC 稳定、且
前瞻成熟后才允许替换规则兜底。

口径与限制：
- 特征只来自 fund_metric_snapshot 的 PIT 快照（365/1095 天），目标来自
  fund_nav_daily.acc_nav 的下一季度收益；
- 每个季度做截面标准化；按 style_code 做组内去均值（风格中性化的代理），
  不是真正的久期/信用/转债因子中性化；
- 时间序列切分：前 N-3 个季度训练，最后 3 个季度测试；不做随机切分；
- 纯 Python ridge（无 numpy），lambda 固定；不调参、不挑特征，避免 OOS 泄漏；
- 季度样本只有 11 对，DSR/IC t 统计都只能作参考，不能据此宣称有技能。
"""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterable

from .. import db
from ..audit.stat_validation import deflated_sharpe
from ..fuse.style import style_asof_map

FEATURES = (
    ("annual_1y", 365, "annual_dec"),
    ("vol_1y", 365, "vol_dec"),
    ("mdd_1y", 365, "mdd_dec"),
    ("sharpe_1y", 365, "sharpe"),
    ("calmar_1y", 365, "calmar_dec"),
    ("non_negative_1y", 365, "non_negative_ratio_pct"),
    ("annual_3y", 1095, "annual_dec"),
    ("mdd_3y", 1095, "mdd_dec"),
)
RIDGE_LAMBDA = 1.0
MIN_STYLE_N = 5
MIN_CROSS_SECTION = 30


def cache_path(cfg, contract: str) -> Path:
    return Path(cfg.root) / "var" / "state" / ("oos-%s.json" % contract)


def load_cached(cfg, contract: str) -> dict[str, Any] | None:
    path = cache_path(cfg, contract)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None

    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _ridge(x: list[list[float]], y: list[float], lam: float = RIDGE_LAMBDA) -> list[float]:
    """(X'X + λI)β = X'y 的高斯消元；不惩罚截距（第一列全 1）。"""
    if not x:
        return []
    p = len(x[0])
    a = [[0.0] * (p + 1) for _ in range(p)]
    for row, target in zip(x, y):
        for i in range(p):
            for j in range(p):
                a[i][j] += row[i] * row[j]
            a[i][p] += row[i] * target
    for i in range(1, p):
        a[i][i] += lam
    for col in range(p):
        pivot = max(range(col, p), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            continue
        a[col], a[pivot] = a[pivot], a[col]
        div = a[col][col]
        for j in range(col, p + 1):
            a[col][j] /= div
        for r in range(p):
            if r == col:
                continue
            factor = a[r][col]
            if factor == 0:
                continue
            for j in range(col, p + 1):
                a[r][j] -= factor * a[col][j]
    return [a[i][p] for i in range(p)]


def _standardize(values: list[float]) -> list[float]:
    if len(values) < 2:
        return [0.0] * len(values)
    mu = mean(values)
    sd = pstdev(values)
    if sd <= 1e-12:
        return [0.0] * len(values)
    return [(v - mu) / sd for v in values]


def _style_neutral(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
    """按 style 去均值；组内少于 MIN_STYLE_N 只时保留原值，避免小组噪声。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r.get("style") or "unknown", []).append(r)
    out: dict[str, float] = {}
    for group in groups.values():
        if len(group) < MIN_STYLE_N:
            for r in group:
                out[r["fcode"]] = float(r[key])
            continue
        mu = mean(float(r[key]) for r in group)
        for r in group:
            out[r["fcode"]] = float(r[key]) - mu
    return out


def _panel(conn, contract: str) -> tuple[list[str], dict[str, dict[str, dict[str, Any]]]]:
    dates = [r["as_of"] for r in conn.execute(
        "SELECT DISTINCT as_of FROM fund_metric_snapshot WHERE formula_contract=?"
        " AND period_days=365 ORDER BY as_of", (contract,))]
    data: dict[str, dict[str, dict[str, Any]]] = {}
    for r in conn.execute(
            "SELECT fcode, as_of, period_days, annual_dec, vol_dec, mdd_dec, sharpe,"
            " sortino, calmar_dec, positive_ratio_pct, non_negative_ratio_pct,"
            " flat_ratio_pct, coverage_ratio"
            " FROM fund_metric_snapshot WHERE formula_contract=?"
            " AND period_days IN (365,1095)", (contract,)):
        data.setdefault(r["as_of"], {}).setdefault(r["fcode"], {})[int(r["period_days"])] = dict(r)
    return dates, data


def _forward_returns(conn, as_of: str, next_as_of: str) -> dict[str, float]:
    """as_of 前 15 天内最后一个 acc_nav → next_as_of 前 15 天内最后一个 acc_nav。"""
    start_limit = (date.fromisoformat(as_of).toordinal() - 15)
    end_limit = (date.fromisoformat(next_as_of).toordinal() - 15)
    rows = conn.execute(
        "SELECT fcode, nav_date, acc_nav FROM fund_nav_daily"
        " WHERE nav_date BETWEEN ? AND ? AND acc_nav IS NOT NULL"
        " ORDER BY fcode, nav_date",
        ((date.fromordinal(start_limit)).isoformat(), next_as_of)).fetchall()
    out: dict[str, float] = {}
    current = None
    first = last = None
    for r in rows:
        code = r["fcode"]
        if code != current:
            if current and first and last and last[0] > first[0]:
                out[current] = last[1] / first[1] - 1.0
            current, first, last = code, None, None
        d = r["nav_date"]
        if d <= as_of and date.fromisoformat(d).toordinal() >= start_limit:
            first = (d, float(r["acc_nav"]))
        if d <= next_as_of and date.fromisoformat(d).toordinal() >= end_limit:
            last = (d, float(r["acc_nav"]))
    if current and first and last and last[0] > first[0]:
        out[current] = last[1] / first[1] - 1.0
    return out


def _rows_for_pair(conn, snapshot: dict[str, dict[str, Any]], as_of: str,
                   next_as_of: str) -> list[dict[str, Any]]:
    fwd = _forward_returns(conn, as_of, next_as_of)
    styles, _hits = style_asof_map(conn, as_of)
    rows = []
    for code, periods in snapshot.items():
        m1 = periods.get(365)
        m3 = periods.get(1095)
        if not m1 or not m3 or code not in fwd:
            continue
        row = {"fcode": code, "target": fwd[code], "style": styles.get(code)}
        ok = True
        for name, period, field in FEATURES:
            src = periods.get(period) or {}
            val = src.get(field)
            if val is None:
                ok = False
                break
            row[name] = float(val)
        if ok:
            rows.append(row)
    return rows


def run(conn, cfg, contract: str | None = None, test_quarters: int = 3,
        cache: bool = True) -> dict[str, Any]:
    """跑一次季度 OOS；只读数据库，结果可缓存到 var/state 供报告引用。"""
    contract = contract or cfg.get("risk", "formula_contract",
                                   default="fundana-risk-2026.11.5")
    dates, data = _panel(conn, contract)
    pairs = [(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]
    if len(pairs) < 4:
        return {"status": "insufficient_history", "contract": contract,
                "snapshots": len(dates), "pairs": len(pairs),
                "note": "季度快照不足 4 对，无法做时间序列 OOS。"}
    test_quarters = max(1, min(int(test_quarters), len(pairs) - 2))
    samples = []
    for as_of, next_as_of in pairs:
        rows = _rows_for_pair(conn, data.get(as_of) or {}, as_of, next_as_of)
        if len(rows) < MIN_CROSS_SECTION:
            continue
        # 风格内去均值后再截面标准化；目标也做风格内去均值。
        for key in ("target",) + tuple(f[0] for f in FEATURES):
            neutral = _style_neutral(rows, key)
            for r in rows:
                r[key] = neutral[r["fcode"]]
        for name, _period, _field in FEATURES:
            z = _standardize([r[name] for r in rows])
            for r, v in zip(rows, z):
                r[name] = v
        samples.append({"as_of": as_of, "next_as_of": next_as_of, "rows": rows})
    if len(samples) < 4:
        return {"status": "insufficient_cross_section", "contract": contract,
                "snapshots": len(dates), "pairs": len(pairs),
                "usable_pairs": len(samples),
                "note": "可用季度截面不足 4 个（每期至少 %d 只）。" % MIN_CROSS_SECTION}
    split = len(samples) - min(test_quarters, len(samples) - 2)
    train, test = samples[:split], samples[split:]

    def design(rows: list[dict[str, Any]]) -> tuple[list[list[float]], list[float]]:
        x = [[1.0] + [float(r[name]) for name, _p, _f in FEATURES] for r in rows]
        y = [float(r["target"]) for r in rows]
        return x, y

    x_train, y_train = design([r for s in train for r in s["rows"]])
    beta = _ridge(x_train, y_train)
    ics = []
    long_short = []
    for s in test:
        x, y = design(s["rows"])
        pred = [sum(b * v for b, v in zip(beta, row)) for row in x]
        ic = _spearman(pred, y)
        if ic is not None:
            ics.append({"as_of": s["as_of"], "ic": round(ic, 4), "n": len(y)})
        order = sorted(range(len(pred)), key=lambda i: pred[i])
        k = max(1, len(order) // 5)
        bottom = mean(y[i] for i in order[:k])
        top = mean(y[i] for i in order[-k:])
        long_short.append(top - bottom)
    mean_ic = mean([x["ic"] for x in ics]) if ics else None
    ic_t = None
    if len(ics) >= 3:
        sd = pstdev([x["ic"] for x in ics])
        if sd > 1e-12:
            ic_t = mean_ic / (sd / math.sqrt(len(ics)))
    ls_mean = mean(long_short) if long_short else None
    ls_sd = pstdev(long_short) if len(long_short) >= 2 else None
    dsr = None
    if len(long_short) >= 3 and ls_sd and ls_sd > 1e-12:
        dsr = deflated_sharpe(ls_mean / ls_sd, n_trials=max(1, len(FEATURES)),
                              n_obs=len(long_short)).get("dsr")
    out = {
        "status": "ok",
        "contract": contract,
        "snapshots": len(dates),
        "pairs": len(pairs),
        "usable_pairs": len(samples),
        "train_quarters": len(train),
        "test_quarters": len(test),
        "train_period": [train[0]["as_of"], train[-1]["next_as_of"]] if train else None,
        "test_period": [test[0]["as_of"], test[-1]["next_as_of"]] if test else None,
        "features": [f[0] for f in FEATURES],
        "coefficients": {name: round(b, 6) for (name, _p, _f), b
                         in zip(FEATURES, beta[1:])},
        "intercept": round(beta[0], 6) if beta else None,
        "ic": ics,
        "mean_ic": round(mean_ic, 4) if mean_ic is not None else None,
        "ic_t": round(ic_t, 3) if ic_t is not None else None,
        "long_short_mean": round(ls_mean, 6) if ls_mean is not None else None,
        "long_short_series": [round(v, 6) for v in long_short],
        "dsr": round(dsr, 4) if dsr is not None else None,
        "note": ("研究模型，不改变筛选。风格中性化只是用 style_code 做组内去均值，"
                 "不是真正的久期/信用/转债因子中性化；季度样本少，IC/DSR 只能作参考，"
                 "不能据此宣称有选基技能。"),
    }
    if cache:
        path = cache_path(cfg, contract)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(out)
            payload["generated_at"] = db.now_iso()
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError:
            pass
    return out
