#!/usr/bin/env bash
# Fund-ANA 登录自启安装器（systemd --user，失败时回退到桌面 autostart）。
#
# 背景：2026-11-02 22:39 主机重启后，web 与 scheduler 都没有再起来；
# serve.log 最后一条是重启前的 21:04，浏览器因此只看到“无法访问此网站”。
# 本项目刻意不要求 root，所以用用户级 systemd（或桌面 autostart）做自启。
#
# 用法：
#   scripts/install-autostart.sh install [--linger]
#   scripts/install-autostart.sh uninstall
#   scripts/install-autostart.sh status
#
# --linger：执行 loginctl enable-linger "$USER"，让服务在开机后、用户登录前也启动。
#           该操作可能触发 polkit 授权；失败不影响登录后自启。
set -u
export TZ=Asia/Shanghai

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
WEB_UNIT="$UNIT_DIR/fundana-web.service"
SCHED_UNIT="$UNIT_DIR/fundana-scheduler.service"
DESKTOP="$AUTOSTART_DIR/fundana.desktop"
PY=$(command -v python3 || echo /usr/bin/python3)
PATH_ENV="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

have_user_systemd() {
  systemctl --user show-environment >/dev/null 2>&1
}

write_units() {
  mkdir -p "$UNIT_DIR"
  cat >"$WEB_UNIT" <<EOF
[Unit]
Description=Fund-ANA web (localhost:8091)
Documentation=file://$ROOT/docs/web.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=TZ=Asia/Shanghai
Environment=PATH=$PATH_ENV
ExecStart=$PY -u -m fundana serve --port 8091
Restart=on-failure
RestartSec=5
TimeoutStartSec=30
TimeoutStopSec=15

[Install]
WantedBy=default.target
EOF
  cat >"$SCHED_UNIT" <<EOF
[Unit]
Description=Fund-ANA daily scheduler
Documentation=file://$ROOT/docs/runbook.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=TZ=Asia/Shanghai
Environment=PATH=$PATH_ENV
ExecStart=$PY -u -m fundana scheduler
Restart=on-failure
RestartSec=5
TimeoutStartSec=30
TimeoutStopSec=15

[Install]
WantedBy=default.target
EOF
  chmod 0644 "$WEB_UNIT" "$SCHED_UNIT"
}

write_desktop() {
  mkdir -p "$AUTOSTART_DIR"
  cat >"$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Fund-ANA
Comment=Fund-ANA web and daily scheduler
Exec=$ROOT/scripts/daemon.sh start
Terminal=false
StartupNotify=false
X-GNOME-Autostart-Delay=5
EOF
  chmod 0644 "$DESKTOP"
}

install_units() {
  if ! have_user_systemd; then
    echo "systemd --user 不可用，改用桌面 autostart（登录桌面后启动）。"
    write_desktop
    echo "已写入：$DESKTOP"
    return 0
  fi
  write_units
  systemctl --user daemon-reload
  if systemctl --user enable --now fundana-web.service fundana-scheduler.service; then
    # enable --now 不会重载已在运行的 unit；显式 restart 让新的 unit 文件生效。
    if systemctl --user is-active --quiet fundana-web.service; then
      systemctl --user restart fundana-web.service fundana-scheduler.service || true
    fi
    echo "已安装并启动："
    echo "  $WEB_UNIT"
    echo "  $SCHED_UNIT"
    echo "查看状态：scripts/install-autostart.sh status"
    if [ "${2:-}" = "--linger" ]; then
      if loginctl enable-linger "$USER" 2>/dev/null; then
        echo "已开启 linger：开机后无需登录也会启动。"
      else
        echo "未能开启 linger（可能需要 polkit 授权）；服务仍会在登录后启动。" >&2
      fi
    else
      echo "提示：当前未开启 linger，服务在用户登录后启动；如需开机即启动："
      echo "      loginctl enable-linger $USER"
    fi
    return 0
  fi
  echo "systemd 启动失败，最近日志：" >&2
  systemctl --user --no-pager status fundana-web.service fundana-scheduler.service 2>&1 | tail -40 >&2
  return 1
}

uninstall_units() {
  if have_user_systemd; then
    systemctl --user disable --now fundana-web.service fundana-scheduler.service 2>/dev/null || true
    rm -f "$WEB_UNIT" "$SCHED_UNIT"
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  rm -f "$DESKTOP"
  echo "已移除 Fund-ANA 自启项。"
}

status_units() {
  echo "== systemd --user =="
  if have_user_systemd; then
    systemctl --user --no-pager --full status fundana-web.service fundana-scheduler.service 2>&1 | sed -n '1,80p'
  else
    echo "不可用"
  fi
  echo "== 进程 =="
  "$ROOT/scripts/serve.sh" status || true
  "$ROOT/scripts/scheduler.sh" status || true
  if [ -f "$DESKTOP" ]; then echo "== 桌面自启 == "; ls -l "$DESKTOP"; fi
}

case "${1:-status}" in
  install)   install_units "$@" ;;
  uninstall) uninstall_units ;;
  status)    status_units ;;
  *) echo "用法：$0 install [--linger] | uninstall | status" >&2; exit 2 ;;
esac
