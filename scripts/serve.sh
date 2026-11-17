#!/usr/bin/env bash
# Fund-ANA 网页前端常驻启动器。
#
# 为什么需要它：`python3 -m fundana serve` 只在项目根目录（fundana 包可导入）成立，
# 换个目录就报 "No module named fundana"；前台进程关掉终端就没了，浏览器只会得到
# "无法访问此网站"。本脚本固定 cwd、后台常驻、写 pidfile 与日志，并在返回前自检 /api/v1/healthz。
#
# 用法：scripts/serve.sh start|stop|restart|status|logs
# 环境变量：FUNDANA_HOST / FUNDANA_PORT / FUNDANA_PYTHON / FUNDANA_WAIT_SECONDS
set -u
export TZ=Asia/Shanghai

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
STATE_DIR="$ROOT/var/state"
PIDFILE="$STATE_DIR/serve.pid"
LOGFILE="$STATE_DIR/serve.log"
PY=${FUNDANA_PYTHON:-python3}
HOST=${FUNDANA_HOST:-}
PORT=${FUNDANA_PORT:-8091}
WAIT_SECONDS=${FUNDANA_WAIT_SECONDS:-20}

mkdir -p "$STATE_DIR"

healthz() {
  "$PY" - "$1" <<'PYEOF' >/dev/null 2>&1
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
}

pid_alive() {
  [ -f "$PIDFILE" ] || return 1
  local pid
  pid=$(cat "$PIDFILE" 2>/dev/null || true)
  [ -n "${pid:-}" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # 防 PID 复用：只有命令行确实是 fundana serve 才认这个 pidfile。
  ps -p "$pid" -o args= 2>/dev/null | grep -q -- 'fundana.*serve'
}

# 监听 8091 的进程号。用于接管“服务已经起来了、但 pidfile 丢了/没写过”的场景：
# 例如重启后手工 nohup 启动、或 systemd 直接拉起 python 而没有经过本脚本。
listener_pid() {
  ss -ltnp 2>/dev/null | grep -E "[:.]$PORT[[:space:]]" | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2
}

# 已有健康服务时接管它，避免 status 误报“未运行”、start 误报“端口被占用”。
adopt_healthy() {
  healthz "$(base_url)api/v1/healthz" || return 1
  local pid
  pid=$(listener_pid)
  [ -n "${pid:-}" ] || return 1
  echo "$pid" >"$PIDFILE"
  return 0
}

base_url() { echo "http://127.0.0.1:$PORT/"; }

# systemd --user 托管时，start/stop/restart 必须交给 systemctl，
# 否则手工 kill 会被 Restart=on-failure 立刻拉起，看起来像“停不掉”。
systemd_unit_exists() { systemctl --user cat fundana-web.service >/dev/null 2>&1; }
systemd_unit_active() { systemctl --user is-active --quiet fundana-web.service; }

wait_ready() {
  local url i
  url=$(base_url)
  for i in $(seq 1 "$((WAIT_SECONDS * 4))"); do
    if healthz "${url}api/v1/healthz"; then return 0; fi
    if [ -f "$PIDFILE" ] && ! pid_alive; then return 1; fi
    sleep 0.25
  done
  return 1
}

port_busy() {
  "$PY" - "$PORT" <<'PYEOF' >/dev/null 2>&1
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(0)
finally:
    s.close()
sys.exit(1)
PYEOF
}

lan_ip() {
  "$PY" - <<'PYEOF' 2>/dev/null
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
except OSError:
    print("")
finally:
    s.close()
PYEOF
}

cmd_start() {
  if systemd_unit_exists; then
    if systemd_unit_active && healthz "$(base_url)api/v1/healthz"; then
      echo "由 systemd 管理：fundana-web.service 已运行 · $(base_url)"
      return 0
    fi
    echo "由 systemd 管理：启动 fundana-web.service（cwd=$ROOT）"
    rm -f "$PIDFILE"
    if systemd_unit_active; then
      systemctl --user restart fundana-web.service
    else
      systemctl --user start fundana-web.service
    fi || {
      echo "systemd 启动失败，最近日志：" >&2
      systemctl --user --no-pager --full status fundana-web.service 2>&1 | tail -30 >&2
      return 1
    }
    if wait_ready; then
      echo "已启动：$(base_url)（systemd：fundana-web.service）"
      return 0
    fi
    echo "systemd 已接受启动，但 ${WAIT_SECONDS}s 内健康检查未就绪。" >&2
    systemctl --user --no-pager --full status fundana-web.service 2>&1 | tail -30 >&2
    return 1
  fi
  if healthz "$(base_url)api/v1/healthz"; then
    if adopt_healthy; then
      echo "已在运行：pid $(cat "$PIDFILE") · $(base_url)（已接管未托管进程）"
    else
      echo "已在运行：$(base_url)（健康检查通过；未能识别 pid，不重复启动）"
    fi
    return 0
  fi
  if [ -f "$PIDFILE" ] && ! pid_alive; then rm -f "$PIDFILE"; fi
  if port_busy; then
    echo "端口 $PORT 已被其他进程占用，无法启动。" >&2
    echo "确认占用者：ss -ltnp | grep :$PORT ；或换端口：FUNDANA_PORT=8092 $0 start" >&2
    return 1
  fi
  echo "启动中：$PY -m fundana serve --port $PORT（cwd=$ROOT）"
  cd "$ROOT" || return 1
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') start ===" >>"$LOGFILE"
  if [ -n "$HOST" ]; then
    nohup "$PY" -u -m fundana serve --host "$HOST" --port "$PORT" >>"$LOGFILE" 2>&1 &
  else
    nohup "$PY" -u -m fundana serve --port "$PORT" >>"$LOGFILE" 2>&1 &
  fi
  echo $! >"$PIDFILE"
  if wait_ready; then
    echo "已启动：pid $(cat "$PIDFILE") · $(base_url)（API: /api/v1/healthz）"
    ip=$(lan_ip)
    [ -n "$ip" ] && echo "局域网：http://$ip:$PORT/"
    echo "日志：$LOGFILE"
    return 0
  fi
  echo "启动失败（${WAIT_SECONDS}s 内 /api/v1/healthz 未就绪）。最近日志：" >&2
  tail -20 "$LOGFILE" >&2
  if [ -f "$PIDFILE" ] && ! pid_alive; then rm -f "$PIDFILE"; fi
  return 1
}

cmd_stop() {
  local pid i
  if systemd_unit_active; then
    systemctl --user stop fundana-web.service
    rm -f "$PIDFILE"
    echo "已停止（systemd：fundana-web.service）。"
    return 0
  fi
  if pid_alive; then
    pid=$(cat "$PIDFILE")
  elif healthz "$(base_url)api/v1/healthz"; then
    pid=$(listener_pid)
    if [ -z "${pid:-}" ]; then
      echo "健康检查通过，但无法识别监听 $PORT 的 pid；请手工确认：ss -ltnp | grep :$PORT" >&2
      return 1
    fi
    echo "接管并停止未托管进程：pid $pid"
  else
    rm -f "$PIDFILE"
    echo "未在运行。"
    return 0
  fi
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
      echo "systemd：active（fundana-web.service）"
    else
      echo "systemd：inactive（fundana-web.service）"
    fi
  fi
  if pid_alive; then
    if systemd_unit_active; then
      echo "进程：运行中（pid $(cat "$PIDFILE")，systemd 托管）"
    else
      echo "进程：运行中（pid $(cat "$PIDFILE")）"
    fi
  elif healthz "$(base_url)api/v1/healthz"; then
    if adopt_healthy; then
      if systemd_unit_active; then
        echo "进程：运行中（pid $(cat "$PIDFILE")，systemd 托管）"
      else
        echo "进程：运行中（pid $(cat "$PIDFILE")，已接管未托管进程）"
      fi
    else
      echo "进程：健康检查通过（pid 未知）"
    fi
  else
    echo "进程：未运行"
  fi
  if healthz "$(base_url)api/v1/healthz"; then
    echo "健康检查：OK · $(base_url)api/v1/healthz"
    echo "页面：$(base_url)"
    return 0
  fi
  echo "健康检查：失败（页面打不开）"
  [ -f "$LOGFILE" ] && { echo "最近日志："; tail -10 "$LOGFILE"; }
  return 1
}

cmd_restart() {
  if systemd_unit_exists; then
    echo "由 systemd 管理：重启 fundana-web.service"
    rm -f "$PIDFILE"
    systemctl --user restart fundana-web.service || return 1
    if wait_ready; then
      echo "已重启：$(base_url)"
      return 0
    fi
    echo "重启后 ${WAIT_SECONDS}s 内健康检查未就绪。" >&2
    systemctl --user --no-pager --full status fundana-web.service 2>&1 | tail -30 >&2
    return 1
  fi
  cmd_stop
  cmd_start
}

case "${1:-start}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    tail -n "${2:-50}" "$LOGFILE" ;;
  *) echo "用法：$0 start|stop|restart|status|logs" >&2; exit 2 ;;
esac
