# 债券型基金筛选策略第四轮审查（v11，2026-11-05）

- 审计日期：2026-11-05
- 审计对象：screen-bond-v11-2026.12.7 / profile bond_v1 / formula fundana-risk-2026.11.5
- 数据基线：run 20261105T073220-screen-0bb3f1；publication generation 55；publication_id a7619d6a0ef7467c95ecdada8f36e414；nav_as_of = metric_as_of = 2026-11-04
- 代码基线：P0/P1/P2 完成后的工作区；python3 -m unittest discover -s tests 为 567 项通过；schema 36 个迁移
- 说明：本文件保留 2026-11-05 审计时的 generation 55 基线；数据补齐后的最新运行态（generation 67、生命周期 1,819、历史持仓 1,900,098 行/7,928 只）见文末“2026-11-05 后续更新”。
- 审计性质：代码、数据库、运行态与公开资料的综合审计；P0 修复已落地，P1/P2 只给规格与路线，不在本轮切换生产阈值
- 结论一句话：**这套系统已经是一套可运行、可复现、治理良好的“风险过滤器 + 候选池生成器”，但还不是经过验证的“优质基金发现器”。它可以帮用户排除不可投、锁定期、规模过小、费用过高、数据不可比、持仓/股票暴露不清的产品；它目前不能证明高分基金未来会更好，因此榜单不能包装成评级、单一指标排名或买入建议。**

## 0. 执行摘要

### 0.1 四个问题的直接回答

| 问题 | 结论 | 主要依据 |
|---|---|---|
| 筛选流程是否科学？ | 部分科学 | 漏斗确定、可复现、有 PIT 与批次冻结、有跨源校验；但目标函数没有写成可检验的假设，固定线以 1 年为主，composite 是相对分位而非绝对质量分 |
| 是否合理？ | 部分合理 | L2 可投性、L3 锁定期、L4/L5 规模、L5.5 费用上限、L8R 风险门方向正确；但风格化硬线没有落到 L6C，L7 动态层实际不工作，增强债与稳健债的准入口径不同却被报告混写 |
| 是否正确？ | 仍有实质性口径问题，P0 已修一批 | 已修：formal_count 159 vs 正式榜 125、L8R 显示“未进入筛选”、增强债被写成“通过固定线”、1.30% 与 0.90% 混用、cohort 3 vs 4、core10 有效 bet 目标未披露；仍需 P1 处理：固定线风格化、动态层退化、真实基准缺失、季度数据不刷新 |
| 能否筛出真正优质、值得长期投资的债基？ | **目前不能证明** | 12 队列前瞻 rank 对年化 IC 仅 +0.011、对 MDD IC -0.025；DSR 0.660（n_trials=256）不能拒绝“无技能”；前瞻 cohort 4 个、成熟正式观测 0；lifecycle 已入库 1,819 只清盘样本（债券型 510，其中 12 只有历史净值），但 walkforward 历史回测宇宙仍从当前 fund 表构造，无法纳入已移除的清盘基金，历史 IC/DSR 的幸存者偏差仍未校正；2026-11-06 已回填 445 只并做对照，见 §13 |

### 0.2 五条最重要的发现

1. **固定线几乎不筛人，composite 是事实上的主淘汰器。** 最新 run 的 L6C 失败者 1,793 只中，composite 原因出现 1,743 次（97.2%）；annual 560、sharpe 420、history_3y 355、non_negative 342、calmar 67、fee 64、mdd 24、vol 1。九项固定线里，除 composite 与 history_3y 外，其余 7 项都是 1 年窗口或 1 年成本。结果是“综合分既是裁判，又是唯一真正在筛人的门槛”。
2. **composite 是相对分位，不是绝对质量分。** fundana/screen/scoring.py:12-32 的 percentile_ranks 返回 0–100 分位；composite 是“可投域内百分位加权”，composite≥62 实际是“位于当日可投池前约 38%”，会随池子构成漂移。固定线中只有 annual_1y≥2%、MDD≤2%、vol≤6%、cost≤0.90% 等是绝对约束；报告必须把“相对分位”与“绝对底线”分开写。
3. **L7 动态层仍然不工作。** 最新 run 的 q70 = 55.52 < 固定线 62，dynamic_inactive=true；L7 的 27 只全部因 dyn_min_dimensions（5/6 维）失败，动态层没有提供独立信息，只是固定线的影子。
4. **增强债与稳健债是两条准入路径，不能共用“固定线”文案。** 稳健榜走 L6C 九项固定线（含 fee≤0.90%）；增强榜走增强债相对准入（同风格分位+地板+严格 7 项至少 5 项+正式分≥55），不要求 70%/2%/0.90% 绝对线。015479 的 1 年非负日占比 64.46%，仍可进增强榜；P0 已把报告与 explain 文案改成“增强债相对准入”，不再声称“通过正式榜固定线”。
5. **没有证据表明它能发现长期优质基金。** 12 个季度队列的 rank 对前瞻年化 IC +0.011、对前瞻 MDD IC -0.025；DSR 0.660（n_trials=256）；前瞻 cohort 4 个、成熟正式观测 0；lifecycle 表 1,819 行（清盘，债券型 510）。这些数字说明系统可以排除风险，但不能证明“高分未来更好”。

### 0.3 必须保留的优点

- universe 口径清楚：债券型 + 指数型-固收，8,141 个份额；QDII 债基 166、偏债混合 1,475 明确不纳入，避免把不同风险属性的产品混在一起。
- 三个日期分离：business_date、nav_as_of、metric_as_of；14 天回看、覆盖率 ≥90% 的最新有效净值日，解决了“今天只有少数基金披露昨日净值”的新鲜度问题。
- L2 可投性、L3 锁定期、L4/L5 规模、L5.5 费用上限，方向正确；这些是“排除不合适产品”，不是“预测收益”。
- 批次冻结、契约一致性、PIT 输入快照、发布门禁、跨源校验，是治理优势；它们保证“同一批次可重放”，但不保证“选得准”。
- 独立风险门（L8R）与 long_core 的 3y/5y 风险权重，方向正确；P0 又把陈旧持仓、增强债无持仓、股票暴露缺失收紧为 block，风险门比上一轮更可信。
- P0 之后报告开始披露“L9 通过 → family/company 折叠 → 正式发布”的全链路数字，以及数据时点、准入路径、core10 有效 bet 目标；这是“说真话”的基础。

## 1. 审计范围、数据与方法

### 1.1 数据范围（最新 generation 67；审计基线为 generation 55）

| 项目 | 数值 |
|---|---|
| 数据库 | var/db/fundana.db，约 6.8 GB |
| 净值行数 | 9,929,590 行，2002-09-20 至 2026-11-04 |
| universe | 8,130 个份额 |
| 基础门禁通过 | 3,021 |
| L9 通过 | 159 |
| family/company 折叠 | 34 |
| 正式榜发布 | 125（稳健 97 + 增强 28） |
| 观察榜 | core 50 / core10 10 / long 20 |
| 研究/受限/新锐 | watch 95 / locked 15 / rookie 30 |
| 指标周期 | 9 个周期（7/30/90/180/365/730/1095/1460/1825 天），每周期 16,086 行 |
| 前瞻 cohort | 4 个（2026-10-30、11-01、11-03、11-04），成熟正式观测 0 |
| 测试 | 567 项通过；schema 36/36 |

### 1.2 审计方法

1. **代码审读**：L1–L10、指标、评分、阈值、费用、风格、风险门、报告、API、前端、测试。
2. **全量 SQL 复算**：不使用抽样；对漏斗、失败原因、分位、相关性、榜单重叠、份额成本逐项复算。
3. **运行态核验**：systemd --user、scheduler heartbeat、run/publication/tracker 表、data-health、verify、alerts、quality。
4. **外部对标**：只使用公开监管规则、评级机构方法、行业协会研究；按 A/B/C 分级，不把行业方法当因果证据。
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

## 2. 运行态：日更、发布与榜单是否正常

### 2.1 当前状态

- systemd --user 的 fundana-web.service 与 fundana-scheduler.service 均 active；网页 http://127.0.0.1:8091/ 健康检查 OK。
- scheduler 工作日 07:30 运行；heartbeat last_run_at=2026-11-05T07:33:24，last_status=ok，next_run_at=2026-11-06T07:30。
- 2026-11-04、2026-11-05 两次真正 07:30 定时日更成功；2026-11-03 是 20:33/20:38 手工 force；2026-11-02 skipped；更早日更多是手工。不能把“最近两天成功”当作长期稳定。
- 最新 publication generation 67（run 20261105T214346-screen-f4a862），296 个文件；verify ok=true，checked=296，problems=[]，gate=warning。
- gate 5 条 warning：forward_not_ready（成熟 0）、valuation_unavailable（fundgz 下线）、dynamic_boundary（正式榜综合分在 62±1 内占比>10%）、cross_board_family、dsr_skill（DSR 0.660<0.95）。`lifecycle_coverage` 已因 1,819 只 EID 清盘样本转为 pass。
- 新增 formal_count_fold pass：“L9 通过 159；family/company 折叠后发布 125；折叠 34”。新增 data_staleness pass：持仓 2026Q3/35 天、费率 2026-11-05、规模 2026-11-03/1 天、profile 2026-11-05。

### 2.2 哪些是正常的

- 日更主链（directory → status → index → NAV 快照 → 跨源核对 → metrics → factors → MRAR → fuse → screen → freeze → publish → tracker → forward/shadow）在 11-04、11-05 均跑通。
- 发布是原子提交：先写 revision，再 manifest，再 .publication_current；verify 能校验 253 个产物。
- 同日多次发布不会虚增 tracker：tracker._rebuild 按 business_date 保留最后一份快照，appearances 按业务日累计；实测 screening_snapshot=5、tracker total=125、appearances=5。
- 跨源核对正常：2026-11-05 n=16,104、mismatch=6（0.04%）、missing=68、date_lag=2；2026-11-04 mismatch=11；2026-11-03 mismatch=0。
- P0 已修日更失败语义：directory/status 失败即 failed；metrics 全失败即 failed；fuse/screen/freeze/publish 异常不再留下 daily running；scheduler 的 needs_catchup 会在当天补跑一次。

### 2.3 哪些还不够稳健

- **季度采集与历史回填已完成。** daily 的 maintenance 覆盖 profile/fee/scale/manager/holding 的周期任务；2026-11-05 完成 2016–2026 历史持仓回填（`src_zqcc_holding` / `fund_holding_bond` 均 1,900,098 行 / 7,928 只 / 43 个季度，2026Q3 7,897 只，raw_sha256 全部可回查），派生表已重建。历史风格/费率/份额/tenure 仍是当前值，不能称完整 PIT 宇宙。
- **失败告警已补，但仍以本机为主。** `fundana/notify.py` 写 `alerts.jsonl` 并支持可选 webhook；systemd unit 已加 `OnFailure=fundana-alert@%N.service`、`WatchdogSec=300`、`Type=notify`；scheduler heartbeat 写 jsonl 历史。没有外部邮件/短信通道，机器整体宕机时仍依赖人工发现。
- **catchup 仍只补一次。** 当天失败会立即补跑一次，之后进入下一次 07:30；如果补跑也失败，要等到第二天。watchdog 只能发现进程卡死，不能替代多轮重试。
- **同日多次发布会保留多代。** tracker 已按业务日去重，但 publication 表会保留 generation 43–55 的多代记录；手工 force 会造成“同一 nav_as_of 多代”噪音。建议加“同日同批次已发布则拒绝 force，除非显式 --override”。
- **P0 风险门收紧已在 gen67 生效。** 陈旧持仓 block、增强债无持仓 block、股票暴露缺失 block 已进入 L8R；gen67 L8R 10 只，正式榜 125，未出现因收紧导致的断崖。后续若 formal 数下降，应先区分“数据完整性改善”与“策略退化”，不能直接归因于市场。

## 3. 筛选链路逐层审查

### 3.1 周期与窗口抓取：完整，缺口主要是产品成立晚

- 9 个周期均已计算：7/30/90/180/365/730/1095/1460/1825 天，每个周期 16,086 行。
- 评分只使用 30/90/180/365/730/1095/1825；7 天与 1460 天采集但不进 PERIOD_KEYS，属于“有数据、未使用”，不是抓取缺失。
- NAV 覆盖 2002-09-20 至 2026-11-04，9,929,582 行；最新有效净值日 2026-11-04，覆盖率 91.67%；stale>10 天 54 只。
- 3 年可用 5,432 只、5 年可用 3,731 只；报告自述“3 年窗口有效≥2.7 年”。缺口主要来自产品成立晚，不是抓取截断。
- L1 采样间隔 ≤7 天；p75>7 天的低频披露基金出指标但不进正式榜（reason sampling_cadence_gt_7d）。docs/architecture.md 原写 ≤3 天，P0 已改为 ≤7 天。

**判断：周期抓取本身完整、口径可复现；真正的问题不是“有没有抓到”，而是“抓到的 1 年数据被过度使用、3/5 年数据没有成为主结论”。**

### 3.2 L1–L5.5：排除不合适产品，方向正确

- L1：身份/成立日/NAV/新鲜度≤10 天/365 天年化存在/p75 采样间隔≤7 天/可比性旗标空/指标日不超前。
- L2：buyable_state=confirmed_yes；最新 11-05 confirmed_yes 6,062 / 8,138。
- L3：effective_days≤90 且非定开封闭。
- L4：family 规模、目标份额规模、持有 1 年总成本可验；规模按 report_period≤as_of-45 天。
- L5：成立≥1 年 + family 规模分档（1 年 2.0/2 年 2.0/3 年 2.5/4 年 3.0/5 年 3.0 亿，中短债 1.5 亿）+ 目标份额≥0.5 亿。
- L5.5：持有 1 年总成本≤1.30%。

这些层的共同问题是“只有下限、没有上限/适度区间”，但当前样本并不支持拍脑袋加规模上限：最新 run 的规模分桶（n / 1y 年化中位 / 1y MDD 中位 / Sharpe 中位）为 0–2 亿 1682/1.77%/0.49%/0.16；2–5 亿 1033/2.09/0.43/0.68；5–10 亿 1041/2.34/0.35/1.14；10–20 亿 1316/2.45/0.31/1.51；20–50 亿 1584/2.34/0.28/1.42；50–100 亿 926/2.34/0.23/1.57；>100 亿 504/2.57/0.37/1.49。大规模组并不差，规模上限必须做风格内 OOS 后再设。

### 3.3 L6C：九项固定线，实际只有 composite 在筛人

- 九项：composite≥62、annual_1y≥2%、sharpe≥0.8、calmar≥2.8、non_negative≥70%、mdd≤2%、vol≤6%、fee≤0.90%、history_3y（is_full_3y）。
- 最新 run L6C 失败者 1,793 只；原因出现次数：composite 1,743（97.2%）、annual 560、sharpe 420、history_3y 355、non_negative 342、calmar 67、fee 64、mdd 24、vol 1。多数失败者同时触发多项，但 composite 是最接近“主淘汰器”的一项。
- 8 条绝对线中，annual/sharpe/calmar/non_negative/mdd/vol/fee 都是 1 年窗口；history_3y 只是完整性；composite 含 3/5 年权重但仍是相对分位。**“长期优质”没有被写成独立的 3/5 年绝对门槛。**
- STYLE_FLOORS 已定义更严的风格底线（short calmar 4.0/mdd 0.8%/vol 2.5%；pure mdd 1.8%/vol 4.5%；credit non_negative 72%；rate sharpe 0.5 等），但 fixed_checks 不执行，只有 dynamic_thresholds 把它当地板。结果是短债 1.5% 回撤也能过 2% 全局线，与“风格内比较”的设计不一致。
- 报告 P0 已把 fee 口径改为 L6C 0.90%、L5.5 1.30%，不再混用。

**判断：固定线是“灾难底线”，不是“质量线”。它能把明显不可投/高费用/高回撤的产品挡掉，但不能区分“稳健的优秀”与“稳健的平庸”。**

### 3.4 L6E：增强债相对准入，与稳健榜不是同一把尺

- 增强债走 enhanced_relative_checks + history_3y + scale_pit + strict_overlay；board=bond_enhanced，path=enhanced_admission。
- 7 项绝对诊断（annual/sharpe/calmar/mdd/positive/fee/scale）；strict_overlay 只要求 7 项至少 5 项通过，不是全过。
- 相对阈值来自同风格分位+地板；015479 的阈值是 annual min 2.608%、sharpe min 0.2、calmar min 0.8、mdd max 5%、positive min 50%、fee max 1.2%、scale min 1 亿、formal_score min 55；其 non_negative=64.46%、positive=56.61%，仍可进增强榜。
- 最新 run L6E 失败 970 只：strict_overlay 863（89%）、calmar 720、sharpe 715、formal_score 643、annual 611、positive 502、mdd 340、history_3y 206、fee 128。
- P0 已把增强债无持仓从 unknown 改为 block；股票暴露缺失对非利率/指数也改为 block。下一次 screen 会体现。

**判断：增强债的相对准入是合理的“同类比较”，但它不是稳健榜的固定线。报告必须分路径写，不能共用“通过正式榜固定线”。P0 已修文案，但“增强债观察榜”是否应计入正式榜 125，仍是一个产品口径问题，建议在报告里明确标注“增强债观察榜”。**

### 3.5 L7：动态层退化，27/27 因维度不足失败

- dynamic_thresholds：composite 在完整可投域取无条件 q70（winsor 5%-95%），再 max(FIXED 62,q70)；q70<62 时记 dynamic_inactive=true；其余维度 max(style_floor,q) 或 min(floor,q)。
- 最新 q70=55.52<62，dynamic_inactive=true；L7 27 只全部因 dyn_min_dimensions（5/6 维）失败。
- 动态层原本要解决“固定线太松/太紧”，现在变成“固定线 62 + 6 维至少 5 项”，而 6 维里多项与 composite 高度相关，独立信息有限。

**判断：L7 当前不是独立层，不能把它当作“动态适应市场”的证据。P1 应先做影子回测，再决定是修 q 值、改成排序/tie-breaker，还是删除。**

### 3.6 L8：经理门槛是“天数”，不是“业绩归因”

- L8 要求经理职业年限≥365 天 + 现任经理在本基金≥365 天（核心榜≥730 天）；缺值默认放行。
- 最新 L8 失败 63 只，全部 manager_tenure_below_365d。
- 长期榜要求经理≥730 天，但前 10 有多只 745/781/847/885/893/1000/1039 天，不足 3 年；3 年业绩并非当前团队完整创造。报告只在“主要短板”提示，不改变排名。
- 外部证据对“经理任职年限与业绩”的结论混杂；不宜把固定天数作为唯一硬门。更合理的是“任期覆盖业绩窗口比例”：长期榜要求覆盖率≥80%，否则降级为观察。

### 3.7 L8R：风险门已收紧，但仍依赖最新一期持仓

- P0 已加 MAX_HOLDING_AGE_DAYS=270：持仓报告期超过 270 天按陈旧处理，返回空持仓并 block；不再把 2021Q3 旧持仓当最新一期。
- 纯债/短债/信用债/增强债无持仓 → block；利率/指数无持仓 → unknown。
- 股票配置缺失（src_pzd_profile 无数据）且非 rate_like → block equity_required；不再让纯债基金带着“股票暴露未知”通过。
- 风险门仍用 fund_style 当前值；assess 在未显式传 style 时用 fund_style_history as-of 最近值，但引擎侧仍显式传当前 style_code，PIT 风格尚未完全解决。
- 压力测试用 acc_nav，窗口 2022-11-01~12-31、2020-05-01~06-30、2016-12-01~12-31 + 近 5 年/全历史最差 60 日；成立晚于窗口视为不适用。持仓只覆盖 2026Q3，历史压力测试不能验证“持仓穿越周期”。

**判断：L8R 是这套系统最有价值的部分之一，但它只能验证“最近一期持仓 + 净值压力”，不能证明基金经理在 2022/2020/2016 的持仓选择。报告应把“持仓时点”与“压力测试窗口”分开写。**

### 3.8 L9/L10：排序、折叠与榜单口径

- rank_score = 0.85×composite + 0.15×6 个月收益分位；composite 含 1/3/5 年权重，但 1 年指标仍是重要组成。
- 排序键：(-rank_score, -formal_score, -composite, -1y annual, -1y sharpe, code)；正式分只作准入/次级键。
- （gen55 基线；最新 gen63 见文末更新）L9 通过 159；family/company 折叠（FAMILY_CAP=1、COMPANY_CAP=max(2,5%)）后正式榜 125；折叠 34。
- 正式榜 = stable 97 + enhanced 28；观察榜 = core 50 / core10 10 / long 20；研究/受限/新锐 = watch 95 / locked 15 / rookie 30。gen67 为 L9 159 / 发布 125（稳健 97+增强 28）；watch 95 均在 L2 被拦下（94 只 `buyable_confirmed_no`、1 只 `exchange_traded`），不是风险门拦截。
- core10 目标 effective bets 1.8，实际 1.676；平均相关 0.552；core10 的贪婪选样只做 max pair corr>0.85 拒绝 + 50×max_corr 罚分，没有 effective bets 停止条件。报告 P0 已明确“未达标，只能作为集中观察组合，不能宣称分散化，也不应据此等权配置”。
- long 榜要求 3 年完整、5 年覆盖≥60%、5 年年化非空、经理≥730 天、份额≥2 亿；但经理 730 天不等于覆盖 3 年，需 P1 改为覆盖率。

**判断：榜单的“排名”与“分数”已经一致（rank_score_order 门禁 Spearman=-1.0，0 inversions），但“正式榜/观察榜/研究池”的产品语义仍需在 API、前端、报告三处统一。**

## 4. 策略是否科学、合理、正确

### 4.1 科学的成分

- 有明确的 universe、PIT 日期、批次冻结、契约版本、发布门禁、跨源校验、可重放快照。
- 有风险调整指标（Sharpe/Sortino/Calmar/MDD/Vol）、尾部指标、因子归因、压力测试、持仓集中度。
- 有同风格分位，避免把久期/信用风险不同的产品直接比。
- 有前瞻 cohort 与 DSR 的设计，虽然还没成熟。

### 4.2 不科学/不合理的成分

1. **目标函数没有写成可检验假设。** 报告说“筛选优质基金”，但没有定义“优质”是 3 年年化、5 年夏普、相对基准超额、回撤约束，还是投资者持有体验。没有目标函数，就无法判断阈值是否最优。
2. **1 年权重过高。** 9 项固定线中 7 项是 1 年窗口或 1 年成本；rank_score 的 15% 是 6 个月动量；short_core 的 1m/3m 权重 40%。这与监管“以五年以上长周期业绩为核心”的导向不一致。
3. **composite 是相对分位，不是绝对质量。** 它回答“今天在池子里排第几”，不回答“这只基金是否值得长期持有”。池子构成变化会让 62 分含义漂移。
4. **没有真实基准。** 报告用“同风格等权中位数”作对照，这是 peer median/代理，不是可交易指数；market_index_daily 只有 000012/000013/000832 三只价格指数，没有中债-新综合财富/中证全债等总回报指数。没有基准，就无法回答“超额来自选基还是久期/信用 beta”。
5. **幸存者偏差尚未完全校正。** lifecycle 表已入库 1,819 只清盘样本（债券型 510 只，且多数不在当前 fund 表；其中仅 12 只有历史净值），`fundana/audit/walkforward.py` 的宇宙仍只从当前存续基金构造；合并样本仍为 0。历史 IC/DSR 仍可能高估。2026-11-06 已回填 445 只债券型清盘基金并做 6 队列对照，见 §13；样本仍不完整。
6. **风格标签有前视。** 风险门/历史 cohort 使用当前 fund_style，不是 point-in-time；回测中的“同风格”可能用了未来信息。
7. **规模只有下限，没有容量曲线。** 行业研究提示信用债基最优规模约 2.86–20.30 亿元、利率债基访谈区间 10–100 亿元，规模与业绩呈倒 U；当前只有下限，且最新样本大规模组并不差。不能拍脑袋加上限，但应做风格内规模曲线。
8. **经理门槛是固定天数。** 730 天不能保证 3 年业绩由当前团队创造；长期榜前 10 有多只不足 3 年。
9. **core10 不是 10 个独立 bet。** 平均相关 0.552、有效 bet 1.676 < 目标 1.8；没有风险预算/久期中性约束。
10. **证据等级低。** 12 队列 IC 近零、DSR 0.66、前瞻成熟 0；不能把回测排名当技能证据。

### 4.3 外部标准对照

| 标准 | 要点 | 对本项目的含义 |
|---|---|---|
| 证监会 2025-05-07 修订《公募基金评价业务管理暂行办法》及自律规则 | 构建以五年以上长周期业绩为核心的评价评奖体系；优化指标；提高“投资者盈亏、业绩比较基准对比”权重；杜绝短期业绩排名 | 当前 1 年权重过高；报告应把 3/5 年与基准超额放到主结论，而不是把 1 年榜当“长期优质” |
| 《证券投资基金评价业务管理暂行办法》（证监会令第 64 号） | 评价包括评级、评奖、单一指标排名；同一分类少于 10 只不得评级/单一指标排名；排名期间少于 3 个月不得单一指标排名；评级更新间隔≥3 个月 | bond_core10 恰好 10 只、bond_locked 15 只处于边界；报告必须声明“不是评级、不是单一指标排名、不构成投资建议”，避免短期排名 |
| 晨星中国评级 | 三年评级用过去 36 个月 MRAR(2)，同类排序分 5 星；季度更新 | “同类内比较 + 长窗口风险调整收益”方向正确；本项目应补 3/5 年主表与同类样本数 |
| 中债-新综合财富/中证全债 | 覆盖银行间与交易所的债券总回报/价格指数，常作为债基业绩基准 | 本项目没有总回报指数；peer median 只能叫代理基准 |
| NAFMII 债券基金规模研究 | 信用债基最优规模约 2.86–20.30 亿元；利率债基访谈区间 10–100 亿元；规模与业绩倒 U | 当前只有下限；应做风格内规模曲线，不直接硬砍 |
| Deflated Sharpe Ratio（Bailey & López de Prado, 2014） | 校正多重检验/选择偏差/非正态 | DSR 0.660 不能拒绝“无技能”；应继续作为发布门禁 warning |
| 债券基金持续性/幸存者偏差研究 | 结论混杂；幸存者偏差对绩效估计有显著影响 | 必须用无幸存者偏差样本、OOS/前瞻 cohort、基准相对与 DSR 共同验证 |

**外部资料链接（节选）**

- 证监会 2025 评价改革报道：https://www.stcn.com/article/detail/1768783.html ；https://finance.sina.com.cn/roll/2025-05-07/doc-inevtptw9618679.shtml
- 《证券投资基金评价业务管理暂行办法》答记者问：https://www.csrc.gov.cn/csrc/c106256/c1653866/content.shtml
- 晨星中国基金评级说明：https://www.morningstar.cn/help/doc/晨星中国基金评级说明.pdf
- 中债-新综合财富指数示例（基金合同基准）：http://disc.static.szse.cn/disc/disk03/finalpage/2026-08-22/d4727587-9ab6-4126-b518-11a689a1fc05.PDF
- 中证全债指数示例：http://eid.csrc.gov.cn/fund/disclose/instance_show_pdf_id.do?instanceid=549550
- NAFMII 债券基金规模研究：https://www.nafmii.org.cn/yj/jrscyj/qk/2022/20220901/202209/P020260814306564287229.pdf
- Deflated Sharpe Ratio 实现说明：https://pypi.org/project/deflated-sharpe-metrics/

## 5. 前后端架构是否结构对齐

### 5.1 已对齐/已修

- 后端 fundana/api/server.py 约 35 个路由；前端 web/app.js 约 28 个唯一 api() 路径；前端调用的路径目前都能在后端找到（手工核对）。
- API helper 在 web/app.js:240，自动加 /api/v1 前缀。
- scripts/check_ui_contract.py 原先退出码 1，4 项不一致；P0 已修：
  - SERVER_ALIASES 增加 fm/m3/m5；
  - DB_FIELD_MAP 增加 flat_ratio_pct/non_negative_ratio_pct；
  - metrics_meta 增加 long_score/share_scale 等 11 个指标说明（41→52 条）；
  - 新增 tests/test_ui_contract.py，临时库执行 migrations 后断言 rc=0；当前脚本“全部一致”，退出码 0。
- labels.py 补 L8R “独立风险门”；label_reason 补 sampling_cadence_gt_<N>d；报告/API 不再把 L8R 显示成“未进入筛选”。
- report_v2 统一 L9 159 / 折叠 34 / 发布 125；builder 增加 admission_path、manager_tenure_coverage_3y；gate 增加 formal_count_fold、data_staleness。

### 5.2 仍未对齐

- server.py 是手写 if/elif 路由，没有 OpenAPI/路由注册表；前端 28 个路径没有“全量契约测试”，只有 check_ui_contract 的指标/排序字典检查。
- 前端仍有硬编码 board 名称/文案；新增榜单或改中文名时，API 的 /meta/labels 与前端可能漂移。
- 报告是 Markdown + JSON 双产物，但没有 JSON Schema 校验；report.json 的字段变化没有契约测试。
- gate 在 report 之前执行，无法直接检查报告文本；formal_count_fold 只能检查 screen_run.formal_count 与 screen_pick 正式榜条数，不能检查报告是否真的写了“折叠 34”。
- 前端 30 个指标键与字典 52 条已一致，但“前端展示的每一个数字都能在后端字典找到说明”仍靠脚本，不靠 CI；建议把 check_ui_contract、unittest、gate dry-run 串成发布前 CI。

## 6. 数据质量与运维缺口

| 缺口 | 现状 | 影响 | P1/P2 建议 |
|---|---|---|---|
| 季度持仓 | 2026-11-05 回填中：源表 2016Q1–2026Q3 共 43 个季度、5,801 只、52.8 万行；2026Q3 5,750 只；派生表仍为 5 期 | 历史集中度/风格证据可用，但派生表未重建；2026Q1/Q2 覆盖仍不足 | 回填完成后重建派生表；每季度披露截止后自动采集；持仓>270 天 block |
| 费率/规模/经理 | 费率 2026-11-03、规模 2026-10-28、经理 2026-10-31；daily 不采集 | 费用/规模/经理会过期；L4/L5/L5.5 可能用旧值 | 季度/月度增量采集；data_staleness 门禁 |
| 真实债券指数 | 只有 000012/000013/000832 价格指数 | 无法算真实超额；peer median 被误叫基准 | 接入中债-新综合财富/中证全债；无授权时明确“无基准” |
| 幸存者偏差 | lifecycle 表 1,819 行（清盘；债券型 510），合并 0 | 历史 IC/DSR 仍可能高估 | 继续补合并样本；回测只用当时存续样本；报告披露样本边界 |
| 前瞻验证 | 4 cohort、0 成熟 | 不能证明选基技能 | 等 30/90/180/365 天成熟；建立 cohort 看板 |
| 失败告警 | 无 OnFailure/webhook/watchdog；heartbeat 无历史 | 失败只能靠人看 | systemd OnFailure + webhook；heartbeat 历史表；watchdog |
| 同日多发布 | publication 保留多代；tracker 已去重 | 发布历史噪音；审计困难 | 同日同批次默认拒绝 force；保留 override 记录 |
| 规模骤降 | alerts 25 条 medium，全部“规模同比<-30%”，只告警不阻断 | 清盘/赎回风险可能被忽略 | 把规模骤降纳入 L8R watch/block 分级；报告披露 |

## 7. P0 已完成的修复（本轮）

| 文件 | 修复 | 验证 |
|---|---|---|
| fundana/publish/report_v2.py | 统一 L9 159/折叠 34/发布 125；新增数据时点表；稳健/增强两条准入路径；core10 目标 1.8/实际 1.676；排序分口径；方法节 0.90/1.30 | report.md 第 9/30/41-49/68/140/211/244/279/309 行已核对 |
| fundana/labels.py | 补 L8R “独立风险门”；补 sampling_cadence_gt_<N>d | tests/test_api.py LabelsTest；L8R 不再“未进入筛选” |
| fundana/explain.py | 增强债走 enhanced_admission；稳健榜 fee 0.90%；non_negative 低于 70 写“低于”；经理覆盖率；排序分文案 | tests/test_explain.py 新增 4 个用例 |
| fundana/publish/builder.py | 增加 admission_path、manager_tenure_coverage_3y | CSV/report 可用 |
| fundana/publish/gate.py | 新增 formal_count_fold、data_staleness；_to_date 支持 2026Q3；_age clamp 0 | generation 55 gate：formal_count_fold pass、data_staleness pass |
| fundana/risk_gate.py | 持仓>270 天 stale block；增强债无持仓 block；股票暴露缺失 block；_style_asof | tests/test_risk_gate.py 新增 2 个用例 |
| fundana/daily.py | directory/status/metrics/fuse/screen/freeze/publish 失败语义；不再留下 daily running | 代码审读 + 全量测试；下一次日更实测 |
| fundana/api/metrics_meta.py | 指标 41→52 条；L8R 说明；L8 补“在本基金任职”；L10 八榜 | tests/test_metrics_meta.py 13 层 |
| scripts/check_ui_contract.py | fm/m3/m5 别名；flat/non_negative 字段；退出码 0 | tests/test_ui_contract.py |
| docs/README | L1 采样 7 天；风险门增强债 block/270 天；L9 八榜；迁移 34；测试 508；当前状态/generation 54/cohort 4/warnings 6 | 文档一致性核对 |

**P0 的边界：** 没有改评分权重、固定线数值、榜单配额；没有切换生产阈值；没有新增真实基准；没有补季度采集。P0 只修“说真话、失败可见、风险门不漏、契约不漂移”。

## 8. 后续报告优化（报告 v3 模板）

### 8.1 报告定位

报告不是“推荐名单”，而是“研究候选 + 风险披露 + 可复现证据”。首页必须写：

> 本报告是规则筛选的研究候选池，不是基金评级、不是单一指标排名、不构成投资建议。正式榜/观察榜/研究池的准入路径不同，不能跨榜直接比较。

### 8.2 建议的 12 节结构

| 节 | 内容 | 必填字段/验收 |
|---|---|---|
| 0 封面 | 日期、run_id、publication generation、nav_as_of、metric_as_of、gate 状态、免责声明 | 6 个字段齐全；gate 状态与 publication_gate 一致 |
| 1 一页结论 | L9 通过/折叠/发布；正式/观察/研究数量；证据等级；三条最重要的 caveat | 数字与 DB 对账；不得只写“125 只优质基金” |
| 2 数据时点与陈旧度 | 持仓报告期、费率观测日、规模报告期、经理起始日、profile 更新时间、距 as_of 天数 | 每项有日期+天数；>270 天持仓标红；>180 天费率/规模标黄 |
| 3 漏斗与边际贡献 | 每层进入/通过/失败；每项固定线的失败次数；composite 分位分布 | 至少给出 L1–L10 计数；固定线失败次数不得只给总数 |
| 4 正式榜（稳健） | L6C 九项固定线逐项实际值/门槛/余量；rank_score/formal_score；3/5 年 | 不得把 composite 写成绝对质量分；必须写“相对分位” |
| 5 正式榜（增强） | 增强债相对准入：同风格分位+地板、strict 5/7、formal≥55；不写“固定线” | 每行 admission_path=enhanced_relative；non_negative<70 必须写“低于” |
| 6 长期证据 | 3/5 年年化、MDD、Sharpe、Calmar、覆盖、peer median、真实基准（若有）、alpha/t | 3/5 年为主；1 年只作辅助；基准类型必须标注 |
| 7 组合体检 | 平均/最大相关、有效 bet、目标 1.8、久期/信用桶、回撤贡献 | 有效 bet<1.8 必须写“未达标，不能宣称分散化” |
| 8 风险门 | L8R 持仓集中度、压力窗口、股票暴露、持仓时点；block/watch/unknown | 每个 block 有原因；持仓过期不得给“ok” |
| 9 费用与份额 | 1/3/5 年持有成本、A/C 份额、盈亏平衡 | 0.90% 是 L6C，1.30% 是 L5.5，不得混用 |
| 10 规模与容量 | 风格内规模曲线；信用债 2.86–20.30 亿、利率债 10–100 亿研究区间；当前榜单规模分布 | 不直接作为硬门槛；只给证据与建议 |
| 11 经理与团队 | 经理任期、在本基金任期、覆盖 3 年窗口比例；长期榜≥80% | 覆盖率<80% 不得称“3 年业绩由当前团队创造” |
| 12 方法与变更 | 契约版本、公式、阈值、P0/P1 变更、数据限制、复现命令 | 每个数字可追溯到 run/DB；变更留痕 |

### 8.3 报告必须新增的字段

- benchmark_type：peer_median / total_return_index / etf_proxy / none；
- benchmark_return_1y/3y/5y、excess_return_1y/3y/5y；
- peer_median_1y/3y/5y（与 benchmark 分开）；
- formal_path：l6c_fixed / enhanced_relative / observation；
- manager_tenure_coverage_3y；
- holding_report_period、holding_age_days、fee_observed_at、scale_report_period；
- effective_bets_target、effective_bets_actual、diversification_ok；
- evidence_level（LEVEL_0–LEVEL_3）、dsr、rank_ic、matured_cohorts；
- board_class：formal / observation / research。

### 8.4 报告验收标准

1. 任何“通过固定线”文案只能出现在 board=bond_stable；增强债只能写“增强债相对准入”。
2. 任何“基准”字样必须带 benchmark_type；peer median 只能叫“同类中位数/代理基准”。
3. L9 通过、折叠、发布三个数字必须同时出现，且满足 L9 = 发布 + 折叠。
4. 每个榜单必须标 board_class；正式/观察/研究不得混排。
5. 数据时点表必须显示持仓/费率/规模/经理/profile 五项日期与天数。
6. core10 必须显示目标/实际有效 bet；未达标必须写“不能宣称分散化”。
7. 长期榜必须显示经理覆盖率；<80% 必须标“团队连续性不足”。
8. 报告中的 cohort 数、formal 数、board 数必须与 DB/API 对账；gate 增加 report_consistency 检查。

## 9. 路线图

### P1（建议 2–4 周，先影子后生产）

1. **写清目标函数**：把“优质”定义为 3/5 年风险调整收益 + 相对基准超额 + 回撤约束 + 可投性；报告首页只展示这个定义下的结论。
2. **固定线风格化（影子）**：把 STYLE_FLOORS 落到 L6C 的影子分支，先跑 8–12 周/12 队列，比较 short/pure/credit/rate 的入选与前瞻表现；不要在未验证前切换生产。
3. **动态层修复（影子）**：比较 q60/q70、绝对地板、排序/tie-breaker 三种方案；以 OOS IC/DSR/换手率为准；若仍无增量，降级为展示项。
4. **真实基准**：接入中债-新综合财富/中证全债；无授权时用债券 ETF 复权净值作代理并标注；报告同时给 peer median 与 benchmark。
5. **季度采集**：在披露截止后自动采集 profile/holding/fee/scale/manager；加 data_staleness 门禁；补齐 2020–2025 持仓。
6. **前瞻验证**：等 30/90/180/365 天成熟；建立 cohort 看板（命中率、IC、DSR、换手）；在 2 个 cohort 成熟前不宣称“优质”。
7. **幸存者偏差**：导入 lifecycle 清盘/合并名单；回测只用当时存续样本；重算 IC/DSR。
8. **经理覆盖率**：长期榜改为覆盖率≥80%；不足降级观察；报告显示覆盖率。
9. **core10 风险预算**：加 effective bets≥1.8 停止条件，或降级为观察；做久期中性/风险预算版本。
10. **运维**：systemd OnFailure + webhook；heartbeat 历史表；同日同批次 force 保护；catchup 重试上限与告警。

### P2（1–2 个季度）

11. **规模容量曲线**：按风格画 1y/3y 业绩 vs 规模；只在 OOS 支持时设上限；报告给研究区间，不硬砍。
12. **API/前端契约**：OpenAPI 或路由注册表；report.json Schema；前端 API 路径集合测试；CI 串 unittest + check_ui_contract + gate dry-run。
13. **报告 v3**：按第 8 节模板重构；加 report_consistency 门禁；所有数字可追溯。
14. **多类型扩展**：偏债混合/QDII 债基是否纳入，需单独风险口径；不要混入当前债券榜。
15. **模型化**：在规则池之上做 OOS 模型（久期/信用/转债因子中性化），但保留规则兜底；模型必须过 DSR/前瞻。

## 10. 不要做的事

- 不要继续增加更多 1 年固定线；review3 已证明边际信息很低（mdd 通过率 85.6%、vol 99.94%）。
- 不要把 peer median 叫“基准”；没有总回报指数就写“无基准，仅有同类对照”。
- 不要把 12 队列 IC +0.011 当作选基技能；DSR 0.66 不能拒绝“无技能”。
- 不要在没有 OOS 的情况下加规模上限；当前大规模组并不差。
- 不要用经理固定天数替代“任期覆盖业绩窗口”。
- 不要把 core10 当“10 个独立 bet”；有效 bet 1.676 < 1.8。
- 不要把增强债观察榜混进稳健正式榜；两者准入路径不同。

## 11. 结论

**运行层面：健康。** 日更、发布、API、门禁、测试都正常；P0 修了报告口径、L8R 标签、增强债文案、formal_count、数据时点、风险门、日更失败语义、UI 契约。当前 567 项测试通过，generation 67 verify 通过（run `20261105T214346-screen-f4a862`，296 文件，gate warning 5 条）；正式榜 125（稳健 97 + 增强 28），watch 95 均在 L2 被拦下（94 只 `buyable_confirmed_no`、1 只 `exchange_traded`），不是风险门拦截。2026-11-05 历史持仓回填完成（1,900,098 行 / 7,928 只 / 43 个季度，2026Q3 7,897 只），正式榜持仓覆盖 2026Q3。

**策略层面：仍是“风险过滤器 + 候选池”，不是“经验证的优质基金发现器”。** 固定线几乎不筛人，composite 是相对分位且是主淘汰器；L7 动态层不工作；增强债与稳健债口径不同；没有真实债券基准；没有季度数据刷新；没有幸存者偏差校正；前瞻成熟 0；DSR 0.66。

**对用户最有价值的下一步：** 报告 v3 的核心“说真话”结构已落地（正式/观察/研究分层、基准类型、数据时点、有效 bet、经理覆盖率、规模容量、OOS 研究、幸存者偏差诊断）；P1/P2 的影子测试与研究工作已完成，但生产阈值仍未切换。不要在证据不足时把榜单包装成“长期优质基金”。

## 12. P1/P2 实施状态（2026-11-05）

| 项 | 状态 | 落点/证据 |
|---|---|---|
| P1-1 目标函数 | 完成 | fundana/screen/objective.py；报告结论表；当前 verdict=candidate_pool |
| P1-2 固定线风格化影子 | 完成（研究） | fundana/screen/style_shadow.py；actual 210→shadow 205，entered 0/exited 5 |
| P1-3 动态层影子 | 完成（研究） | q70/k5 175 vs actual 183；q60/k4 210（+27），未验证不切换 |
| P1-4 基准 | 完成 | fundana/benchmarks.py；优先中国债券信息网 CBC_CFZS_00 真实指数（6,211 个交易日、raw 可回查），ETF 仅作风格代理；9 风格；报告第 6 节标注“全市场指数、非合同基准、非风格指数” |
| P1-5 维护采集 | 完成 | fundana/maintenance.py；profile 30 天、fee 30 天、scale 80 天、manager 30 天、holding 80 天；daily 自动跑 |
| P1-6 前瞻看板 | 完成 | forward.summary 增加 maturity_date/days_to_maturity/progress_pct；Web 观察期进度表 |
| P1-7 幸存者偏差 | 完成（诊断） | fundana/survivorship.py；6 天目录快照、3 只消失候选；不冒充清盘 |
| P1-8 长期榜经理覆盖 | 完成 | LONG_MANAGER_COVERAGE_MIN=0.80；不足 reason=long_manager_coverage |
| P1-9 core10 风险预算 | 完成 | _core10_risk_budget；目标 1.8、gen69 实际 1.707（有界单换后），仍标降级为集中观察 |
| P1-10 运维 | 完成 | notify.py、heartbeat jsonl、systemd Type=notify/WatchdogSec=300/OnFailure；scheduler 与 web 已重启并验证 |
| P2-11 规模容量 | 完成（研究） | fundana/capacity.py；6 个规模桶；不设硬上限 |
| P2-12 契约 | 完成 | report_schema.py 写盘前校验；check_ui_contract 路径检查；scripts/ci.sh |
| P2-13 报告 v3 | 完成 | 13 节；facts + report_consistency 门禁；formal_count_fold |
| P2-14 多类型扩展 | 完成（披露） | fundtypes.uncovered；报告“范围外类型”表；不混入债券榜 |
| P2-15 OOS 模型 | 完成（研究） | fundana/screen/oos.py；8 季训练/3 季测试；mean IC 0.146、t=0.89、DSR 0.091；不切换生产 |

**契约 ID 说明：** `screen-bond-v11-2026.12.7` 的日期段是迭代标识，不是生效日期；为避免破坏已冻结批次与历史 publication，保留该字符串，文件头已与常量同步。

**tracker 修复：** 旧发布把 11-04 净值记成 11-05 快照，导致同日重发后跟踪榜按业务日排序错位、出现假换手；已删除该错标快照并按 nav_as_of 重建（4 个快照：11-01/02/03/04），tracker.update_after_publish 新增 older_business_date 防护，重跑旧净值日不再倒灌。

**仍未完成/不能宣称：** 合并样本 0 条；前瞻成熟观测 0 条；历史季度持仓已回填 43 个季度，但历史风格/费率/份额/tenure 仍是当前值，不能称完整 PIT 宇宙；walkforward 已可纳入 EID 已确认且有公开净值的 445 只清盘基金（见 §13），但样本仍不完整，不能称“无幸存者偏差”。

**2026-11-05 后续更新：** 已接入中国债券信息网公开 JSON 接口的中债-新综合财富（总值）指数 CBC_CFZS_00（6,211 个交易日，2002-01-04 至 2026-11-04，raw_sha256 1dd7458d…）；报告第 6 节稳健风格展示真实指数与 peer 中位数，增强风格标“无合适基准”、ETF 只作参考；该指数不是合同基准、不是风格指数。2026-11-05 历史持仓回填完成：`src_zqcc_holding` / `fund_holding_bond` 均 1,900,098 行 / 7,928 只 / 43 个季度（2016Q1–2026Q3，2026Q3 7,897 只），raw_sha256 无缺失。重跑后 generation 67（run `20261105T214346-screen-f4a862`）正式榜 125（稳健 97+增强 28）、L9 159、折叠 34、watch 95（94 只 `buyable_confirmed_no`、1 只 `exchange_traded`），verify 296 文件通过，gate warning 5 条，`benchmark_index`/`lifecycle_coverage`/`provenance_integrity`/`data_staleness` pass。证监会 EID 清盘样本 1,819 只已入库（债券型 510、合并 0）。全量测试 565 项通过。

**2026-11-06 后续更新（gen69）：** core10 增加有界单换（`_core10_local_search`，最多 3 轮、排序分损失≤2、有效 bet 提升≥0.005），在 gen68 基础上把 017708 换成 006633，有效 bet 1.676→1.707、平均相关 0.552→0.540、最大相关 0.7195→0.804；仍低于 1.8，状态保持 `downgraded`。screen run `20261106T082838-screen-89190e`，generation 69，L9 157、折叠 33、正式 124（稳健 97+增强 27）、watch 94、core10 10、long 20；verify 294 文件通过，gate warning 5 条。全量测试 567 项通过。

## 13. 幸存者偏差校正与清盘基金回填（2026-11-06）

**清盘基金回填。** `scripts/backfill_liquidated.py` 从 `fund_lifecycle`（证监会 EID，1,819 条已确认清盘）中筛出当前 `fund` 表缺失的 6 位代码，用公开档案 `em_jbgk` 判断是否为债券型，用 `em_pzd` 取成立以来净值，再写 `fund`/`fund_profile`/`fund_share_class`/`fund_style`，并补采规模、经理、费率。全量 run `20261106T084506-backfill_liquidated-5998b5` 完成：检查 1,807 只，债券型 **445 只**、非债券 1,348 只、公开净值缺失 15 只；写入 **332,872 行** NAV。当前 `fund` 8,586 只（当前存续 8,141 + 历史清盘 445），全部 445 只都有公开净值。成立日/规模/费率/经理均来自公开档案，缺失就留空；定开/封闭只按名称标注 `lockup_type`，不编造持有天数。

**walkforward 关键 bug（先复验再修）。** `fundana/audit/walkforward.py` 的 `_asof_candidates` 在 dry-run 重算指标后没有更新 `metric_end_date`，仍沿用库内最新指标（2026-11）的结束日。结果 2021-06-30 队列 3,623 只中 2,968 只被判 `metric_lookahead`，`gate_pass` 仅 34（旧基线 1,998），历史回测实际失效。修复为 `metric_end_date = metrics[365]["end_date"][:10]`；修复后同一队列 `gate_pass` 1,651（排除清盘）/1,704（纳入清盘）。**结论：此前写入 config evidence 的 12 队列 IC（+0.011）在修复前口径下不可直接引用，需重算后再作为技能证据。**

**幸存者偏差对照（2020–2025 六个半年队列，top=118，365 天）。** 同一套 walkforward 代码分别排除/纳入 445 只已确认清盘基金：

| 排序键 | IC(年化) 排除 | IC(年化) 纳入 | ΔIC | 命中率 排除 | 命中率 纳入 |
|---|---:|---:|---:|---:|---:|
| rank_score | 0.3780 | 0.3712 | **-0.0068** | 0.5607 | 0.5706 |
| composite | 0.1933 | 0.1936 | +0.0003 | 0.5240 | 0.5282 |
| formal_score | 0.3407 | 0.3390 | -0.0017 | 0.5551 | 0.5664 |
| return_6m | 0.5277 | 0.5158 | -0.0119 | 0.6172 | 0.6271 |
| return_1y | 0.5306 | 0.5204 | -0.0102 | 0.6003 | 0.6130 |

各队列纳入清盘后宇宙增加 63–235 只、过基础门增加 42–74 只、过固定线增加 2–8 只。**读法：** 在这 445 只样本内，旧口径对 rank_score 年化 IC 的高估约 0.007（相对 1.8%），对 6 个月/1 年收益类排序键高估约 0.010–0.012；命中率反而略升，因为模型把清盘基金排在后段。不能据此说“幸存者偏差已解决”：1,819 条清盘中只有 445 只债券型且有公开净值，合并样本仍为 0，历史风格/费率/份额仍是当前值。**结论仍是：历史 IC 只能作为研究证据，不能证明实盘选基技能。**

**新增审计入口。** `python3 scripts/audit_survivorship.py --years 2020,2021,2022,2023,2024,2025` 输出 JSON + Markdown 到 `reports/analysis/audit_2026_11/`；`walkforward` 新增 `--include-liquidated/--no-include-liquidated`（默认纳入），便于复现新旧口径。

**测试。** 全量 574 项通过；CI 与前后端契约检查通过。

---

**附：关键文件与行号**

- fundana/screen/engine.py:422-460（score_candidate）、:504-532（fixed_checks）、:583-626（enhanced_checks/strict_overlay）、:697-726（enhanced_relative_checks）、:729-786（dynamic_thresholds）、:1520-1632（_select_concentrated）、:1635-1783（_rank）
- fundana/screen/thresholds.py:19（SCREEN_CONTRACT）、:69-85（FIXED）、:125-137（STYLE_FLOORS）、:139-160（ENHANCED_ADMISSION/RELATIVE）
- fundana/publish/report_v2.py:359-370（结论）、:386（run_meta）、:538-540（方法节固定线）、:471-472（有效 bet）、:502（排序分文案）
- fundana/explain.py:17-35（固定线/增强分支）、:79-92（non_negative/fee）、:99-104（经理）
- fundana/labels.py:96-111（LAYER_LABELS）、:253/275（fallback）
- fundana/publish/gate.py:312-338（formal_count_fold）、:457-509（data_staleness）
- fundana/risk_gate.py:147-167（_latest_holdings）、:382-397（无持仓）、:429-439（股票暴露）
- fundana/daily.py:59-71、:118-135、:163-171、:181-203、:216-229（失败语义）
- scripts/check_ui_contract.py（SERVER_ALIASES/DB_FIELD_MAP）
- tests/test_report_v2.py、tests/test_explain.py、tests/test_risk_gate.py、tests/test_ui_contract.py、tests/test_api.py、tests/test_metrics_meta.py

**2026-11-06 报告口径更新（gen71）：** 新增 evidence.survivorship 配置与 report_v2._survivorship_note()，报告结论第 15 行先声明“旧 12 队列 IC/DSR 待重算”（walkforward 的 metric_end_date 前视 bug 于当日修复），第 17 行披露 6 个半年队列的对照：rank_score 年化 IC 0.3780→0.3712（Δ-0.0068）、composite 0.1933→0.1936；同时写明“EID 清盘 1,819 条中 445 只债券型已回填、458 只有公开净值，合并 0，只能算部分校正”。risk 节不再声称“已清盘/合并基金不在当前 fund 表”，改为说明已回填 445 只但样本仍不完整。重新 screen + publish 生成 generation 71（run 20261106T094408-screen-653783，revision aa0d63097b7546aead1104f0a8a93db6，294 文件 verify ok，gate warning 5 条），正式榜 124（稳健 97+增强 27）、L9 157、折叠 33、watch 94。全量测试 574 项通过，CI 通过。

**2026-11-06 数据真实性可见化（gen74）：** 报告第 2 节新增“数据来源与真实性”：12 张源表共 14,824,337 行，发布门禁核对 3,159,203 行（NAV 取 2026-08-07 起近 90 天，其余源表全量），raw_sha256 缺失 0、格式错 0、未注册来源 0；明确缺失值保持 NULL、不用 0/模拟值填充，ETF 只作参考、peer median 只作代理基准。API /api/v1/operations/status 新增 provenance 字段，前端“运行状态”页新增数据真实性卡片；两者都复用最近一次发布门禁证据，不重复扫描全表。全量测试 576 项通过，check_ui_contract.py 退出码 0，发布 generation 74（revision 380412e52d6b4cbaac3e6fedccdedd27，294 文件 verify ok，gate warning 5）。
