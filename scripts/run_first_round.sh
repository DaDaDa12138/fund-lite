#!/usr/bin/env bash
# 首轮全流程：等 NAV 采集结束 → 指标 → 档案采集 → 融合 → 筛选 → 发布 → 校验 → 验收
# 用法：bash scripts/run_first_round.sh [--skip-profile]
set -euo pipefail
cd "$(dirname "$0")/.."
LOG=var/state/round1.log
: > "$LOG"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

log "等待 NAV 采集进程结束"
while pgrep -f "fundana collect --domain nav" >/dev/null; do sleep 20; done
log "NAV 采集已结束"

log "① 指标计算（全部债券基金 × 9 周期）"
python3 -m fundana metrics >>"$LOG" 2>&1

if [[ "${1:-}" != "--skip-profile" ]]; then
  log "② 档案采集（当日可申购，跳过已采集）"
  python3 -m fundana collect --domain profile --types bond --only-buyable --skip-collected >>"$LOG" 2>&1
fi

log "③ 融合（identity/profile/style/coverage）"
python3 -m fundana fuse --types bond >>"$LOG" 2>&1

log "④ 筛选 L1–L10"
python3 -m fundana screen --profile bond_v1 >>"$LOG" 2>&1

log "⑤ 发布（原子提交）"
python3 -m fundana publish >>"$LOG" 2>&1

log "⑥ 校验发布产物"
python3 -m fundana verify --fast >>"$LOG" 2>&1

log "⑦ 验收 7 条"
python3 scripts/acceptance.py >>"$LOG" 2>&1 || log "验收存在未通过项（见上）"
log "完成"
