# Fund-ANA V2 · 债券型基金筛选系统

从公开免 Key 接口采集国内公募基金数据 → 融合成基金级事实 → 计算九周期风险收益指标 →
按 L1–L10 规则筛选 → 原子发布带 SHA256 校验的报告。**首期只覆盖债券型基金**。

这份 README 的目标很具体：**任何人或任何 AI，从一台空机器开始照着做就能把它跑起来**，
并且知道每一步的预期输出、失败后该看哪里。业务口径与历史决策不在这里，见 [docs/decisions.md](docs/decisions.md)。

> **免责声明**：系统输出是候选池与研究视图，不是投资建议、不是基金评级，也不预测收益。
> 通过筛选只代表满足当前规则条件。数据来自公开接口，本项目只做本地融合与规则计算，
> 不转售原始数据、不提供代客理财。使用者自行承担判断责任。

---

## 0. 先选一条路

三条路径共用同一套代码、配置与命令，区别只在**谁在什么时间跑 `daily`**。

| 路径 | 适合谁 | 额外需要 | 数据落在 | 每天自动 |
| --- | --- | --- | --- | --- |
| **A 本机跑一轮** | 想先拿到一份报告/CSV，或要改代码 | Python 3.10+、约 15GB 磁盘 | 本机 `var/` 与 `reports/` | 否 |
| **B 本机常驻** | 要局域网网页 UI，且有台机器能一直开着 | 同 A + systemd --user 或桌面自启 | 本机 | 是（07:30 本地定时器） |
| **C 纯 GitHub Actions** | 不想为这件事开电脑 | GitHub 账号、**公开**仓库、`gh` CLI | 仓库 Release 里的状态包 | 是（GitHub cron） |

**三条路径都会写同一个 SQLite 库**，所以同一时刻只允许一个写库方：
SQLite 单写者，并行写会直接 `database is locked`（实测 `tail` 全量重算与 `collect` 并行时 fee 进程崩溃）。
从 B 切到 C 或从 C 切回 B，都要先停掉另一方，见 [第 4.10 节](#410-回滚与停机)。

---

## 1. 环境要求

| 项 | 要求 | 说明 |
| --- | --- | --- |
| Python | 3.10+ | CI 在 3.13、开发机在 3.14 上验证；代码未使用 3.13+ 专属语法，3.10–3.12 未实测 |
| 依赖 | `requirements.txt`：PyYAML>=6.0、requests>=2.31 | 其余全用标准库（sqlite3 / http.server / unittest） |
| 可选 | `poppler-utils`（证监会 PDF）、`zstd`（状态包）、`markdown`（静态站）、`gh`（路径 C） | 缺了对应功能降级，不阻塞主流程 |
| 磁盘 | 空库 <100MB；全量回填后 DB 约 9GB、原始证据 1.6GB、报告约 1GB | 状态包瘦身后 5.4GB、压缩后 1.4GB |
| 时区 | **必须 `Asia/Shanghai`** | `config/app.yaml` 的 `app.timezone` 写别的值会在启动时直接失败（ADR-082） |
| 网络 | 能访问东财、证监会 EID、中债 | 采集按每主机 ≤2 QPS 限速 |

```bash
python3 --version                      # 期望 3.10 以上
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

> 所有命令都在**仓库根目录**执行。`python3 -m fundana` 依赖当前目录在 `sys.path` 里，
> 在别的目录下运行会报 `No module named fundana`。

---

## 2. 路径 A：本机跑通一轮

### 2.1 初始化数据库

```bash
python3 -m fundana bootstrap --verify-config --migrate
```

预期输出（形状，不是逐字）：

```text
config_hash=<16 位摘要>
code_version=0.1.0  db=/…/var/db/fundana.db
配置文件: ['app.yaml', 'field_policy.yaml', 'source_registry.yaml']
  source em_directory         domain=directory ttl=1d qps=2 parser=…
  …（逐个源列出）
迁移: ['0001_init.sql', …]
表数: 62
```

命令会创建 `var/db/`、`var/raw/`、`var/freeze/`、`var/state/`、`reports/` 并应用全部迁移。
**任何配置错误都会在这一步失败**，这是最快的排错点。

### 2.2 数据源探针

```bash
python3 -m fundana probe --sources all --strict
```

HTTP 200 不算通过：每个源都真实抓取并解析，还有业务体量下限（名录/状态 ≥10,000 行、
净值序列 ≥100 点、档案 ≥5 个非空字段、持仓/排行 ≥1 行），低于下限记 `TOO_SMALL`。
这一步失败通常是上游改版或本机网络问题，**不要在探针红着的时候开始全量采集**。

### 2.3 首轮全量采集

耗时是本项目最容易误判的部分。以下是实测值（2 QPS 限速上限，单机串行）：

| 步骤 | 命令 | 规模 | 实测耗时 |
| --- | --- | --- | --- |
| 名录 + 状态 + 排行 | `collect --domain directory/status/rank --types bond` | 全市场一次请求级 | 分钟级 |
| 历史净值 | `collect --domain nav --types bond` | 7,454 只 | **约 62 分钟**（≈2 只/秒） |
| 档案 | `collect --domain profile --types bond --only-buyable --skip-collected` | 5,535 只 | 约 1 小时 |
| 债券持仓 | `collect --domain holding --types bond --only-buyable --skip-collected` | 5,535 只 | **约 2.5 小时**（0.6 只/秒） |
| 交易费用 | `collect --domain fee --types bond --only-buyable --skip-collected` | 5,535 只 | 60–75 分钟 |
| 规模历史 | `collect --domain scale --types bond --skip-collected` | 8,138 只 | 70–90 分钟 |
| 经理历史 | `collect --domain manager --types bond --skip-collected` | 8,229 只 | 约 1.5 小时 |
| 历史前十大持仓 | `python3 scripts/backfill_holdings.py --years 2016-2026` | 43 个季度 | **约 6.4 小时**（可断点续跑） |

**一次只跑一个写任务。** collect / metrics / tail / fuse / screen / publish 都写库。
NAV 采集每只基金约 3,000 行的批量写会长时间占写锁，并行只会更慢甚至崩。

首轮全流程可以一键串起来（等 NAV 结束 → 指标 → 档案 → 融合 → 筛选 → 发布 → 校验 → 验收）：

```bash
bash scripts/run_first_round.sh                 # 日志 var/state/round1.log
bash scripts/run_first_round.sh --skip-profile  # 已有档案时跳过采集
```

### 2.4 融合 → 指标 → 筛选 → 发布

```bash
python3 -m fundana fuse --types bond
python3 -m fundana metrics
python3 -m fundana comparability
python3 -m fundana screen --profile bond_v1
python3 -m fundana publish
python3 -m fundana verify --fast
```

现实中最常用的是**一条命令干完上面这些（含当日增量采集）**：

```bash
python3 -m fundana daily --quiet      # stdout 是纯 JSON，可直接 tee run.json
```

`daily` 的阶段顺序（`run.json` 里的 `stages`，`ok=false` 的段就是失败点）：

| 阶段 | 作用 | 失败后果 |
| --- | --- | --- |
| directory / status | 刷新名录与当日申赎状态 | 阻塞（状态缺失会让候选归零） |
| index | 指数点位 | 非阻塞 |
| snapshot | 一次请求拿全市场当日净值 | 阻塞 |
| nav_backfill | 停机跨日的缺口回补（上限 200 只） | 非阻塞 |
| cross_check | 第二来源核对当日净值 | 非阻塞（进门禁统计） |
| metrics | 九周期指标 | 阻塞 |
| comparability | 可比性旗标 | **阻塞** |
| factors / mrar | 因子归因、长期质量 | 非阻塞 |
| fuse | 基金级事实融合 | 阻塞 |
| screen | L1–L10 筛选 | 阻塞 |
| publish | 构建产物 + 原子提交 + 跟踪榜 | 阻塞（失败保旧） |
| maintenance / lifecycle / forward / shadow | 维护采集、生命周期、前瞻、影子 | 非阻塞 |

`daily` 的退出码：`ok` 或 `controlled_skip`（当日没有新的有效净值日）返回 0，其余返回 1。
常用开关：`--force` 忽略「有效净值日」判断、`--skip-publish` 只算不发、`--types bond`。

### 2.5 结果在哪

```
reports/.publication_current → runs/<run_id>/revisions/<publication_id>/
```

先看 `summary.md`（一页看完计数与稳健榜前十），再看 `report.md`（全量报告）、
`bond_*.csv`（各榜单）、`decisions/<fcode>.json`（单只基金的阈值/分数/原因码）、
`diagnostics/*.csv`（覆盖、冲突、NULL、风格证据、费用覆盖…）。

`verify --fast` 通过意味着：符号链接指向的 `validated.json` 逐文件 SHA256 与大小都对得上，
且发布门禁没有 `blocked`。

---

## 3. 路径 B：本机常驻（网页 + 每日 07:30）

```bash
scripts/daemon.sh start      # 网页(8091) + 日更调度器一起后台常驻
scripts/daemon.sh status     # 两个服务都做真实健康检查
scripts/daemon.sh stop
```

也可以单跑：`scripts/serve.sh start|stop|status|logs`（只管网页）、
`scripts/scheduler.sh ...`（只管日更）。日志在 `var/state/serve.log`、`var/state/scheduler.log`。

**开机/登录自启**（主机重启后服务没起来，是「前端打不开」最常见的原因）：

```bash
scripts/install-autostart.sh install           # systemd --user，登录后自启
scripts/install-autostart.sh install --linger  # 开机即启动，不要求先登录（可能触发 polkit 授权）
scripts/install-autostart.sh status
scripts/install-autostart.sh uninstall
```

unit 名：`fundana-web.service`、`fundana-scheduler.service`。安装器按仓库实际路径渲染 unit，
写进 `~/.config/systemd/user/`（`deploy/systemd/` 里的是参考副本，用 `%h/fund` 占位）。

调度器行为：工作日 07:30 触发一次 `python3 -m fundana daily --quiet`（本地时间，Asia/Shanghai），
单例锁 + 心跳文件 `var/state/scheduler_heartbeat.json`；
失败按 `scheduler.max_attempts`（默认 3 次、间隔 900s）重试，**publish 阶段失败不重试**；
进程晚启动会 `needs_catchup` 补跑当天。

网页默认绑定 `0.0.0.0:8091`（便于局域网访问）；只想本机访问把 `config/app.yaml` 的 `api.host`
改成 `127.0.0.1`。默认只读无鉴权，可选 `api.auth_token` 启用 Bearer Token。
**不要做端口映射暴露到公网。**

---

## 4. 路径 C：纯 GitHub Actions（不依赖本机）

这条路径把「抓净值 → 筛选 → 计算 → 发布」整体搬到 GitHub 托管 runner：每天定时拉一个
**状态包**（瘦身后的数据库 + 最近几期报告）→ 跑 `daily` → 把新状态包推回去 → 部署静态站。
本机只在首次接入时提供一次数据库，之后可以长期关机。

### 4.1 前置检查

```bash
gh --version && gh auth status          # 需要 repo + workflow 权限
git --version
```

### 4.2 把代码推到自己的仓库

新建仓库：

```bash
git init -b main
git add -A
git commit -m "chore: import fundana"
gh repo create <OWNER>/<REPO> --public --source . --remote origin --push
```

已有空仓库：

```bash
git init -b main
git remote add origin https://github.com/<OWNER>/<REPO>.git
git add -A && git commit -m "chore: import fundana"
git push -u origin main
```

> **必须是公开仓库**才有免费 Actions 分钟数；私有仓库按 Linux 2 核 2 倍计费。
> 公开也意味着**数据全部公开**——库、CSV、报告谁都能下。不能接受就把仓库设为私有并接受计费。

推送后 `ci.yml` 会立刻跑一次（语法 + 660 项单测 + 前后端契约自检），约 30 秒。**先让它绿**。

### 4.3 一次性建 Pages 站点

`GITHUB_TOKEN` **建不了** Pages 站点（会报 `Resource not accessible by integration`），
所以站点要手工建一次；建好之后工作流里的 `configure-pages` 只做读取部署：

```bash
gh api -X POST repos/<OWNER>/<REPO>/pages -f build_type=workflow
```

预期返回 JSON 里有 `html_url`（形如 `https://<owner>.github.io/<repo>/`）。
也可以在网页 Settings → Pages 里把 Source 设为 **GitHub Actions**。

### 4.4 生成并上传 bootstrap 状态包

**推荐做法：用一台已经跑过全量回填的机器导出**（历史净值是几十小时攒出来的，空库起跑等于从零开始）。

```bash
cp var/db/fundana.db var/db/fundana.bootstrap.db
python3 scripts/state_bundle.py export --root . --db var/db/fundana.bootstrap.db --slim --out dist/state
rm -f var/db/fundana.bootstrap.db      # 5.4GB 的副本，导出完就删
ls -lh dist/state
```

预期得到 `manifest.json` + `state-<时间戳>.part.aa`（单片 ≤1900MiB，超了自动切片 `.ab`/`.ac`…）。
实测：8.9GB 的库瘦身到 5.39GB、压成 1.42GB 单片，耗时约 3 分钟。

`export` 会**默认清空个人信息表**（当前只有 `fund_watchlist` 自选/备注），
详见 [第 10 节](#10-脱敏与隐私)。只想做本机私有备份时才加 `--keep-personal`。

上传（**顺序是强制的：分片先，manifest 最后**）：

```bash
gh release create state dist/state/state-*.part.* --title "fundana state" --notes "滚动状态包：每日覆盖，manifest.json 最后上传。"
gh release upload state dist/state/manifest.json
```

中途失败也不要紧：旧 manifest 仍指向上一套完整分片，下一轮照旧能起来，孤儿分片会被下一轮清掉。

**没有本机历史库时**只能空库起跑：

```bash
gh workflow run daily.yml -f bootstrap=true
```

空库能跑，但只拿到当日全市场快照；历史净值靠每日 `nav_backfill`（上限 200 只）慢慢攒，
指标窗口要很久才够。**这只适合验证链路，不适合当正式数据源。**

### 4.5 触发并验证

```bash
gh workflow run daily.yml -f force=true    # 忽略交易日闸门，第一次验证用
gh run watch                               # 约 4 分钟（实测 4 分 02 秒）
```

验收清单（全部通过才算接好）：

| 检查 | 命令 | 期望 |
| --- | --- | --- |
| 工作流全绿 | `gh run list --workflow daily.yml --limit 1` | `completed success` |
| 发布前进了一代 | `gh release view state --json assets` / run 日志的 `publication_state.json` | generation 比上一轮 +1 |
| 站点可访问 | `curl -s -o /dev/null -w '%{http_code}' https://<owner>.github.io/<repo>/` | `200` |
| 归档存在 | `gh release view report-<nav_as_of> --json assets` | 9 个 CSV |
| 保活生效 | `gh api repos/<OWNER>/<REPO>/contents/.ci/last_run.json?ref=status` | 有当天时间戳 |

然后**再触发一次**（不带 `force`）：第二轮应当走 `controlled_skip` 提前收工——这是幂等性验证，
重复发布同一个净值日只会 `generation+1`、跟踪榜不重复计数。

### 4.6 每天发生什么

| # | 步骤 | 说明 |
| --- | --- | --- |
| 1 | 检查磁盘 | 可用 <25GB 就删预装 SDK（dotnet/android/ghc/CodeQL） |
| 2 | 安装依赖 | Python 3.13 + `pip -r requirements.txt -r requirements-ci.txt` + `apt poppler-utils` |
| 3 | 交易日闸门 | `scripts/trading_day.py`：非交易日退出 78，后续全跳过（`-f force=true` 可绕过） |
| 4 | 拉状态包 | 先下 `manifest.json`，再按清单下分片 |
| 5 | 恢复状态 | 逐片校验 SHA256 → `zstd -d` → 解包到仓库根，边解压边删分片 |
| 6 | 跑流水线 | `python -m fundana daily --quiet`；被发布门禁拦下最多重试 3 次、间隔 600s |
| 7 | 校验发布 | `python -m fundana verify --fast` |
| 8 | 打包状态 | `state_bundle.py export --slim`（`always()`，失败也尽量把进度带回去） |
| 9 | 上传状态包 | 新分片 → manifest `--clobber` → 删旧资产 |
| 10 | 生成站点 | 最近 20 期渲染成静态页（无 JS、无 CDN） |
| 11 | 部署 Pages | `configure-pages` → `upload-pages-artifact` → `deploy-pages` |
| 12 | 报告归档 | 建/更新 `report-<nav_as_of>` Release，上传 9 个 CSV |
| 13 | 保活 | 往 `status` 分支写 `.ci/last_run.json`，绕开公开仓库 60 天无活动停用定时任务 |
| 14 | 摘要 / 告警 | 写 `GITHUB_STEP_SUMMARY`；失败时开（或评论）issue 并上传失败现场 |

**顺序不是随意的**：状态包上传排在 Pages 之前——数据库推进必须先落地，
Pages 挂了下一轮重发即可，DB 没落地等于这一天的净值白跑。

### 4.7 状态包契约

- 资产：`state-YYYYmmddTHHMMSS.part.{aa,ab,…}`（单片 ≤1900MiB，Release 单资产上限 2GiB）+ `manifest.json`
- `manifest.json` 字段：`format`、`stamp`、`as_of`、`nav_as_of`、`generation`、`publication_id`、`db_bytes`、`total_bytes`、`member_count`、`parts[{name,sha256,size}]`
- 打包内容**只有**：`var/db/fundana.db`、`reports/publication_state.json`、`reports/.publication_current`、最近 20 期 `revisions/`、最近 5 次 `reports/screen/`
- **不打包**：`var/raw`（原始证据镜像）、`var/freeze`、`var/state`、`reports/screenshots`、`reports/analysis`
- 瘦身规则：`keep_run = 最近 20 期发布 ∪ 最近 5 次筛选`，其余 `screen_run_input`/`screen_decision` 删除；
  `keep_raw = 12 张 provenance 表引用的 raw_sha256 并集`（有日期列且 ≥ as_of-120d 才加条件，
  `src_zqcc_holding`/`src_manager_history` 无业务日期列只能整表保留）；最后 `wal_checkpoint(TRUNCATE)` → `VACUUM`

### 4.8 手动触发

```bash
gh workflow run daily.yml                     # 普通补跑（走交易日闸门）
gh workflow run daily.yml -f force=true      # 忽略闸门，周末/节假日试跑
gh workflow run daily.yml -f smoke_only=true # 只测数据源连通性（海外 runner 能否拉到东财/证监会/中债）
gh workflow run daily.yml -f bootstrap=true  # 没有 state release 时允许空库起跑
```

### 4.9 排障

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 后续步骤全 skipped，闸门输出 `run=no` | 当天不是交易日 | 正常；要试跑加 `-f force=true` |
| 报「没有状态包且未开 bootstrap」 | `state` release 不存在，或上一轮打包步骤被跳过 | 走 4.4 或 `-f bootstrap=true` |
| `import` 报 `sha256 mismatch` | 分片传坏了，或有人手改了 Release 资产 | 重传该 release，或本机重新 export |
| `export` 报 `quick_check failed` | 上一轮 daily 异常中断，库不一致 | 用本机库重建状态包，别盲目重跑 |
| `daily` 退出码 1 | `status` 不是 ok/controlled_skip | 看 `run.json` 里 `stages` 的 `ok=false` 段与 `error` |
| `verify --fast` 失败 | `validated.json` 与产物不配套 | 状态包和 `reports/` 不配套，重新 import |
| Pages 部署失败 | 站点没建（4.3 没做），或 environment 未就绪 | 建站后重跑；看 Settings → Environments |
| 磁盘不足 | runner 可用空间 <25GB | 已自动清 SDK；仍不足就调小 `--keep-publications` |
| 发布被门禁拦下（`validation_hard_block`） | 跨源不一致超过 1%，常见于上游刷新中间态 | 工作流已自动重试 3 次；仍失败见 `run.json` 的 `error` |

### 4.10 回滚与停机

```bash
# 停：禁用定时任务
gh workflow disable daily.yml

# 切回本机前，先把最新状态包拉回本机（否则本机库停在切流那天，接管后第一次运行会算错增量）
gh release download state --dir /tmp/state-latest
python3 scripts/state_bundle.py import --root . --src /tmp/state-latest

# 恢复本机调度
systemctl --user enable --now fundana-scheduler.service   # 或 scripts/daemon.sh start
bash scripts/scheduler.sh status
```

代码回退不影响数据；**数据回退只能靠事前留档**——Release 是滚动的，覆盖即丢失：

```bash
gh release download state --dir /tmp/state-backup   # 想留档就定期存一份
```

---

## 5. 配置

三个文件都在 `config/`：`app.yaml`（运行参数）、`source_registry.yaml`（17 个源的地址/角色/限速/解析器）、
`field_policy.yaml`（字段口径与优先级）。常用项：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `app.timezone` | `Asia/Shanghai` | **必须是这个值**，否则启动失败 |
| `db.path` / `paths.*` | 相对路径 | 全部相对仓库根，迁移到别的机器不用改 |
| `universe.types` | `[bond]` | 参与采集/筛选的类型，新增类型必须补齐插件（`python3 -m fundana types` 看缺口） |
| `fetch.max_qps_per_host` | `2` | 每主机限速；调高有被封风险，别动 |
| `collect.nav_backfill_max_funds` | `200` | 停机跨日时逐只回补的基金上限 |
| `risk.formula_contract` | `fundana-risk-2026.11.5` | 指标口径版本；改它等于换一套指标 |
| `screen.dynamic_gate_rule` | `composite_and_k_of_n` | 动态分位门槛规则 |
| `screen.enhanced_strict_min` | `5` | 增强债相对准入之外要求的严格项通过数 |
| `gate.mode` | `strict` | `strict` 命中 block 不切换 current；`warn` 只记录 |
| `gate.mismatch_ratio_max` | `0.01` | 跨源不一致比例上限 |
| `validation.cross_daily` | `true` | 每日用第二来源复核净值 |
| `api.host` / `api.port` | `0.0.0.0` / `8091` | 网页绑定；只想本机访问改成 `127.0.0.1` |
| `api.auth_token` | 空 | 填了就要求 `Authorization: Bearer <token>` |
| `maintenance.tasks` | 见文件 | 维护采集周期（档案/费率/经理 30 天，持仓/规模 80 天） |
| `scheduler.max_attempts` | `3` | 日更失败重试次数（不进 `config_hash`） |

改完配置先跑一次 `python3 -m fundana bootstrap --verify-config`，再跑 `bash scripts/ci.sh`。

---

## 6. 产物与验证

`reports/.publication_current` 指向 `runs/<run_id>/revisions/<publication_id>/`，内含：

| 文件 | 内容 |
| --- | --- |
| `summary.md` / `report.md` | 一页摘要 / 全量报告 |
| `bond_stable.csv`、`bond_enhanced.csv` | 稳健榜、增强榜 |
| `bond_core.csv`、`bond_core10.csv` | 核心精选榜（50 只）及其 10 只子集 |
| `bond_watch.csv`、`bond_rookie.csv`、`bond_locked.csv` | 观察榜、新锐榜、持有期 >90 天受限榜 |
| `all_formal_candidates.csv` | 全部正式候选（含未入榜原因） |
| `decisions/<fcode>.json` | 单只基金的阈值、分数、原因码、九周期指标 |
| `diagnostics/*.csv` | 覆盖、冲突、NULL、风格证据、尾部覆盖、费用覆盖、家族重复等 |
| `validated.json` | 逐文件 SHA256 + 大小，发布门禁的唯一凭据 |

验证手段（从轻到重）：

```bash
python3 -m fundana verify --fast      # 符号链接 + 清单校验，秒级
python3 -m fundana verify             # 完整校验（含文件内容）
python3 -m fundana data-health --skip-integrity --days 7   # 库/迁移/净值/采集/来源/发布健康
python3 scripts/acceptance.py         # 15 条验收标准（含跨源核对、门禁、前瞻 not_ready）
```

---

## 7. 测试与 CI

```bash
bash scripts/ci.sh
```

三步：`compileall` 全量语法 → `unittest discover -s tests`（660 项，约 9 秒）→
`scripts/check_ui_contract.py`（列头说明 / 排序白名单 / SQL 列名三处静态比对）。
CI 断言不联网、不写库；没有数据库时契约自检自动降级为静态检查。

`.github/workflows/ci.yml` 在每次 push 与 PR 上跑同一条命令，Python 3.13。

---

## 8. 目录结构

```text
config/            app.yaml、source_registry.yaml、field_policy.yaml
.github/workflows/ ci.yml（语法+单测+契约）、daily.yml（每日抓取→筛选→计算→发布→Pages）
scripts/           首轮脚本、状态包、静态站、数据源冒烟、交易日判断、保活、验收、CI
fundana/
  cli.py           全部子命令入口
  config.py        配置加载与 config_hash
  db.py            SQLite 连接 / 迁移 / run / fetch_log / raw_object
  collect.py       采集与解析落库（名录/状态/档案/净值/排行/费用/规模/经理/持仓/指数/生命周期）
  fetch/           HTTP 限频、TTL 缓存、解析器
  fuse/            身份 / 份额 / 画像 / 净值 / 风格 / 覆盖 融合
  metrics/         窗口 / 收益 / 风险 / 尾部 / 契约 指标引擎
  fees/            费用阶梯 + 持有期总成本
  screen/          阈值 / 打分 / L1–L10 引擎
  publish/         builder / commit / verify（唯一提交点）
  validate/        跨源核对
  api/             本地只读 HTTP API
  migrations/      0001_init.sql … 共 36 个
web/               零构建单页前端（index.html / app.js / styles.css）
docs/              架构、ADR、口径、数据目录、运行手册、审查报告
tests/             stdlib unittest
deploy/systemd/    参考 unit（真正安装用 scripts/install-autostart.sh）
var/               db / raw / freeze / state（运行时生成，不入库）
reports/           runs/<run_id>/revisions/<publication_id>/ + .publication_current
```

`var/`、`reports/`、`site/`、`dist/` 都在 `.gitignore` 里——**数据不进版本库**。

---

## 9. 数据来源与合规

全部是**公开免 Key** 接口，限速每主机 ≤2 QPS、超时 20s、失败重试 3 次（退避 1/4/9s）：

| 域 | 来源 |
| --- | --- |
| 名录 / 状态 / 排行 / 净值 | `fund.eastmoney.com`、`api.fund.eastmoney.com/f10/lsjz` |
| 档案 / 费率 / 规模 / 经理 / 持仓 | `fundf10.eastmoney.com` |
| 社区数据 | `danjuanfunds.com`（djapi） |
| 指数点位 | `push2his.eastmoney.com` |
| 清盘/合并公告 | `eid.csrc.gov.cn`（月度 PDF，需 `pdftotext`） |
| 债券指数 | `yield.chinabond.com.cn`（月度 LPR 与国债收益率曲线上限参考） |

合规口径：只做本地融合与规则计算，不存储/转售原始数据，不提供投资建议；
每一条入库记录都保留 `raw_sha256`（原始响应镜像），可离线重放与审计。

---

## 10. 脱敏与隐私

本仓库在发布前做过的脱敏处理，以及**你 fork 之后需要自己确认的事**：

**已处理（仓库内不再出现）**

- 本机绝对路径：systemd 参考 unit 改用 `%h` 占位，文档里的复现命令改用 `$(git rev-parse --show-toplevel)`
- 内网地址：文档中的局域网 IP 换成 `<本机内网 IP>` 占位
- 账号与仓库 slug：`gh` 命令不再硬编码 `--repo`，在仓库目录内执行即作用于当前仓库
- 一次性调试脚本：已删除（硬编码业务日与本机路径的那种）
- 导出状态包时**默认清空个人信息表** `fund_watchlist`（自选代码 + 备注），见 `scripts/state_bundle.py` 的 `redact()`；
  只想本机私有备份时显式加 `--keep-personal`

**你 fork 后要自己确认**

```bash
grep -rn --exclude-dir=.git --exclude-dir=var --exclude-dir=reports -E '(/home/|/Users/|[0-9]{1,3}([.][0-9]{1,3}){3})' .
```

- `config/app.yaml` 的 `api.auth_token` 留空再提交；要启用就放 `secrets`，不要写进仓库
- `config/source_registry.yaml` 里如果加过带个人参数的地址，**不要提交**
- `var/`、`reports/`、`dist/` 已在 `.gitignore` 里，确认 `git status` 没有它们
- **公开仓库 = 全量数据公开**：数据库、9 份 CSV、全部报告谁都能下载。路径 C 请把这条当成前提接受
- 历史提交也会泄露：改完 HEAD 之后 `git log -p` 仍能翻到旧内容，需要改写历史并强推；
  更强的做法是新仓库只推一份干净的初始提交

---

## 11. 给 AI / 自动化执行者的执行清单

按顺序做，每步都有**通过判据**；不通过就停下修，不要继续往下堆。

| # | 动作 | 通过判据 |
| --- | --- | --- |
| 1 | `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt` | `python3 -c "import yaml, requests"` 无异常 |
| 2 | `python3 -m fundana bootstrap --verify-config --migrate` | 输出含 `迁移:` 与 `表数: 62` |
| 3 | `bash scripts/ci.sh` | `OK`（660 项）+ `结论：全部一致` |
| 4 | `python3 -m fundana probe --sources all --strict` | 退出码 0 |
| 5 | 采集（按 2.3 顺序，**串行**） | `python3 -m fundana stats` 里 `fund_nav_daily` 行数在涨 |
| 6 | `python3 -m fundana daily --quiet` | 退出码 0，`run.json` 的 `status` 是 `ok` 或 `controlled_skip` |
| 7 | `python3 -m fundana verify --fast` | 退出码 0 |
| 8 | `bash scripts/ci.sh`（改过代码再跑） | 同上 |
| 9 | 路径 C：`gh repo create … --push` | `gh run list --workflow ci.yml` 为 success |
| 10 | 路径 C：建 Pages（4.3） | 返回 JSON 含 `html_url` |
| 11 | 路径 C：`state_bundle.py export --slim` + 上传 | `gh release view state` 有 `manifest.json` 与 `part.aa` |
| 12 | 路径 C：`gh workflow run daily.yml -f force=true` | 全绿；`curl` 站点 200；`report-<nav_as_of>` 有 9 个 CSV |

**常见误判**：

- 探针只看 HTTP 200 就当通过 —— 必须看业务体量下限，空结构也算失败
- 并行跑采集想省时间 —— SQLite 单写者，会 `database is locked`，串行更快
- 空库直接上 GitHub Actions 当正式源 —— 只有当日快照，历史靠每天 200 只回补，指标窗口很久才够
- 用 `MAX(nav_date)` 判断有没有新数据 —— 应看「有效净值日」（14 天回看 + 覆盖率 ≥90%），否则节假日会空跑
- 改动 `config/app.yaml` 的 `app.timezone` —— 启动即失败，是刻意的

---

## 12. 已知限制

- **只覆盖债券型**。混合型/货币型的插件地基在 `fundana/fundtypes.py`，但采集域、风格体系、阈值标定都还没做。
- **上游中间态会误报**：东财状态列表刷新当天可能出现「净值列已更新、净值日期列还停上一交易日」，
  跨源核对会把这类行判 `date_lag` 而不是 `mismatch`（已重分类并在 detail 留痕），但门禁仍可能在极端情况下抖动。
- **前瞻 cohort 需要真实时间成熟**，30 天后才有第一份结论；未到期一律 `not_ready`，不写 0。
- **MGOLD 评分无公开接口**，暂用东财评分代替。
- **幸存者偏差只做了部分校正**：证监会 EID 确认清盘的 445 只已回填，合并样本仍不完整。
- 路径 C 的 runner 磁盘官方口径 14GB（实测多见 65–73GB，但不保证），工作流已加自动清理；
  每日上下行 2.5–6GB，不是「流量很小」的方案。
- 定时任务只保证「跑」，不保证「有新数据」：净值没更新时会 `controlled_skip`，不产生新发布。

---

## 13. 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 分层架构、数据流、模块边界 |
| [docs/decisions.md](docs/decisions.md) | ADR 全集（每条口径变更的原因与替代方案） |
| [docs/screening-rules.md](docs/screening-rules.md) | L1–L10 筛选中每一层的规则与阈值 |
| [docs/fusion-rules.md](docs/fusion-rules.md) | 融合口径：身份、份额、风格、覆盖 |
| [docs/formula-contract.md](docs/formula-contract.md) | 指标公式契约（版本化，可回放） |
| [docs/data-catalog.md](docs/data-catalog.md) | 表结构与字段口径 |
| [docs/runbook.md](docs/runbook.md) | 本机运行手册（采集耗时、增量策略、维护） |
| [docs/runbook-github-actions.md](docs/runbook-github-actions.md) | GitHub Actions 每日发布手册（时间线、状态包契约、排障、回滚） |
| [docs/web.md](docs/web.md) | 网页前端与 API |