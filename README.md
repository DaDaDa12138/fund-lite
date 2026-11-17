# Fund-ANA V2 · 债券型基金筛选（首期）

从零重建的国内公募基金筛选系统。**首期只做债券型基金**，其他类型按插件方式后续接入。
数据全部来自公开免 Key 接口；本项目只做本地融合、指标计算与规则筛选，不构成投资建议。

## 设计来源

- 工程主架构书《Fund-ANA_V2_国内公募基金全类型筛选_从零重建工程主架构书》
- 参考《Fund-ANA 完整审查与无源码重建规格书》（只参考口径，不照搬源码）
- 《公募基金API接口手册-2026-10》/《公募基金开放API清单-2026-10》

## 目录

```
config/            app.yaml, source_registry.yaml, field_policy.yaml
fundana/
  cli.py           命令行入口（bootstrap/probe/collect/fuse/metrics/screen/publish/verify/replay/serve/stats/walkforward/turnover/exposure/factors/portfolio/fees/events/alerts）
  config.py        配置加载与 config_hash
  db.py            SQLite 连接/迁移/run/fetch_log/raw_object
  collect.py       17 个源的采集与解析落库（名录/状态/档案/净值/排行/费用/规模/经理/持仓/指数/生命周期等）
  fetch/           HTTP 限频、TTL 缓存、解析器
  fuse/            identity/profile/nav/style/coverage 融合
  metrics/         window/returns/risk/tail/contract 指标引擎
  fees/            jjfl 阶梯 + 持有期总成本（A/C 份额同尺；fees 命令可从 raw 回放）
  screen/          thresholds/scoring/engine L1–L10（含 L8R 独立风险门）
  audit/           walkforward / portfolio / turnover / exposure / events 复盘工具
  publish/         builder/commit/verify 唯一提交点
  api/             本地只读 HTTP API（server/query/storage）
web/              零构建单页前端（index.html/app.js/styles.css）
  migrations/      0001_init.sql … 0036_maintenance_state.sql（36 个迁移）
docs/              architecture / decisions(ADR) / data-catalog / fusion-rules /
                   formula-contract / screening-rules / runbook /
                   audit-2026-11-review4.md（第四轮审查与报告优化）
tests/             stdlib unittest（576 项，2026-11-06）
var/               db / raw / freeze / state（运行时生成）
reports/           runs/<run_id>/revisions/<uuid>/ + .publication_current
```

## 快速开始

```bash
python3 -m fundana bootstrap --verify-config --migrate
python3 -m fundana probe --sources all --strict
python3 -m fundana collect --domain directory --types bond
python3 -m fundana collect --domain status --types bond
python3 -m fundana collect --domain rank --types bond
python3 -m fundana collect --domain nav --types bond
# 档案/持仓按"当日可申购"收窄，避免为已被 L2 淘汰的基金浪费配额（见 docs/decisions.md ADR-012）
python3 -m fundana collect --domain profile --types bond --only-buyable --skip-collected
python3 -m fundana collect --domain holding --types bond --only-buyable --skip-collected
# 债券基金的历史前十大持仓（2016–2026 按年回补；scripts/backfill_holdings.py 可断点续跑）
python3 scripts/backfill_holdings.py --years 2016-2026
python3 -m fundana collect --domain fee --types bond --only-buyable --skip-collected
# 规模/经理历史：walkforward 的 as-of 规模与经理任职证据（研究用，不改正式榜准入）
python3 -m fundana collect --domain scale --types bond --skip-collected
python3 -m fundana collect --domain manager --types bond --skip-collected
# 定向补齐个别公开序列缺口（不改变全量采集逻辑）
python3 -m fundana collect --domain scale --codes 000108,000140 --force
python3 -m fundana fuse --types bond
python3 -m fundana metrics
python3 -m fundana comparability --as-of 2026-10-30  # P0-2：可比性旗标（market_as_of）
python3 -m fundana tail          # V2 尾部指标（3 年，影子）
python3 -m fundana screen --profile bond_v1
python3 -m fundana shadow-rank --run-id <run_id>      # v9 同池影子排序（research_only）
python3 -m fundana shadow-forward --run-id <run_id>   # v9/v10 core10/core50 前瞻（research_only）
python3 -m fundana replay-screen --run-id <run_id>    # P0-1：PIT 快照重放 base_gate+score（0/0 才通过）
python3 -m fundana lifecycle-collect --start 2015-01-01  # 证监会 EID 清盘/合并公告（可月度增量）
python3 -m fundana lifecycle-import path/to/lifecycle.csv  # 也支持外部 CSV 导入
python3 -m fundana portfolio-diagnostics --run-id <run_id> --board bond_core10  # P1-1：相关矩阵/有效 bet
python3 -m fundana stat-validation --run-id <run_id> --board bond_core10      # P1-3：块自举 + Deflated Sharpe
python3 -m fundana data-health --skip-integrity --days 7  # P2：DB/迁移/净值/采集/来源/发布健康
python3 -m fundana survivorship --lookback-days 30 --limit 20  # P1-7：目录消失候选（不等于清盘）
python3 scripts/audit_survivorship.py --years 2020,2021,2022,2023,2024,2025  # 清盘纳入/排除的 walkforward 对照
python3 -m fundana capacity --as-of 2026-11-04        # P2-11：风格×规模桶业绩中位数（研究）
python3 -m fundana oos --test-quarters 3              # P2-15：季度 OOS 研究模型（research_only）
./scripts/ci.sh                                       # P2-12：单测 + 前后端契约 CI
python3 -m fundana publish
python3 -m fundana verify --fast
python3 -m fundana alerts             # 当前发布的产品告警（净值/经理/规模/回撤/风格）
python3 -m fundana walkforward --years 2020,2021,2022,2023,2024,2025 --portfolio
python3 -m fundana walkforward --years 2020,2021,2022,2023,2024,2025 --portfolio --portfolio-weighting inverse_vol  # 风险预算实验，不设默认
python3 -m fundana events --years 2020,2021,2022,2023,2024,2025
python3 -m fundana serve            # 网页前端：http://127.0.0.1:8091/（API 在 /api/v1/）
python3 scripts/acceptance.py       # 15 条验收标准（含跨源核对、发布门禁、前瞻 not_ready、研究池与缓释审计）
```

> 注意：同一时刻只跑一个**写库**任务（collect / tail / fuse / screen / publish 都算）。SQLite 单写者，NAV 采集每只基金约 3,000 行的批量写入
> 会长时间占用写锁，使并行的档案采集降到 0.2 只/秒（实测），串行反而更快。

## 网页前端

日常用常驻启动器（推荐）：

```bash
scripts/daemon.sh start     # 网页(8091) + 日更调度器一起后台常驻
scripts/daemon.sh status    # 两个服务都做真实健康检查（web 探 /api/v1/healthz，scheduler 看心跳新鲜度）
scripts/daemon.sh restart   # 改完代码后重启
scripts/daemon.sh stop
```

也可以单跑：`scripts/serve.sh start|stop|restart|status|logs`（只管网页）、`scripts/scheduler.sh ...`（只管日更）。
启动器固定 cwd 在项目根目录（直接在别的目录跑 `python3 -m fundana serve` 会报 `No module named fundana`），
用 `nohup` 脱离终端，并在启动失败时自动打印最近 20 行日志；端口被占用会明确提示占用者。日志在 `var/state/serve.log`、`var/state/scheduler.log`。

**开机/登录自启**（2026-11-02 主机重启后两个服务都没有自启，这是“前端打不开”的根因）：

```bash
scripts/install-autostart.sh install           # systemd --user，登录后自启 web + scheduler
scripts/install-autostart.sh install --linger  # 开机即启动，不要求先登录（可能触发 polkit）
scripts/install-autostart.sh status
scripts/install-autostart.sh uninstall
```

没有 `--linger` 时服务在用户登录后启动；要开机未登录也启动，执行
`loginctl enable-linger $USER`。unit 名：`fundana-web.service`、`fundana-scheduler.service`。
`serve.sh` / `scheduler.sh` 检测到这些 unit 时会把 start/stop/restart 委托给
`systemctl --user`，并在 pidfile 丢失但健康检查通过时自动接管监听进程。

手工前台运行：`python3 -m fundana serve` 后打开 <http://127.0.0.1:8091/>，十五个页面：概览、榜单、跟踪、**对比**、自选（收藏）、持有期、研究、
基金详情、数据库、运行状态、诊断、质量、**API 健康**、前瞻、发布报告。默认绑定 `0.0.0.0`，启动日志会打印局域网地址
（本机实测 <http://127.0.0.1:8091/>）；只想本机访问就把 `config/app.yaml` 的 `api.host` 改回
`127.0.0.1`。默认只读无鉴权；在 `config/app.yaml` 设置 `api.auth_token` 可启用 Bearer Token（ADR-092），
但仍**不要做端口映射暴露到公网**。

- **配色按国内习惯：红涨绿跌**（`--up #d92b2b` / `--down #0e9f6e`，暗色自动换色）；0 与 NULL 不着色，NULL 显示「—」。
- **概览页**：先横截面（近一年中位年化 +2.04%、正收益占比、中位夏普、中位回撤、收益分布直方图、各风格表现表），
  再往下才是筛选漏斗与淘汰原因；四格 hero 给出债基数/净值观测/当日可申购/正式入选。
- **榜单页**：五个周期列（近 1 周/1 月/3 月/6 月/1 年），可切换「区间收益 / 年化」；表头可点排序；
  每行有 ☆ **收藏开关**（已收藏再点即取消，成功给绿色提示，不再报「已经在收藏里」）与**对比勾选**（最多 3 只）。
- **对比页**：最多 3 只并排 15 行指标 + 归一化净值多线（起点=100）+ 年度收益表，支持 `#/compare?codes=000032,000003`。
- **API 健康页**：列出 `source_registry.yaml` 全部接口的地址、角色、调用数、业务成功率、p50/p95、慢请求率、最近成功、新鲜度与健康度；异常/降级接口单列详情，时间窗 1/7/30 天。
- **基金详情页**：hero 头（最新净值/日涨跌/1 年年化/1 年回撤/家族规模）+ **八个页签**——概览（九周期 + 同类分位
  「好于同类 x%」+ 净值曲线）、业绩（阶段涨幅 + 年度收益条形图 + 周/月/年复投表）、净值明细（全历史逐日分页）、
  风险（九周期全表 + 三年尾部 V2 + 回撤曲线）、持仓（债券明细 + 类别环形图）、费用（到手收益计算器 + 申赎阶梯）、
  经理、档案；页签可深链 `#/fund/000032?tab=risk`。
- **收藏页**：长期观察列表（输入代码 + 备注，或从榜单/详情点 ☆ 加入），显示风格、1 年年化、Sharpe、
  1 年回撤、日胜率、家族规模与当日可否申购，可随时移除；只存指针，每次打开都取最新一次采集
  （ADR-032）。
- **数据库页**：只回答两件事——数据库文件多大（含 WAL）、整个项目目录占多少硬盘；再给占用构成
  （DB / `var/raw` / `reports` / `var/freeze`）与所在磁盘剩余。表级行数用
  `python3 -m fundana stats` 看，页面不再展开 32 张表。
- **只读 SQL 接口**：页面已下线，`POST /api/v1/query` 保留给验收脚本与调试（单条 `SELECT`/`WITH`、
  最多 1000 行、3 秒超时、`mode=ro` 打开，ADR-016）。
- **诊断页**：当前发布的 9 个诊断 CSV（覆盖/冲突/NULL/风格证据/家族重复/增强债差项/家族去重榜/尾部覆盖/费用覆盖），
  点卡片预览前 500 行；增强榜为空时榜单页会自动读 `enhanced_near_miss.csv` 解释“为什么是 0 只”（当前增强榜 103 只，该解释块不出现）。
- **净值曲线**：基金详情页内联 SVG（近 3 年、等距抽样 ≤700 点），不引外部图表库
- **逐日净值明细**：全历史可查，起止日期 + 「近 1 年 / 近 3 年 / 全部」快捷区间 + 每页 50–500 翻页
  （`/api/v1/funds/<code>/nav/rows`）；周/月/年表同时给"复投总回报"与"单位净值涨跌"两个口径
  （`/periods`，差异就是分红贡献，见 ADR-029）
- **指标字典与列头说明**：52 条指标（口径、单位、方向）与 13 层漏斗门槛说明都由 `fundana/api/metrics_meta.py` 生成，
  列头 ⓘ 悬停看口径、点击弹说明卡；漏斗每层直接写出门槛数值（改 `screen.thresholds` 自动同步）。
- **全列排序**：榜单与持有期榜的表头都可点排序（白名单在 `_rankings.sort_map` / `locked.SORT_MAP`，未知键回退排名）；**Shift + 点击**可叠加最多 3 个排序键（表头显示 ↓¹↓²，地址栏同步 `&sort=a,b&order=desc,desc`），每个榜记住上次排序。
- **排名走势**：基金详情「决策」页签顶部画历次筛选的排名折线（左轴排名反转、右轴正式分虚线），能看出名次变化是自身指标还是门槛线移动造成的。
- **契约自检**：`scripts/check_ui_contract.py` 静态比对「列头说明 / 排序白名单 / SQL 列名」三处，第 5 项反向遍历 `PRAGMA table_info` 找「既无说明又没标内部列」的缺口，已并入验收第 10 条。
- **导出与反事实**：榜单/受限榜可把当前筛选与排序原样导出 CSV（上限 5,000 行，UTF-8 BOM、代码保文本）；决策页可看「按上一轮门槛我能不能过」与估算名次（只算门槛不重跑，ADR-041/042）。
- **决策轮次对比**：基金详情「决策」页签可下拉选择历史轮次，看门槛移动量、自身指标变化与「转为通过/未通过」。
- **中文标签层**：页面不出现 `bond_* / em_* / annual_1y` 这类内部代码，服务端统一翻译
  （`fundana/labels.py`，十四组字典，ADR-028）；风格、置信度、筛选层、淘汰原因、诊断列名、
  运行模式/状态/数据来源、费用口径与适配都是中文；发布产物 `summary.md` 写盘时即已翻译
- **研究页**：增强债 3 年/2 年影子榜（`fundana shadow --window 1095`，research_only，不参与发布）、
  v9 同池影子排序（`fundana shadow-rank`，daily 自动记录）与增强观察池（距增强准入门槛差 ≤1 项）；
  v9/v10 影子前瞻由 `fundana shadow-forward` 写入独立 `shadow_*` 表，并通过
  `/api/v1/shadow-forward/summary` 提供研究 API；研究页已展示证据等级、成熟/未到期与
  v9/v10 中位差，不混入正式前瞻证据；
  基金详情页另有现任 + 历任经理表（职业年限/任期收益/东财评分/起止日期/任职天数，非 MGOLD）。
- **P1 证据**：榜单主表显示风险门状态与 3/5 年覆盖、份额规模、现任经理最短任职；
  成本列 1/3/5 年 + 满 1 年赎回费；`portfolio-diagnostics` 输出平均相关/有效独立 bet
  （gen69 core10：0.540 / 1.707），`stat-validation` 输出块自举超额与 Deflated Sharpe
  （同轮 DSR 0.660，5% 不显著）。
- **P2 数据健康**：`data-health` 检查 DB 完整性、迁移漂移、净值新鲜度、指标覆盖、采集成功率、
  来源冗余与发布门禁；默认跳过 3.9GB 库的 `quick_check`，加 `--skip-integrity` 用于日常。
- **发布报告页**：summary.md 全文 + manifest 文件清单，点击文件名可下载（白名单校验，防目录穿越）。

支持深链：`#/rankings?board=bond_enhanced&sort=annual_1y`、`#/fund/000032?tab=risk`、`#/compare?codes=000032,000003`、
`#/watchlist`、`#/diagnostics`。

细节见 [docs/web.md](docs/web.md)。

## 关键口径

- **收益 = 日增长率链式复利的复投总收益**；单位净值/累计净值都不是总回报（实测 000053 差 91pp）。
- 夏普/索提诺/卡玛为**策略口径**（`standard_metric_claim=false`）；Sortino 零下行 → NULL，Calmar 零回撤 → NULL。
- 缺失一律 NULL，禁止 0 填充；`attempted=false` 与 NULL 严格区分。
- 发布原子提交：`reports/.publication_current` 符号链接 + `validated.json` 逐文件 SHA256；失败保旧。
- 本工具仅个人研究使用，遵守接口限频（≤2 QPS），不转售原始数据。

## 费用情景

`/cost` 回答“买入金额 A、持有 D 天，按披露费率算到手多少”，不冒充真实成交价
（`return_type=fee_net_scenario_assumed`，`standard_metric_claim=false`）。

```bash
curl -s "http://127.0.0.1:8091/api/v1/funds/000032/cost?amount=10000&holding_days=400" | python3 -m json.tool
```

返回 `nav_return_pct`（**与持有天数同窗口**的链式复投总回报，见 `nav_return_window_days`）、`net_return_pct`（扣申购/赎回费后）、
`net_return_discount_pct`（按优惠费率）、命中的申购/赎回档位、`user_fit` 五态：
`conflict_lockup`（持有期短于锁定期，先于费用判定）→ `compatible_with_cost_unknown`（缺档位/缺 1 年收益）
→ `risk_budget_conflict`（超出用户回撤预算）→ `unknown_or_unavailable`（当日非确认可申购）
→ `compatible_under_modeled_assumptions`。网页基金详情页有同一计算器。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 产物

`reports/.publication_current/`（符号链接指向 `runs/<run_id>/revisions/<uuid>/`）：

| 文件 | 内容 |
| --- | --- |
| `bond_stable.csv` / `bond_enhanced.csv` / `bond_core.csv` / `bond_core10.csv` / `bond_watch.csv` / `bond_rookie.csv` / `bond_locked.csv` | 稳健榜 / 增强榜 / 核心精选榜（50 只子集） / 收益精选榜（10 只子集） / 观察榜 / 新锐榜 / 持有期>90天受限榜 |
| `all_formal_candidates.csv` | 全部正式候选（含未入榜原因） |
| `decisions/<fcode>.json` | 单只基金的阈值、分数、原因码、九周期指标 |
| `diagnostics/coverage.csv` · `conflicts.csv` · `nulls.csv` | 净值覆盖、多源冲突、字段缺失 |
| `diagnostics/style_evidence.csv` | 风格证据分布（含 `with_holdings`，看持仓证据覆盖） |
| `diagnostics/tail_coverage.csv` | 3 年尾部指标按风格聚合的完整度与平均回撤 |
| `diagnostics/fee_coverage.csv` | 每只基金的 jjfl 档位覆盖（7,454 行，缺档位一眼可见） |
| `diagnostics/family_duplicates.csv` · `bond_stable_by_family.csv` | 同家族多份额入榜明细 / 家族去重视图 |
| `diagnostics/enhanced_near_miss.csv` | 增强债"差几项"清单（解释空榜） |
| `freeze/*.jsonl.gz` | 本轮冻结输入（离线重放用） |
| `validated.json` | 逐文件 SHA256 + 大小，发布门禁的唯一凭据 |

## 状态

- M0–M6 已实现：采集、融合、指标、筛选、发布、API、CLI、重放。
- **当前状态（2026-11-06，run `20261106T094408-screen-653783`，generation 74）**：候选 8,130 只，
  基础门禁 3,024 只，L9 通过 157 只，family/company 折叠 33 只后正式榜 **124** 只
  （稳健 97 + 增强 27）；观察榜 core50 50 / core10 10 / long 20，研究榜 watch 94 / locked 15 / rookie 30；
  单测 576 项、schema 36 个迁移；发布门禁 `status=warning`（5 条：forward_not_ready、valuation_unavailable、
  dynamic_boundary、cross_board_family、dsr_skill）。watch 94 只均在 L2 被拦下：93 只
  `buyable_confirmed_no`、1 只 `exchange_traded`，是“质量达标但当前不可申购/场内交易”的观察名单，
  不是风险门拦截，也不是推荐名单；2026-11-05 已完成历史持仓回填（1,900,098 行 / 7,928 只 / 43 个季度，
  2026Q3 7,897 只），其中可申购基金覆盖 5,995/6,062（98.9%），正式榜持仓覆盖 2026Q3。
  2026Q3 无持仓 244 只：57 只无成立日、125 只 2026 年成立（报告期未覆盖）、62 只老基金
  （40 只后端收费份额无独立持仓页，22 只多为已清盘），不是回填失败，不能用模拟持仓补。
  数据覆盖：当前存续 8,141 只、历史清盘 445 只（共 8,586 只）；profile 8,586、
  nav 8,693、fee 8,644、scale 8,500、manager 8,229、lifecycle 1,819（其中 458 只有公开净值）。
  当前存续基金残余缺口 86/32/90/1（scale/manager/nav/fee）含后端收费份额、无成立日/2026 新基金、
  C/E/D 类无独立公开序列；逐只重采已验证为公开源无数据（provider_null/404/空表），
  不是解析器漏字段，公开 API 无数据时不编造。证监会 EID 已确认清盘的 445 只债券型基金已用公开档案/净值回填，
  仅用于历史 walkforward 与可投性披露；剩余清盘样本、合并样本仍不完整，历史 IC/DSR 的幸存者偏差只做了部分校正（见 docs/audit-2026-11-review4.md §13）。
- 历史基线（2026-11-01，7,459 只债券型，发布 generation 21）：基础门禁通过 2,910 只、正式入选 **205** 只
  （稳健 118 + 增强 87）、持有期受限 292 只；当时单测 188 项、验收 15/15。
- 净值业务日修正（ADR-043）：pingzhongdata 的 x 是北京时间零点，原实现按 UTC 解释导致 901 万行 nav_date
  整体左移一天；已改为固定 +08:00 并用 raw_object 重放重写（7,365 只 / 0 失败 / 479s），
  再用 B1 全市场快照抽 50 只交叉核对，mismatch 0。
- 日更管线（ADR-044/101）：一次 B1 请求拿全市场 `showday[0]/[1]` 两天净值 + 官方日涨幅 + 申赎状态，
  daily 依次跑 名录→状态→快照→指标（约 52s）→融合→筛选→发布→跟踪；用 **有效净值日**（14 天回看、
  覆盖率 ≥90%）而不是 `MAX(nav_date)` 判断是否前进，没有新有效日才 controlled_skip；批次号带契约版本；
  scheduler 工作日 07:30 触发（单例锁 + 心跳），进程晚启动会 `needs_catchup` 补跑当天。
- 长期跟踪榜（ADR-045）：以 publication_id 幂等累积出现次数/频率/连续在榜/进出，重复发布同一快照不重复计数；
  不回填历史（19 份旧发布都在同一天，回填等于伪造频率），从 generation 20 起累积。
- 榜单新增净值日期、日涨跌、持有期三列（含 CSV 导出）；持有期列让「0-3 年期」这类**指数久期**与
  真正的锁定期一眼可分（该基金无锁定期、当日开放申赎，进稳健榜正确）。
- 增强榜口径改造（ADR-031）：原绝对门槛实测落在各风格 p95 以上（1 年年化 p50：转债 −0.50%、一级 1.96%、
  二级 −0.05%），改为同风格分位（年化/卡玛 q60、夏普 q50、日胜率 q45、回撤/波动 q75、费率 q85）+ 绝对地板
  + 正式分 ≥55 + 每风格配额 40/总量 110；严格口径仍逐只留档在 `thresholds_json.enhanced_strict`。
- 经理维度接入（ADR-019）：7,365 只基金 / 11,107 条现任经理证据；职业年限最短者 <365 天触发 L8，
  gen9 的 132 只中 8 只被拦 → 132 → 124（007199/007329/007330/007520/012465/012603/014655/015942）。
- 3 年影子榜（ADR-020）：同一套增强准入在 730 天有 26 只全过、1095 天 21 只（一级 7/二级 2/转债 12）、
  365 天 0 只；仅作研究展示，不写 `screen_pick`。
- 持有期推断修正（ADR-018）：名称形如 `180天持有` / `6个月持有期` / `一年持有` 的 752 只债基此前
  漏判为无锁定期，修正后受限榜 13 → 292 只、稳健榜 136 → 132 只。
- 结构化持仓（`zqcc`）覆盖 7,928 只基金（2026Q3 7,897；历史 43 个季度 1,900,098 行），其中可申购基金 5,995/6,062 = 98.9%（2026-11-05，历史回填已完成），
  风格闸门已打开：转债风格由 87 只纠正为 575 只
  （467 只靠债券代码前缀被 `structured_confirmed` 识别），详见 `docs/fusion-rules.md §7.1`。
- 规模口径：`em_jbgk` 是份额级净资产，基金级 = 同家族各份额之和；`em_rank` 第 18 段实测是区间涨幅，
  不是规模（ADR-014）。
- 基准对照（2026-11-05）：接入中国债券信息网公开 JSON 接口的中债-新综合财富（总值）指数
  （CBC_CFZS_00，6,211 个交易日，2002-01-04 至 2026-11-04，`raw_sha256` 可回查）；报告第 6 节同时给
  真实指数、同风格 peer 中位数和参考 ETF（增强类不计算超额）。该指数覆盖全市场债券，不是任何基金合同基准，也不是单风格指数，
  报告只作研究对照、不宣称跑赢基准（`fundana/benchmarks.py`、`docs/architecture.md §4`）。
- V2 尾部指标（ADR-022）：3 年窗口 7,365 只全部计算，平均完整度 0.634；提供 3 年 MDD、最差滚动 90/180 日、
  CVaR5%、最差单日/单周、自然月正收益比例、最长水下观测数；只做研究展示，不进入正式分与榜单排序。
- 交易费用模型（ADR-023/024/025）：jjfl 四张表解析为申购/赎回阶梯 + 运作费，按"申购外扣 + 赎回内扣"算到手收益；
  5,536 只已采档位（35,273 档）；收益基准与持有天数同窗口；持有期不足先判 `conflict_lockup`；
  输出 `return_type=fee_net_scenario_assumed`，缺档位/缺收益时精确费后收益为 NULL，绝不默认 0。
- 前端信息层（ADR-028/029/030）：中文标签层（含运行状态页与发布报告页）+ 周/月/年聚合
  （复投 vs 净值涨跌）+ 逐日净值分页（起止日期 + 近 1 年/近 3 年/全部快捷区间）。
- 前端改版（ADR-033）：红涨绿跌（上涨红、下跌绿）、收藏做成开关（再点即取消）、基金详情拆
  hero + 九页签（可深链 `?tab=risk`）、新增对比页（最多 3 只，归一化净值 + 年度收益）、
  概览先给横截面（中位年化 +2.04%、收益分布、各风格表现）；12 页枚举泄漏 0 命中。
- 增强债年化窗口（ADR-035）：准入年化改为 `0.5 × 1 年 + 0.5 × 3 年`，回退顺序 1y+3y → 1y_only → 3y_only；
  增强榜 103 → 87（转债 30→22、一级 33→28、二级 40→37），L6E 688 → 592；混合年化门槛（同风格 q60）
  二级 2.23% / 转债 2.52% / 一级 2.70%；3 年期指标非空 5,036 只。决策页把这一行标成「年化（1 年 + 3 年各半）」，
  与概览页的近一年年化不是同一个数。
- 持有期受限榜独立成页（ADR-036）：`#/locked` 展示 292 只（持有期 279 / 定期开放 13）、档位分布
  91–180 天 195 / 181–365 天 95 / 366–730 天 2 / 731+ 0、持有期中位数 180 天，并区分未来开放与
  「开放期已于 X 开始（上游未给下一次）」——E3 的第 7 列是本次开放期起始日，不是下次开放日。
- 决策快照（ADR-036）：基金详情新增「决策」页签，直接读 `screen_decision.thresholds_json`，
  每组给「通过 X/Y 项」，每项给实际值/门槛/余量/是否通过/窗口，增强风格额外折叠留档严格口径。
- 同家族去重开关（ADR-037）：榜单工具栏可勾选「同家族只显示一只」（132 → 100 条，排名保留真实跳号），
  行内徽章显示「同家族 N 只」；份额不合并仍是硬规则，去重只影响展示。
- 持仓市值回填（ADR-034）：`fund_holding_bond.market_value_wan` 曾整列为 NULL（融合层漏传，
  129,845 行），已修 `fuse/style.py` 并回填，网页持仓表恢复显示万元市值与来源 sha。
- 采样节奏（ADR-026/027）：最少点数按序列自身节奏折算（周频 365 天 → 42 点），节奏用观测间隔 p75
  （混合披露的中位数会落在 1 天）；1 年期有指标 6,336 → 6,687 只；p75 >7 天的低频披露基金出指标但不进正式榜
  （L1 `sampling_cadence_gt_7d`）。
- 跨源校验（ADR-050）：当日净值用 B1 快照全量复核（14,752 行、0 不一致），阶段收益用 `syl_1n` 对本地 365 天
  复投（6,611 行、6 条不一致 = 0.09%，全是定开债基的上游口径差异）；结果落 `nav_cross_check`，页面「质量」可见。
- 前瞻验证（Batch A）：每个 cohort 冻结正式/同风格对照/风险对照三组成员，观察期 30/90/180/365 天；
  未到期一律 `not_ready` 且不写收益，当前 4 个 cohort、0 条成熟观测，证据等级停在 LEVEL_1。
- 发布门禁（ADR-049）：`blocked` 拒绝提交并保留上一代，`warning` 允许发布但写进 summary 与 API；
  当前 `status=warning`、`blocks=[]`、`warnings=["forward_not_ready","valuation_unavailable","dynamic_boundary","cross_board_family","dsr_skill"]`
  （`lifecycle_coverage` 已因证监会 EID 清盘样本入库转为 pass）。
- 单一原因缓释（ADR-047）：只对"恰好一项失败"且满足边界（规模 ≥1.8 亿 / 年化差 ≤0.15pp / 日胜率差 ≤0.05pp
  + 综合分门槛）的基金放行，放行后信心 ×0.9 并记 `relief:<type>`；实测 862 条检查 **0 放行**，
  因为候选综合分最高 78.68 而规则要求 ≥80（敏感性见 `reports/analysis/relief_sensitivity.csv`，ADR-052）。
- 研究优先池（ADR-048）：正式榜之外 36 只研究线索，每公司 ≤2、同家族只取一只，只写 `screen_research_pool`，
  不进榜单、不进跟踪榜。
- 指标重建口径（ADR-051）：窗口致命原因一律写 NULL（但保留实际 `n_obs`/`years` 作为证据），
  零波动 / 零下行 / 零回撤分别产出 NULL 而不是 Infinity；`--changed-only` 只跳过净值签名未变的基金。
- 风格因子归因（ADR-078）：国债/企债/转债三因子先正交化再标准化，365 天 OLS 反推久期/信用/转债 beta；
  7,426/8,138 只 R² ≥5% 有 beta，详情页「因子」页签显示同批分位，低 R² 留空不展示。
- 增强债严格口径回测（ADR-079）：`walkforward --enhanced` 同时跑 7 项绝对 strict 与当前 8 项相对 relative；
  2020–2025 六队列 strict 入选 259 只加权年化 3.60%、relative 283 只 3.62%，严格线没有收益优势
  （夏普略高但被 2023 年 6 只样本放大），生产继续用 relative，strict 只作诊断。
- 增强债严格口径叠加（ADR-080）：相对准入之外要求 7 项严格检查至少 5 项通过；2017–2025 九队列
  加权年化 4.11%、夏普 1.80（基线 3.99%/1.70），2024 年收益略低 0.22pp；当前增强榜 64→42。
- 同业务日同批次一致性（ADR-081）：screen_batch.baseline_run_id 冻结滞回基线，申赎状态按
  business_date <= 当前业务日取快照，候选/指标/同分排序全部加 fcode 全序；同批次连续两次
  生产运行 8,138 条决策与 368 条榜单记录零差异。历史回测无当时申赎快照时显式走
  buyable_fallback（研究口径），生产筛选不开启。
- 全链路上海时区（ADR-082）：业务日/时间戳/调度/导出统一按 Asia/Shanghai；裸时间按 +08:00
  解释，配置写其他时区启动即失败；采集排行请求与 CSV 文件名不再依赖宿主机本地日期。
- 待办：MGOLD 评分（无公开接口，暂用东财评分）、**非债类型接入**（地基已完成：`fundana/fundtypes.py` 注册表 + `universe.types` 配置 + `python3 -m fundana types` 列缺口，见 ADR-077；混合型/货币型等仍缺采集域、风格体系与阈值标定。**偏债混合 200 只试点已做，结论不并入债券口径**，见 ADR-093 与 `docs/hybrid-pilot-2026-11.md`）、前端鉴权（**已完成**：可选 Bearer Token，ADR-092）、前瞻 cohort 需要真实时间成熟（30 天后才有第一份 30 天结论）。
