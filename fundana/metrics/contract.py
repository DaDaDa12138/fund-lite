"""指标契约：公式版本、周期定义、rf 与封顶。任何改变数值的修改都必须升版。"""
from __future__ import annotations

FORMULA_CONTRACT = "fundana-risk-2026.11.5"
STANDARD_METRIC_CLAIM = False

# 周期（自然日）→ 契约键
PERIODS: dict[int, str] = {
    7: "1w", 30: "1m", 90: "3m", 180: "6m", 365: "1y",
    730: "2y", 1095: "3y", 1460: "4y", 1825: "5y",
}

MIN_POINTS_FLOOR_SHORT = 3      # 7 日窗口
MIN_POINTS_FLOOR_DEFAULT = 10   # 其余窗口
MIN_POINTS_RATIO = 0.8
# 采样节奏：用观测间隔的 p75（而非中位数）作为节奏。混合披露（开放期日更 + 封闭期周更）
# 的中位数会落在 1 天，p75 才反映"至少 75% 的间隔不超过几天"，见 ADR-027。
CADENCE_DAILY_MAX_GAP_DAYS = 3
CADENCE_MIN_POINTS_RATIO = 0.8
TRADING_DAYS_PER_YEAR = 252
ANNUALIZATION_BASIS_DAYS = 365.25
CAPS = {"sharpe": 5.0, "sortino": 10.0, "calmar": 20.0}

CONTRACT_NOTES = {
    "return_basis": "daily_growth_pct(首选) > accum_nav(accum_proxy) > unit_nav(price_return_proxy)",
    "annualization": "annual = wealth**(365.25/D) - 1，D 为窗口边界锚点至末点的自然日",
    "volatility": "sample_std(ddof=1) * sqrt(freq)，freq = max(1, min(252, n/Y))",
    "sharpe": "(annual - rf) / vol；同时存 sharpe_t = (mean/std)*sqrt(n)",
    "sortino": "(annual - MAR) / downside_dev；零下行 → NULL(zero_downside)，不再回退 vol*0.1",
    "calmar": "annual / mdd；单一版本，不再使用 (ret-rf)/max(mdd, 风格地板)",
    "drawdown": "peak 含初始财富 1.0；max_drawdown_duration 单位=净值观测数",
    "var": "VaR95/VaR99 = 日收益 5%/1% 分位（线性插值）；CVaR5 = 收益 <= VaR95 的均值",
    "caps": "显示层封顶 ±5/±10/±20，raw_* 与 *_cap_hit 同存",
    "min_points": "max(floor, ceil(0.8*252*days/365))；观测间隔 p75 > 3 天时改按 ceil(0.8*days/p75_gap)（只放宽不收紧）",
    "growth_reconstruction": "growth_pct 为 0 但单位净值相对上一观测变动 >1e-6 时判为伪零，改用 nav 环比（累计净值口径更接近时优先 acc）",
    "daily_win_rates": "positive=上涨日占比；flat=0.00%持平日占比；non_negative=(上涨+持平)日占比。低波动债基的 0.00% 日不是亏损日，门槛与打分使用 non_negative，positive 仅展示。",
}