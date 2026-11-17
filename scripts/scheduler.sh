#!/usr/bin/env bash
# Fund-ANA 日更调度器常驻启动器（工作日 07:30 跑一次 daily，单例锁 + 心跳）。
#
# 为什么需要它：`python3 -m fundana scheduler` 是前台进程，关掉终端就停；
# 心跳停在旧日期时，日更其实已经几天没跑，但页面上看不出来。
# 本脚本固定 cwd、nohup 常驻、写 pidfile，并用"心跳新鲜度"判断是否真的活着。
#
# 用法：scripts/scheduler.sh start|stop|restart|status|logs|once
#   once  = 立刻跑一次日更（前台，等价 python3 -m fundana scheduler --once）
set -u
export TZ=Asia/Shanghai

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
STATE_DIR="$ROOT/var/state"
PIDFILE="$STATE_DIR/scheduler.pid"
LOGFILE="$STATE_DIR/scheduler.log"
HEARTBEAT="$STATE_DIR/scheduler_heartbeat.json"
PY=${FUNDANA_PYTHON:-python3}
WAIT_SECONDS=${FUNDANA_WAIT_SECONDS:-20}

mkdir -p "$STATE_DIR"

pid_alive() {
  [ -f "$PIDFILE" ] || return 1
  local pid
  pid=$(cat "$PIDFILE" 2>/dev/null || true)
  [ -n "${pid:-}" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

# 调度器每 <=60s 写一次心跳；>180s 没更新说明循环卡死或进程已退出。
heartbeat_ok() {
  "$PY" - "$HEARTBEAT" <<'PYEOF' >/dev/null 2>&1
import json, os, sys, time
try:
    age = time.time() - os.stat(sys.argv[1]).st_mtime
except OSError:
    sys.exit(1)
if age > 180:
    sys.exit(1)
try:
    json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
sys.exit(0)
PYEOF
}

systemd_unit_exists() { systemctl --user cat fundana-scheduler.service >/dev/null 2>&1; }
systemd_unit_active() { systemctl --user is-active --quiet fundana-scheduler.service; }

cmd_start() {
  if systemd_unit_exists; then
    if systemd_unit_active && heartbeat_ok; then
      echo "由 systemd 管理：fundana-scheduler.service 已运行（心跳新鲜）"
      return 0
    fi
    echo "由 systemd 管理：启动 fundana-scheduler.service（cwd=$ROOT）"
    rm -f "$HEARTBEAT"
    if systemd_unit_active; then
      systemctl --user restart fundana-scheduler.service
    else
      systemctl --user start fundana-scheduler.service
    fi || {
      echo "systemd 启动失败，最近日志：" >&2
      systemctl --user --no-pager --full status fundana-scheduler.service 2>&1 | tail -30 >&2
      return 1
    }
    local i
    for i in $(seq 1 "$((WAIT_SECONDS * 4))"); do
      if heartbeat_ok; then
        echo "已启动：systemd：fundana-scheduler.service"
        cat "$HEARTBEAT"
        return 0
      fi
      sleep 0.25
    done
    echo "systemd 已接受启动，但 ${WAIT_SECONDS}s 内心跳未就绪。" >&2
    systemctl --user --no-pager --full status fundana-scheduler.service 2>&1 | tail -30 >&2
    return 1
  fi
  if pid_alive && heartbeat_ok; then
    echo "已在运行：pid $(cat "$PIDFILE")（心跳新鲜）"
    return 0
  fi
  if pid_alive && ! heartbeat_ok; then
    echo "进程在（pid $(cat "$PIDFILE")）但心跳不新鲜，先停掉再起。"
    cmd_stop
  fi
  rm -f "$PIDFILE"
  cd "$ROOT" || return 1
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') start scheduler ===" >>"$LOGFILE"
  nohup "$PY" -u -m fundana scheduler >>"$LOGFILE" 2>&1 &
  echo $! >"$PIDFILE"
  local i
  for i in $(seq 1 "$((WAIT_SECONDS * 4))"); do
    if pid_alive && heartbeat_ok; then
      echo "已启动：pid $(cat "$PIDFILE")"
      echo "下一次日更：$(cat "$HEARTBEAT")"
      echo "日志：$LOGFILE"
      return 0
    fi
    pid_alive || break
    sleep 0.25
  done
  echo "启动失败（心跳未就绪）。最近日志：" >&2
  tail -20 "$LOGFILE" >&2
  pid_alive || rm -f "$PIDFILE"
  return 1
}

cmd_stop() {
  if systemd_unit_active; then
    systemctl --user stop fundana-scheduler.service
    rm -f "$PIDFILE" "$HEARTBEAT"
    echo "已停止（systemd：fundana-scheduler.service）。"
    return 0
  fi
  if ! pid_alive; then
    rm -f "$PIDFILE"
    echo "未在运行。"
    return 0
  fi
  local pid i
  pid=$(cat "$PIDFILE")
  kill -TERM "$pid" 2>/dev/null || true
  for i in $(seq 1 40); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "进程 $pid 未响应 TERM，发送 KILL。"
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
  echo "已停止（pid $pid）。"
}

cmd_status() {
  if systemd_unit_exists; then
    if systemd_unit_active; then
      echo "systemd：active（fundana-scheduler.service）"
    else
      echo "systemd：inactive（fundana-scheduler.service）"
    fi
  fi
  if pid_alive; then
    echo "进程：运行中（pid $(cat "$PIDFILE")）"
  elif heartbeat_ok; then
    echo "进程：运行中（心跳新鲜；pidfile 未记录，通常由 systemd 托管）"
  else
    echo "进程：未运行"
  fi
  if heartbeat_ok; then
    echo "心跳：OK"
    cat "$HEARTBEAT"
    return 0
  fi
  echo "心跳：不新鲜或缺失（>180s），日更可能已经停摆。"
  [ -f "$LOGFILE" ] && { echo "最近日志："; tail -10 "$LOGFILE"; }
  return 1
}

cmd_restart() {
  if systemd_unit_exists; then
    echo "由 systemd 管理：重启 fundana-scheduler.service"
    rm -f "$HEARTBEAT"
    systemctl --user restart fundana-scheduler.service || return 1
    local i
    for i in $(seq 1 "$((WAIT_SECONDS * 4))"); do
      if heartbeat_ok; then
        echo "已重启：systemd：fundana-scheduler.service"
        cat "$HEARTBEAT"
        return 0
      fi
      sleep 0.25
    done
    echo "重启后 ${WAIT_SECONDS}s 内心跳未就绪。" >&2
    systemctl --user --no-pager --full status fundana-scheduler.service 2>&1 | tail -30 >&2
    return 1
  fi
  cmd_stop
  cmd_start
}

case "${1:-status}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    tail -n "${2:-60}" "$LOGFILE" ;;
  once)    cd "$ROOT" && "$PY" -u -m fundana scheduler --once ;;
  *) echo "用法：$0 start|stop|restart|status|logs|once" >&2; exit 2 ;;
esac
