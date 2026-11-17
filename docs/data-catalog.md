# 数据目录（Data Catalog）— 公募基金 API 接口手册 2026-11

本文件是《公募基金API接口手册-2026-10》的工程化摘要：哪些接口被首期采用、返回什么、字段单位是什么、有哪些已知坑。
所有接口免 Key、免 Token；判活一律看**业务字段**，不看 HTTP 状态码。

## 1. 域与主源（首期 6 个主源 + 3 个辅助源：费用 / 规模历史 / 经理任职历史）

| 域 | source_id | 主源 | TTL | 业务判活 |
|---|---|---|---|---|
| 名录 | `em_directory` | `fund.eastmoney.com/js/fundcode_search.js` | 1d | 数组非空、代码为 6 位 |
| 申赎状态快照 | `em_status_snapshot` | `fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx?t=8&page=1,50000&js=reData&sort=fcode,asc` | 1d | `datas` 非空、record>0 |
| 基本档案 | `em_jbgk` | `fundf10.eastmoney.com/jbgk_{code}.html` | 56d | 页面含“基金全称”表 |
| 单基金全档案/净值 | `em_pzd` | `fund.eastmoney.com/pingzhongdata/{code}.js` | 1d | `Data_netWorthTrend` 非空 |
| 排行横截面 | `em_rank` | `fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=kf&ft=zq…` | 1d | `rankData.datas` 非空 |
| 债券持仓 | `em_zqcc` | `fundf10.eastmoney.com/FundArchivesDatas.aspx?type=zqcc&code={code}` | 7d | `apidata.content` 存在 |
| 规模历史 | `em_gmbd` | `fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&code={code}` | 30d | `apidata.content` 含份额/资产规模表 |
| 经理任职历史 | `em_jjjl` | `fundf10.eastmoney.com/jjjl_{code}.html` | 30d | 表含“起始期/截止期/基金经理/任职期间/任职回报” |
| 基金生命周期 | `csrc_lifecycle` | `eid.csrc.gov.cn/fund/disclose/advanced_search_report.do` | 3650d | `aaData` 非空、`success!=false` |
| 生命周期 PDF | `csrc_lifecycle_pdf` | `eid.csrc.gov.cn/fund/disclose/instance_show_pdf_id.do?instanceid={id}` | 3650d | 响应以 `%PDF` 开头 |
| 债券总回报指数 | `cb_new_composite` | `yield.chinabond.com.cn/cbweb-mn/indices/singleIndexQuery` | 1d | JSON `CFZS_00` 序列非空 |

费用辅助源：`em_jjfl`（`fundf10.eastmoney.com/jjfl_{code}.html`，T30d）只服务“费用与到手收益”情景模型，不参与正式评分与榜单排序。

规模与经理历史是 2026-11 新增的 as-of 数据源：`em_gmbd` 落 `fund_scale_history`，`em_jjjl` 落 `src_manager_history` + `fund_manager_tenure`；P0-5 起 screen 引擎也复用 45 天披露滞后，只有 `profile_current` 的基金不得进正式榜。

其余可选、不参与首期正式评分：`jdzf`（阶段涨幅）、`FundManagerList`（历任经理）、`funddataIndex dt=8/9`（分红/拆分）、蛋卷 `djapi/fund/nav/history`（历史净值备源）、`FundGuideapi`（筛选器）。

## 2. 六个主源的字段与单位

### 2.1 em_directory（名录）
- 一次全量，约 28,000+ 行（2026-10-31 实测 28,236），5 列：`[code, pinyin_abbr, name, type_text, pinyin_full]`。
- `type_text` 是二级中文类型（如“债券型-长债”“债券型-混合二级”“指数型-固收”）。
- 目录每日变动，**不得硬编码数量**；代码必须按文本处理（保留前导 0）。

### 2.2 em_status_snapshot（全市场申赎状态）
- 单页 27,929 行（`pages:"1"`），13 列：
  `0 code / 1 name / 2 type_text / 3 unit_nav / 4 nav_date(MM-DD) / 5 purchase_status / 6 redemption_status / 7 next_open_date / 8 min_purchase / 9-10 限额 / 11 flag_code / 12 fee_pct`。
- 响应是 `var reData={datas:[[...]],record:"27929",pages:"1",curpage:"1",showday:[...]}`，**键名未加引号**，严格 `json.loads` 会失败 → 先用正则给裸键加引号。
- 申购状态枚举：开放申购 / 限大额 / 暂停申购 / 封闭期 / 认购期 / 空。
- `限大额` 仍可小额申购 → 可投性映射为 `confirmed_yes`。

### 2.3 em_jbgk（基本档案，逐只）
- 静态 HTML 表（`table.info.w790`），单元格 `<th>标签</th><td>值</td>`，**部分 td 未闭合** → 用“到下一个 th/td/tr”的 lookahead 正则。
- 关键字段：基金全称、成立日期/规模、基金管理人、基金托管人、净资产规模（形如 `63.80亿元（截止至：2026年09月30日）`）、份额规模、管理费率、托管费率、销售服务费率、最高申购/赎回费率、业绩比较基准、跟踪标的、基金类型。

### 2.4 em_pzd（pingzhongdata，单基金全历史）
- 一次请求约 500KB、27 个 JS 变量；`Data_netWorthTrend` 为 `[{x:毫秒时间戳, y:单位净值, equityReturn:日增长率%}]`（全历史，实测单基金最多 3741 点）。
- `Data_ACWorthTrend`：`[[毫秒时间戳, 累计净值]]`。
- 其他：`syl_1n`（近 1 年 **累计**收益，非年化）、`fund_Rate`/`fund_sourceRate`、`fund_minsg`、`Data_currentFundManager`、`Data_fluctuationScale`（规模序列，亿）、`Data_assetAllocation`、`Data_holderStructure`、`Data_performanceEvaluation`。
- 这是**全历史 NAV 的最高性价比来源**：一只基金一次请求拿全序列，实测覆盖率 40/40。

### 2.5 em_rank（排行横截面）
- `op=ph&dt=kf&ft=zq&rs=&gs=0&sc=1nzf&st=desc&sd=YYYY-MM-DD&ed=YYYY-MM-DD&qdii=&tabSubtype=,,,,,&dx=1&pi=N&pn=500`。
- `datas` 每行是逗号分隔字符串，前 17 位：`0 代码 1 名称 2 拼音 3 净值日期 4 单位净值 5 累计净值 6 日涨幅 7 近1周 8 近1月 9 近3月 10 近6月 11 近1年 12 近2年 13 近3年 14 今年来 15 成立来 16 成立日`；17 自定义标识、**18 自定义区间涨幅（不是规模）**、19 原费率、20 现费率、21+ 其他标记。
- 第 18 段实测纠错（2026-10-31）：与 `近3年` 相关系数 **0.9989**（000003 该段 16.1290 / y3 15.27；110027 10.7606 / 10.52；005273 62.8714 / 62.55），与 jbgk 净资产完全无关（000003 家族合计 0.80 亿）→ 落库为 `src_rank_row.custom_pct`，`scale_yi` 恒为 NULL。**规模只能从 jbgk 按家族汇总**。
- `sd/ed` 必须是真实日期，用 `..` 会返回 0 字节假成功。

### 2.6 em_zqcc（债券持仓，季报）
- `FundArchivesDatas.aspx?type=zqcc&code={code}&topline=10`，Referer 必须精确到 `https://fundf10.eastmoney.com/ccmx1_{code}.html`。
- 响应 `var apidata={ content:"<HTML>",arryear:[...],curyear:...};`，HTML 内单引号被转义（`\'`），需先切 content 再反转义。
- 行：债券代码 / 债券名称 / 占净值比例（%） / 持仓市值（万元）。
- 用途：可转债仓位证据（代码前缀 110/111/113/118/123/127/128/132 或名称含“转债”）。
- 2026-11-05 按年回填 2016–2026：`src_zqcc_holding` 1,900,098 行 / 7,928 只 / 43 个季度，2026Q3 7,897 只；`scripts/backfill_holdings.py` 可断点续跑，成功响应落 `raw_object/fetch_log`，不生成模拟持仓。

### 2.7 em_jjfl（申赎费阶梯，费用辅助源）
- `https://fundf10.eastmoney.com/jjfl_{code}.html`（46–50KB），Referer 同域；四张表：申购（含原费率与优惠费率两列）、认购、赎回（按持有天数分档）、管理费/托管费/销售服务费。
- 解析为 `operating{mgr_fee_pct,trust_fee_pct,sales_fee_pct}` + `purchase[] / subscription[] / redemption[]` 三组阶梯；每档含金额区间或天数区间、`rate_pct`、`discount_pct`；“每笔1000元”这类固定费 → `fixed_cny=1000`、`rate_pct=NULL`（不许当 0）。
- 实测 000032：申购 0.80/0.08、0.50/0.05、0.30/0.03、≥500 万固定 1000 元；赎回 0–6 天 1.50%、7–29 天 0.75%、30–364 天 0.10%、365–729 天 0.05%、≥730 天 0；运作费 0.35/0.10/0.00。
- 2026-11-02 全量回放：`fund_fee_tier` 54,047 行、`fund_fee_schedule` 37,781 行（sub 19,017 / red 18,764）；每档保留原始文案 `raw_label`（“不小于7天/小于30天/T<7日”等），`fund_fee_meta.redemption_1y_pct` 覆盖 8,068 只。
- 解析器修正后用 `python3 -m fundana fees` 从 `raw_object` 回放，不重新联网；回放 8,138 只 0 失败。

### 2.8 em_gmbd（规模历史，研究/回测）
- `https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&code={code}`，返回 `var apidata={content:"<HTML>",...}`，content 内是一张“报告期 / 期间申购 / 期间赎回 / 期末总份额（亿份）/ 期末净资产（亿元）”表。
- 解析器 `fundana.fetch.parsers.parse_gmbd` 返回 `{rows:[{report_period, share_scale_billion, asset_scale_billion}], count}`；落 `fund_scale_history`（PK `fcode+report_period`，`src='em_gmbd'`）。
- 000003 实测 57 期（2013-03-20 至 2026-09-30）；全库 8,055 只 / 234,415 行 / 3,185 个报告期（2002-09-20 至 2026-11-03）。86 只无有效规模行，与无净值/后端别名/2026 新基金高度重合，不强行补零。这是 `walkforward` 与 `screen` 的 as-of 规模来源：`fundana.fuse.scale.scale_asof_map(conn, as_of, lag_days=45)` / `share_scale_asof_map(...)` 只取 `report_period <= as_of-45天` 的最近一期，避免季报前视；缺失时回退 `fund_profile.asset_scale_billion`，并标 `profile_current`，正式榜拒绝。

### 2.9 em_jjjl（经理任职历史，研究/证据）
- `https://fundf10.eastmoney.com/jjjl_{code}.html`，约 47KB；第二张表（class 含 `jloff`）列：起始期 / 截止期 / 基金经理 / 任职期间 / 任职回报。
- 解析器 `parse_jjjl` 返回 `{rows:[{mgr_id,mgr_name,start_date,end_date,days,return_pct}], count}`；一行多经理按 `<a href=".../manager/{id}.html">` 拆分；`end_date` 为空表示在任。
- 落 `src_manager_history`（原始行）与 `fund_manager_tenure`（按 `business_date` 重算 `days`，在任者 `end_date=NULL`）。000003 实测 8 条；全库 **8,109 只 / 46,936 行**，32 只无有效任职表（2026 新基金/无经理页）。
- 注意：`fund_manager_evidence.career_days` 是证券从业年限，不是本基金任职年限；两者不可混用。

### 2.10 csrc_lifecycle（证监会 EID 生命周期公告）
- 仅使用证监会基金电子披露网站公开接口；`http://eid.csrc.gov.cn/fund/disclose/advanced_search_report.do`。
- `aoData` 必须是 DataTables 数组（含 `sEcho/iDisplayStart/iDisplayLength`），不能是扁平 dict，也不能二次 URL 编码；无 `fundCode` 时日期窗口必须 ≤30 天，否则返回 `success:false`。
- 枚举 `FB060010` 清算报告、`FC030010` 合同终止、`FC030030` 基金清算、`FC050050/FC050060` 合并；2015 起按 30 天切片，1 req/s。
- 落 `fund_lifecycle(fcode,status,effective_date,last_nav_date,reason,source,raw_json,created_at)`；`effective_date` 优先 PDF“进入清算期”，其次“最后运作日”，最后才用公告日并写 `effective_date_source`。
- 边界：合并公告实测 2023–2026 为 0 条，不能凭“目录消失/NAV 停更”推断清盘；没有公开的批量清盘名单。
- 2026-11-05 全量：1,819 只清盘样本（PDF 解析 1,564 成功、1 失败），合并 0；EID 公告无基金类型，回测宇宙只纳入当前仍在 `fund` 表或公告名称明确含“债”且不含混合/股票/货币的样本（债券型 510 只），宁可少纳入也不把非债基金塞进债券宇宙。2026-11-06 用 `scripts/backfill_liquidated.py` 把其中 445 只债券型清盘基金按公开档案/净值补入 `fund`（332,872 行 NAV）；合并样本仍为 0。

### 2.11 cb_new_composite（中债-新综合财富总值指数）
- `https://yield.chinabond.com.cn/cbweb-mn/indices/singleIndexQuery`，POST 表单 `indexid=8a8b2ca0332abed20134ea76d8885831&qxlxt=00&ltcslx=&zslxt=CFZS&lx=1`，返回 `{"CFZS_00":{"<ms_epoch>":value}}`。
- 解析器 `parse_chinabond_index` 按 UTC+8 把毫秒时间戳转交易日，落 `market_index_daily`（`source_id=cb_new_composite`，代码 `CBC_CFZS_00`）；实测 6,211 个交易日（2002-01-04…2026-11-04）。
- 用途：稳健债基的真实总回报基准；增强债仍不适用债券总回报基准，报告只列 ETF 参考。

## 3. 派生研究表（非外部源）

### 3.1 fund_metric_snapshot（季度指标快照）
- 迁移 `0034_metric_snapshot.sql`；主键 `(fcode, as_of, period_days, formula_contract)`，索引 `(as_of, period_days, formula_contract)`。
- 字段：`start_date/end_date/n_obs/years/annual_dec/vol_dec/mdd_dec/sharpe/sortino/calmar_dec/positive_ratio_pct/non_negative_ratio_pct/flat_ratio_pct/coverage_ratio/nav_sig/computed_at`。
- 生成：`python3 -m fundana snapshot-metrics --as-of 2026-09-30 --quarters 12`；用 `metrics_engine.compute_fund(..., dry_run=True)` 逐季重算，只写快照表，不碰生产 `fund_metric`。
- 2026-11-04 回填：248,763 行、12 个季末（2023-12-31…2026-09-30）、365/1095/1825 各 82,921 行；最新季 8,018 只；每季失败 2–14 只（净值不足/窗口无效），不写 0；formula 全部 `fundana-risk-2026.11.5`。
- 边界：逐季重算不是当时生产库原样；历史风格/费率/份额/tenure 仍是当前值；快照只含存续基金（清盘样本已进 `fund_lifecycle`，但未进该快照表）；`walkforward` 尚未默认读快照。

### 3.2 fund_factor_exposure / 久期信用桶（诊断）
- `fund_factor_exposure` 存每只基金对国债（000012）、企债（000013）、中证转债（000832）的 365 天日收益回归：`beta_duration_raw`、`beta_credit_raw`、`beta_convertible_raw`、`r2`、`n_obs`、`as_of`、`alpha_daily/alpha_annual_pct/alpha_t`。
- `fundana/factor_buckets.py` 只读该表与 `fund_style`：duration 用 raw beta，short<0.25、mid 0.25–0.75、long≥0.75；credit low<0.10、mid 0.10–0.60、high≥0.60；`n_obs/252<0.60` 或 `r2<0.05` → unknown。
- 桶只用于报告与组合诊断，不进入筛选分、不改变准入；`style_duration_matrix` 输出风格×桶矩阵与各风格 beta p10/p50/p90/跨度。

### 3.3 公开序列覆盖缺口（2026-11-05）
- 定向补齐后（含 2026-11-06 清盘基金回填）：当前存续 8,141 只、历史清盘 445 只，共 8,586 只；profile 8,586；nav 8,693；fee 8,644；scale 8,500；manager_current 8,229；holding 7,928（2026Q3 7,897；历史 43 个季度、1,900,098 行）；lifecycle 1,819（其中 458 只有公开净值）。
- 残余缺口：scale 86、manager 32、nav 90、fee 1。manager 32 只全是“(后端)”收费份额，东财无独立经理页；scale 86 含 32 只后端份额、47 只无成立日的新/特殊份额，其余为 C/E/D 类无独立规模页；nav 90 含 32 只后端份额、48 只无成立日份额，其余为 C/E/D 类或理财债基无独立净值序列；fee 缺口 028660 为 2026-08 新基金，页面尚无管理费/托管费表。
- 2026-11-05 22:00 对上述缺口逐只重采验证：nav 90 只全部 `provider_null`（pingzhongdata 无净值序列），scale 86 只 `parse_failed`（gmbd 返回 404/无表），manager 32 只 `parse_failed`（jjjl 页面“基金经理变动一览”为空），fee 1 只 `parse_failed`（jjfl 页面只有通用费率说明、无管理费/托管费表）。这是公开源确实没有数据，不是解析器漏字段。
- 2026Q3 无持仓的基金共 244 只：57 只无成立日、125 只 2026 年成立（Q3 报告期尚未覆盖）、62 只 2026 年前成立；62 只中 40 只是“(后端)”收费份额（东财无独立持仓页、历史上也 0 行），其余 22 只有历史持仓但无 2026Q3，其中多只已清盘（519722、519324、008549、008516、008904、010254、014384、019406、019836 等）。这是公开源披露时点/份额结构造成的缺口，不是回填失败；不能用模拟持仓补。
- 这些代码不进入正式榜的规模/经理/费率门槛；不得用 A 类数据、模拟值或目录推断填充。

## 4. 通用工程约定

1. **Referer 精确到页面**：FundArchivesDatas 必须带 `ccmx_{code}.html` / `ccmx1_{code}.html`；`f10/lsjz` 必须 `fundf10.eastmoney.com`。
2. **编码三分**：腾讯/新浪 = GBK；HBJJ_pjsyl = GB18030；其余 UTF-8（解码顺序 utf-8 → gb18030 兜底）。
3. **HTTP 200 ≠ 成功**：空数组、`Datas:null`、`ErrCode:-999`、Content-Length:0、HTML 错误页均按失败处理，且**不重试**（业务空）。
4. **分页上限**：`f10/lsjz` 单页固定 20 条（pageSize 被忽略，500 会软失败）；`rankhandler` pn 可达 10000；蛋卷 nav/history size 可到 1000。
5. **限频**：单 IP 并发 ≤ 2 QPS；名录/档案 24h、净值 6h、实时 5s、公告 1h TTL。
6. **无风险利率 rf 无公开接口**：中债 `searchYc` GET 405 / POST 返回 `[]`，货币网两个候选端点都是 HTML 首页 → rf 只能配置化观测值（`config/app.yaml: risk.risk_free_rate=0.0164, observed_at=2026-10-25, source=chinabond_10y`），变更时全量重算。

## 5. 已知失效接口（不要浪费时间）

| 接口 | 现状 |
|---|---|
| `fundgz.1234567.com.cn` 实时估值 | 已下线 |
| 新浪 `fundInfo` openapi | 已下线（NetValueReturnOpen 正确路径亦难打通） |
| `quotes.sina.cn` | 404 |
| `Market_Center` fund_* 场外节点 | 返回空 |
| `gs.amac.org.cn` | 500 |
| 韭圈儿 / 雪球 / 蛋卷 fund/asset、holdings、rate、estimate | 需登录或 405 |
| `datacenter-web RPT_FUND_*` | 9501 未公开 |
| 中债 `yield.chinabond.com.cn` 收益率曲线 | `searchYc` GET 405 / POST 空数组；仅指数 JSON 接口 `singleIndexQuery` 可用（见 2.11） |
| `FundMNewApi/*` | 需 App 签名（ErrCode 61136403） |