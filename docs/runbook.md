# 运行手册（Runbook）

## 0. 环境

- Python 3.14+，仅依赖 `requests`、`PyYAML`（stdlib sqlite3 / http.server / unittest）。
- 工作目录 = 仓库根；数据库 `var/db/fundana.db`（WAL）。
- 所有时间按 `Asia/Shanghai`：代码唯一常量是 `fundana/db.py` 的 `CN`；裸时间戳按 `+08:00` 解释；
  `config/app.yaml` 的 `app.timezone` 写其他值会在启动时直接失败（ADR-082）。

## 1. 首次初始化

```bash
python3 -m fundana bootstrap --verify-config --migrate
```

输出 `config_hash`、`code_version`、源清单、已应用迁移、表数。任何配置错误在此时失败。

## 2. 源探针（必须全部业务通过）

```bash
python3 -m fundana probe --sources all --strict
```

每个源真实抓取并解析：名录/状态/排行用真实日期；jbgk/pzd/zqcc 用 000032 样本。
**只看业务字段**：HTTP 200 + 空数据 = 失败。除解析成功外还有"业务体量下限"
（名录/状态 ≥10,000 行、pzd ≥100 点、jbgk ≥5 个非空字段、zqcc/rank ≥1 行），
低于下限记 `TOO_SMALL`，避免"解析器返回空结构但状态码 200"被当成健康。

## 3. 采集

```bash
# 名录 + 状态（一次请求拿全市场）
python3 -m fundana collect --domain directory --types bond
python3 -m fundana collect --domain status --types bond
# 排行横截面（债券型）
python3 -m fundana collect --domain rank --types bond --page-size 500 --max-pages 40
# 全历史净值（逐只，2 QPS，实测约 2 只/秒 → 7,454 只约 62 分钟）
python3 -m fundana collect --domain nav --types bond
# 档案（成立日/基金级规模/管理+托管+销售服务费）
# --only-buyable：只采当日可申购的基金（L2 之后才可能进正式筛选，其余已淘汰）
# --skip-collected：跳过 src_jbgk_profile 里已有的代码，可安全重跑
python3 -m fundana collect --domain profile --types bond --only-buyable --skip-collected
# 债券持仓（季报，用于转债/利率/信用的结构化证据）
python3 -m fundana collect --domain holding --types bond --only-buyable --skip-collected
# 实测约 0.6 只/秒（Referer + HTML 解析比 NAV 重），5,535 只约 2.5 小时；
# 只影响风格置信度（不影响收益/风险指标），可分批增量：--skip-collected 会跳过已有代码。
# 交易费用阶梯（jjfl：申购/赎回/管理费/托管费四张表）
python3 -m fundana collect --domain fee --types bond --only-buyable --skip-collected
# 实测约 1.2–1.5 只/秒，5,535 只约 60–75 分钟；只用于 /cost 情景计算，不进正式评分。
# 解析器修正后从 raw_object 回放费用阶梯（不联网；8,138 只约 1 分钟）
python3 -m fundana fees
# 规模历史（gmbd：2002 年起的季报份额/净资产，walkforward 的 as-of 规模来源）
python3 -m fundana collect --domain scale --types bond --skip-collected
# 实测约 100–120 只/分钟，8,138 只约 70–90 分钟；--skip-collected 可安全重跑补失败者。
# 经理任职历史（jjjl：历任经理的起止期与任职回报，研究/证据用）
python3 -m fundana collect --domain manager --types bond --skip-collected
# 实测约 1–2 只/秒；end_date 为空表示在任，days 按 business_date 重算。
```

**一次只跑一个写任务**（collect / metrics / tail / fuse / screen / publish 全是写任务）。SQLite 是单写者：
NAV 采集每只基金约 3,000 行的批量写入会长时间占用写锁，并行的档案采集会退化到 0.2 只/秒（实测 18 分钟
仅 218 只）；更严重的是并行写会直接失败——实测 `tail` 全量重算（7,365 行）与 `collect --domain fee` 并行时，
fee 进程崩在 `db.put_raw` 的 `sqlite3.OperationalError: database is locked`。串行反而更快也更安全。

首轮全流程可一键串起来（等 NAV 结束 → 指标 → 档案 → 融合 → 筛选 → 发布 → 校验 → 验收）：

```bash
bash scripts/run_first_round.sh              # 日志 var/state/round1.log
bash scripts/run_first_round.sh --skip-profile   # 已有档案时跳过采集
```

增量策略：
- 当日净值走 `em_status_snapshot`（单页 27,929 行）。
- 全历史 `pingzhongdata` 7 天滚动（≈1,058 只/日）。
- 空响应冷却 7 天，只更新 `attempted/status`，不清旧值。
- 连续失败 5 次 → 熔断 30 分钟。

## 4. 融合

```bash
python3 -m fundana fuse --business-date 2026-10-30
```

依次执行 identity → share_classes → profiles → style → coverage，输出：
- `var/state/coverage_<run_id>.json`：来源分布、风格分布、冲突数、NULL 比例、NAV 覆盖。
- 数据库 L2 表。

## 5. 指标

```bash
python3 -m fundana metrics --formula fundana-risk-2026.11.3 --as-of 2026-10-29
python3 -m fundana metrics --changed-only          # 只算净值签名变化的
python3 -m fundana comparability --as-of 2026-10-30  # P0-2：刷新可比性旗标（必须用 market_as_of）
# V2 尾部指标（3 年窗口，影子口径，不参与正式分）
python3 -m fundana tail --as-of 2026-10-29
# 换契约后清掉旧契约行（默认只报告，--apply 才删；metrics / tail 跑完也会提示）
python3 -m fundana prune-contracts --apply
# 增强债换窗口影子榜（research_only，输出到 reports/analysis/）
python3 -m fundana shadow --window 1095 --limit 60
```

## 6. 筛选

```bash
python3 -m fundana screen --profile bond_v1 --business-date 2026-10-30
python3 -m fundana shadow --window 1095 --limit 60   # 增强债 3 年影子榜（research_only，不写 screen_pick）
python3 -m fundana shadow-rank --run-id <run_id>     # v9 同池影子排序（research_only，不写 screen_pick）
python3 -m fundana shadow-forward --run-id <run_id>  # 冻结并评估 v9/v10 core10/core50 前瞻
```

`shadow-rank` 固定当前 v10 的 L9 通过集合与 core 约束，只替换 v9 长期分/排序分；
输出 `reports/analysis/shadow_rank/<run_id>.json/.csv` 与 `history.csv`。它不是完整
v9 管线，也不是 PIT；只用于每日对照，不改变发布。

`shadow-forward` 把同一份 v9/v10 core10/core50 冻结到独立的 `shadow_cohort /
shadow_member / shadow_observation` 表，按 30/90/180/365 天只取 cohort 之后的净值；
样本不足写 `not_ready`，不写 0 收益。它不进入正式 `forward_*` 证据等级。

输出 `screen_run / screen_decision / screen_pick`，并写冻结输入
`var/freeze/<run_id>/inputs.jsonl.gz + inputs_pit.jsonl.gz + manifest.json`（SHA256 可校验）。
`inputs_pit.jsonl.gz` 是 P0-1 的 37 列输入快照（含 as-of 规模/份额/任职、指标、分位与评分），
`screen_run_input` 表同时逐只落库；同一 run 可用 `replay-screen` 逐字段重放：

```bash
python3 -m fundana replay-screen --run-id <run_id>
# checked=8138, score_mismatches=0, gate_mismatches=0, lookahead_metrics=0 才算通过；
# post_gate_reasons_ignored 是 L2/L3 质量扫描追加的 risk:* 原因，不在 base_gate 重放范围。
```

P0-2 在 L1 增加可比性旗标（`amortized_cost / zero_risk / sparse_nav / stale_nav`）。
`metrics` 写完后会自动调用 `comparability.refresh`；手工回填用
`comparability --as-of <market_as_of>`，不能用 business_date，否则 `load_latest` 读不到。
旗标不删原始指标，只让该基金退出分位分布；榜单 API/CSV/网页会显示 `metric_comparable`
与 `metric_flags`。

P0-3 在 L8 经理门之后增加独立风险门 `fundana/risk_gate.py`：按债券名称重分类、归一
发行人，算前十大/单一债券/信用发行人/转债/股票集中度，并用 `acc_nav` 做 2022Q4、
2020H1、2016Q4 与近5年最差60日压力测试。block 的基金以 `L8R` 留在 `screen_decision`，
原因码是 `risk:<flag>`，完整明细在 `risk_json`；watch/unknown 只展示，不改变收益分。
排查某只基金：

```bash
python3 - <<'PY'
import json, sqlite3
c = sqlite3.connect("var/db/fundana.db"); c.row_factory = sqlite3.Row
r = c.execute("select risk_json from screen_decision where run_id=? and fcode=?",
              ("<run_id>", "020083")).fetchone()
print(json.dumps(json.loads(r["risk_json"]), ensure_ascii=False, indent=2))
PY
```

清盘/合并名单必须从外部 CSV 导入（不接受 NAV 停更推断）：

```bash
python3 -m fundana lifecycle-import path/to/lifecycle.csv
python3 -m fundana lifecycle-status --fcode 000507 --as-of 2024-06-30
```

CSV 列：`fcode,status,effective_date[,last_nav_date,reason,source]`；status 只接受
`liquidated/merged/suspended/active`。导入后 `_load_candidates(as_of=...)` 会排除
as-of 前已清盘/合并的基金，`lifecycle.universe_as_of()` 用于回测宇宙。

**同批次重跑（ADR-081）**：同一 `business_date` + batch 的滞回基线冻结在 `screen_batch.baseline_run_id`，
重跑沿用该基线，不会因为“最近一次 run”不同而漂移；申赎状态只取当前业务日及以前的最近快照。
历史回测（walkforward）在最早快照晚于回测日时显式传 `buyable_fallback=True` 退回最新快照，
这是研究口径，生产 `screen` 不开启。
验证一致性时先确认输入快照未变（净值/规模/费率/经理/申赎），再比 `screen_decision` 与 `screen_pick`：

```bash
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('var/db/fundana.db')
for run in ('<run_a>', '<run_b>'):
    print(run, con.execute('select count(*) from screen_decision where run_id=?', (run,)).fetchone()[0],
          con.execute('select count(*) from screen_pick where run_id=?', (run,)).fetchone()[0])
PY
```

### P1 组合诊断 / 5 年成本 / 统计验证（只读）

P1 不改筛选结果，用于回答“这 10/50 只是不是独立风险”“5 年成本多少”“超额是否显著”：

```bash
python3 -m fundana portfolio-diagnostics --run-id <run_id> --board bond_core10
python3 -m fundana portfolio-diagnostics --run-id <run_id> --board bond_core50
python3 -m fundana stat-validation --run-id <run_id> --board bond_core10
```

- 组合诊断输出平均/最大两两相关、有效独立 bet、公司/家族/风格集中度；默认基准为同一 run 的
  `screen_decision passed=1`。c50c49 的 core10 平均相关 0.726、有效 bet 1.33，core50 为 1.41；
  `effective_bets < max(2, 0.5n)` 会报 `high_correlation`，最大相关 ≥0.90 报 `near_duplicate_pair`。
- 成本列已扩到 1/3/5 年，API/CSV 另有满 1 年赎回费；5 年成本是静态费率表口径，不是现金流折现。
- 统计验证用 20 日块自举与 Deflated Sharpe。c50c49 的 core10 相对 formal 池年化超额 0.574%
  （95% CI 0.297–0.825），但 rf 调整后的 DSR 0.72（`n_trials=256`），5% 水平不显著；
  只有 1 年窗口、基准含 core10 自身，不能写成 alpha。
- 复现文件：`reports/analysis/portfolio_diag_core10.json`、`portfolio_diag_core50.json`、
  `stat_validation_core10.json`。

## 7. 发布

```bash
python3 -m fundana publish --run-id <run_id>
python3 scripts/check_ui_contract.py   # 前端契约自检：列头说明 / 排序白名单 / SQL 列名是否一致（不一致退出码 1）
python3 -m fundana verify --fast
```

产物目录：`reports/runs/<run_id>/revisions/<uuid>/`
- `bond_stable.csv / bond_enhanced.csv / bond_locked.csv / all_formal_candidates.csv`
- `summary.md`、`diagnostics/rejections.json`、`diagnostics/layers.json`
- `validated.json`（逐文件 SHA256 + size）

**唯一提交点**：`reports/.publication_current` 符号链接的最后一次 `os.replace`。
任何一步失败 → 旧 current 保持有效。

## 8. 校验与重放

```bash
python3 -m fundana verify --fast        # 只校验 symlink 与 schema
# full 校验逐文件 SHA：
python3 - <<'EOF'
from fundana.config import load_config
from fundana.publish.verify import verify
print(verify(load_config(), full=True))
EOF
python3 -m fundana replay --run-id <run_id> --offline   # 逐字段比对，差异≠0 退出码 1
```

## 8.1 数据健康（P2，只读）

```bash
python3 -m fundana data-health --skip-integrity --days 7
python3 -m fundana data-health --days 7 --json --out /tmp/data_health.json  # 含 quick_check
```

- 检查项：db_integrity（PRAGMA quick_check，大库约 30s，可跳过）、schema（迁移文件 vs
  schema_migration）、nav_freshness（**有效净值日**与覆盖率；没有 ≥90% 有效日或 age>10 天失败）、
  metric_freshness（有效日当天有净值基金的指标 current 比例；partial/stale 失败）、metrics
  （当前 FORMULA_CONTRACT 的周期与 NULL 率）、fetch（近 N 天 business_ok 成功率 <90% 失败）、
  source_redundancy（主链路单源域只告警不失败）、scheduler（heartbeat 的 next_run_at 过期或 last_status=failed 只告警；heartbeat 缺 last_status 时回退到 run 表最近一次 daily）、publication（最新 generation/契约/gate）。
- 退出码：任一硬检查失败为 1；单源域、发布 gate warning 不改变退出码。
- 当前基线：schema 31/31；NAV effective=2026-11-02（91.68%）、max=2026-11-03（85.51%）；
  指标 9 周期 72,387 行、`MAX(end_date)=2026-11-02`；采集 99.16%；单源域
  directory/status/profile/scale/manager/fee；publication generation 42、gate warning。

## 9. 本地只读 API

常态运行用统一启动器（后台常驻、固定 cwd、就绪自检）：

```bash
scripts/daemon.sh start|stop|restart|status      # web + scheduler
scripts/serve.sh start|stop|restart|status|logs # 只管理网页
```

临时前台调试：

```bash
python3 -m fundana serve --host 127.0.0.1 --port 8091
```

端点：
- `GET /api/v1/healthz`
- `GET /api/v1/publications/current`（无发布 → 503）
- `GET /api/v1/universe/coverage`
- `GET /api/v1/rankings?board=bond_stable&limit=50`
- `GET /api/v1/funds/000003`
- `GET /api/v1/operations/status`
- `GET /api/v1/rankings?...&fmt=csv`、`GET /api/v1/locked?...&fmt=csv`（导出当前视图，上限 5,000 行）
- `GET /api/v1/funds/<code>/decision`（含 `counterfactual` 反事实块）

完整端点表（导航、对比、净值明细、诊断、导出）见 `docs/web.md` 第 2 节；页面在 <http://127.0.0.1:8091/>。

## 10. 故障处理

| 现象 | 处置 |
|---|---|
| 源返回 200 但空 | 记 `provider_null`，不重试，冷却 7 天 |
| 5xx / 超时 | 重试 3 次（1/4/9s），仍失败记 `http_error` |
| 解析失败 | `parse_failed`，保留旧值，人工看 `fetch_log.error_msg` |
| 发布中途崩溃 | 旧 `.publication_current` 仍有效；删除 `reports/runs/<run_id>` 与 `.tmp` 后重跑 |
| 指标出现 NULL | 查 `undefined_reasons_json`（insufficient_points / zero_downside / zero_drawdown / return_basis） |
| 数据库过大 | SQLite backup API 热备（禁止 `cp`，会漏 WAL）；历史 NAV 可导出 Parquet |
| 页面打不开（无法访问此网站） | 先 `scripts/serve.sh status`；未运行则 `scripts/serve.sh start`，失败看 `var/state/serve.log`；端口被占用会明确提示。若重启后复现，检查是否装过自启：`scripts/install-autostart.sh status`；没有则 `install`（开机未登录也要跑加 `--linger`） |
| 页面/调度器重启后消失 | 主机重启不会自动拉起旧进程；安装 `scripts/install-autostart.sh install`，用 `systemctl --user status fundana-web.service fundana-scheduler.service` 与 `journalctl --user -u ...` 排查 |

## 11. 备份与恢复

```bash
python3 - <<'EOF'
import sqlite3
src = sqlite3.connect("var/db/fundana.db")
dst = sqlite3.connect("var/db/fundana.backup.db")
src.backup(dst)
dst.close(); src.close()
EOF
```

恢复后先 `PRAGMA integrity_check`，再 `verify --fast`。

## 12. 已知边界

- 无法从公开 API 获得：真实债券持仓评级/久期、真实跟踪误差、历史真实可买状态、用户成交价、未来收益。
- rf 只能配置观测值（0.0164，2026-10-25），变更需全量重算。
- 目录覆盖无法证明 100%，覆盖率报告只陈述 observed/declared。
## 13. 日更与调度（每日增量）

日更只采三样东西：名录、申赎状态、全市场当日净值快照（B1）。全历史仍由 pingzhongdata 负责（首次或回补时跑 collect --domain nav）。

    python3 -m fundana daily                 # 有新的业务日才发布，否则 controlled_skip
    python3 -m fundana daily --force         # 显式重算并发布（门槛/公式变更后使用）
    python3 -m fundana daily --skip-publish  # 只跑到筛选，不产生新发布
    python3 -m fundana scheduler --once      # 立即跑一次并写心跳
    python3 -m fundana scheduler             # 常驻：工作日 07:30 触发（跳过周六日）

daily 依次执行：collect_directory → collect_status → collect_nav_snapshot → 指标（全部债基，约 52s）→ fuse → screen → freeze → publish → tracker 记账 → forward cohort → shadow-rank → shadow-forward。任何一步失败都不会替换当前发布（发布仍是唯一提交点）；forward、shadow-rank 与 shadow-forward 失败只记录在 stages，不阻断。

B1 快照同时解析 `showday[0]` 与 `showday[1]`：当天写 `em_b1`，前一交易日写 `em_b1_prev`，只补缺失、不覆盖已有 pzd。

判断「今天该不该发布」的规则（2026-11-03 起）：先用 `freshness.effective_nav_date` 算有效净值日（回看 14 天，取覆盖率 ≥90% 的最新日）。若有效日没有前进（`as_of <= prev_effective`）且 365 天指标 current 比例 ≥90%，才 `controlled_skip`，reason=`no_new_effective_date`；否则继续算指标/筛选。

批次号是 `daily-<有效日>-<契约版本>`（例如 `daily-2026-11-02-v10`），避免与旧日期已冻结在 v8 的批次冲突；run 同时记录 `business_date`、`nav_as_of`、`metric_as_of`、`freshness_json`。

daily 只在指标算完、筛选/发布成功后才标 success；任何一步失败都不会替换当前发布。scheduler 在工作日 07:30 之后启动时，如果当天没有 success/skipped 的 daily，会先 `needs_catchup` 补跑。

调度器状态文件：var/state/scheduler.lock（单例）、var/state/scheduler_heartbeat.json（last_run_at / next_run_at / last_status）。

排查“榜单日期不动”：

    python3 - <<'PY'
    from fundana.config import load_config
    from fundana import db, freshness
    cfg=load_config(); conn=db.connect(cfg)
    print(freshness.nav_coverage(conn, types=('bond',), as_of=None))
    print(freshness.metric_coverage(conn, as_of=freshness.effective_nav_date(conn, types=('bond',))))
    conn.close()
    PY

看 `effective_date`、`max_date`、`effective_coverage_pct`、`current_pct`；不要只看 `MAX(nav_date)`。

## 14. 跟踪榜

    python3 -m fundana tracker --limit 20          # 当前跟踪榜（按出现次数、最近正式分排序）
    python3 -m fundana tracker --fcode 006431      # 单只基金的进出历史
    python3 -m fundana tracker --include-exited    # 含已退出
    python3 -m fundana tracker --rebuild           # 按业务日重算跟踪表（同日多份快照只留最后一份）

记账发生在发布成功之后：同一交易日再次发布会 supersede 当天前一份快照，再按业务日重放全表，所以「出现次数」永远等于在榜的交易日数。publication_id 仍然幂等——同一份快照重复发布只返回 skipped。改过跟踪口径（例如这次改成按日去重）后跑一次 `--rebuild` 修历史数据；跑之前先备份 screening_snapshot / screening_tracker / screening_tracker_event（`var/state/tracker-before-rebuild-*.sql.gz` 就是这次留下的样例）。

## 15. 净值日期修正（一次性，已执行）

    python3 -u scripts/reparse_nav_from_raw.py     # 从 raw_object 重放 pzd，重写 fund_nav_daily

用于 2026-11-01 修正「业务日按 UTC 解释导致整体左移一天」（ADR-043）。脚本不联网，只读已落库的原始响应，因此可离线重放、可审计。修完必须重跑 metrics + 一次 daily --force 才能让发布与指标对齐。

## 16. 跨源校验 / 前瞻 / 质量看板（Batch A）

三条命令都在**发布之后**跑（都需要一轮完整的 screen_run 与 publication）：

```bash
# 跨源核对：B1 快照 vs 本地净值、pzd syl_1n vs 本地 365 天复投
python3 -m fundana validate --cross

# 可选：抽样深检（逐只拉蛋卷/东财历史净值，慢，只在怀疑口径时用）
python3 -m fundana validate --cross --sample 20

# 前瞻：为最近一次筛选冻结 cohort，并用后续净值评估
python3 -m fundana forward --create
python3 -m fundana forward --cohort <cohort_id> --rows

# 质量看板（终端版，页面见 #/quality）
python3 -m fundana quality --days 7
```

**注意**

- `validate --cross` 必须在 `screen` 之后：阶段收益核对要用本轮的 `screen_run` 与 `fund_metric`；重复跑会覆盖该 field 的历史行（只保留最新一轮，避免重复计数）。
- `forward --create` 每个 cohort 只能建一次（同一 run 重复建会被拒绝），`--source retro` 仅用于回测研究，不写正式证据等级。
- 质量看板与门禁都读 `publication_gate` / `nav_cross_check` / `fetch_log`，不修改任何数据。

**缓释敏感性（离线，不重跑筛选）**

```bash
python3 scripts/relief_sensitivity.py   # → reports/analysis/relief_sensitivity.csv
```

用途：当缓释一条都不放行时，用已落库的 `relief_json` 做反事实（综合分 80/78/75/72 × 差距 0.15/0.10/0.05），先拿证据再决定要不要改常量（ADR-052）。

**验收**

`python3 scripts/acceptance.py` 现为 15 项：新增第 12 项（跨源核对有数据且不一致率在阈值内）、第 13 项（门禁不是 blocked 且 `validated.json` 带 gate）、第 14 项（cohort 正式成员数 == 正式榜人数、`not_ready` 不得有收益）、第 15 项（研究池 24–36 只、单公司 ≤2、缓释留痕存在）。
