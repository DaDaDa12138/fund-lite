# 架构总览（fundana / 债券型基金首期）

本文件描述**已实现**的架构，与 `docs/data-catalog.md`（数据源）、`docs/fusion-rules.md`（融合规则）、
`docs/formula-contract.md`（指标口径）、`docs/screening-rules.md`（筛选规则）、`docs/runbook.md`（操作）
互为索引。所有数字均来自本仓库实际运行结果，不是设计假设。

## 1. 分层与职责

```
┌──────────────────────────────────────────────────────────────────────┐
│ L0 证据层   raw_object(sha256 PK) / fetch_log / source_state / run    │
│             原始字节 gzip 入库 + var/raw/sha256/<前2位>/<sha>.gz 副本  │
│             一次抓取 = 一条 fetch_log（含 business_ok / raw_sha256）    │
├──────────────────────────────────────────────────────────────────────┤
│ L1 源级层   src_directory / src_snapshot_status / src_rank_row        │
│             src_pzd_profile / src_jbgk_profile / src_zqcc_holding     │
│             按源原样落地，不做跨源判断，可单独重放                      │
├──────────────────────────────────────────────────────────────────────┤
│ L2 实体层   fund / fund_share_class / fund_family / fund_profile /    │
│             fund_manager_evidence（现任经理职业年限/任期收益/东财评分） │
│             fund_fee_schedule / fund_holding_policy / fund_style      │
│             fund_buyable_daily / fund_nav_daily / fund_nav_revision   │
│             fund_scale_history / fund_holding_bond / fund_coverage    │
│             跨源融合：字段级 field_src + 冲突双值保留 + 单位归一        │
├──────────────────────────────────────────────────────────────────────┤
│ L3 决策层   fund_metric / fund_tail_metric / fund_fee_* / screen_* / publication │
│             publication（manifest + 唯一提交点）                       │
└──────────────────────────────────────────────────────────────────────┘
```

**为什么 NAV 不设 `src_pzd_nav`**：单只基金全历史约 3,000 点，7,454 只 ≈ 2,300 万行；若 L1 原样再存一份，
等于同一批数据双写。NAV 直接落 `fund_nav_daily`，但每行带 `nav_source` 与 `raw_sha256`，
可回溯到 `raw_object` 的原始字节并离线重放。这是**有意的偏差**，已在 `docs/fusion-rules.md` 记录。

## 2. 数据流（首期 6 个源）

| 阶段 | 源 | 产出 | 作用 |
| --- | --- | --- | --- |
| 目录 | em_directory（fundcode_search.js） | src_directory → fund | 28,236 只全市场名录，取 type_group=bond 的 7,454 只 |
| 状态 | em_status_snapshot（Fund_JJJZ_Data t=8） | src_snapshot_status → fund_buyable_daily | 一次请求拿全部申购/赎回状态，**替代 7,454 次单基金调用** |
| 档案 | em_jbgk（jbgk_{code}.html） | src_jbgk_profile → fund_profile | 成立日、基金级净资产、管理/托管/销售服务费、认申购费上限、业绩基准 |
| 净值 | em_pzd（pingzhongdata/{code}.js） | fund_nav_daily + fund_coverage | 单请求拿全历史净值/累计净值/日增长率（约 500KB/只） |
| 排行 | em_rank（rankhandler ft=zq） | src_rank_row | 横截面交叉校验（规模/费率/阶段涨幅），非全集证据 |
| 持仓 | em_zqcc（FundArchivesDatas type=zqcc） | src_zqcc_holding → fund_holding_bond | 债券持仓明细，用于转债风格的结构化证据 |

可选源（已实现解析，首期不参与评分）：`em_jjfl`（申赎费阶梯）、`em_jdzf`（阶段涨幅）、
`em_manager_list`（历任经理）、`em_dividend`/`em_split`（分红拆分）、`dj_nav`（蛋卷净值）、`dj_profile`。

## 3. 融合的关键决策

1. **单位后缀纪律**：`*_pct` = 百分点，`*_dec` = 小数比率，`*_billion` = 亿元，`*_days/*_count` = 计数。
   规模只在入口 `/1e8` 一次，之后不再换算。
2. **缺失四态**：`attempted` + `status(not_requested|provider_null|parse_failed|stale|real_zero|ok)`。
   空串/`-`/`--`/`null` 一律 `None`，禁止 `Number("")` → 0。
3. **冲突不择优**：超出容差（NAV abs≤0.001 或 rel≤1%；收益 abs≤0.5pp 或 rel≤5%；规模 rel≤5%；
   净值日差≤5 天 soft_lag）时保留双值，canonical 置 NULL 并写 `conflict_json`。
4. **规模两级语义**：jbgk/rank 是**基金级净资产**，pzd 是**份额级规模末点**。先比基金级两源，
   只有两级都缺才回退 pzd 并标 `asset_scale_level=share_class`。实测修正：005273 华商可转债A
   rank=62.87 亿 vs pzd=31.x 亿，A/C 两行 rank 接近整只基金 → 旧逻辑把 1,252 只 A/C 误判为冲突。
5. **A/C 不合并**：`fcode` 是份额主键；`family_id` 只在有结构化证据时生成，否则 `heuristic_family`。
6. **Universe 作用域**：`fuse --types bond` 只写债券型，并 `purge` 掉非本期 L2 行。
   教训：首次未加作用域时 28,236 只全进 `fund`，风格把 17,007 只非债基金默认成 `bond_pure`，母体被污染。

## 4. 指标引擎

- 收益口径优先级：`daily_growth_pct`（链式复利，首选）> `accum_nav`（accum_proxy）> `unit_nav`（price_return_proxy）。
  实测（2013 至 2026-10-30）：000053 链式 +96.9552% vs 单位净值 +5.5000% vs 累计净值 +90.1200%，
  证明单位净值与累计净值都**不是**复投总收益。
- 窗口：(end−period, end] 另取边界锚点；九个周期 7/30/90/180/365/730/1095/1460/1825 天。
- 最少点数 `max(3 或 10, ceil(round(days*252/365)*0.8))`；最大间隔 `min(45, max(days//3, 7))`；
  覆盖宽限 7 天。窗口不合格 → 全部指标 NULL + `undefined_reasons_json`，**不产出部分指标**。
- 年化统一 `Y=D/365.25`；`freq=max(1,min(252,n/Y))`；`vol=sample_std(ddof=1)*sqrt(freq)`。
- Sharpe/Sortino/Calmar 同存 raw 与封顶值（±5/±10/±20）并记 `*_cap_hit`；
  `standard_metric_claim=false`，对外称"策略夏普/策略索提诺/策略卡玛"。
- rf=0.0164（中债 10 年期，observed_at 2026-10-25，配置化）。中债收益率曲线没有稳定的公开机器接口
  （GET 405 / POST 空数组），因此 rf 只能按观测值配置，变更时全量重算；中债-新综合财富（总值）指数
  另有公开 JSON 接口（POST form），仅用于基准，不用于 rf。

## 5. 筛选 L1–L10（债券型）

| 层 | 判据 | 失败语义 |
| --- | --- | --- |
| L1 | 身份 ok、成立日、有效 NAV、新鲜度 ≤10 天、有 1 年指标、NAV 采样间隔 ≤7 天 | 不通过并记原因码 |
| L2 | `buyable_state=confirmed_yes`（当日快照；场内 ETF/LOF 记 `exchange_traded`） | `buyable_*` |
| L3 | `effective_policy_days ≤ 90`，且不是定期开放/封闭式 | `locked_*d` → `bond_locked` 榜；`lockup_*` 不进正式榜 |
| L4 | family 规模、目标份额规模、持有期成本可验；规模/份额按 `report_period <= as_of-45 天` 取最近一期（P0-5） | `scale_missing`/`share_scale_missing`/`fee_missing`；仅 `profile_current` 的正式榜另记 `scale_pit:not_pit` |
| L5 | 成立 ≥1 年；family 规模分档 2/2/2.5/3/3 亿（短期 1.5）；目标份额 ≥0.5 亿 | `scale_below_*`/`share_scale_below_*` |
| L5.5 | 持有期总成本（1 年）≤1.30 个百分点 | `holding_cost_above_1.30` |
| L6 | 9 项固定硬比较（综合≥62、1年年化≥2%、Sharpe≥0.8、Calmar≥2.8、非负日占比≥70%、MDD≤0.020、vol≤0.060、fee≤0.90%、3年完整业绩） | 逐项原因码；1–3 年走新锐榜；L6D 缓释生产关闭 |
| L7 | 动态分位：母体=**L1–L5.5 可投域**的同风格样本（≥15 才分风格），综合分过绝对底线 62 且其余 6 项过 5 项；已上榜者用 exit 档滞回 | `global_fallback`/`dyn_*` |
| L8 | 经理后过滤：职业年限 ≥365 天；现任经理在本基金最短任职 ≥365 天（核心榜 ≥730 天） | `manager_*` |
| L8R | 独立风险门：持仓集中度（前十大/单一债券/信用发行人/转债/股票）+ 累计净值压力测试（2022Q4、2020H1、2016Q4、近5年最差60日）；纯债/短债/信用债无持仓不放行 | `risk:*` → 退出正式榜，保留 risk_json |
| L9 | 排序：`rank_score=0.85×综合分+0.15×6个月收益分位`；同分依次比正式分/综合/1年年化/Sharpe/fcode | 超限用 stability_rank |
| L10 | `bond_stable`（纯债动态双榜）/ `bond_enhanced`（一级/二级/转债：同风格分位+绝对地板+正式分≥55+严格门 5/7+每风格配额 40）/ `bond_core50`/`bond_core10`（核心风格 caps）/ `bond_long`（5 年覆盖≥60% 且 5 年年化非空，影子观察） | family 折叠、公司上限 |

增强债不参与动态分位（避免转债高波动污染纯债分布），走独立准入路径。

## 6. 发布与可重放

- 产物目录：`reports/runs/<run_id>/revisions/<uuid>/`，含 `summary.md`、`report.md`、`report.json`、
  `bond_stable.csv`、`bond_enhanced.csv`、`bond_long.csv`、`bond_locked.csv`、`all_formal_candidates.csv`、`decisions/<fcode>.json`、
  `diagnostics/{coverage,conflicts,nulls,style_evidence,family_duplicates,bond_stable_by_family,enhanced_near_miss,tail_coverage,fee_coverage}.csv`、`validated.json`。
- `validated.json` 逐文件 `sha256+size`；提交用 `os.replace` 原子切换 `.publication_current` 符号链接，
  `generation` 递增；失败时旧 publication 保持可用。
- 发布 as-of 单调性（ADR-117）：新 run 的 `(nav_as_of, metric_as_of)` 早于当前 publication 时
  抛 `publication_older_than_current`；只有 `--allow-older` 才允许回滚，避免手工重跑旧日期覆盖新榜单。
- 读端强校验：symlink 指向必须在 `reports/` 内、`schema_version`、`sha256` 逐文件一致、路径不逃逸。
- 冻结输入：`var/freeze/<run_id>/inputs.jsonl.gz` + `inputs_pit.jsonl.gz` + `manifest.json`。
  `inputs.jsonl.gz` 供 `replay --offline` 断网重算到 1e-9；`inputs_pit.jsonl.gz` 是 P0-1 的
  37 列 as-of 输入（规模/份额/任职/指标/分位/评分），同时落 `screen_run_input` 表，
  `replay-screen` 逐字段比对评分与 L1–L5.5 门禁（不重跑 L6–L10、经理门与风险门）。清盘/合并优先由证监会 EID 公开公告采集进 `fund_lifecycle`（`lifecycle-collect`，
  日更月度增量），也支持外部 CSV 导入；`_load_candidates(as_of=...)` 排除 as-of 前已清盘的基金。
- 数据可比性：`fund_metric_comparability` 按 as_of 记录 `amortized_cost / zero_risk /
  sparse_nav / stale_nav` 旗标；筛选 L1 以 `metric_*` 原因码退出，`compute_percentiles`
  只用干净母体估计分位。旗标只影响可比性，不修改 `fund_metric` 原始指标。
- 数据健康：`data-health` 只读检查 DB 完整性、迁移漂移、净值新鲜度、指标覆盖、采集成功率、
  主链路来源冗余与发布门禁；单源域只告警，不改变退出码。迁移 0030 为 fetch_log 时间窗口与
  fund_metric 契约查询补索引。
- P2 研究/诊断：`fund_metric_snapshot` 由 `snapshot-metrics --as-of --quarters 12` 逐季回填
  365/1095/1825 指标（不写生产 `fund_metric`）；`fundana/factor_buckets.py` 按 raw
  `beta_duration_raw`/`beta_credit_raw` 与覆盖/R²门槛分 short/mid/long、low/mid/high，
  只进报告第 6 节与组合体检，不改变准入；`gate` 新增 `lifecycle_coverage` 与 `dsr_skill`
  两条 warning，分别披露清盘样本覆盖与 DSR 0.660<0.95（清盘样本入库后 lifecycle_coverage 转 pass）。
- 独立风险门：`fundana/risk_gate.py` 在 L8 之后、L9 之前读 `fund_holding_bond` /
  `fund_nav_daily` / `src_pzd_profile`，按债券名称重分类并归一发行人；压力测试只用
  `acc_nav`，避免分红/拆分造成假回撤。block 写 `screen_decision.risk_json` 并记 L8R，
  watch/unknown 只展示，不改变收益分。

## 7. 接口与运行

- CLI：`bootstrap` / `probe` / `collect --domain {directory,status,profile,nav,holding,rank}` /
  `fuse --types bond` / `metrics` / `screen --profile bond_v1` / `publish` / `verify` / `replay` /
  `serve` / `scheduler`；研究/运维只读命令 `portfolio-diagnostics` / `stat-validation` /
  `data-health` / `snapshot-metrics` / `walkforward` / `forward` / `shadow-forward`。
- 只读 API（stdlib http.server，默认 127.0.0.1:8091）：`/api/v1/healthz`、`/publications/current`、
  `/universe/coverage`、`/rankings?board=&limit=`、`/funds/<code>`、`/operations/status`。
  无发布时返回 `503 publication_unavailable`，不是空 200。`/rankings` 的净值与指标 JOIN 钉住
  run 的 `nav_as_of`/`metric_as_of`（ADR-116），发布后新入库的净值不会提前进入旧榜单；
  `/funds/<code>` 详情页仍展示最新档案，二者不能混读。
- 调度器（ADR-117）：`scheduler` 每轮用子进程执行 `python3 -m fundana daily --quiet`，cwd 固定
  项目根；代码、配置、迁移与公式契约每轮重新加载，不再依赖长驻进程的内存状态。
- 只读 API 缓存（ADR-117）：以当前 publication revision 为 key，`/overview` 600s、`/quality` 300s、
  `/portfolio/diagnostics` 600s、`/decision` 600s；发布切换后旧 key 自然失效。组合体检默认读
  发布产物 `report.json` 的 `sections[id=portfolio]`，缺失才重算；服务启动后后台预热。

## 8. 已知边界

- 目录/状态快照是**当日**观测，不能证明历史真实可买状态。
- 排行接口只覆盖 5,068/7,454 只债券基金，不能当全集覆盖证据；成立日与规模以 jbgk 为准。
- 东财把 86 只名称含"转债"的基金全部归入"债券型-混合二级"，**没有"转债"类型标签**，
  转债风格必须靠名称 + 债券持仓（可转债代码前缀）判定。
- 无风险利率无公开接口，rf 为配置观测值。
- 真实久期/信用评级/跟踪误差不可由这些接口证明，只能作研究参考。