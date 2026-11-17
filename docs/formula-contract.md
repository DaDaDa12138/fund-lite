# 指标口径契约（Formula Contract）

`formula_contract = fundana-risk-2026.11.5`
`standard_metric_claim = false`（夏普/索提诺/卡玛为**策略口径**，不是教科书标准指标）

## 1. 收益序列：必须用日增长率链式复利

优先级（`metrics/window.py: resolve_returns`）：

1. `daily_growth_pct`（pingzhongdata 的 `equityReturn`，百分点）— **首选**
2. `accum_proxy`（累计净值差分）— 仅在日增长率缺失 ≥10% 时降级，标记 `return_basis:accum_proxy`
3. `price_return_proxy`（单位净值差分）— 最后兜底，标记 `return_basis:price_return_proxy`

**实测证据（2026-10-31，本机复算）**：
| 基金 | 链式 ∏(1+JZZZL) | 单位净值首末比 | 累计净值首末比 |
|---|---|---|---|
| 000003 中海可转债A | **+9.9521%** | −6.4000% | +14.6000% |
| 000053 鹏华永诚一年定开A | **+96.9552%** | +5.5000% | +90.1200% |
| 000032 易方达信用债A | **+78.3019%** | +13.1300% | +65.9100% |

结论：单位净值严重低估（000053 差 91pp），累计净值也低估（只加回分红、不做复利）。**只有链式复利才是复投总收益。**

## 2. 窗口规则

- 周期：`PERIODS = {7:1w, 30:1m, 90:3m, 180:6m, 365:1y, 730:2y, 1095:3y, 1460:4y, 1825:5y}`
- 窗口 `(end − period, end]`，另取窗口起点之前**最后一个观测**作为边界锚点（anchor），用于计算真实跨度 D。
- 最少点数：`max(3 或 10, ceil(0.8 × round(days×252/365)))` → 7d=4, 30d=17, 90d=50, 365d=202。
- 最大间隔：`min(45, max(days//3, 7))`。
- 覆盖宽限：`coverage_grace_days=7`；仅当窗口起点前**完全没有观测**（anchor=None）且 lag > max_gap 才记 `coverage_gap`（避免把国庆/春节长假误判为缺口）。
- **无效窗口一律 NULL + reasons**：`empty_window / insufficient_points / coverage_gap / gap_exceeded` → 该周期全部指标为 NULL，绝不输出“短历史算出的长周期优势”。

## 3. 指标公式

设窗口收益序列 `r_1..r_n`（小数），真实跨度 `D = end − anchor` 天。

```
wealth_0 = 1
wealth_t = wealth_{t-1} × (1 + r_t)
total_return_pct = (wealth_n − 1) × 100
years = D / 365.25
annual_dec = wealth_n^(1/years) − 1
freq = max(1, min(252, n / years))
vol_dec = sample_std(r, ddof=1) × sqrt(freq)
positive_ratio_pct = 100 × count(r > 0) / n          ← 仅展示“上涨日占比”
flat_ratio_pct = 100 × count(r == 0) / n             ← 持平日占比（0.00%）
non_negative_ratio_pct = 100 × count(r >= 0) / n     ← 非负日占比，稳定风格门槛/评分用
drawdown_t = 1 − wealth_t / peak_t      (peak 含初始 1.0)
mdd_dec = max(drawdown_t)
dd_duration_obs = 最长连续 drawdown>0 的观测数（单位=观测数，不是自然日）
sharpe = (annual_dec − rf) / vol_dec
sharpe_t = (mean(r)/std(r)) × sqrt(n)    ← 显著性检验用
downside_dev = sqrt(mean(min(r_i − MAR/freq, 0)^2)) × sqrt(freq)
sortino = (annual_dec − MAR) / downside_dev
calmar_dec = annual_dec / mdd_dec
var95_dec = r 的 5% 分位；cvar5_dec = r ≤ var95 的均值
```

- **为什么区分三种日胜率（2026-11.5）**：低波动债基大量交易日的日增长率四舍五入为 0.00%，`r > 0` 会把“没跌”记成“没赚”，与低波动目标方向相反。实测 `corr(positive_1y, vol_1y) = −0.259`，旧 `positive≥55` 淘汰 38.9% 的基金。稳定风格改用 `non_negative≥70`（复算通过率 57.9%，与旧口径 58.6% 接近），`positive` 仅保留展示；增强风格仍用上涨日参与率。
- `rf = 0.0164`（中债 10 年期，observed_at=2026-10-25，**配置观测值**；无公开自动源）。
- `MAR = 0`；`trading_days_per_year = 252`；年化统一按 365.25（消灭旧系统“交易日/365.25”双口径）。
- `vol_dec = 0` / `downside_dev = 0` / `mdd_dec = 0` → 对应指标 **NULL** + `undefined_reasons`（`zero_volatility / zero_downside / zero_drawdown`），禁止 Infinity。
- 封顶（只影响展示值，**raw_* 与 *_cap_hit 同时保留**）：Sharpe ±5、Sortino ±10、Calmar ±20。

## 3.1 采样节奏：最少点数按序列自身节奏折算

| 序列形态 | 观测间隔 p75 | 最少点数 |
|---|---|---|
| 日频（含周末 3 天间隔） | ≤ 3 天 | `max(floor, ceil(0.8 × 252 × days / 365))`（365 天 → 202） |
| 周频/月频/定开/混合披露 | > 3 天 | `max(floor, ceil(0.8 × days / p75))`（365 天、7 天间隔 → 42） |

只放宽、不收紧；`max_gap` 与覆盖检查不变。窗口把 `median_gap_days`、`p75_gap_days`、`min_points_used`
写入 `fund_metric`，“为什么这只基金没有指标”可以直接用这三列回答。

为什么用 p75 而不是中位数：定开基金常“开放期日更、封闭期周更”，中位数会落在 1 天，
但 p75 仍能反映“至少 75% 的间隔不超过几天”。实测 000053 中位 1 天、p75 3 天（等于普通
日频含周末的形态，仍按 202 门槛 → 192 点不足，如实记 insufficient_points），
001246 中位 1 天、p75 6 天（混合披露）→ 门槛降到 49，116 点可算。

效果：1 年期有指标的基金 6,336 → 6,687 只；`no_1y_metrics` 拒绝 1,029 → 678 只，
另有 495 只按 `sampling_cadence_gt_3d` 拒绝入正式榜（L1，ADR-026/027）。
这些基金**只出指标、不进正式双榜**：周频的正收益比例是“周胜率”，与日频“日胜率”不可横比。

## 4. 为什么 Sortino 的零下行不再回退 vol×0.1

旧系统（strategy-risk-2026-10-13.1）在无下行样本时用 `vol × 0.1` 充当下行偏差，这不是标准定义。标准 Sortino 的下行偏差是目标半方差（概率加权、分母覆盖全部观测）。本项目：无下行样本 → **NULL + zero_downside**，不伪造。

参考：<https://en.wikipedia.org/wiki/Sortino_ratio>

## 5. 为什么 Calmar 必须叫“策略卡玛”

Young 原始定义 = 最近 36 个月平均年化收益 / 最近 36 个月最大回撤（MAR ratio 才用成立以来）。本项目用 1 年窗口，属**非标准变体**，因此：
- 字段名 `calmar_dec` + 展示名“策略卡玛”
- `standard_metric_claim = false`
- 另算 `calmar_36m` 作为 shadow（后续版本）

参考：<https://en.wikipedia.org/wiki/Calmar_ratio>

## 6. Sharpe 显著性

`t = SR × sqrt(T)`，T 为收益观测数。排序时若两只基金差异 t 很小，不应宣称“更优”。首期只在诊断中输出 `sharpe_t`，不作为硬门槛。

参考：<https://en.wikipedia.org/wiki/Sharpe_ratio>

## 7. 重放一致性

`python3 -m fundana replay --run-id <id> --offline`：
- 读取同一冻结输入（或数据库现状），用 `dry_run` 重算 9 个周期；
- 逐 13 字段比对：`n_obs / total_return_pct / annual_dec / vol_dec / positive_ratio_pct / flat_ratio_pct / non_negative_ratio_pct / mdd_dec / sharpe / sortino / calmar_dec / var95_dec / cvar5_dec`；
- 容差 `1e-9`；任何差异 → 退出码 1。

## 8. 字段语义速查

`SYL_Z=1周 / SYL_Y=1月 / SYL_3Y=3月 / SYL_6Y=6月 / SYL_1N=1年累计(非年化) / SYL_JN=年初至今`；
`JZZZL` 为百分点；`ENDNAV` 元需 /1e8；`MGOLD` 是经理评分。

## 9. 尾部风险指标（V2 影子口径）

契约 `fundana-tail-2026.11.1`，窗口 1095 天（3 年），**只用于研究展示，不参与正式分、榜单排序或发布筛选**。

- 滚动窗口：`rolling_worst(90/180 天)` 按自然日锚点取 `(end-window, end]`，边界误差 >7 天或窗口内有净值缺口的整窗跳过；输出最差区间与起止日。
- 单日/单周：单日 = 窗口内最低 `JZZZL`（百分点）；单周 = 连续 5 个有效观测的链式复利（proxy，非自然周），取最差。
- CVaR 5%：日收益最差 ceil(5%) 个观测的算术均值；样本 <100 个 → NULL（`cvar5_unavailable`）。
- 自然月正收益比例：只统计观测数 ≥10 的完整自然月；`months_total/months_positive` 同存。
- 最长水下观测数：连续 `drawdown>0` 的最大观测数（单位=观测数，不是自然日）；`ongoing_drawdown=1` 表示当前仍在回撤中。
- 完整度 `completeness = 非空组件数 / 8`（组件：mdd_3y / rolling_90d / rolling_180d / cvar5 / worst_day / worst_week / positive_month_ratio / underwater）。
- 单位：`mdd_dec / cvar5_dec` 是小数比率；`worst_rolling90d_pct / worst_rolling180d_pct / worst_day_pct / worst_week_pct / positive_month_ratio_pct` 是百分点。
- 历史不足或覆盖缺口 → 整个记录为空 + `undefined_reasons_json`，禁止用 0 或短窗口冒充 3 年。

## 10. 指标重建与"伪零"口径（ADR-051）

日更之后，同一只基金的每个窗口都会右移；窗口也可能因为净值缺失而变短。重建规则必须保证三件事：**旧值不会冒充新值、缺失不会变成 0、窗口不足不会算出长周期优势**。

**10.1 写入方式**

- `fund_metric` 主键 `(fcode, period_days, formula_contract)`，重建走**全列覆盖**（upsert 覆盖所有指标列），不做"只更新变化字段"。
- 每条指标带 `nav_snapshot_sig`（该基金参与计算的净值序列签名）与 `formula_contract`；签名变、公式版本变、rf 变，三者任一都触发重算。

**10.2 致命原因 → 全 NULL**

窗口出现下列任一原因时，`total_return_pct / annual_dec / vol_dec / positive_ratio_pct / flat_ratio_pct / non_negative_ratio_pct / mdd_dec / dd_duration_obs / sharpe / sharpe_t / sortino / calmar_dec / var95_dec / cvar5_dec` 全部写 `NULL`，并把原因原样写进 `undefined_reasons_json`：

| 原因 | 含义 |
| --- | --- |
| `empty_window` | 窗口内一条净值都没有 |
| `insufficient_points` | 观测点数 < `max(3 或 10, ceil(round(days*252/365)*0.8))` |
| `coverage_gap:<days>d` | 窗口起点早于该基金净值起点（历史不够，不是数据缺失） |
| `gap_exceeded:<days>d` | 相邻观测最大间隔 > `min(45, max(days//3, 7))` |

**`n_obs` 与 `years` 例外：始终写实际观测数与跨度**。这是"为什么没有值"的证据——写 0 会让"没采到"和"真没有"混为一谈。

**10.3 零值不是 0**

- `vol == 0` → `sharpe = NULL`、`sortino = NULL`（原因 `zero_volatility`）；
- `downside_dev == 0` → `sortino = NULL`（原因 `zero_downside`），**不再回退 `vol×0.1`**（见 §4）；
- `mdd == 0` → `calmar = NULL`（原因 `zero_drawdown`）；
- 任何情况下不产出 `NaN` / `Infinity`（单测 `test_metrics` 与 `test_edgecases` 双向断言）。

**10.4 `--changed-only` 的真实语义**

`metrics --changed-only` 只跳过"净值签名未变"的基金，**不跳过窗口会变的基金**。换句话说，只要出现新业务日，所有基金的 7/30/…/1825 天窗口都右移了一格，必须重算。

**10.5 发布与重放**

- CSV/XLSX 中 `NULL` 输出为空字段，**不得写 0**；前端显示为 `—`。
- `replay --offline` 逐字段比对时，`NULL` 与非 `NULL` 的不一致同样算失败（NULL 是结论，不是缺省）。
- 实测：`--changed-only` 全量重算 7,365 只耗时 32 秒；`insufficient_points` 1,563 条、`coverage_gap` 11,039 条是 NULL 的两大来源。
