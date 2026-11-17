# GitHub Actions 每日发布运行手册

本仓库的每日流水线**完全跑在 GitHub 托管 runner 上**：抓净值、筛选、计算、发布报告，
不依赖任何一台常开的本机。本机只作为历史数据库的源头（bootstrap 一次）。

- 仓库：https://github.com/DaDaDa12138/fund-lite （public）
- workflow：.github/workflows/daily.yml
- 触发：cron "30 23 * * *"（UTC）= 次日 **07:30 北京时间**，外加手动 workflow_dispatch
- 站点：Settings → Pages（部署产物无 JS、无 CDN，只是报告快照）
- 报告归档：每期一个 Release，tag 为 report-YYYY-MM-DD

## 1. 一次运行的时间线

| # | 步骤 | 干什么 | 失败后果 |
| --- | --- | --- | --- |
| 1 | 检查磁盘 | 可用 < 25GB 就删预装 SDK（dotnet/android/ghc/CodeQL） | 不致命 |
| 2 | 安装依赖 | pip -r requirements（PyYAML、requests）、apt poppler-utils（CSRC PDF 用） | 致命 |
| 3 | 数据源冒烟 | 仅在 smoke_only=true 时跑；只验连通性，不写库 | 不致命 |
| 4 | 交易日闸门 | scripts/trading_day.py：非交易日退出 78，后续步骤全部跳过 | 正常收工 |
| 5 | 拉状态包 | gh release download state（先 manifest，再按 manifest 列出的分片） | 没有 state release 且未开 bootstrap → 致命 |
| 6 | 恢复状态 | scripts/state_bundle.py import：先逐片校验 sha256，再 zstd -d \| tar 解到仓库根 | 校验失败 → 致命（不会污染旧数据） |
| 7 | 跑流水线 | python -m fundana daily --quiet \| tee run.json；被发布门禁拦下时最多重试 3 次、间隔 600s | 退出码非 0 → 后续发布步骤跳过，但仍会打包状态 |
| 8 | 校验发布 | python -m fundana verify --fast（校验 .publication_current 指向的 validated.json） | 致命 |
| 9 | 打包状态 | scripts/state_bundle.py export --slim：wal_checkpoint(TRUNCATE) → VACUUM → 切片 | 用 always() 兜住，尽量把进度带回去 |
| 10 | 上传状态包 | 先传新分片 → 最后传 manifest（--clobber）→ 删掉不在 manifest 里的旧资产 | 同上 |
| 11 | 生成站点 | scripts/site_build.py：把最近 20 期 report.md/summary.md 渲染成静态页 | 只影响站点 |
| 12 | 部署 Pages | configure-pages → upload-pages-artifact → deploy-pages | 只影响站点，下一轮重发即可 |
| 13 | 报告 Release | 读 reports/publication_state.json 的 nav_as_of，建/更新 report-<nav>，上传 9 个 CSV | 只影响归档 |
| 14 | 保活 | scripts/keepalive.py 往 status 分支写 .ci/last_run.json，重置公开仓库 60 天无活动停用计时 | 失败只警告 |
| 15 | 摘要 / 告警 | 写 GITHUB_STEP_SUMMARY；failure() 时开（或评论）一个 issue，并上传 run.json + diagnostics | - |

**顺序不是随意的**：状态包上传排在 Pages 之前。数据库推进必须先落地——Pages 挂了下一轮重发即可，
DB 没落地等于这一天的净值白跑。

**为什么第 7 步要重试**：发布门禁里有一条跨源核对（东财状态列表 vs 东财分页净值快照），
上游在刷新当天的列表时会出现「净值列已更新、净值日期列还停在上一交易日」的中间态，
同一批基金在两列之间自相矛盾，门禁判 mismatch。实测 2026-11-17 14:51 那次手动运行因此被拦
（379/16148 = 2.35% > 1%）。这种中间态通常几分钟到十几分钟就自洽，所以 workflow 只在
「publish 阶段 ok=false」时重跑，最多 3 次；硬错误（库损坏、依赖缺失）不重试，直接失败。

## 2. 状态包（state Release）

滚动资产，唯一 tag 是 state：

- state-YYYYmmddTHHMMSS.part.aa、.ab、…（单片 ≤ 1900MiB，Release 单资产上限 2GiB）
- manifest.json：format、stamp、as_of、nav_as_of、generation、publication_id、db_bytes、total_bytes、member_count、parts[{name, sha256, size}]

打包内容（**只有这些**）：

- var/db/fundana.db（瘦身后）
- reports/publication_state.json、reports/.publication_current
- reports/runs/<run>/revisions/<pub>/** 最近 20 期
- reports/screen/<run>/** 最近 5 期

**不打包**：var/raw（原始证据镜像）、var/freeze、var/state、reports/screenshots、reports/analysis。

瘦身（scripts/state_bundle.py slim，只作用于 runner 上的副本，本机数据库一行不动）：

- keep_run = publication 最近 20 期 ∪ screen_run 最近 5 期，其余 screen_run_input / screen_decision 删除
- keep_raw = 12 张 provenance 表的 raw_sha256 并集（有日期列且 ≥ as_of-120d 才加 WHERE；
  src_zqcc_holding / src_manager_history 没有业务日期列，只能整表保留）
- wal_checkpoint(TRUNCATE) → VACUUM → PRAGMA optimize；实测 8.94GB → 5.3–5.6GB

一致性约定：

- **manifest 永远最后上传**。上传中途断掉时，Release 上的旧 manifest 仍指向上一套完整分片，
  下一轮照旧能起来；新分片成了孤儿，由下一轮「删掉不在 manifest 里的资产」清掉。
- 导入端**先校验 sha256 再解压**，任何一片对不上就 SystemExit，不会解出半个数据库。
- 导入端边解压边删分片，避免峰值磁盘 = 压缩 + 解压同时占满。

## 3. 手动触发

~~~bash
# 普通补跑（走交易日闸门）
gh workflow run daily.yml --repo DaDaDa12138/fund-lite

# 忽略闸门，强制跑（周末/节假日试跑）
gh workflow run daily.yml -f force=true

# 只测数据源连通性（海外 runner IP 能不能拉到东财/证监会/中债）
gh workflow run daily.yml -f smoke_only=true

# 没有 state release 时允许空库起跑（第一次接入用）
gh workflow run daily.yml -f bootstrap=true
~~~

## 4. 排障

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 所有后续步骤 skipped，闸门输出 run=no | 当天不是交易日 | 正常。要试跑加 -f force=true |
| 没有状态包，且未开启 bootstrap | state release 不存在或被打包步骤跳过了 | 走第 6 节「首次接入」或 -f bootstrap=true |
| import 报 sha256 mismatch | 分片传输损坏，或有人手改了 Release 资产 | 删掉 state release 重来，或从本机重新 export 上传 |
| export 报 quick_check failed | 上一轮 daily 异常中断，DB 处于不一致状态 | 用本机数据库重建状态包（第 6 节），不要盲目重跑 |
| daily 退出码 1 | status 不是 ok / controlled_skip（见 run.json 的 stages） | 看 run.json 里失败阶段的 error，多半是数据源或 gate 阻断 |
| verify --fast 失败 | .publication_current 指向的 validated.json 校验不过 | 状态包和 reports/ 不配套，重新 import |
| Pages 部署失败 | 首次需要 enablement，或 environment 未就绪 | configure-pages 已带 enablement: true；再看 Settings → Environments |
| 磁盘不足 | runner 落地 < 25GB | 已自动清 SDK，仍不足则说明体积又涨了，调 keep-publications |

## 5. 回滚与停机

~~~bash
# 停止每日发布
gh workflow disable daily.yml --repo DaDaDa12138/fund-lite

# 恢复本机调度（切换回来后本机才是唯一写库方）
systemctl --user enable --now fundana-scheduler.service
bash scripts/scheduler.sh status

# 想把某一轮的状态包留档（manifest 一旦被覆盖就找不回来了）
gh release download state --dir /tmp/state-backup
# 上传前用 manifest 里的 sha256 逐片核对
~~~

代码回退不影响数据；**数据回退只能靠事前留档**——Release 是滚动的，覆盖即丢失。

## 6. 首次接入（bootstrap）

~~~bash
# 1) 用本机数据库做一份瘦身副本来生成状态包，绝不动原库
cp var/db/fundana.db var/db/fundana.bootstrap.db
python3 scripts/state_bundle.py export --root . --db var/db/fundana.bootstrap.db --slim --out dist/state
rm -f var/db/fundana.bootstrap.db
du -sh dist/state

# 2) 建 state release 并上传（分片在前，manifest 在后）
gh release create state dist/state/state-*.part.* --repo DaDaDa12138/fund-lite \
  --title "fundana state" --notes "滚动状态包：每日覆盖。manifest.json 最后上传。"
gh release upload state dist/state/manifest.json --repo DaDaDa12138/fund-lite

# 3) 触发一轮，同日再触发一轮验幂等（第二轮的闸门会因 controlled_skip 提前收工）
gh workflow run daily.yml -f bootstrap=true
gh run watch
~~~

## 7. 已知限制

- **公开仓库 = 全量数据公开**。DB、9 份 CSV、所有报告都能被任何人下载。不接受就把仓库改私有
  （私有仓库 Actions 分钟数计费，Linux 2 核按 2 倍计费，不免费）。
- 每日传输 2.5–6GB（下载 + 上传）。这不是「流量小」的方案，是「本机不用开机」的方案。
- 托管 runner 官方口径 14GB 磁盘，实际 root fs 常见 65–73GB，但**不保证**；已加自动清理。
- runner 是 **Python 3.13**，本机是 3.14。改动涉及 3.14 专属语法会先在 CI 炸。
- cron 有 5–20 分钟漂移，高峰期可能更久。净值发布窗口足够宽，不需要精确。
- 公开仓库 **60 天无活动**会停用定时任务，keepalive 步骤每天写 status 分支绕开这条。
- Pages 只发布报告快照，**不带 Web UI**（web/app.js 需要本机 API 服务）。
- 定时任务只保证「跑」，不保证「有新数据」：净值未更新时流水线会 controlled_skip，不产生新发布。

## 8. 本地对照命令

| 目的 | 本机 | CI |
| --- | --- | --- |
| 跑一遍 | python -m fundana daily | python -m fundana daily --quiet \| tee run.json |
| 语法+单测+契约 | bash scripts/ci.sh | 同左（ci.yml） |
| 校验发布 | python -m fundana verify --fast | 同左 |
| 打包状态 | python3 scripts/state_bundle.py export --root . --slim --out dist/state | 同左 |
| 恢复状态 | python3 scripts/state_bundle.py import --root . --src dist/state | 同左 |
| 生成站点 | python3 scripts/site_build.py --out site | 同左 |
| 数据源冒烟 | python3 scripts/smoke_sources.py | -f smoke_only=true |
| 交易日判断 | python3 scripts/trading_day.py | 闸门步骤 |

注意：export --slim 会就地瘦身并 VACUUM 它打包的那个库文件。本机操作时务必把 --db 指向副本，
归档里记录的名字仍是 var/db/fundana.db（解包即标准路径），所以 --db 指向副本不会把原库搞坏。
