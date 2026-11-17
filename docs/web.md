# 网页前端（本地 / 局域网只读）

`python3 -m fundana serve` 启动后访问 <http://127.0.0.1:8091/>。`config/app.yaml` 的 `api.host`
默认 `0.0.0.0`，因此同一网段的设备可用启动日志里打印的局域网地址访问（本机实测
<http://127.0.0.1:8091/>）；只想本机访问就把 `api.host` 改回 `127.0.0.1`。

只读；服务与 `collect/fuse/screen/publish` 共用同一个 SQLite 文件（WAL 模式，读不阻塞写）。
默认无鉴权；在 `config/app.yaml` 设置 `api.auth_token` 后，除 `/api/v1/healthz` 外所有 API 都要求
`Authorization: Bearer <token>`（也接受 `X-API-Key`），前端首次 401 会提示输入并保存在本机 localStorage（ADR-092）。

> 安全边界：仅限家庭/办公内网使用，**不要做路由器端口映射或反向代理到公网**。即使启用 token，
> 它也只是单一口令，不是多用户账号体系；需要审计/轮换/TLS 时请在前置反向代理层做。

## 0. 启动与开机自启

日常启动用 scripts/serve.sh start（只网页）或 scripts/daemon.sh start（网页 + 日更调度器）。
两者都会固定 cwd 到项目根目录，并在返回前探测 /api/v1/healthz；端口被占用会打印占用者，
不会静默失败。

2026-11-02 22:39 主机重启后，网页与调度器都没有再起来：var/state/serve.log 最后一条
start 停在重启前的 21:04，8091 没有任何监听，浏览器只得到“无法访问此网站”。页面与 JS
本身没有坏（18 个路由用无头浏览器逐一渲染均正常），根因是没有开机/登录自启，且
serve.pid / scheduler.pid 是重启前遗留的陈旧 pid。

安装用户级自启（不需要 root）：

    scripts/install-autostart.sh install          # 登录后自启 web + scheduler
    scripts/install-autostart.sh install --linger # 开机即启动，不要求先登录（可能触发 polkit）
    scripts/install-autostart.sh status
    scripts/install-autostart.sh uninstall

安装器写入 ~/.config/systemd/user/fundana-web.service 与
fundana-scheduler.service，两者都是 Type=simple，直接执行
python3 -u -m fundana serve --port 8091 / python3 -u -m fundana scheduler，
Restart=on-failure。systemd --user 不可用时回退到
~/.config/autostart/fundana.desktop（执行 scripts/daemon.sh start）。

注意：没有 --linger 时，unit 在用户登录后启动；要在开机后、登录前也启动，需要
loginctl enable-linger $USER。检查：

    systemctl --user status fundana-web.service fundana-scheduler.service
    journalctl --user -u fundana-web.service -n 50 --no-pager
    scripts/serve.sh status

serve.sh / scheduler.sh 会检测同名 systemd unit：unit 存在时 start/stop/restart
委托给 systemctl --user，避免手工 kill 后被 Restart=on-failure 立刻拉起；
pidfile 丢失但健康检查通过时，会用 ss -ltnp 找到监听进程并接管。

## 1. 页面

| 页面 | 内容 | 数据来源 |
| --- | --- | --- |
| 概览 `#/overview` | 四格 hero（债基数/净值观测/当日可申购/正式入选）；hero 下方显示**数据截止（有效净值日，覆盖率）**、最新局部日（若覆盖率不足）、指标日与 run；**近一年横截面**（中位年化、正收益占比、中位夏普、中位回撤）与**年化收益分布直方图**；**各风格表现表**（样本/中位年化/正收益占比/中位回撤/中位夏普）；筛选漏斗（每层带**门槛说明**，数值从 `screen.thresholds` 常量生成；带进度条）；风格分布；主要淘汰原因；稳健榜前 10 | `/api/v1/overview`、`/market/summary`、`/stats/storage`、`/rankings`、`/publications/current` |
| 榜单 `#/rankings` | 稳健/增强/持有期受限三榜 + **增强观察池（研究）**；代码或名称搜索；风格筛选；**表头可点排序**（代码/名称/正式分/风格/各周期收益/夏普/1 年回撤/3 年回撤/最差 90 日/规模/费率/信心，白名单在 `_rankings.sort_map`）；**Shift + 点击**最多叠加 3 个排序键，表头显示 ↓ 与序号（↓¹↓²），地址栏同步 `&sort=annual_1y,sharpe&order=desc,desc`，每个榜的排序记忆在 localStorage；**每个指标列头带 ⓘ**，悬停显示口径与「越大越好/越小越好」，点击弹出说明卡；**五个周期列**（近 1 周/1 月/3 月/6 月/1 年）可切换「区间收益 / 年化」；每行 **☆ 收藏开关** + **对比勾选**（最多 3 只，底部浮条「去对比」）+ 同家族徽章「同家族 N 只」；工具栏 **「同家族只显示一只」勾选框**（去重后 132 → 100 条，官方名次保留真实跳号；表格首列「#」始终是当前视图的行序，另设「名次」列显示官方排名）；工具栏 **「导出 CSV」** 把当前筛选 + 当前排序原样导出（最多 5,000 行，UTF-8 带 BOM，代码列保持文本，缺失写空串而不是 0）；分页 | `/api/v1/rankings?board=&q=&style=&sort=&order=&dedupe=&limit=&offset=&fmt=csv`、`/api/v1/research/enhanced_watch` |
| 对比 `#/compare` | 最多 3 只基金并排：15 行指标（风格/公司/成立日/家族规模/持续计提费/1 年年化/累计/夏普/索提诺/卡玛/1 年回撤/波动/日胜率/3 年回撤/正式榜）+ **归一化净值多线图**（起点=100）+ **年度收益表**（含「未结束」徽章）；可从榜单勾选带过来，也可直接输代码（`#/compare?codes=000032,000003`） | `/api/v1/compare?codes=` |
| 研究 `#/research` | 3 年/2 年窗口的增强债影子榜（**research_only**，不参与发布）；增强观察池（距门槛 ≤1 项） | `/api/v1/research/enhanced_shadow`、`/api/v1/research/enhanced_watch` |
| 基金详情 `#/fund/<code>` | **hero 头**（类型·公司、名称+代码、风格/置信度/可申购/入选徽章、最新净值与日涨跌、1 年年化、1 年最大回撤、家族规模）+ **十个页签**：①概览（九周期风险收益 + **同类分位**「好于同类 x%」+ 净值曲线 + 筛选结论）②决策（读库里的门槛快照：每组「通过 X/Y 项」、每项实际值/门槛/余量/是否通过/窗口，增强风格标注「年化（1 年 + 3 年各半）」，严格口径折叠留档，底部原因码中文徽章；**门槛变化对比卡**可选择历史轮次，列出门槛移动量、自身指标变化与「转为通过/未通过」；**反事实卡「按上一轮门槛」**：拿上一轮的 min/max 判定本轮数值，给出「可过/仍不过」、通过项数、仍差哪几项与差距，以及按上一轮同榜正式分分布估算的名次（只做算术，不重跑筛选）；顶部**排名走势折线**：左轴榜单排名（反转，越靠上越好）、右轴正式分虚线，悬停圆点看轮次，头部写「共 N 轮 · 最好第 X 名 · 最新第 Y 名」）③业绩（阶段涨幅、年度收益条形图、周/月/年复投表）④净值明细（曲线 + 全历史逐日净值：起止日期、近 1 年/近 3 年/全部快捷区间、每页 50–500、翻页、日涨跌与来源）⑤风险（九周期全表：累计/年化/波动/回撤/夏普/索提诺/卡玛/日胜率/观测门槛、三年尾部 V2 影子、回撤曲线）⑥因子（三因子标准化 beta：久期/信用/转债 + 同批分位 + R²，低 R² 留空并说明）⑦持仓（债券持仓表 + 类别环形图 + **历史持仓表**：按报告期倒序最多 12 期，五类券种占比/前五合计/前三大重仓）⑧费用（到手收益情景计算器 + 申购/赎回阶梯表）⑨经理（现任 + 历任：职业年限/任期收益/东财评分/起止日期/任职天数，非 MGOLD；历任来自 jjjl，任职天数按 as-of 重算）⑩档案（全称/公司/托管/成立日/规模口径/费率拆分/业绩基准/申购状态与下一开放日/净值区间/最远筛选层）。页签可深链：`#/fund/000032?tab=risk`，切换页签时地址栏同步 | `/api/v1/funds/<code>`、`/nav?days=1095`、`/nav/rows`、`/periods`、`/peer`、`/drawdown`、`/cost`、`/decision`、`/meta/labels` |
| 持有期受限 `#/locked` | 292 只因最短持有期 >90 天被 L3 拦下的基金单列一页（**不参与正式评分与排序**）：四格摘要（受限基金 292 / 定期开放 13 / 最短持有期 279 / 持有期中位数 180 天）、档位分布（91–180 天 195 / 181–365 天 95 / 366–730 天 2 / 731 天以上 0）、按受限类型与档位筛选、每只的开放状态徽章（未来开放 →「距下次开放日 N 天」；日期已过去 →「开放期已于 X 开始（上游未给下一次）」；持有期 →「买入后 N 天可赎回」）、**近 1 周/1 月/3 月/6 月收益列**（可切区间收益/年化）、表头可点排序、分页 200/页 | `/api/v1/locked?lockup=&bucket=&limit=&offset=&sort=&order=` |
| 数据库 `#/database` | 只看两件事：数据库文件多大（含 WAL）、整个项目目录占多少硬盘；再给一张占用构成表（DB / `var/raw` / `reports` / `var/freeze`）与所在磁盘剩余空间。**不提供表级行数、dbstat 明细与表预览**（需要时用 `python3 -m fundana stats`） | `/api/v1/stats/storage` |
| 收藏 `#/watchlist` | 长期观察列表：输入 6 位代码 + 备注加入；也可在榜单/基金详情点 ☆ 加入（**再点一次即取消**，成功给绿色提示，不再报「已经在收藏里」）；表格显示风格、1 年年化、夏普、1 年回撤、日胜率、家族规模、当日可否申购，可移除 | `GET/POST /api/v1/watchlist`、`DELETE /api/v1/watchlist/<code>` |
| 运行状态 `#/runs` | 最近 30 次 run（模式/状态中文）；抓取状态分布与按源统计（来源名中文） | `/runs`、`/operations/status` |
| 产品告警 `#/alerts` | 对当前已发布榜单扫描净值停更/换帅/规模过小或缩水/回撤超限/风格漂移；四格摘要（扫描基金/告警总数/高/中优先级）、按榜单筛选、明细表（级别/基金/所在榜单/类型/说明）与按类型计数；只读，不改变入选结果 | `/api/v1/alerts?board=&as_of=` |
| API 健康 `#/api-health` | 全部数据源接口清单：主接口地址、角色、调用数、业务成功率、p50/p95 延迟、慢请求占比、最近成功、新鲜度、健康度与状态；异常/降级接口单列详情（错误、连续失败、健康度拆解）；时间窗 1/7/30 天 | `/api/v1/api-health?days=` |
| 诊断 `#/diagnostics` | 当前发布的 9 个诊断 CSV（覆盖/冲突/NULL/风格证据/家族重复/增强债差项/家族去重榜/尾部覆盖/费用覆盖），点卡片预览前 500 行 | `/api/v1/diagnostics`、`/api/v1/diagnostics/<name>.csv` |
| 发布报告 `#/report` | generation、文件数、summary.md 全文、文件清单（可下载） | `/publications/current`、`/publications/summary`、`/publications/files/<path>` |

榜单列：序号（#，当前视图行序）、名次（官方排名，去重或换排序后会跳号）、对比勾选、☆、代码、名称、正式分、风格、近 1 周/1 月/3 月/6 月/1 年、夏普、1 年最大回撤、3 年最大回撤（窗口/基金指标）、3 年覆盖、5 年覆盖、份额规模、经理最短、可比性、风险、最差 90 日、家族规模（份额级汇总）、持续计提费率、信心分；周期列默认显示**区间收益**，切到「年化」后按窗口折算年化（短周期年化是外推值，仅用于横向比较）；

**配色遵循中国习惯：红涨绿跌**（`--up #d92b2b` / `--down #0e9f6e`，暗色主题自动换色）。收益、日涨跌、年度收益条、回撤曲线、统计卡片的数值全部按符号着色；0 与 NULL 不着色（NULL 显示「—」，不是 0）。
达到封顶值（Sharpe ±5 / Sortino ±10 / Calmar ±20）的数字带 `*` 并带 tooltip，与
`docs/formula-contract.md` 的 cap 契约一致 —— 封顶值只能用于排序展示，不能当作真实风险调整收益。

前端是零构建的单页应用：`web/index.html` + `web/app.js` + `web/styles.css`，无 CDN、无 npm 依赖，
离线可用；样式跟随系统深浅色。

## 2. API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/healthz` | 存活、表数、代码版本 |
| GET | `/api/v1/publications/current` | 当前发布 manifest（含 generation/committed_at） |
| GET | `/api/v1/publications/summary` | 当前 revision 的 summary.md 原文 |
| GET | `/api/v1/publications/files/<path>` | 下载 manifest 白名单内的文件 |
| GET | `/api/v1/overview` | 首页聚合（漏斗/风格/原因码/榜单规模） |
| GET | `/api/v1/universe/coverage` | 债基数、净值行数与最新日、风格分布 |
| GET | `/api/v1/rankings` | board/q/style/limit/offset/**sort/order**（**逗号分隔最多 3 键**，如 `sort=annual_1y,sharpe&order=desc,desc`；NULL 一律排最后，末尾自动补 `rank` 升序保证翻页稳定；返回 `meta.sort_spec`）；每行含五个周期的 `ret_*_pct` 与 `annual_*_dec`（7/30/90/180/365 天）；sort 白名单 rank/formal_score/annual_1y/sharpe/mdd/scale/fee/ret_1w/1m/3m/6m，未知键丢弃、全部未知回退 rank；`fmt=csv` 直接下载当前视图（同 sort/order/q/style/dedupe，最多 5,000 行） |
| GET | `/api/v1/locked` | 持有期受限榜：`?lockup=holding|periodic_open&bucket=91-180|181-365|366-730|731%2B&limit=&offset=&sort=&order=`，返回逐只指标（含 `ret_1w_pct`…`ret_6m_pct` 与 `annual_1w_dec`…`annual_6m_dec`）+ `hint`（开放状态）+ `summary`（分档统计、中位数、`as_of`）；`sort` 白名单 rank/lockup/annual_1y/sharpe/mdd/positive/scale/fee/ret_*/annual_*，未知值回退 rank；`fmt=csv` 下载当前视图（最多 5,000 行） |
| GET | `/api/v1/funds/<code>/decision` | 决策快照：读 `screen_decision.thresholds_json`，返回 `groups[]`（基础门槛 / 动态分位门槛 / 增强债准入，每项含 `value/threshold/op/passed/margin/unit/window`）、`scores`、`reason_codes`（带中文 label）、`has_thresholds`；另返回 `history[]`（该基金出现过的轮次）与 `previous{run_id,score_delta,changes[]}`（默认取**最近一次门槛真正变化**的轮次，`?prev=<run_id>` 可指定）；另返回 `counterfactual{run_id,groups,n_passed,n_items,would_pass,failed,rank_estimate}`（按上一轮门槛判定本轮数值）；无决策 404 |
| GET | `/api/v1/funds/<code>/rank-history` | 排名走势：该基金在所有轮次里的 `board/rank/formal_score/layer/passed`，返回 `items[]`、`runs`、`best_rank/worst_rank/latest_rank`、`score_delta`；`?limit=` 默认 40；基金不存在 404 |
| GET | `/api/v1/search` | 全局搜索：`?q=000032&limit=8`，按代码前缀 + 名称模糊，精确代码优先 |
| GET | `/api/v1/market/summary` | 近一年横截面：样本数、中位/均值/四分位年化、正收益占比、中位夏普与回撤、六桶收益分布、各风格中位表现 |
| GET | `/api/v1/funds/<code>/peer` | 同风格分位（样本 <20 返回 `percentile=null`）：1 年年化/夏普/卡玛/1 年回撤/日胜率，`percentile` 可读作「好于同类 x%」 |
| GET | `/api/v1/funds/<code>/drawdown` | 回撤曲线：`?days=1095`（30–3650），返回逐点 `dd_pct`（负数）、`max_dd_pct` 与当前回撤 |
| GET | `/api/v1/compare` | 基金对比：`?codes=a,b,c`（最多 3 只），返回档案 + 1 年期指标 + 3 年回撤 + 归一化净值序列 + 最近 6 个自然年收益 |
| GET | `/api/v1/funds/<code>` | 档案 + 画像 + 风格 + 九周期指标 + 决策 + 持仓 + **历史持仓**（`holding_history`：按报告期倒序最多 12 期，五类券种占比/前五合计/前三大重仓） + 三年尾部指标 + 费用阶梯 + **风格因子暴露**（久期/信用/转债 beta、R²、同批分位；低 R² 时 beta 为 null） |
| GET | `/api/v1/funds/<code>/nav` | 净值序列：`?days=1095`（30–3650）；超过 700 点等距抽样并保留首尾，返回 `points/n_total/n_points/start/end/downsampled` |
| GET | `/api/v1/funds/<code>/nav/rows` | 逐日净值明细分页：`?start=YYYY-MM-DD&end=&page=1&page_size=100`（page_size ≤500）；返回日期倒序的行、total/pages 与整段 `bounds`；非法日期或 start>end 返回 400 |
| GET | `/api/v1/funds/<code>/periods` | 周/月/年聚合：`?granularity=week\|month\|year&limit=120`（≤600）；每行含 `chain_return_pct`（复投总回报）、`nav_return_pct`（单位净值涨跌）、`mdd_pct`、`n_obs`、`partial/partial_reason` |
| GET | `/api/v1/meta/labels` | 中文标签字典（风格/置信度/榜单/筛选层/规模来源/口径/适配/债券类别/申购状态），前端下拉框与图例用 |
| GET | `/api/v1/meta/metrics` | 指标字典：28 条 `{key,label,unit,direction,direction_label,desc}` + 三档方向文案 + 12 层漏斗门槛说明（`layer_details`，数值由 `screen.thresholds` 常量格式化）。列头 ⓘ 与漏斗说明的唯一数据源 |
| GET | `/api/v1/funds/<code>/cost` | 费用情景：`?amount=10000&holding_days=30&max_drawdown=`；返回申购/赎回档、原费率/优惠费率净收益、`nav_return_pct` 与 `nav_return_window_days`（与持有天数同窗口的链式复投总回报）、`return_type=fee_net_scenario_assumed`、`user_fit` 与假设 |
| GET | `/api/v1/stats/storage` | 整体体量：`db`（bytes/wal/shm）、`disk`（项目目录、raw/reports/freeze 的文件数与字节、所在磁盘总容量与剩余）、`total_bytes`；**不含表级行数与 dbstat** |
| GET | `/api/v1/schema` | 表与列定义（内部核对用，前端不再展示） |
| GET | `/api/v1/diagnostics` | 诊断 CSV 清单（只列 `*.csv`，附字节数） |
| GET | `/api/v1/diagnostics/<name>.csv` | 单个诊断文件，默认 500 行、`?limit=` 最大 2000；名称白名单 `^[a-z0-9_]+\.csv$`，越界/穿越返回 400 |
| GET | `/api/v1/research/enhanced_watch` | 增强债观察池（**research_only**）：从 `enhanced_near_miss.csv` 取距门槛 ≤1 项的基金，最多 200 只 |
| GET | `/api/v1/research/enhanced_shadow` | 增强债影子榜（**research_only**）：`?window=365\|730\|1095\|1825&limit=`，读 `reports/analysis/enhanced_shadow_<w>d.csv` 与 `.json` 元数据；非法窗口 400、未生成 404 |
| GET | `/api/v1/shadow-forward/summary` | v9/v10 core10/core50 影子前瞻（**research_only**）：`?cohort=` 可选；返回证据等级、各 board/strategy/horizon 的收益/MDD/胜率对比与 `cohort_list`；不进入正式 `forward_*` 证据 |
| GET | `/api/v1/runs` | 运行历史 |
| GET | `/api/v1/operations/status` | run + fetch_log 状态分布 |
| GET | `/api/v1/api-health` | API 健康页：`?days=1\|7\|30`；返回 `registry{version,count,unregistered}`、`summary` 与逐源 `url/role/calls/success_rate_pct/p50_ms/p95_ms/slow_rate_pct/last_success_at/freshness/health_score/health`；健康度 = 60% 成功率 + 25% 延迟分 + 15% 新鲜度分，全部来自 `fetch_log` 与注册表 |
| GET | `/api/v1/alerts` | 产品告警：`?board=bond_core10&as_of=2026-11-02`；返回 `generation/publication_id/funds/alerts[]/summary.by_code/by_severity`；只读当前已提交发布 |
| GET | `/api/v1/watchlist` | 收藏列表（JOIN 最新净值、当日申购、1 年期指标） |
| POST | `/api/v1/watchlist` | 加入收藏：`{"fcode":"000032","note":"可选","source":"rankings"}`；空码/库内无此基金/重复返回 400，成功 201 |
| DELETE | `/api/v1/watchlist/<code>` | 移出收藏；不存在返回 404 |
| POST | `/api/v1/query` | 只读 SQL（**前端页面已下线**，保留给验收脚本与调试）：`{"sql": "...", "limit": 200}` |

统一响应 `{ok, data, meta, error}`；错误码：`publication_unavailable`(503)、`query_rejected`(400)、
`file_not_in_manifest`(404)、`not_found`(404)、`internal_error`(500)。

## 2.1 中文标签层（不暴露内部枚举）

页面上不应出现 `bond_secondary_enhanced`、`multi_field_consistent`、`em_jbgk_family_sum`、
`annual_1y` 这类内部代码。做法是**服务端翻译、前端只显示中文**：

- `fundana/labels.py` 维护十四组字典（风格、置信度、榜单、筛选层、规模来源、口径、适配、
  债券类别、申购状态、数据来源、抓取状态、运行模式、运行状态、风格档案）与
  `label_reason()`（把 `composite:below_min`、`scale_below_3.00`、
  `manager_career_below_365d:81d`、`nav_return_window_proxy:365d`
  等结构化原因码翻成一句话；未知码统一显示「其他原因」）。标签模块放在包根而不是
  `fundana/api/`，因为发布产物（`summary.md`）也要用同一套中文；`fundana/api/labels.py`
  只是转发层。
- `/api/v1/overview`、`/universe/coverage`、`/rankings` 直接返回 `{code,label,count}`
  或 `style_label`；`/funds/<code>` 额外返回 `labels` 块（含逐条原因的中文）。
- 前端 `ensureLabels()` 缓存 `/meta/labels`，`labelOf(group, code)` 取文案；下拉框、图例、
  诊断表头（`DIAG_CN`/`COL_CN`）、影子榜「差在哪」列（`CHECK_CN`）、运行状态页的
  模式/状态/来源、费用卡的口径与适配、费用未算出的原因徽章全部走中文。
- 发布报告页的 `summary.md` 由 `fundana/publish/builder.py` 在写盘时就用
  `label_style`/`label_board`/`label_profile` 翻译（榜单名、风格名、风格档案），
  所以下载下来的报告本身就是中文，不需要前端二次加工。
- 代价：改文案要动服务端；换来的是前端没有第二份枚举真相，也不会因为漏改一处而泄漏代码。
  原始代码仍保留在 `title` 悬停与 CSV/JSON 产物里，便于核对。

## 3. 只读 SQL 接口的安全模型（前端已下线，接口保留）

1. **连接层**：`sqlite3.connect("file:<db>?mode=ro", uri=True)` + `PRAGMA query_only=1`，
   即使语法闸门被绕过也无法写盘。
2. **语法层**：只允许单条以 `SELECT`/`WITH` 开头的语句；出现分号（多语句）直接拒绝。
3. **关键字层**：拒绝 `INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/REPLACE/ATTACH/DETACH/PRAGMA/
   VACUUM/REINDEX/ANALYZE/BEGIN/COMMIT/ROLLBACK/SAVEPOINT/LOAD_EXTENSION` 等整词。
4. **结果层**：`limit` 夹在 1–1000，多取一条用于标记 `truncated`，绝不无界返回。
5. **时间层**：`set_progress_handler` 每 1 万条 VM 指令检查墙钟，超过 3 秒 abort 并返回
   "请加 WHERE 或 LIMIT"。

下载接口只放行 `validated.json` 里登记过的相对路径，解析后再校验是否仍在 revision 目录内，
`..` 穿越返回 404（实测 `--path-as-is` 攻击被拒）。

## 4. 已知限制

- 单进程 `ThreadingHTTPServer`，无连接池；复杂查询会占用一个线程（有 3 秒上限兜底）。
- 只读接口有进程内 TTL 缓存：`/overview` 600s、`/quality` 300s、`/portfolio/diagnostics` 600s、
  `/decision` 600s；key 以当前 publication revision 开头，发布切换后自然失效。多 worker 部署时
  各自一份；冷启动 0.5s 后后台预热，首个请求若早于预热完成仍可能等待冷查询。
- 鉴权是可选的单 token（`api.auth_token`），默认关闭；启用后保护所有 `/api/v1/*`（healthz 除外），
  但没有用户、过期、轮换与审计日志。仍只能在可信内网使用，不要做端口映射或反向代理到公网。
- 报告文件下载按 manifest 白名单，不提供目录列表。
- 静态资源不加缓存头（`no-store`），改前端后刷新即生效，代价是每次多几百 KB。
- `/api/v1/overview` 里 `screen_pick` 只统计最近一次 run；历史 run 的榜单需带 `run_id` 查询。
- 导出 CSV 走同一套接口与排序白名单，上限 5,000 行（超出只导前 5,000 行并保持页面顺序）；
  单位换算只做「小数比率 ×100」，不做四舍五入，所以导出值与页面显示可能末位不同。
- 反事实卡只回答「能不能过上一轮的门槛」，不含评分、排序与配额；估算名次是「上一轮同榜里正式分比我高的有几个 + 1」，不是重跑结果。
- 排序键最多 3 个；`mdd` / `tail` 是 `mdd_1y` / `tail_completeness` 的别名（`metrics_meta.ALIASES`），新增指标时必须同时改字典与 `sort_map`，否则列头没有 ⓘ 或点了不排序——用 `scripts/check_ui_contract.py` 自检。该脚本第 5 项会反向遍历 `PRAGMA table_info`：库里每个指标列要么在字典里有说明，要么被显式列为内部列（如 `raw_*` / `cap_hit` / `nav_sig`），两边都不沾就算缺口。
## 5. 跟踪榜（#/tracker）

跟踪榜不是「又一次筛选」，而是**跨交易日的累积记录**：每个交易日只记一份快照，展示出现次数、频率、连续在榜、首次/最近入榜、正式分变化与当前状态。

- 快照单位是**业务日**而不是 publication：同一天重跑采集、重算指标后重新发布，以当天最后一次的入选集合为准，`screening_snapshot` 里那一天也只留一行（`tracker._rebuild` 按 business_date 去重后整表重放）。这样「出现次数」才等于「在榜的交易日数」。
- 频率 = 出现次数 ÷ 已记交易日数。首次启用时只有 1 个交易日，所以首批都是 100%，随每个交易日发布才会分化。
- 只记正式榜（稳健 / 增强）；持有期受限榜是另一套语义，不进累积。
- 「含已退出」勾选后显示曾入榜但最新交易日不在榜的基金（status=exited，streak 归零）。
- 数据来自 GET /api/v1/tracker（board / limit / offset / include_exited）与 GET /api/v1/tracker/<fcode>。

## 6. 榜单证据列

| 列 | 含义 | 排序键 |
| --- | --- | --- |
| 净值日期 | 该基金最新一条净值的业务日，可能晚于市场有效日；市场有效日/覆盖率看表格上方 meta | nav_date |
| 指标日 | 365 天指标实际算到的日期；早于有效日时标红「旧」 | metric_end_date |
| 日涨跌 | 最新业务日的官方净值涨跌幅（百分点） | day_pct |
| 持有期 | 最短持有期天数；无锁定期显示「无锁定」 | lockup_days |
| 3 年覆盖 | 3 年指标实际覆盖的窗口/3 年；低于 90% 灰色显示，不冒充完整窗口 | — |
| 5 年覆盖 | 5 年指标实际覆盖的窗口/5 年；低于 90% 灰色显示，缺失不补 0 | — |
| 份额规模 | 当前份额级规模（亿元），与家族规模分开；小于 0.5 亿不进核心榜 | share_scale |
| 经理最短 | 现任经理在本基金的最短任职天数；核心榜要求 ≥365 天 | manager_tenure_min_days |
| 可比性 | 摊余成本、零风险、稀疏/过期净值标为「不可比」，不参与分位排序 | — |
| 风险 | 独立风险门：通过/关注/未知/拦截；悬停显示前十大、信用前三、2022Q4、近5年最差60日；拦截已退出正式榜，关注/未知只提示 | — |

榜单 meta 同时显示「净值 `<nav_as_of>` · 指标 `<metric_as_of>` · 新鲜度 `<status>` · N 项提示」。净值日期是行级最新日，不等于市场有效日；有效日取 14 天回看中覆盖率 ≥90% 的最新日。

这些列同时进入导出 CSV（含 3 年/5 年覆盖、基金指标回撤、份额规模、经理 tenure、可比性旗标、风险门三列、指标截止日与指标落后标记）。

## 7. 质量页（#/quality）

一页回答"这批数据能不能用"：

| 区块 | 内容 |
| --- | --- |
| 四张卡 | 净值行数 / 净值有效日（副标题显示覆盖率、最新局部日） / 净值年龄（按有效日，天） / 最近门禁（pass·warning·blocked，附阻断与警告条数） |
| 源成功率 | 每个 source_id 的调用数、`business_ok` 率、最近失败原因（`em_pzd` 当前 96.7%，281 次业务空数据集/缺字段） |
| 关键字段缺失率 | 规模、三项费率、成立日、基准文本、1 年各指标的 NULL 占比 |
| 跨源核对（按日） | `nav_cross_check` 按业务日汇总：比对行数、不一致数、比例 |
| 发布门禁历史 | 每次发布的 status / blocks / warnings / 时间 |
| 覆盖标记 | `gap` / `stale` / `short_history` 三档计数 |

时间窗可选 7 / 30 / 90 天（`?days=`）。数据来自 `GET /api/v1/quality?days=`。

## 7.1 API 健康页（#/api-health）

一页回答“这些公开接口现在能不能用、慢不慢、数据新不新”：

| 区块 | 内容 |
| --- | --- |
| 四张卡 | 接口总数（注册表版本/未注册数）、健康/降级、窗口调用（业务成功/慢请求率）、总成功率（p95/平均延迟） |
| 接口清单 | 每个 source_id 的中文名、角色（主链路/跨源校验/交叉校验）、主接口地址、调用数、业务成功率、p50/p95、慢请求率、最近成功、新鲜度、健康度、状态 |
| 异常与降级详情 | 非健康接口单列：地址、状态说明、健康度拆解（成功率/延迟/新鲜度）、最近错误、连续失败 |
| 健康度口径 | 60% 成功率 + 25% 延迟分 + 15% 新鲜度分；成功率 <99% 或 p95 超预算 2 倍或超过 TTL → 降级；成功率 <90% 或健康度 <60 → 异常；无调用 → 未验证；fetch_log 有但注册表无 → 未注册 |

时间窗可选 1 / 7 / 30 天（`?days=`）。数据来自 `GET /api/v1/api-health?days=`，只读
`fetch_log` / `source_state` 与 `config/source_registry.yaml`，页面不额外发请求。
延迟预算按 domain 区分（批量快照 5s、单基金档案 2.5s、排行 6s），避免拿同一个阈值误判批量接口。

## 8. 前瞻页（#/forward）

回答"这套筛选到底有没有用"——但**只能等真实时间**：

- 顶部五张卡：证据等级（LEVEL_0–4）、cohort 数、成熟正式观测数、未到期观测数、不同决策月份数；
- cohort 一览：cohort_id、决策日、来源（live/retro）、正式/同风格对照/风险对照人数、观察期列表、各状态观测数；
- 按观察期对比：正式 vs 同风格对照 vs 风险对照的超额收益（无成熟样本时显示提示，而不是 0）；
- 证据等级说明卡：每一级的准入条件写清楚，避免把 `LEVEL_1` 读成"已验证"。

当前状态：2 个 cohort、0 条成熟观测（全部 `not_ready`），所以证据等级停在 `LEVEL_1`。**页面必须让读者看到"还没有结论"**，而不是给一个空表格。

影子前瞻（v9 同池 vs v10）不混入本页的正式前瞻证据：它写入独立 `shadow_*` 表，由
`GET /api/v1/shadow-forward/summary` 提供研究 API，研究页已展示证据等级、最新 cohort 的
board×strategy×horizon 表与成熟/未到期计数；首轮 cohort 240 条观测同样全部 `not_ready`。
在至少 12 个决策月、100 条成熟观测之前，不能把影子差异读成业绩结论。

## 9. 决策卡新增两张卡

基金详情 → 决策页签，在"反事实"之后追加：

**9.1 单一原因缓释**：显示 `relief_json` 的三种形态——未检查（字段为空）、检查未放行（`allowed=false` + `blocked` 原因 + 差额 + 综合分）、已放行（`allowed=true` + 被豁免项；v2 起只留痕，不改数据质量分）。当前 862 条检查全部是"未放行"，卡里直接写"规则要求综合 ≥80，本基金 61.70，差 18.30"。

**9.2 跨源核对**：列出该基金最近 6 条 `nav_cross_check`（业务日、字段、来源 A/B、两侧数值、绝对/相对偏差、状态）。来源组合有三种：E3 状态快照 vs B1 净值快照（当日双上游）、本地历史 vs 蛋卷/东财历史（抽样深检）、`syl_1n` vs 本地 365 天复投（阶段收益）。用途是让"这只基金的净值/收益是不是本地算错"变成一个可自查的问题，而不是靠信任。

### 9.3 排名走势：未入榜基金也画得出来

`/api/v1/funds/<code>/rank-history` 每轮返回 `rank`（未入榜为 null）、`formal_score`（只有进了榜的轮次才有）
与 `score`（该轮决策快照里的正式分，`score_source=pick|decision`）。走势图优先画排名（左轴），
右侧虚线画 `score`；从未入榜的基金左轴留空并标「未入过榜」，避免出现一张只有坐标轴的空图
（全库 7,200 多只基金属于这种情况）。悬停圆点可看该轮正式分、综合分与到达层级。
