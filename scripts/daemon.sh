#!/usr/bin/env bash
# Fund-ANA 常驻服务统一入口：web（网页前端）+ scheduler（日更调度器）。
#
# 用法：
#   scripts/daemon.sh start            启动 web 与 scheduler
#   scripts/daemon.sh stop             停止全部
#   scripts/daemon.sh restart [web|scheduler]
#   scripts/daemon.sh status           看两个服务是否真的活着（web 探 healthz，scheduler 看心跳）
#   scripts/daemon.sh logs [web|scheduler]
#
# 两个子脚本各自也能单跑：scripts/serve.sh、scripts/scheduler.sh。
set -u

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ACTION=${1:-status}
SCOPE=${2:-all}

run_web() { "$ROOT/scripts/serve.sh" "$@"; }
run_sched() { "$ROOT/scripts/scheduler.sh" "$@"; }

rc=0
case "$SCOPE" in
  web)       run_web "$ACTION" || rc=1 ;;
  scheduler) run_sched "$ACTION" || rc=1 ;;
  all)
    run_web "$ACTION" || rc=1
    run_sched "$ACTION" || rc=1
    ;;
  *) echo "未知服务：$SCOPE（可选 web|scheduler|all）" >&2; exit 2 ;;
esac
exit $rc
