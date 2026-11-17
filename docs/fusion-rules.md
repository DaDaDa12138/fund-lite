# 本地融合规则（Fusion Rules）v2026.11.1

目标：把 6 个异构接口的返回值融合成一份**可审计、可重放、冲突不静默**的本地数据层。
原则：原始数据先固化（L0），再归一化（L1），再融合（L2），最后才计算（L3）。任何一步都不允许覆盖原始证据。

## 1. 三层数据模型

### L0 原始层（不可变）
- `raw_object(sha256 PK, bytes_gz, raw_size, content_type, encoding, url, http_status, fetched_at)`
  - 正文 gzip 同时落盘 `var/raw/sha256/<前2位>/<sha>.gz`；数据库存 BLOB 副本。
  - 同一 URL/参数重复抓取若 `sha256` 相同则复用（TTL 缓存）。
- `fetch_log(run_id, source_id, key, url, http_status, business_ok, status, error_msg, raw_sha256, attempt)`
  - `status ∈ ok | provider_null | parse_failed | http_error | timeout | soft_fail | skipped_fresh`。
  - **`business_ok=0` 表示请求到达但业务判活失败**，与“未请求”严格区分。
- `source_state(source_id, key, last_attempt_at, last_success_at, last_status, consecutive_failures, cooldown_until)`。
- `run(run_id, mode, status, config_hash, code_version)`。

### L1 源镜像层（每源一张表，保留原貌）
| 表 | 来源 | 说明 |
|---|---|---|
| `src_directory` | em_directory | 每日全量名录 |
| `src_snapshot_status` | em_status_snapshot | 申赎状态快照（13 列） |
| `src_rank_row` | em_rank | 排行横截面（阶段收益/费率/成立日；**不含规模**，第 18 段是 `custom_pct`） |
| `src_pzd_profile` | em_pzd | 净值点数、规模 JSON、经理 JSON 等 |
| `src_jbgk_profile` | em_jbgk | 档案（成立日/规模/费率/基准） |
| `src_zqcc_holding` | em_zqcc | 季报债券持仓 |
| `src_manager_history` | 预留 | 历任经理 |

### L2 融合层（业务实体）
`fund / fund_share_class / fund_family / fund_profile / fund_fee_schedule / fund_holding_policy / fund_style / fund_buyable_daily / fund_nav_daily / fund_nav_revision / fund_scale_history / fund_holding_bond / fund_manager_tenure / fund_coverage`

### L3 计算与决策层
`fund_metric / screen_run / screen_decision / screen_pick / publication`

## 2. 单位与命名纪律

| 后缀 | 含义 | 示例 |
|---|---|---|
| `*_pct` | 百分点（3.5 表示 3.5%） | `positive_ratio_pct`, `recurring_fee_pct` |
| `*_dec` | 小数比率（0.035 表示 3.5%） | `annual_dec`, `mdd_dec`, `vol_dec` |
| `*_billion` | 亿元 | `asset_scale_billion` |
| `*_days / *_obs / *_count` | 计数（自然日或观测数） | `effective_days`, `dd_duration_obs` |

- `ENDNAV` 元 → 亿元：**只做一次** `/1e8`。
- `JZZZL` 是百分点；`SYL_1N` 是近 1 年**累计**收益（非年化）；`SYL_Z/Y/3Y/6Y` 分别是 1 周/1 月/3 月/6 月。
- `MGOLD` 是经理评分，绝不是收益。

## 3. 缺失与冲突语义

### 3.1 缺失四态
每字段必须能回答“为什么没有值”：
`not_requested（attempted=0） / provider_null / parse_failed / stale / real_zero（真实 0） / ok`。
**空串、'-'、'--'、'null'、'暂无' 一律转 NULL，禁止 `Number('')` → 0。**

### 3.2 多源冲突
- 规模分两层，**不跨层判冲突**（口径不同）：
  - 份额级净资产：`em_jbgk(净资产规模)`，一只基金一个份额一个值（000003 A 类 0.37 亿、C 类 0.43 亿）。
  - 基金级净资产：同 `family_id` 下各份额 `em_jbgk` 净资产之和（000003 家族 = 0.37+0.43 = 0.80 亿），写 `asset_scale_src=em_jbgk_family_sum`、`field_src_json.asset_scale_level=family`。
  - 份额级兜底：`em_pzd(规模变动末点)`，仅当家族汇总缺失时使用，标 `asset_scale_level=share_class`。
  - **筛选门槛一律用基金级**（L5 规模分档 2/2/2.5/3/3 亿是“整只基金”口径；用份额级会把 A 类小额份额误杀）。
  - **`em_rank` 不含规模**（2026-10-31 实测纠正）：排行第 18 段是「自定义区间涨幅」，与 `y3_pct` 相关系数 0.9989（000003 该段 16.1290 而 y3=15.27；110027 10.7606 而 y3=10.52），与 jbgk 净资产无相关性（16.13 vs 0.80 亿）。旧实现把它当规模导致 4,699 条假冲突、并把 1,252 只 A/C 的规模判成整只基金。现该段落 `src_rank_row.custom_pct`，`scale_yi` 恒为 NULL 且不再参与融合。
  - 修正前后（2026-10-31 实测）：`profile_conflicts` 4,907 → 0；`base_pass` 2,050 → 3,025；正式候选 110 → 136。
- 净值：`em_pzd` 主源；`abs ≤ 0.001 或 rel ≤ 1%` 视为一致；净值日期差 ≤ 5 天记 `soft_lag`。
- 收益：`abs ≤ 0.5pp 或 rel ≤ 5%` 视为一致；阶段收益日期必须精确一致（不做日期容差）。
- 类型/身份冲突 → `fund.identity_status='quarantine'`。

### 3.3 净值修订
`fund_nav_daily` 主键 `(fcode, nav_date)`。同一业务日再次抓取且数值变化超过 1e-9 → 旧值写入 `fund_nav_revision`，并 `revision_no+1`。修订不新增行、不删除历史。

## 4. 身份与份额

- `fcode` 是份额主键；A/C 类**不合并**（不同份额有不同费率与净值）。
- `split_class(name)`：末位单个大写字母 A–Z 视为份额字母。
- `family_id = f'{company_name or "UNKNOWN"}::{name_without_class}'`，证据写 `fund_family.evidence_json`。
- 名称相似只能作为 `heuristic_family`，无证据时 `family_scale=NULL`。

## 5. 可投性（buyable_state）

映射（`fuse/profile.py: map_buyable`）：
- `开放申购 / 限大额 / 开放` → `confirmed_yes`
- `暂停申购 / 封闭期 / 认购期 / 暂停` → `confirmed_no`
- 空 → `unknown`

说明：**限大额判可买**（小额仍可申购，只是有单日上限）；未知不伪造。日频快照 `fund_buyable_daily` 保留 `next_open_date` 与 `min_purchase`。

## 6. 持有期与锁定

- `effective_days = max(structured_days, name_inferred_days)`
- 名称推断（按顺序）：
  1. 前缀式 **`N天/日/个月/月/年 持有`** 与 **`一年/两年/三年/五年/半年/十八个月 持有`** —— 东财主流写法
     （`易方达兴利180天持有债券A`、`浦银6个月持有期债券A`、`国富恒丰一年持有期债券A`）；
  2. 后缀式 `持有期N天/日/个月/月/年`（`最短持有180天` 这类来自结构化文本）；
  3. `N个月定开`、`一年/两年/三年/五年/半年/十八个月定开`、`封闭`。
- 换算：日=1、月=30、年=365。
- **2026-10-31 修正**：此前只认后缀式，导致 752 只名称形如 `X个月持有期` / `X天持有` 的债基
  `effective_days=NULL`，直接进入正式榜。修正后 `bond_locked` 13 → **292** 只
  （180 天 167、365 天 75、120 天 28、270 天 12、360 天 8、540 天 2），
  `bond_stable` 136 → **132**（4 只原本入榜的持有期基金被正确移到受限榜），
  `base_pass` 3,025 → 2,906。见 docs/decisions.md ADR-018。
- `effective_days > 90` → `bond_locked` 榜（**不丢弃**，因为占债基池约 33%）。
- 原始 `raw` 字段继续保持 NULL，不做字符串数字混用。

## 7. 风格分类（与主架构书的一处有意偏差）

判定顺序（`fuse/style.py: classify`）：
1. **可转债证据优先**：名称含“转债/可转换/可交换”，或季报持仓中转债市值占披露债券市值 ≥ 20% → `bond_convertible`
2. 类型含“混合二级” → `bond_secondary_enhanced`
3. 类型含“混合一级” → `bond_primary_enhanced`
4. 类型含“中短债” → `bond_short`
5. 类型以“指数型”开头或名称含“债券指数/指数” → `bond_index`
6. 名称含利率债关键词（国债/国开/进出口/农发/口行/政策性金融/地方政府债/政金债/央行票据/利率债）→ `bond_rate`
7. 名称含信用债关键词（信用债/产业债/城投/二级资本债/永续债/中期票据/短期融资券/超短期融资券/公司债/企业债/金融债）→ `bond_credit`
8. 名称含“现金/货币增强” → `bond_cash_enhanced`
9. 其余 → `bond_pure`

**为什么转债优先于类型**：东财把可转债基金统一归入“债券型-混合二级”（2026-10-31 实测 86 只名称含“转债”的基金全部落在该类型）。若先按类型判，会把转债基金错并入二级增强组，导致风险阈值（二级增强 MDD 8% vs 转债 3%）完全放错。

`confidence ∈ structured_confirmed | multi_field_consistent | text_heuristic | unknown`；`conflict/unknown` 不进需要同风格比较的正式组。

### 7.1 持仓证据的覆盖率闸门
`zqcc` 是季报、逐只抓取（实测 0.6 只/秒），不可能与 NAV 同步完成。若只用“有持仓的那部分”判定，
会出现同一类基金一半按持仓、一半按名称的混合口径，风格标签不可比。

- `quality.style_holdings_min_coverage=0.80`，分母 = 当日 `buyable_state=confirmed_yes` 的基金数；
- 覆盖率 ≥ 阈值 → 全部基金都用持仓证据（缺持仓的按名称/类型，证据里 `holding_total_pct=0`）；
- 覆盖率 < 阈值 → 整体不用持仓（`holdings_used=false`），所有基金处在同一证据层级。
- 实测（2026-10-31）：788 / 5,535 = 14% → 闸门关闭，风格全部由类型+名称判定；
  补齐后 5,477 / 5,535 = **98.95%** → 闸门打开（`holdings_used=true`），
  覆盖率进度见 `diagnostics/style_evidence.csv` 的 `with_holdings` 列。
- 打开后的风格迁移（同一批 7,454 只，实测）：`bond_convertible` 87 → **554**（其中 467 只是
  `structured_confirmed`，靠持仓债券代码前缀识别），`bond_secondary_enhanced` 1,915 → 1,695，
  `bond_primary_enhanced` 959 → 713，`bond_pure` 3,439 → 3,438。
  即"名字看不出转债、但持仓里可转债占比 ≥20%"的 467 只被结构性证据纠正 —— 这正是闸门存在的意义。
- 迁移对稳健榜的影响很小（`bond_stable` 136 只 → 持有期修正后 132 只，风格构成 pure 91 / short 35 / rate 7 / credit 2 / cash 1），
  因为转债/增强份额本来就被独立准入路径接走（L6E）。

## 8. NAV 写入策略

- **不设 `src_pzd_nav` 中间表**：`pingzhongdata` 的全历史净值采集时直接落 `fund_nav_daily`，行级带 `nav_source='em_pzd'` 与 `raw_sha256`。理由：全市场约 2300 万行，双写会放大一倍存储与 I/O，且原始响应已在 `raw_object` 完整保留（可重放）。
- 采集粒度：首期全历史 7 天滚动（约 1,058 只/日）；当日增量走 `em_status_snapshot` 快照的单位净值/日期，不全量重抓。
- 空响应（provider_null）→ 7 天内只更新 `attempted_at/last_status`，**不清空旧值**；连续失败 5 次熔断 30 分钟。

## 9. 覆盖度（coverage）与不可证明边界

- `fund_coverage` 记录每只基金 nav_first/last_date、nav_points、gap_max_days、freshness_days。
- flags：`no_nav / stale(>10d) / gap(>14d) / short_history(<365d) / growth_missing`。
- 目录“全覆盖”无法由 API 证明：只能说明“本次抓到 declared_total 与 unique 的比值”，不得 drop_duplicates 后宣称 100%。
- 无法由 API 证明：真实债券持仓评级/久期、真实跟踪误差、历史真实可买状态、用户成交价、未来收益。
- 单源覆盖实测（2026-10-31）：`em_rank?ft=zq` 只返回 5,068 行（declared_total 同为 5,068，页内自洽），而目录口径债券型为 7,454 只 → **排行接口不是债券全集的覆盖证据**，成立日/规模必须由 `em_jbgk` 补齐（规模另需按家族汇总），`fund_coverage` 只把 rank 当“已覆盖子集”。

## 10. Universe 作用域（首期只做债券型）

- `fuse --types bond` 只把目标类型的 `src_directory` 行写入 `fund`；`purge_non_universe()` 会删除不在本期的 L2 行（含其 `fund_share_class/fund_profile/fund_style/fund_buyable_daily/fund_metric/screen_decision...`），并重建 `fund_family`。
- 实测：第一次未加作用域时 `fund` 表写入了全部 28,236 只，风格分类把 17,007 只非债基金默认成 `bond_pure` → 债券榜母体被污染。加作用域后 `fund`=7,454，purge 清掉 20,782 行。
- 后续加入股票/混合型时，只需把 `--types` 扩展为多值，禁止在同一榜内混类型比较。

## 11. 费用模型（jjfl 阶梯 + 外扣情景）

- 数据：`em_jjfl` → `fund_fee_meta`（管理/托管/销售服务费）+ `fund_fee_tier`（purchase/subscription/redemption 三组阶梯）。
- 情景口径 `return_type=fee_net_scenario_assumed`、`fee_model_state=scenario_assumed`：**不是真实成交价**。
- `R_nav` = **与持有天数同窗口**的链式复投总回报（窗口 `(end-D, end]`，只用 `fund_nav_daily.growth_pct`，与指标引擎同口径）；该窗口净值不足 2 个观测时回退 1 年期并记 `nav_return_window_proxy:365d`，两者都没有则 `nav_return_window_unavailable`。
- 比例费：`net = (1+R_nav) × (1-f_out) / (1+f_in) - 1`（申购外扣、赎回内扣）；固定费：先按 `(amount-fixed)/amount` 折算投资本金，再乘净值收益与赎回费；`fixed > amount` → 拒绝并记 `fixed_fee_exceeds_amount`。
- 档位选择：金额/天数区间左闭右闭（天数档 `days_hi` 含端点；最后一档为开区间）；缺档位 → `purchase_tier_not_found / redemption_tier_not_found`，精确费后收益 NULL。
- `user_fit` 顺序（自上而下取第一条命中）：持有天数 < `fund_holding_policy.effective_days` → `conflict_lockup`（附原因码 `lockup_conflict:365d`）；缺档位或缺 1 年净值收益 → `compatible_with_cost_unknown`；用户最大回撤预算 < 基金 1 年 MDD → `risk_budget_conflict`；当日非 `confirmed_yes` → `unknown_or_unavailable`；否则 `compatible_under_modeled_assumptions`。锁定期先于成本判定：算不出费用不代表可以提前赎回。
- 假设四条写入响应 `assumptions`：页面当日费率、无平台额外折扣、自然日/先进先出、管理费托管费销售服务费已在净值中计提不重复扣。
- 费用辅助源不参与正式评分；正式榜的费率仍用 `recurring_fee_pct`（管理+托管+销售服务，持续计提口径），与交易费（申购/赎回）严格分列，不混算。

### 7.2 持仓字段（`fund_holding_bond`）

`bond_code / bond_name / pct_nav / market_value_wan / bond_class / raw_sha256` 全部从 `src_zqcc_holding` 逐行透传：`pct_nav` 是占净值比例（百分点），`market_value_wan` 是持仓市值（万元，与比例独立——比例受规模变化影响，市值不会）；`raw_sha256` 指向那次抓取的原始响应，用于行级重放。早期版本把后两列写成 NULL（ADR-034），已修复并回填 129,845 行。
