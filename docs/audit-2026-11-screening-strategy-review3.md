# 债券型基金筛选策略第三轮审计（v10，2026-11）

- 审计日期：2026-11-03
- 审计对象：screen-bond-v10-2026.12.1 / profile bond_v1
- 数据基线：run 20261103T204009-screen-99d035，batch daily-2026-11-02-v10，nav_as_of = metric_as_of = 2026-11-02
- 证据包：reports/analysis/audit_2026_11_review3/
- 审计性质：只读代码与数据库审计；本文件只提出规格与路线图，不改生产代码
- 结论一句话：**这是一套合格的风险过滤器与候选池生成器，不是经过验证的“优质基金发现器”。当前版本不能证明能筛出真正值得长期投资的债券型基金；它可以帮用户排除不可投、锁定期、规模过小、费用过高、数据不可比的产品，但榜单本身不是买入建议。**

## 0. 执行摘要

### 0.1 四个问题的直接回答

| 问题 | 结论 | 主要依据 |
|---|---|---|
| 筛选流程是否科学？ | 部分科学 | 漏斗确定、可复现、有 PIT 与批次冻结；但目标函数未定义，指标口径有方向性错误，分位阈值是条件分位，缺少样本外技能证据 |
| 是否合理？ | 部分合理 | L2 可投性、L3 锁定期、L4/L5 规模、L5.5 费用上限符合直觉；但 8 条固定线中 7 条只看 1 年；composite 虽含 3y/5y，长期证据却没有独立门槛 |
| 是否正确？ | 存在实质错误 | 正收益占比把 0.00% 日算作非正；composite 隐含 3 年门槛；L7 实际只剩 composite 一维；报告按 rank 排序却展示 formal_score；core10 不是 10 个独立 bet |
| 能否筛出真正优质、值得长期投资的债基？ | **目前不能证明** | 12 队列前瞻 rank 对年化 IC 仅 +0.011；DSR 0.660；前瞻 cohort 0 个成熟；L9 通过的 138 只全部 3 年以上，但报告没有 3/5 年表 |

### 0.2 五条最重要的发现

1. **正收益占比方向错误（P0）。** fundana/metrics/risk.py:104 用 r > 0 判定“正收益日”，把大量 0.00% 的平坦日算作非正。7,189 只、至少 200 个日观测的基金，平坦日占比中位数 12.86%、均值 15.93%；corr(positive_1y, vol_1y) = -0.2594，positive 越低波动反而越高。低波动债基被系统性压低，和“稳健”目标方向相反。
2. **composite 隐含 3 年门槛，但 L5 只要求成立 1 年（P0）。** fundana/screen/engine.py:408-442：3 年覆盖不足 90% 时 long_core = 0，composite 上限只有 40；FIXED 却要求 composite ≥ 58。1,878/6,764（27.76%）只基金 long_core = 0，最高 composite 39.1，永远过不了 58。规则没有显式写“正式榜必须 3 年”，但 L9 通过的 138/138 都是 3 年以上。
3. **L7 动态层已经退化为一维（P0）。** 201 条 L7 退出全部且仅因为 dyn_composite:below_dynamic；k=4 的 6 维检查从未成为约束。composite 阈值是在“已过 58 分”的子池上取 q70，是条件分位，不是可投域 q70。
4. **报告存在产品级矛盾（P0）。** fundana/publish/builder.py:630-635 按 p.rank 排序，却展示 formal_score；稳健榜 Spearman(rank, formal_score) = -0.8323，第一名 formal 75.69，低于第二名 82.65。用户看到的“第 1 名”与“分数”互相矛盾。
5. **没有证据表明它能发现长期优质基金（P0/P1 边界）。** review2 的 12 个季度队列：v10 rank 对前瞻年化的 IC +0.011、对前瞻 MDD 的 IC -0.025；v10 相对 v9 平均低 17.2bp，只在 3/12 队列更高；DSR 0.660（n_trials=256），不能拒绝“无技能”。这不等于系统无用，但等于**不能把榜单包装成长期优质结论**。

### 0.3 必须保留的优点

- universe 口径清楚：债券型 + 指数型-固收，8,141 个份额；QDII 债基 166、偏债混合 1,475 明确不纳入，避免把不同风险属性的产品混在一起。
- 三个日期分离：business_date、nav_as_of、metric_as_of；14 天回看、覆盖率 ≥90% 的最新有效净值日，解决了“11-03 只有 185 只披露 11-02 净值”的新鲜度问题。
- L2 可投性、L3 锁定期、L4/L5 规模、L5.5 费用上限，方向正确；这些是“排除不合适产品”，不是“预测收益”。
- 批次冻结、契约一致性、PIT 输入快照、发布门禁、跨源校验，是治理优势；它们保证“同一批次可重放”，但不保证“选得准”。
- 独立风险门（L8R）与 long_core 的 3y/5y 风险权重，方向正确；问题是指标与门槛还没有完全对齐长期目标。

## 1. 审计范围、数据与方法

### 1.1 数据范围

| 项目 | 数值 |
|---|---|
| 数据库 | var/db/fundana.db，约 4.1 GB，只读 |
| 净值行数 | 9,921,617 行，2002-09-20 至 2026-11-03 |
| universe | 8,141 个份额 |
| 基础门禁通过 | 3,034 |
| L9 通过 | 138 |
| 正式榜入榜 | stable 84 + enhanced 26 = 110；core 46（44 与 stable 重叠、2 只 core-only）；core10 10 为 stable 子集 |
| 通过未入榜 | 26（family/company cap 或配额） |
| 观察/受限/新锐 | watch 50 / locked 11 / rookie 30 |
| 有 composite 的基金 | 6,764 |
| 至少 200 个日观测的基金 | 7,189 |

### 1.2 审计方法

1. **代码审读**：L1–L9、指标、评分、阈值、费用、风格、风险门、报告、API、测试。
2. **全量 SQL 复算**：不使用抽样；对漏斗、失败原因、分位、相关性、榜单重叠、份额成本逐项复算。
3. **证据包**：所有数字落在 reports/analysis/audit_2026_11_review3/ 的 CSV；生成脚本为 generate_evidence.py，复现入口为 reproduce.sh。
4. **外部对标**：只使用公开的监管规则、评级机构方法、学术论文与行业研究；按 A/B/C 分级，不把行业方法当因果证据。
5. **边界**：单一时点 run；没有真实 3–5 年前瞻；持仓数据有季报滞后；外部研究以美国市场为主；本审计不评价数据供应商的原始准确性。

### 1.3 “检索算法”在本项目中的含义

本项目不是 IR/向量检索系统，而是**确定性 SQL 全量取数 + 规则过滤 + 分位打分**。因此“检索算法是否正确”应拆成四个问题：

- **取数是否完整**：universe 是否漏掉应纳入的份额；NAV/指标/规模/费用/经理是否按 as-of 取。
- **规则是否正确**：每个门槛是否与目标一致；是否存在隐含门槛、死代码、方向性错误。
- **分位是否可比**：同风格样本是否足够；风格是否真的隔离了久期/信用风险；缺失值如何处理。
- **排序是否有效**：样本外 IC、DSR、前瞻 cohort 是否支持“高分未来更好”。

本报告的判断按这四层展开，而不是只看“代码有没有报错”。

### 1.4 证据分级

| 级别 | 定义 | 本报告用法 |
|---|---|---|
| A | 监管规则或同行评议学术证据 | 约束“最低证据期”“费用/持续性”等方向性结论 |
| B | 评级机构公开方法、行业协会披露、头部机构研究 | 校准 3 年/同类样本/评级期等门槛 |
| C | 媒体、科普、二手引用 | 只提示风险方向，不做定量结论 |

## 2. 漏斗逐层审阅（L1–L9）

### 2.1 终态计数

| 层 | 终态数量 | 占 universe | 前三大原因 | 判断 |
|---|---:|---:|---|---|
| L1 | 1,377 | 16.91% | no_1y_metrics 690；metric_end_before_as_of 548；metric_sparse_nav 495 | 数据资格整体合理，但指标日硬门过严 |
| L2 | 1,252 | 15.38% | buyable_confirmed_no 1,252；其中 20 带风险原因（19 holdings、1 convertible） | 可投性过滤合理；风险原因与可投性混在一层，解释性不足 |
| L3 | 434 | 5.33% | locked_180d 213；locked_365d 79；locked_120d 79 | 合理，和行业“定开/封闭单独可比”一致 |
| L4 | 0 | 0.00% | 本轮无终态 | 规模/份额/费用缺失在本轮未形成独立淘汰 |
| L5 | 1,992 | 24.47% | share_scale_below_0.50 678；scale_below_3.00 649；scale_below_2.50 274 | 偏严但可解释；规模门槛是最大的单一过滤器 |
| L5.5 | 52 | 0.64% | holding_cost_above_1.30 52 | 合理；1 年费用上限只砍最贵的一小部分 |
| L6C | 1,707 | 20.97% | composite 1,597；annual_1y 653；sharpe 509 | 主问题层：composite 一项造成 914 只单独失败 |
| L6E | 940 | 11.55% | strict_overlay 849；sharpe 758；calmar 726 | 增强债准入极严，需单独验证，不宜与稳健榜混读 |
| L7 | 201 | 2.47% | dyn_composite 201 | 已退化为一维；k=4 从未咬合 |
| L8 | 46 | 0.57% | manager_tenure_below_365d 46；manager_career_below_365d 13 | 方向合理，但经理覆盖不全 |
| L8R | 2 | 0.02% | risk 2（top10、convertible） | 风险门约束存在但触发率极低，不能把 0 block 当安全证明 |
| L9 | 138 | 1.70% | relief 1 | 最终池很小；高选择性不等于高质量 |

> 终态计数是“最终停留层/退出层”，不是“曾经到达该层”。同一只基金可以有多个原因码；上表原因计数可重叠。

### 2.2 逐层判断

**L1 身份与数据资格。** 身份、成立日、NAV、新鲜度、365 天年化、采样间隔、可比性旗标，方向都对。问题在 engine.py:237-242：指标截止日必须严格等于 as_of，否则记 metric_end_before_as_of。548 只被这一条排除，其中 132 只仅此一条；p75 采样间隔 7 天的有 368 只、1 天的 151 只；中位 n_obs 68、中位 years 1.0。它们多数是周更净值或低频披露产品，不是“数据质量差”。freshness.py 已有 7 天容忍常量，base_gate 没有用。建议：允许 0–7 天容忍并记 metric_stale_days；>7 天或 lookahead 才阻断。

**L2 可投性。** buyable_state != confirmed_yes 退出正式榜，正确；场内 ETF/LOF 单列 exchange_traded，也正确。1,252 条中 1,232 只仅因不可申购，19 只另带 holdings_required，1 只 convertible。把风险原因塞进 L2 会让“不可投”和“不可验证风险”混在一起，报告应拆开。

**L3 锁定期。** 180 天 213 只、365 天 79 只、120 天 79 只、定开 37 只、270 天 16 只。把定开/封闭排除正式榜，和银河“定开/封闭与开放式分开评级”的行业做法一致。保留 locked 榜展示是合理的，但必须明确“不是同一条赛道”。

**L4/L5 规模。** 本轮 L4 终态 0；L5 是最大过滤器，share_scale_below_0.50 678 只、family 规模分档合计 1,239 只（3.00 亿 649、2.50 亿 274、2.00 亿 250、短债 1.50 亿 66）、maturity_lt_1y 75 只。规模门槛能降低清盘/大额申赎风险，但 family 规模按份额最近报告期汇总，必须确认口径不重复；更重要的是，engine._load_candidates 没有复用 fundana/fuse/scale.py:39 的 45 天披露滞后，回测会前视。

**L5.5 费用。** 1 年持有成本 ≤1.30%，52 只失败；方向合理。问题是只算 1 年、不含赎回费，长期投资者可能被引向错误份额（见 §7）。

**L6C 稳健固定线。** 这是全系统最关键的一层：1,707 只终态，914 只仅因 composite 失败（53.54%）；249 只 composite+annual+sharpe；140 只 composite+annual+sharpe+positive；120 只 composite+annual；65 只仅 annual。composite 是主淘汰器，其他 7 条大多只是伴随项。

**L6E 增强债。** 940 只终态；strict_overlay 849、sharpe 758、calmar 726、formal 632、annual 594、positive 500、mdd 327、fee 126。增强债走相对准入 + 7 项严格口径至少 5 项，只有 26 只入榜；选择性极高，统计功效很低。历史记录显示增强榜在 2024/2025 相对同风格跑输 2.30pp/3.60pp（v9 审计数字，非本轮复算），因此增强榜不能承担“长期核心”角色。

**L7 动态分位。** 201 条全部且仅失败 dyn_composite；6 维 k=4 规则从未成为约束。名义上是“综合分 + 6 维中至少 4 维”，实际是“composite 单维”。这是本审计最重要的规则退化之一。

**L8/L8R。** 46 只全部经理任职期不足；L8R 仅 2 只（top10、convertible）。风险门有明确阈值，但 watch 不阻断、block 才阻断；最新批次 7 ok/3 watch/0 block/0 unknown。它更像尾部风险提示，不是质量信号。报告不能把“0 block”写成“风险已排除”。

## 3. 基金周期范围抓取：抓到了什么，缺什么

### 3.1 抓取范围本身是完整的

- fund_metric 对 8,043 只基金写了 9 个 period：7/30/90/180/365/730/1095/1460/1825 天。
- fund_nav_daily 覆盖 2002-09-20 至 2026-11-03，9,921,617 行。
- 按首笔净值年份：2002 年 2 只、2003 年 9 只、…、2015 年 84 只、2016 年 422 只、2017 年 329 只、2018 年 480 只、2019 年 744 只、2020 年 724 只、2021 年 711 只、2022 年 875 只、2023 年 813 只、2024 年 1,083 只、2025 年 856 只、2026 年 542 只。
- 因此“没有 3 年/5 年数据”主要不是抓取截断，而是**产品成立时间不足**。

### 3.2 长周期指标覆盖

| period | annual 非空 | 占比 |
|---:|---:|---:|
| 7 天 | 7,529 | 93.61% |
| 30 天 | 7,474 | 92.93% |
| 90 天 | 7,773 | 96.64% |
| 180 天 | 7,621 | 94.75% |
| 365 天 | 7,353 | 91.42% |
| 730 天 | 6,536 | 81.26% |
| 1095 天 | 5,424 | 67.44% |
| 1460 天 | 4,587 | 57.03% |
| 1825 天 | 3,719 | 46.24% |

- 3 年覆盖 ≥90%（is_full_3y）5,689 只，约 70%；5 年覆盖 ≥90%（is_full_5y）4,128 只，约 51%。
- L9 通过的 138 只：is_full_3y 138/138，is_full_5y 91/138；47 只没有 5 年证据。
- 结论：**周期抓取不是瓶颈；瓶颈是规则没有把“3 年/5 年证据”显式化，也没有把 1–3 年产品放到独立赛道。**

### 3.3 指标日硬门的误杀

- 548 只 metric_end_before_as_of：p75 gap 7 天 368 只、1 天 151 只、3 天 22 只、2 天 4 只、5 天 1 只、3614/3523 天各 1 只。
- 中位 n_obs 68、中位 years 1.0；多数是周更净值或低频披露产品。
- 这不是“指标过期”，而是“披露节奏不同”。用严格相等做硬门，会把可投的定开/理财型产品误杀，扭曲分位母体。
- 建议：把 metric_as_of 与 nav_as_of 的差分成 metric_stale_days；0–7 天可接受，>7 天或未来日期阻断；报告中披露 stale 天数。

## 4. 指标数学与口径

### 4.1 年化、波动、Sharpe、Sortino、Calmar

| 指标 | 当前实现 | 评价 | 建议 |
|---|---|---|---|
| 年化收益 | wealth^(1/years)-1，years = span/365.25 | 几何年化，方向正确；窗口含 anchor，实际略长于 period | 保留；报告注明 span 与 period 的差异 |
| 年化波动 | std × sqrt(freq)，freq = min(252, n/years) | 对周更/低频产品是近似；不规则采样可能高估/低估 | 用时间加权或实际观测频率；对低频产品标注 |
| Sharpe | (几何年化 - 常数 rf) / 算术波动 | 分子几何、分母算术，口径混合；rf 固定 1.64%，不随日期变 | 用日/月超额收益均值/标准差 × sqrt(252/12)，或明确披露混合口径 |
| Sharpe t | (mean/std) × sqrt(n) | 这是均值 t 值，不是 Sharpe 的 t 值，标签错误 | 改名 mean_tstat 或删除 |
| Sortino | (年化 - MAR) / 下行差 | 方向正确；下行差除以 n，不是只除以下行样本 | 保留，但披露分母定义 |
| Calmar | 年化 / MDD | 方向正确；依赖 MDD 窗口 | 保留；同时展示 3y/5y Calmar |
| MDD | 窗口内自 1.0 起，无窗口前高水位 | 修复期基金的回撤被低估；1y MDD 尤其明显 | 增加“从窗口前高点到当前”的 ongoing drawdown；报告展示 max_underwater_obs |
| 封顶 | Sharpe 5 / Sortino 10 / Calmar 20 | 极端值被压缩，排名可能失真 | 披露 cap 命中率；评分层不要静默封顶 |

### 4.2 正收益占比：最严重的指标错误

fundana/metrics/risk.py:104：

    positive_ratio = count(r > 0) / n

0.00% 的平坦日被算作“非正”。对低波动债基，平坦日是常态：7,189 只基金平坦日占比中位数 12.86%、均值 15.93%、p90 34.02%；7,079 只至少有一个平坦日。

按 positive 十分位分组（positive_ratio_bias.csv；样本 n=7,341，365 天指标齐全）：

| 十分位 | n | 平均 positive | 平均年化波动 | 平均年化收益 | 平均 Sharpe |
|---:|---:|---:|---:|---:|---:|
| 1 | 734 | 40.92% | 3.39% | -1.27% | -1.47 |
| 2 | 734 | 48.84% | 4.90% | -1.40% | -0.69 |
| 3 | 734 | 51.49% | 3.70% | 1.17% | -0.31 |
| 4 | 734 | 54.14% | 1.75% | 1.94% | 0.27 |
| 5 | 734 | 56.60% | 0.76% | 2.37% | 0.92 |
| 6 | 734 | 58.66% | 0.66% | 2.53% | 1.27 |
| 7 | 734 | 60.88% | 0.58% | 2.51% | 1.34 |
| 8 | 734 | 63.53% | 0.47% | 2.47% | 1.70 |
| 9 | 734 | 66.56% | 0.44% | 2.58% | 2.13 |
| 10 | 735 | 75.69% | 0.386% | 2.79% | 2.86 |

这张表说明：positive 越高，平均波动越低；positive 实际上是“波动/平坦日”的代理，而不是“胜率质量”。corr(positive, vol) = -0.2594，corr(positive, sharpe) = +0.7216；positive < 55 的基金有 2,854 只（38.88%），< 50 有 1,356 只（18.47%）。当前固定线 positive ≥ 55，会把大量低波动稳健产品排除，和“稳健”目标方向相反。

修复方案：

1. 新增 flat_ratio_pct = count(abs(r) < epsilon) / n。
2. 新增 non_negative_ratio_pct = count(r >= 0) / n。
3. positive_ratio_pct 只展示，不再作为固定门槛。
4. 固定门槛改为 non_negative ≥ 70（需 12 队列校准）；当前样本中 non_negative≥70 约 4,157/7,189，和 positive≥55 的 4,215/7,189 选择性接近，不会突然放水。
5. 动态分位也改为 non_negative；保留 positive 作为解释字段。
6. 加测试：常数 0 序列应得到 positive=0、non_negative=100、flat=100；不能把“没有波动”判成“没有胜率”。

### 4.3 收益重构与代理

- fundana/metrics/window.py:123 用分红日 Δacc-Δnav 判定分红，返回 d_acc/prev_unit_nav；方向正确，但依赖累计净值与单位净值的对齐。
- resolve_returns 优先 daily_growth_pct，否则 accum_proxy/price_return_proxy；会写 basis/proxy_rows 标记。
- undefined_reasons 中大量是 growth_reconstructed；这不是错误，但说明多数收益是重构的，不是上游直接给出。
- 报告应展示“重构收益占比/代理行数”；如果一只基金 80% 的收益来自代理，和 100% 上游 growth 的基金不可直接比较。

### 4.4 其他口径问题

- sharpe_t 标签错误（见上）。
- positive_ratio 对 0 收益记非正（见上）。
- MDD 没有窗口前高水位（见上）。
- 5 年 MDD 只有 is_full_5y 才计权，方向正确；但 composite 中 long_core 权重 60%，long_core 内 3y/5y 指标权重约 90%，折算长期证据约 54%，1 年短期证据仍占约 46%，对“长期”仍不够。
- 年化收益用 365.25 天，而窗口用 365 天；差异很小，但应在公式契约中写清楚。

## 5. 评分、阈值与榜单

### 5.1 composite 的结构性上限

当前公式（engine.py:408-442）：

    short_core = weighted(1m, 3m, sharpe, sortino, vol)
    long_core  = weighted(3y annual, consistency, 3y mdd, 3y vol, 3y calmar,
                          5y mdd, 3y sortino, calmar, mdd, renormalize=True)
    if !is_full_3y or long_coverage < 0.60: long_core = 0
    composite = 0.4 * short_core + 0.6 * long_core

因此：

- 没有 3 年覆盖 → long_core = 0 → composite 上限 40。
- FIXED composite_min = 58 → 无 3 年覆盖者永远不可能通过 L6C。
- 1,878/6,764（27.76%）long_core = 0；中位 composite 19.39、p90 29.92、最高 39.1。
- 但 L5 只要求成立 ≥1 年；L9 通过的 138/138 却都是 3 年以上。
- 这不是“严格”，而是**规则没有把隐含门槛显式化**：用户看到“成立 1 年可入选”，实际“没有 3 年不可能入选”。

建议：

- 正式榜显式要求 is_full_3y = 1；1–3 年产品只进 bond_rookie。
- 如果希望保留 1–3 年产品，给它们单独的 1 年评分与榜单，不与 3 年产品比 composite。
- 报告写清楚“正式榜最低证据期 3 年；新锐榜 1–3 年”。

### 5.2 固定线的通过率：composite 一家独大

在可解析的 fixed_or_admission 记录中：

| 检查 | 通过率 | 评价 |
|---|---:|---|
| composite ≥58 | 38.51% | 主淘汰器 |
| annual_1y ≥2.0% | 61.70% | 有约束 |
| sharpe ≥0.8 | 55.53% | 有约束 |
| calmar ≥2.8 | 62.69% | 有约束 |
| positive ≥55% | 69.20% | 方向错误 |
| mdd ≤2.0% | 85.60% | 几乎不约束 |
| vol ≤6.0% | 99.94% | 形同虚设 |
| fee ≤0.90% | 87.35% | 弱约束 |
| formal_score | 37.53% | 与 composite 高度相关 |
| strict_overlay | 18.39% | 增强债专用 |

> 注：composite 与 vol 的分母是 1,545 条可解析记录；formal_score 为 1,468；其余多数为 3,013。不同检查的覆盖不同，百分比不能相加。

- 8 条固定线中，mdd 和 vol 几乎不筛选；composite 一项造成 914 只“仅 composite”失败。
- 这说明系统实际上是“composite + 少数收益/夏普线”，不是“8 维稳健筛选”。
- v9 审计已经发现同样问题（mdd 98.8%、vol 100%、calmar 95.5% 通过率），v10 没有实质改变。

### 5.3 动态分位：条件分位，不是可投域分位

engine.py:715-723：

    comp_pool = [v for v in comp_vals if v >= 58]
    if len(comp_pool) < 15: comp_pool = comp_vals
    comp_q = quantile(winsorize(comp_pool), 0.70)
    composite_min = max(58, comp_q)

问题：

- 母体不是“可投域”，而是“已过 58 的子池”；q70 是条件分位。
- 通过率被 58 锚死；池子结构变化时，阈值漂移不透明。
- 实测：bond_pure 67.0027（n=1,091）、bond_index 63.5544（n=300）、bond_rate 69.7619（n=72）、bond_credit 58.0（n=31）。
- 201 条 L7 退出全部只失败 dyn_composite；k=4 的 6 维检查从未成为约束。
- 名义 7 维，实际 1 维；这不是“动态多因子”，而是“composite 的第二道绝对线”。

建议：

1. 在正式母体（L1–L5.5 + 显式 3 年）上取无条件 q70；再 max(58, q70)。
2. 如果 q70 < 58，记录 dynamic_inactive = true，不要假装动态线在起作用。
3. 设定目标配额（例如 formal 120–160），用配额反推分位，而不是用条件分位反推通过率。
4. k=4 保留，但记录每个维度的 binding 次数；如果 12 队列仍然 0 次，删除这 6 维或改 k=5。
5. 统计 threshold_unavailable 分支（当前 bound 缺失会 pass），不能让缺失阈值变成免检。

### 5.4 缓释：1/992 的放行率

fundana/screen/relief.py:13-21 要求 composite ≥80 才允许规模/年化/positive 缓释。实际：

- relief_json 共 992 条检查；1 条 allowed=true，fcode 011961，composite 83.5451，annual 1.9784%，threshold 2.0，gap 0.021594pp。
- 其余 991 条 false：914 条 composite-only 但 type_not_eligible、65 条 annual、10 条 fee、2 条 positive。
- scale 路径从未触发。
- 结论：不是完全死代码，但放行率 1/992，结构上接近不可达；不能把“有 1 条缓释”当作可信度证据。
- 建议：生产关闭 L6D；或把 composite 门槛降到 70 并在 12 队列上做假阳性评估；无论哪种，报告都要披露放行率与个案。

### 5.5 排序分与展示分：产品级矛盾

- rank_score = 0.85 × composite + 0.15 × return_6m（engine.py:435-436）。
- formal_score = 0.45 × short_formal + 0.55 × long_formal（engine.py:433）。
- 稳健榜按 rank 排序；builder.py:630-635 展示 formal_score。
- 实测 Spearman(rank, formal_score) = -0.8323；Spearman(rank, rank_score) = -1.0（排序分降序）。
- 前十 (rank, formal, rank_score)：1 (75.6869, 79.6619)、2 (82.6468, 78.369)、3 (78.2992, 78.1495)、4 (75.3959, 77.9091)、5 (81.7381, 77.7492)、6 (79.2729, 77.5834)、7 (75.9271, 77.4166)、8 (71.0323, 77.4158)、9 (75.082, 75.6794)、10 (74.3246, 75.5362)。
- 用户看到“第 1 名 formal 75.69”，第 2 名 82.65，会直接怀疑榜单。
- 修复：报告同时显示“排序分”和“质量分”，明确“排序按排序分；质量分是另一把尺”；发布检查 Spearman(rank, rank_score) ≥0.99；如果展示 formal_score，就不要按 rank 排序。

### 5.6 榜单重叠与“独立 bet”假象

- 七榜 257 行 / 203 唯一代码 / 194 family；49 个 family 出现 >1 次，最多 4 次。
- 五个榜（stable/enhanced/core/core10/rookie）196 行 / 142 唯一代码 / 136 family；48 个 family 出现 >1 次，最多 3 次；跨榜重复主要来自同一 family 的 A/C/E 份额。
- bond_core 46 行 / 46 family，44 与 stable 重叠；bond_core10 10/10 全部已在 stable；bond_enhanced 26、watch 50、locked 11、rookie 30。
- 跨榜 family cap 不存在；FAMILY_CAP=1 只在单榜内生效。
- core10 不是“10 个独立 bet”，而是“稳健榜的 10 只集中子集”。报告必须这样写，否则用户会误以为 10 只基金分散了风险。

## 6. 风格、久期与信用：同风格并不等于同风险

### 6.1 风格分布

| 风格 | 数量 | 占比 | 判断 |
|---|---:|---:|---|
| bond_pure | 3,440 | 42.26% | 兜底类过大，混合了不同久期/信用暴露 |
| bond_secondary_enhanced | 1,701 | 20.90% | 增强债，波动与权益暴露差异大 |
| bond_short | 762 | 9.36% | 久期短，和 pure 不可直接比收益 |
| bond_primary_enhanced | 713 | 8.76% | 一级增强，含转债/股票 |
| bond_index | 677 | 8.32% | 指数债基，跟踪误差与费率结构不同 |
| bond_convertible | 556 | 6.83% | 转债基金，风险接近混合型 |
| bond_rate | 195 | 2.40% | 利率债，久期暴露最纯 |
| bond_credit | 67 | 0.82% | 信用债，样本太少，分位不稳定 |
| bond_cash_enhanced | 30 | 0.37% | 现金增强，样本太少 |

问题：

- 42% 的基金落在兜底 bond_pure，说明分类器对“普通纯债”的识别过度依赖名称/类型文本，而不是实际久期/信用暴露。
- bond_credit 只有 67 只、bond_cash_enhanced 只有 30 只，低于 DYNAMIC_MIN_STYLE_N=15 的风格还能用风格分位，但统计功效很低；应合并或回退全局。
- 增强债（secondary 1,701 + primary 713 + convertible 556 = 2,970，36.5%）被放在同一把“债券基金”尺子下，但它们的权益/转债暴露与纯债完全不同；虽然 L6E 有单独准入，报告仍把它们与稳健榜并列，容易误导。

### 6.2 风格内久期离散

以 2026-11-02 因子暴露、R²≥0.05、标准化 beta_duration 计：

| 风格 | n | p10 | p50 | p90 | p90-p10 |
|---|---:|---:|---:|---:|---:|
| bond_pure | 3,139 | 0.2716 | 0.6311 | 0.7350 | 0.4635 |
| bond_short | 757 | 0.2725 | 0.5156 | 0.6475 | 0.3750 |
| bond_index | 650 | 0.2310 | 0.6212 | 0.7071 | 0.4760 |
| bond_primary_enhanced | 627 | 0.1148 | 0.3528 | 0.6710 | 0.5562 |
| bond_secondary_enhanced | 1,431 | 0.0174 | 0.0903 | 0.2527 | 0.2353 |
| bond_rate | 186 | 0.4942 | 0.6622 | 0.7252 | 0.2310 |
| bond_credit | 63 | 0.507 | 0.701 | 0.764 | 0.257 |
| bond_convertible | 552 | -0.0297 | 0.0414 | 0.2498 | 0.2795 |

- bond_pure 内 p10 到 p90 的久期敏感度差 0.46 个标准化单位；bond_primary 差 0.56。也就是说，同一风格里既有短久期防御产品，也有长久期进攻产品。
- 在利率下行期，长久期产品天然收益更高；在利率上行期相反。把它们放在同一分位组，等于把“久期 beta”当成“管理能力”。
- 2021–2024 walkforward 的 IC 很高（0.29–0.43），很可能部分来自利率下行期的久期暴露，而不是选基技能。

### 6.3 分数与因子暴露

以 R²≥0.05 的 raw beta_duration（经济含义更直观）计，n=6,650：

- corr(composite, beta_duration_raw) = -0.0157；
- corr(rank_score, beta_duration_raw) = +0.0932；
- corr(rank_score, beta_convertible_raw) = -0.2345（同一 R²≥0.05 样本，n=6,650）。

解释：

- composite 基本不押久期；这是好事。
- rank_score 轻微偏向高久期、低转债；方向不强，但在利率上行期可能形成系统性回撤。
- 不能用标准化 beta 的相关系数（+0.16/+0.22）解释经济含义；标准化 beta 受组内分布影响，只适合比较风格内离散。

### 6.4 建议

1. 报告新增“风格 × 久期桶”矩阵：短（beta_duration <0.3）、中（0.3–0.6）、长（>0.6）；每格显示数量、平均 1y/3y 收益、MDD。
2. 正式榜的风格分位改为“风格 × 久期桶”分位，至少先做研究版；样本不足 15 时回退到风格层。
3. 对 bond_pure 增加“久期标签”，不改变筛选，只用于解释。
4. 增强债与纯债分开报告，不共用“稳健”标题。
5. 在报告里写明“同风格不等于同久期；分数未完全剔除久期 beta”。

## 7. 费用、份额与持有期

### 7.1 当前费用口径

fundana/fees/holding.py:26：

    holding_cost(h) = recurring_pct + (max_sub_fee_pct × 0.1 if has_discount else max_sub_fee_pct) / (h / 365)

- 只算 1 年（COST_HORIZON_DAYS=365）；cost_3y 仅展示；赎回费不入门槛。
- 申购费按 0.1 折扣摊到 1 年；对 3/5 年持有，申购费应摊得更薄。
- 结果是：A 类份额的长期优势可能被低估，也可能被高估；C 类份额的持续费劣势被短期化。不能按份额字母判断，必须按具体费率和持有期计算。

### 7.2 实例：同一 family 的份额成本

| 份额 | recurring | max_sub | 折扣 | 1y 成本 | 3y 成本 | 5y 成本 |
|---|---:|---:|---|---:|---:|---:|
| 006596 A | 0.40% | 0.60% | 是 | 0.46% | 0.42% | 0.412% |
| 028497 C | 0.41% | 0 | 否 | 0.41% | 0.41% | 0.41% |
| 007408 A | 0.40% | 0.60% | 是 | 0.46% | 0.42% | 0.412% |
| 007409 C | 0.80% | 0 | 否 | 0.80% | 0.80% | 0.80% |
| 014504 D | 0.40% | 0.80% | 是 | 0.48% | 0.4267% | 0.416% |

- 006596 A 与 028497 C：1 年 0.46% vs 0.41%，3 年 0.42% vs 0.41%，5 年 0.412% vs 0.41%，A 在 5 年内仍略贵。按当前费率，C 在 5 年内更便宜，约 6 年后 A 才持平；这说明“长期一定选 A”不成立，必须按具体费率和持有期算。
- 007408 A 与 007409 C 差距很大：1 年 0.46% vs 0.80%，3 年 0.42% vs 0.80%，5 年 0.412% vs 0.80%；A 在所有展示期限都更便宜。当前系统按 rank 保留份额，不按持有期成本选择；这里只是说明单看 cost_1y 时 A 也更便宜，但报告没有解释“为什么不是 C”。
- 014504 D 的 1/3/5 年成本 0.48%/0.4267%/0.416%，都高于同 family 的 007408 A（0.46%/0.42%/0.412%）；D 在当前费率下被 A 支配。不是所有 D 份额都更便宜，必须逐项算，不能按份额字母推荐。

### 7.3 盈亏平衡持有期

简化公式（忽略赎回费与收益差异）：

    break_even_years = (A_sub_fee - C_sub_fee) / (C_recurring - A_recurring)

以 006596 A vs 028497 C 为例：A 申购费 0.06%（折扣后）、C 持续费高 0.01%，盈亏平衡约 6 年。也就是说：

- 持有 <6 年，C 可能更便宜；
- 持有 >6 年，A 可能更便宜；
- 当前系统只按 1 年成本选份额，对长期投资者可能给出错误份额。

### 7.4 建议

1. 新增 recommended_share_class（按 1y/3y/5y 三个持有期分别推荐）与 break_even_years。
2. 报告展示同一 family 的 A/C/D 成本阶梯，不只展示入选份额。
3. 费用门槛改为“3 年成本 ≤ 1.50%”或“1 年 + 3 年双门槛”，避免只优化短期。
4. 赎回费单独展示；对 7 天内赎回惩罚、持有期 <30 天的产品加警示。
5. 不自动替换榜单份额；先展示建议，由用户按持有期选择。
6. 对机构份额（I/Y）与零售份额（A/C）分开；机构份额有最低申购额，不应直接推荐给零售用户。

## 8. 组合、榜单重叠与“独立 bet”

### 8.1 榜单重叠

- 七榜 257 行 / 203 唯一代码 / 194 family；49 个 family 出现 >1 次，最多 4 次。
- 五个榜（stable/enhanced/core/core10/rookie）196 行 / 142 唯一代码 / 136 family；48 个 family 出现 >1 次，最多 3 次；跨榜重复主要来自同一 family 的 A/C/E 份额。
- bond_core 46 行 / 46 family，44 与 stable 重叠。
- bond_core10 10/10 全部已在 stable；10 个 family 也全部已在 stable/enhanced。
- 跨榜 family cap 不存在；FAMILY_CAP=1 只在单榜内生效。

这意味着用户如果同时看 stable + core + core10，会看到大量重复；“46 只核心”并不是 46 个独立选择。

### 8.2 相关性

| 组合 | 平均两两相关 | 最大相关 | 有效 bet | 分散化比率 |
|---|---:|---:|---:|---:|
| core10（2026-11-02） | 0.7262 | 0.9305（006596/016575） | 1.327 | 0.1327 |
| core10（2026-11-03 快照） | 0.7568 | 0.9064（006596/016799） | 1.28 | — |
| core50（bond_core） | 0.7053 | 0.9701（008677/009541） | 1.406 | 0.0287 |
| formal 152 只基线（诊断时点；当前 L9 通过 138） | 0.6379 | 0.9992（005138/005139） | 1.562 | — |

- core10 的有效 bet 只有 1.33，意味着 10 只基金的实际分散效果接近 1–2 只。
- 最大相关 0.93 的配对，几乎是同一风险因子；用户以为买了 10 只，实际只买了 1 个久期/信用暴露。
- formal 152 只的最大相关 0.9992 是同一基金不同份额（005138/005139），说明份额去重仍不彻底。

### 8.3 建议

1. 报告明确写“core10 是稳健榜的集中观察组合，不是 10 个独立 bet；有效 bet 约 1.3”。
2. 跨榜 family cap：同一 family 在 stable/core/core10 中最多出现一次；或至少报告去重后的独立代码数。
3. P1 增加“组合体检”页：相关矩阵、有效 bet、风格/久期暴露、最大回撤贡献。
4. P2 改 core10 选择：贪婪分散（score - λ × max_corr），目标有效 bet ≥1.8、最大相关 ≤0.85；或直接改为“核心 5 + 卫星 5”。
5. 份额去重：同一 family 只保留推荐份额；其他份额在详情页展示，不占榜单行。

## 9. 数据、回测与统计证据

### 9.1 已有治理优势

- PIT 输入快照：replay-screen 能重放同一批次输入。
- 批次契约校验：batch_screen_contract_mismatch 防止跨契约混算。
- 发布门禁与跨源校验：16,104 行不一致率 0.0000%。
- 前瞻 cohort：冻结正式榜与同风格对照，按 30/90/180/365 天观察。

这些是“可复现性”优势，不是“有效性”证据。

### 9.2 12 队列复核（review2）

- v10 rank 对前瞻年化 IC +0.011；对前瞻 MDD IC -0.025。
- v10 预算年化 3.58%、MDD 1.65%；v9 3.75%/1.64%；基准 3.41%/1.55%。
- v10 只在 3/12 队列年化高于 v9，平均低 17.2bp。
- 结论：v10 的风险倾斜未获支持；rank 对收益几乎没有预测力，对 MDD 的方向甚至是“分数越高、回撤越大”（弱）。

### 9.3 Walkforward 的高 IC 与低 IC 差异

reports/analysis/audit_2026_11/walkforward-v4.json：

- 2021-06-30：rank_score 前 118 的前瞻年化 4.51%、命中率 61.9%、IC 0.4345。
- 2022-06-30：3.22%、64.4%、IC 0.2923。
- 2023-06-30：4.43%、61.9%、IC 0.4136。
- 2024-06-30：3.57%、71.2%、无 IC。

差异原因：

- walkforward 在“已过固定线”的子池内算 IC，样本被截断，且 2021–2024 是利率下行期，久期 beta 主导横截面。
- review2 的 12 队列含 2025、含更全的池，IC 接近 0。
- 两者不矛盾：高 IC 是“在特定利率环境、特定子池内”的横截面排序，不是跨周期的选基技能。

### 9.4 DSR 与多重检验

- DSR 0.660（n_trials=256），低于 0.95，不能拒绝“无技能”。
- n_trials=256 是近似，未覆盖所有规则变体；DSR 假设正态，债基收益有偏/尖峰。
- 结论：当前证据不足以支持“高分基金未来更好”的强主张；只能支持“低分基金更可能不可投/高费用/高回撤”。

### 9.5 前瞻 cohort 的成熟度

- 当前 2 个 cohort，0 个成熟；evidence_level=LEVEL_1。
- horizon 最长 365 天，无法验证 3–5 年。
- control_style 只含 L6C/L7/L8 失败者，不含 L1–L5 被排除者；对照是“同样可投但没选上”，不是全市场。
- 因此“正式榜跑赢对照”即使未来出现，也不能直接推广到“跑赢全市场”。

### 9.6 其他数据问题

- 规模 as-of 前视：engine._load_candidates 用 report_period <= as_of，未复用 fundana/fuse/scale.py:39 的 45 天披露滞后；回测在季末后 45 天内会用到尚未披露的规模。
- 幸存者偏差：清盘/合并基金从数据库消失，存续基金平均收益被抬高；当前没有回补。
- 风格前视：style_asof_map 历史表 2026-11 才有数据；更早回测用当前标签。
- 指标日硬门：548 只周更/低频基金被排除，扭曲分位母体。
- 持仓数据季报滞后：风险门用最新持仓，不是 PIT；季报公布前的调仓不可见。

## 10. 外部基准对照

### 10.1 监管与行业方法

| 来源 | 关键要求 | 对本项目的含义 | 级别 |
|---|---|---|---|
| 证监会《证券投资基金评价业务管理暂行办法》（证监会令第64号，2020年第177号修订） | 基金/管理人评级的评级期间不得少于 36 个月；同一分类少于 10 只不得评级/单一指标排名；合同生效不足 6 个月不得评奖/排名；坚持长期性原则 | 正式榜应显式要求 3 年；1–3 年产品只能进新锐榜；同一风格样本 <10 不应发布星级/排名 | A |
| 晨星（中国）基金评级方法 | 需 3 年以上业绩；提供 3/5/10 年评级，默认 3 年；季度更新；同类 <10 只不发布；MRAR(2) 惩罚下行波动 | 3 年是行业最低证据期；风险调整应惩罚下行，不只看波动；同类样本门槛合理 | B |
| 银河证券基金评级规则 | 成立至少满 3 年；建仓期 13 周不纳入；至少 169 周净值增长率；同类 ≥10 只；指数/货币不参与星级；定开/封闭与开放式分开 | 建仓期应剔除；定开/封闭单独赛道；指数债基不应与主动债基共用星级 | B |
| 银河分类 | 普通债券型分一级/二级；特定策略不参与同类型评级；定开/封闭与开放式分开 | 增强债、转债、指数债应分开报告；当前 9 风格方向对，但兜底 pure 过大 | B |

### 10.2 学术与行业研究

| 来源 | 结论 | 对本项目的含义 | 级别 |
|---|---|---|---|
| Blake, Elton, Gruber (1993), Journal of Business 66(3) | 债券基金扣费后跑输指数；费用每增加 1 个百分点，业绩约下降 1 个百分点；无证据用过去业绩预测未来 | 费用应作为硬约束；不要用 1 年收益外推长期；基准比较不可缺 | A |
| Morningstar, Alpha Persistence Among Bond Managers | 控制久期/高收益/抵押风险后，过去 12 个月 alpha 与未来 12 个月 alpha 正相关；过去 3 年 alpha 与未来 3 年相关；更长期优势消失 | 3 年证据有用，但不能外推到 5 年以上；必须控制久期/信用暴露 | B |
| 晨星《2025 年中国公募基金费率研究》 | 费率与基金未来生存能力、超额收益潜力显著相关；2024 年资产加权平均费率 0.76% | 费用门槛方向正确；应展示净费后收益与费率分位 | B |
| Campisi 归因（行业方法） | 持有收益=票息+价格收敛；价格收益=利率曲线管理+信用利差管理 | 报告可增加“收益来源”解释，但不能把归因当因果 | C |
| 幸存者偏差研究（Linnainmaa 2013 等） | 清盘/合并基金消失会抬高存续基金平均收益；科普文章常引用“约 1/10 基金活不过 4 年、1/3 活不过 10 年”，具体数字需以原始数据为准 | 回测必须回补清盘基金；当前结论可能偏乐观 | A/B |

### 10.3 对照结论

- **证据期**：行业普遍要求 3 年；本项目 L5 只要求 1 年，但 composite 隐含 3 年。应显式化，而不是“实际 3 年、文档 1 年”。
- **同类样本**：行业要求 ≥10 只；本项目 DYNAMIC_MIN_STYLE_N=15，方向更严；但 bond_credit 67 只、cash 30 只仍低于稳定分位所需。
- **风险调整**：晨星用 MRAR(2) 惩罚下行；本项目 Sharpe/Sortino/Calmar 混合，且 Sharpe 分子几何、分母算术。应统一口径或披露。
- **费用**：学术证据支持费用是负向预测因子；本项目只算 1 年、不含赎回费，长期费用信号不完整。
- **持续性**：学术证据支持 1–3 年 alpha 有一定持续性，但更长周期消失；本项目 12 队列 IC 接近 0，说明当前分数没有捕捉到这种持续性。
- **幸存者偏差**：本项目没有回补清盘基金，历史 IC 可能被高估；需要在报告中披露。

## 11. 结论：科学性、合理性、正确性

### 11.1 四问四答

| 维度 | 结论 | 理由 |
|---|---|---|
| 科学性 | **流程科学，验证不足** | 漏斗确定、可复现、有 PIT/批次冻结/发布门禁；但目标函数未定义，指标口径有方向性错误，缺少样本外技能证据 |
| 合理性 | **排除逻辑合理，选择逻辑不合理** | 可投性、锁定期、规模、费用上限符合直觉；但 8 条固定线中 7 条只看 1 年；composite 虽含 3y/5y，长期证据却没有独立门槛，与“长期优质”目标错配；动态层退化为一维 |
| 正确性 | **存在实质错误** | positive 把 0 收益记非正；composite 隐含 3 年；L7 只剩 composite；报告 rank 与 formal 矛盾；core10 不是独立 bet |
| 能否筛出真正优质、值得长期投资的债基 | **目前不能证明** | 12 队列 IC +0.011、DSR 0.660、前瞻 0 成熟；L9 通过 138 只全部 3 年以上，但报告没有 3/5 年表；增强榜 2024/2025 历史跑输（v9 审计数字） |

### 11.2 它能做什么

- 排除不可申购、锁定期、规模过小、费用过高、数据不可比的产品。
- 把 8,141 个份额缩到 138 只通过全部层级的候选，降低用户搜索成本。
- 提供风险提示（集中度、压力测试、经理任职、回撤）。
- 作为“候选池 + 尽调清单”，而不是“买入清单”。

### 11.3 它不能做什么

- 不能证明高分基金未来 3–5 年收益更高。
- 不能替代久期/信用配置决策；同风格内久期差异很大。
- 不能保证分散；core10 有效 bet 约 1.3。
- 不能处理清盘/合并/幸存者偏差。
- 不能把 1 年费用当成长期费用；A/C 份额选择需要持有期。
- 不能把“0 block”当成“无风险”；风险门只覆盖部分尾部风险。

### 11.4 一句话判断

**这是一套合格的风险过滤器与候选池生成器，不是经过验证的“优质基金发现器”。** 如果目标是“帮用户排除明显不合适的产品，并给出可尽调的候选”，它有价值；如果目标是“告诉用户哪 10 只基金未来 3–5 年最值得买”，当前证据不支持。

## 12. v10 声明与实现对照

| 文档/声明/假设 | 实现或证据 | 差异 | 严重度 |
|---|---|---|---|
| “L1–L9 漏斗” | 代码有 L1–L9，但 L7 实际只由 composite 约束 | 名义 7 维，实际 1 维 | 高 |
| “8 条固定线稳健筛选” | mdd 通过率 85.6%、vol 99.94%；composite 一家独大 | 不是 8 维稳健筛选 | 高 |
| “动态分位 + k=4” | 201 条 L7 全部只失败 dyn_composite；k=4 从未咬合 | 规则退化 | 高 |
| “L6D 单一原因缓释” | 992 检查 1 放行；composite≥80 接近不可达 | 接近死代码 | 中高 |
| “L5 成立 1 年即可” | composite≥58 隐含 3 年；L9 通过 138/138 都是 3 年+ | 文档与实现不一致 | 高 |
| “L8R 独立风险门” | 仅 2 条触发；watch 不阻断 | 覆盖有限，不能当安全证明 | 中 |
| “core10 核心组合” | 10/10 已在 stable；有效 bet 1.327；最大相关 0.93 | 不是独立 bet | 高 |
| “v10 风险倾斜假设” | 12 队列 v10 平均低 17.2bp，仅 3/12 更高 | 未获支持 | 高 |
| “前瞻 cohort 验证” | 2 个 cohort、0 成熟、LEVEL_1 | 尚未验证 | 中 |
| “DSR 0.660” | 低于 0.95 | 不能拒绝无技能 | 高 |

## 13. 优化路线图

### 13.1 P0：先修正确性，再谈策略

| 编号 | 任务 | 文件/范围 | 验收标准 | 预估 |
|---|---|---|---|---|
| P0-1 | 修正正收益占比：新增 flat_ratio_pct、non_negative_ratio_pct；门槛改 non_negative；positive 只展示 | fundana/metrics/risk.py、contract.py、migration 0032、tests/test_metrics.py | 常数 0 序列 positive=0、non_negative=100、flat=100；12 队列重算；选择性变化 <2pp | 3–5 天 |
| P0-2 | 显式 3 年门槛：正式榜要求 is_full_3y；1–3 年只进 rookie；screen_run 记录 formal_pool/rookie_pool | engine.py、thresholds.py、docs/screening-rules.md、tests/test_screen.py | 正式榜 100% is_full_3y；rookie 100% 1–3 年；文档与实现一致 | 2–3 天 |
| P0-3 | 动态阈值改为无条件 q70；记录 binding 次数；关闭或重校准 L6D | engine.py:690-745、relief.py、screen_threshold | binding 统计可见；若 L7 仍 100% 只失败 composite，记录并决定删除 6 维或改 k=5；缓释放行率披露 | 3–5 天 |
| P0-4 | 指标日容忍 0–7 天；记录 metric_stale_days；>7 天阻断 | engine.py:237-242、freshness.py | 548 只中多数回到母体；分位分布变化可解释 | 2 天 |
| P0-5 | 规模 as-of 加 45 天滞后；记录 scale_source/scale_asof；profile_current 禁入正式榜 | engine.py:109-145、fuse/scale.py:39 | 回测不再前视；正式榜 scale_source 全为 report | 2–3 天 |
| P0-6 | 报告排序与展示分一致：同时显示 rank_score/formal_score；发布检查 Spearman | builder.py:630-635、publish gate | Spearman(rank, rank_score) ≥0.99；用户能看到两把尺 | 2 天 |
| P0-7 | 报告加“候选池不是买入建议”与 IC/DSR/LEVEL_1 披露 | builder.py summary、docs | 每份报告首屏有声明；数字与审计一致 | 1–2 天 |

P0 总预估：15–22 个工作日；必须先做 P0-1/P0-2/P0-3，再做 P0-6/P0-7。

### 13.2 P1：让报告可解释、可选择

| 编号 | 任务 | 验收标准 | 预估 |
|---|---|---|---|
| P1-1 | 报告 v2 十节模板 | 每份报告含一页结论、漏斗、长期核心、稳健/增强、组合体检、费用/持有期、风险/反面证据、证据等级、方法附录 | 5–7 天 |
| P1-2 | 3y/5y 长期表 | 每只 top10 展示 3y/5y 年化、MDD、Sharpe、Calmar、覆盖、经理任职、份额、3y/5y 成本 | 3 天 |
| P1-3 | why/caveat | 每只 top10 有“为什么选它/它不适合谁”；由规则命中与风险门生成，不写泛泛文案 | 4 天 |
| P1-4 | A/C 份额建议 | 展示 1y/3y/5y 成本、break_even_years、推荐份额；不自动换榜 | 3 天 |
| P1-5 | 组合体检 API/页 | 相关矩阵、有效 bet、风格/久期暴露、最大回撤贡献 | 4 天 |
| P1-6 | 跨榜 family cap | 同一 family 在 stable/core/core10 最多一次；报告去重后独立代码数 | 2 天 |
| P1-7 | bond_long 长期核心榜 | 3y 完整 + 5y 覆盖 ≥60% + 经理任职 ≥730 天 + 份额 ≥2 亿 + 核心风格；配额 20 | 5 天 |
| P1-8 | API/CSV/Web 字段 | /rankings?board=bond_long、/funds/<code>/decision 的 why/share_class_compare、/portfolio/diagnostics、/report/current、/methodology | 5–7 天 |

bond_long 评分建议：0.6 × long_formal + 0.4 × long_core；long_core 要求 5y 覆盖 ≥60%；风格/公司/家族 cap 与 core 一致；先做影子榜，不替换 stable。

P1 总预估：25–35 个工作日。

### 13.3 P2：让策略可验证、可进化

| 编号 | 任务 | 验收标准 | 预估 |
|---|---|---|---|
| P2-1 | 久期/信用桶 | 覆盖 ≥60%、R²≥0.05；风格×久期分位；样本不足回退 | 5–7 天 |
| P2-2 | core10 贪婪分散 | 目标有效 bet ≥1.8、最大相关 ≤0.85；对比等权/分数前 10 | 4–5 天 |
| P2-3 | 季度指标快照 12 队列 | 真正 PIT 的 3y/5y 指标；可复现 12 队列 | 8–10 天 |
| P2-4 | 清盘/合并回补 | 回测含清盘基金；幸存者偏差披露 | 5–7 天 |
| P2-5 | DSR/多重检验协议 | n_trials 口径固定；DSR>0.95 才发布“有技能” | 3 天 |
| P2-6 | 基准与归因 | 同风格基准、Campisi/因子归因；报告展示超额 | 5–7 天 |

P2 总预估：30–40 个工作日。

### 13.4 不建议现在做的事

- 不要继续调 composite 权重来“优化”榜单；在 P0 修正前，调参只会过拟合。
- 不要增加更多 1 年期固定线；边际信息很低。
- 不要把增强债并入稳健榜；风险属性不同。
- 不要用 2021–2024 walkforward 的高 IC 证明策略有效；那是特定利率环境 + 截断子池。
- 不要在没有 12 队列重算前宣称 v11 更优。

## 14. 报告 v2 设计规格

### 14.1 设计原则

1. **先结论，后证据**：第一页回答“这份报告能做什么、不能做什么、今年最值得看什么”。
2. **候选池，不是买入清单**：每份报告首屏必须有“这不是投资建议；榜单是尽调候选”的声明。
3. **长期优先**：正式榜只展示 3 年以上；1–3 年单独新锐榜；5 年证据不足要标注。
4. **可解释**：每只 top10 有“为什么选它/它不适合谁”；不能只有分数。
5. **反面证据**：把 IC、DSR、前瞻 LEVEL_1、增强榜跑输、core10 高相关写进报告，不藏在附录。
6. **持有期**：A/C 份额、赎回费、锁定期、建议持有期必须展示。
7. **可复现**：报告附 run_id、batch_id、契约版本、生成时间、证据包路径。

### 14.2 十节模板

| 节 | 标题 | 内容 | 数据来源 |
|---|---|---|---|
| 1 | 一页结论 | 本期候选数、正式榜数（stable+enhanced）、长期核心数、最重要的 3 条变化、3 条风险提示 | screen_run、board_diff |
| 2 | 数据与范围 | universe、排除项、as_of、覆盖率、数据缺口 | freshness、coverage |
| 3 | 漏斗 | L1–L9 数量与原因；最大淘汰层；与上期对比 | screen_decision |
| 4 | 长期核心榜 | 3y/5y 年化、MDD、Sharpe、Calmar、经理任职、规模、费用、证据等级 | fund_metric、quality |
| 5 | 稳健榜/增强榜 | 分风格前 10；增强债单独一节；标注“增强债不是纯债” | board_picks |
| 6 | 组合体检 | 相关矩阵、有效 bet、风格/久期暴露、最大回撤贡献、重复份额 | portfolio_diagnostics |
| 7 | 费用与持有期 | A/C/D 成本阶梯、break_even、赎回费、锁定期、建议持有期 | fee_meta、holding_policy |
| 8 | 风险与反面证据 | 风险门 watch/block、压力测试、IC/DSR、前瞻 LEVEL、增强榜 2024/2025 历史跑输（v9 数字） | risk_gate、stat_validation、forward |
| 9 | 证据等级与统计显著性 | 每只基金 evidence_level；组合 DSR；12 队列 IC；不能拒绝无技能 | stat_validation、walkforward |
| 10 | 方法与附录 | 公式、阈值、来源、查询、复现命令、免责声明 | docs、queries.md |

### 14.3 长期核心表字段

以下为字段示例；基金与数值均为虚构，仅用于说明格式，不代表真实产品。

| 字段 | 说明 | 示例 |
|---|---|---|
| 排名 | 按 long_rank 排序 | 1 |
| 代码/名称 | 基金代码与名称 | 000000 示例纯债 A（虚构） |
| 份额 | 推荐份额（A/C/D） | A |
| 3y 年化 | 3 年几何年化 | 4.21% |
| 3y MDD | 3 年最大回撤 | 0.85% |
| 3y Sharpe | 3 年 Sharpe | 2.31 |
| 3y Calmar | 3 年 Calmar | 4.95 |
| 5y 年化 | 5 年几何年化（不足则“—”并标注） | 4.05% |
| 5y MDD | 5 年最大回撤 | 1.62% |
| 5y Sharpe | 5 年 Sharpe | 2.08 |
| 经理任职 | 当前经理在本基金任职天数 | 1,245 天 |
| 规模 | family/份额规模 | 86.4 亿 / 12.3 亿 |
| 1y/3y/5y 成本 | 持有成本 | 0.46%/0.42%/0.412% |
| 证据等级 | A/B/C + 覆盖 | A（3y 100%、5y 100%） |
| 风险状态 | ok/watch/block | ok |
| 为什么选它 | 2–3 条规则命中 + 1 条风险提示 | 3y Calmar 前 10%、回撤 0.85%、经理任职 3.4 年 |
| 它不适合谁 | 1 条反向说明 | 不适合追求 1 年以上高收益；久期偏短 |

### 14.4 份额成本表

示例数值取自 §7.2，仅用于说明格式。

| 份额 | 1y 成本 | 3y 成本 | 5y 成本 | break-even | 推荐持有期 | 备注 |
|---|---:|---:|---:|---:|---|---|
| A | 0.46% | 0.42% | 0.412% | 6.0 年 | >6 年 | 有申购费折扣 |
| C | 0.41% | 0.41% | 0.41% | — | <6 年 | 无申购费，持续费高 |
| D | 0.48% | 0.4267% | 0.416% | —（被 A 支配） | 不推荐 | 1/3/5y 均高于 A |

规则：

- 推荐份额只做展示，不自动替换榜单。
- 若份额成本差 <0.05pp，标注“差异不显著”。
- 赎回费单独列；7 天内赎回惩罚用警示色。
- 机构份额（I/Y）标注最低申购额，不默认推荐。

### 14.5 组合体检节

必须包含：

1. 相关矩阵（top10 两两相关；高相关 >0.85 标红）。
2. 有效 bet（1/(w'Σw) 或等价指标）；目标 ≥1.8，当前 1.3。
3. 风格/久期暴露：9 风格计数 + 久期桶（短/中/长）。
4. 最大回撤贡献：每只基金对组合 MDD 的边际贡献。
5. 重复份额：同一 family 的多份额只保留推荐份额，其余折叠。
6. 结论句：“这 10 只不是 10 个独立 bet；有效 bet 约 1.3，最大相关 0.93；如果你要分散，需要补充低相关资产。”

### 14.6 风险与反面证据节

必须写：

- 风险门 watch/block 数量与个案；watch 不阻断，block 才阻断。
- 压力测试：2022Q4、2020H1、2016Q4、近 5 年最差 60 日。
- 12 队列 IC：+0.011（年化）、-0.025（MDD）。
- DSR：0.660，不能拒绝无技能。
- 前瞻 cohort：2 个、0 成熟、LEVEL_1。
- 增强榜 2024/2025 相对同风格 -2.30pp/-3.60pp（v9 数字，标注来源）。
- 幸存者偏差：未回补清盘基金，历史收益可能偏乐观。
- 结论句：“以上证据不支持‘高分基金未来 3–5 年必然更好’；榜单是候选池，不是收益保证。”

### 14.7 report.json（schema_version=2）

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| schema_version | int | 2 |
| run_id / batch_id / contract | string | 可复现标识 |
| generated_at | string | ISO 时间 |
| as_of | string | nav_as_of |
| universe / base_pass / formal / long_core | int | 数量 |
| sections | array | 10 节，每节 title/body/table/chart |
| boards | object | stable/enhanced/core/core10/long/watch/locked/rookie |
| picks | array | 每只基金的 why/caveat/evidence/share_class |
| portfolio | object | corr/effective_bets/style/duration/mdd_contrib |
| evidence | object | ic/dsr/forward_level/cohorts |
| disclosures | array | 免责声明、数据缺口、方法限制 |
| checks | object | publish gate 结果 |

### 14.8 发布门禁

| 检查 | 条件 | 动作 |
|---|---|---|
| 排名一致性 | Spearman(rank, rank_score) ≥0.99 | block |
| 正式榜 3 年 | formal 100% is_full_3y | block |
| 新锐榜期限 | rookie 100% 1–3 年 | block |
| 份额去重 | 同 family 同榜最多 1 行 | block |
| 跨榜 family cap | stable/core/core10 同 family 最多 1 次 | warn |
| 风险门 | block 数 >0 时报告必须列个案 | block |
| 前瞻成熟度 | LEVEL_1 时必须显示“未验证” | block |
| DSR | <0.95 时必须显示“不能拒绝无技能” | block |
| 数据缺口 | 5y 覆盖 <60% 的基金不得进长期核心 | block |
| 费用口径 | 必须展示 1y/3y/5y 成本 | warn |

### 14.9 API/CSV/Web

API：

- GET /api/v1/rankings?board=bond_long
- GET /api/v1/funds/<code>/decision?include=why,caveat,share_class
- GET /api/v1/funds/<code>/share-classes
- GET /api/v1/portfolio/diagnostics?board=bond_core10
- GET /api/v1/report/current?format=json|md
- GET /api/v1/methodology

CSV 新增列：

- long_rank、evidence_level、why_selected、caveat、recommended_share_class、break_even_years、cost_1y/3y/5y、flat_ratio_pct、non_negative_ratio_pct、duration_bucket、family_member_count。

Web 新增 tab：

- 长期核心（bond_long）
- 组合体检（相关矩阵/有效 bet）
- 方法与证据（IC/DSR/LEVEL）
- 份额与持有期（A/C 对比）

### 14.10 why/caveat 生成规则

why（正向，最多 3 条）：

- 3y Calmar 在风格内前 10%；
- 3y MDD ≤ 风格 p25；
- 经理任职 ≥3 年；
- 费用 ≤ 风格 p25；
- 5y 覆盖 ≥90%。

caveat（反向，至少 1 条）：

- 久期 beta >0.6（利率上行敏感）；
- 规模 <5 亿（清盘/流动性）；
- 经理任职 <2 年；
- 5y 覆盖 <60%；
- 风险门 watch（集中度/转债/压力测试）；
- 近 1 年收益低于风格中位数。

禁止：

- 不使用“稳健”“优秀”“放心买”等绝对化词；
- 不写“未来会涨”；
- 不把 IC/DSR 负面结论藏在附录。

### 14.11 报告验收

- 用户能在 30 秒内回答：这份报告是什么、不是什么、今年候选有多少、最重要风险是什么。
- 每只 top10 能回答：为什么选它、它不适合谁、买哪个份额、持有多久。
- 每份报告能回答：如果榜单错了，证据在哪里。
- 所有数字可追溯到 run_id/CSV/查询。

## 15. 验收、复现与回滚

### 15.1 阶段验收

| 阶段 | 验收 | 不通过的处理 |
|---|---|---|
| P0 | 单元测试全绿；12 队列重算完成；正式榜 100% 3 年；L7 binding 统计可见；报告 rank 一致 | 不回滚生产，只回滚单个迁移；保留旧 run |
| P1 | 报告 v2 生成成功；top10 why/caveat 非空；A/C 表完整；组合体检 API 200 | 以 feature flag 关闭新节，保留旧模板 |
| P2 | 久期桶/贪婪分散/季度快照在 12 队列上不劣于 v10；DSR 协议可复现 | 只研究版，不进生产 |

### 15.2 测试

- tests/test_metrics.py：常数 0 序列、全正序列、全负序列、平坦日占比、non_negative。
- tests/test_screen.py：正式榜 3 年、rookie 1–3 年、动态阈值无条件 q70、binding 统计、relief 关闭。
- tests/test_publish.py：rank/score 一致性、报告节存在、why/caveat 非空、发布门禁 block。
- tests/test_api.py：/rankings?board=bond_long、/portfolio/diagnostics、/report/current。
- tests/test_holding_cost.py：A/C 1y/3y/5y、break_even、无折扣。

### 15.3 复现

    cd "$(git rev-parse --show-toplevel)"
    python3 -m fundana screen --profile bond_v1
    python3 -m fundana publish
    python3 reports/analysis/audit_2026_11_review3/generate_evidence.py

证据包：reports/analysis/audit_2026_11_review3/（funnel.csv、positive_ratio_bias.csv、composite_distribution.csv、l6c_reasons.csv、l7_binding.csv、style_duration.csv、board_overlap.csv、share_class_cost.csv、external_sources.md、queries.md、reproduce.sh）。

### 15.4 回滚

- 迁移 0032/0033/0034 均可逆：新增列可空，不删旧列；回滚只停写新列。
- 报告 v2 用 feature flag：report_v2=true/false；false 时走旧模板。
- 新榜 bond_long 用独立 board key，不影响旧榜。
- 任何 P0 修改必须先跑 12 队列影子，不能直接替换生产榜。

## 16. 附录

### 16.1 外部来源

监管：

- 证监会《证券投资基金评价业务管理暂行办法》：https://www.gov.cn/zhengce/2021-12/16/content_5724599.htm
- 证监会发布页：https://www.csrc.gov.cn/csrc/c106256/c1653866/content.shtml

行业方法：

- 晨星（中国）基金评级方法：https://www.morningstar.cn/help/data/fundrating.html
- 晨星评级方法 PDF：https://www.amac.org.cn/fwdt/wyc/jjpjcx/jjpjyw_jjpjiang/jjpj_cx/cx_xxpl/202004/P020231126392159855282.pdf
- 银河证券基金评级规则：https://www.amac.org.cn/fwdt/wyc/jjpjcx/jjpjyw_jjpji/jjpji_yhzq/yhzq_pjff/202004/P020231126388832941420.pdf
- 银河证券基金评级规则（续）：https://www.amac.org.cn/fwdt/wyc/jjpjcx/jjpjyw_jjpji/jjpji_yhzq/yhzq_pjff/202004/P020231126388831615966.pdf
- 银河分类说明：https://www.amac.org.cn/fwdt/wyc/jjpjcx/jjpjyw_jjpji/jjpji_yhzq/yhzq_xxpl/202501/P020250123528681947043.pdf

学术/研究：

- Blake, Elton, Gruber (1993)：https://ideas.repec.org/a/ucp/jnlbus/v66y1993i3p370-403.html
- Morningstar, Alpha Persistence Among Bond Managers：https://www.morningstar.com/content/dam/marketing/shared/pdfs/Research/Alpha_Persistence_Among_Bond_Managers_final.pdf
- 晨星《2025 年中国公募基金费率研究》：https://www.morningstar.cn/articles/e3cc5b5b5756f39349bd6e6f27657101.html
- Campisi 归因（行业方法）：https://wenku.baidu.com/view/5769ad246f85ec3a87c24028915f804d2b1687d0.html

幸存者偏差：

- https://www.assetlibrary.net/asset-basics/historical-return-predict-future/
- https://ag.yueniuzq.com/fund/survivorship-bias-in-fund-rankings/

### 16.2 关键文件与行号

- fundana/screen/engine.py:36 _load_candidates；:109-127 规模 SQL；:176 _attach_cost；:215 base_gate；:237-242 指标日；:388 coverage_meta；:408-442 score_candidate；:467 fixed_checks；:690-745 dynamic_thresholds；:715-723 comp_pool；:796 apply_dynamic；:848 dynamic_gate；:1087 run_screen；:1362 _select_concentrated；:1435 _rank。
- fundana/screen/thresholds.py:64 FIXED；:92 DYNAMIC_QUANTILES；:117 HYSTERESIS；:143 STYLE_FLOORS；:167 ENHANCED_ADMISSION；:189 ENHANCED_RELATIVE；:226 SHORT_CORE_WEIGHTS；:233 LONG_CORE_WEIGHTS；:237 SHORT_FORMAL_WEIGHTS；:240 LONG_FORMAL_WEIGHTS；:254 RANK_WEIGHTS；:293 CORE_STYLES；:294 CORE_STYLE_CAPS；:319 GRADE_BANDS。
- fundana/metrics/risk.py:104 positive_ratio；:113 sharpe；:119 sharpe_t；:127 sortino；:135 calmar。
- fundana/metrics/window.py:56 build_window；:123 reconstruct_return；:170 resolve_returns。
- fundana/fees/holding.py:26 holding_cost；fundana/fees/model.py:97 _trailing_return_pct、:127 quote。
- fundana/fuse/scale.py:39 scale_asof_map。
- fundana/publish/builder.py:54 collect_rows；:514 _summary_md；:630-635 稳健榜前十。
- docs/data-catalog.md:71 scale_asof_map 45 天；docs/decisions.md ADR-094…101；docs/screening-rules.md §12 18 条局限。

### 16.3 查询

所有 SQL 与复算逻辑在 reports/analysis/audit_2026_11_review3/generate_evidence.py；查询说明在 queries.md；一键复现为 reproduce.sh。核心查询包括：

- 漏斗终态与原因码：screen_decision + screen_run。
- 固定线通过率：thresholds_json.fixed_or_admission。
- composite 分布：screen_decision.scores_json。
- 正收益偏差：fund_metric 365 天 + fund_nav_daily 日收益。
- 风格久期：fund_factor_exposure + fund_style。
- 榜单重叠：screen_pick + fund_share_class。
- 份额成本：fee_meta + holding_cost 公式。

### 16.4 限制

- 单一时点 run，不是 12 队列全量复算；12 队列数字来自 review2。
- 外部研究以美国市场为主；中国债基的久期/信用结构与美国不同。
- 持仓数据季报滞后；风险门不是 PIT。
- 没有清盘基金回补；历史收益可能偏乐观。
- 没有真实 3–5 年前瞻；前瞻 cohort 0 成熟。
- 本审计不评价数据供应商原始准确性；跨源校验 0 不一致率只说明内部一致。

## 17. 最后判断与建议

### 17.1 对用户的直话

- 这份榜单可以帮你**少看 95% 的产品**，但不能告诉你**哪一只未来 3–5 年最赚钱**。
- 如果你要的是“稳健、低回撤、费用合理、经理稳定”的候选，它有价值；如果你要的是“长期高收益”，它目前没有证据。
- L9 通过的 138 只全部 3 年以上；但报告没有 3/5 年表，这是产品缺陷，不是用户的理解问题。
- core10 不是 10 个独立 bet；有效 bet 约 1.3，最大相关 0.93；不要把它当分散组合。
- 增强榜 2024/2025 历史跑输同风格（v9 审计数字，非本轮复算）；不要因为“增强”两个字就认为收益更高。

### 17.2 未来 30 天建议

1. 第 1 周：修 P0-1（positive/flat）与 P0-2（显式 3 年）；跑 12 队列影子。
2. 第 2 周：修 P0-3（动态阈值/缓释）与 P0-4（指标日容忍）；出对比报告。
3. 第 3 周：修 P0-5（规模滞后）与 P0-6/P0-7（报告一致性/披露）；发布 v10.1。
4. 第 4 周：上报告 v2 前 4 节（结论/数据/漏斗/长期核心）；收集用户反馈。

### 17.3 成功标准

- 用户能说清楚“这份报告是候选池，不是买入建议”。
- 每只 top10 有 why/caveat/份额/持有期。
- 正式榜 100% 3 年以上；长期核心 100% 5 年覆盖 ≥60%。
- 报告首屏披露 IC/DSR/LEVEL，不再把“0 block”当安全证明。
- 12 队列影子显示 P0 修复后选择性变化 <2pp，且没有把 2024/2025 的跑输扩大。

### 17.4 一句话

**先把“正确性”修好，再把“长期证据”补上，最后才谈“优质基金发现”。** 当前版本最大的价值不是选出了谁，而是把不可投、锁定期、规模过小、费用过高、数据不可比的产品挡在了外面；下一步要把“挡错的”和“选对的”分开验证。




