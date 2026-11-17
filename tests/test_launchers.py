"""启动器与自启安装器的回归测试。

前端“打不开”的根因通常不在页面代码，而在服务没有常驻/没有自启。
这些测试不启动真实服务，只锁定启动器的关键契约：
- shell 语法可执行；
- pidfile 丢失时能通过监听端口接管；
- systemd unit 存在时 start/stop/restart 委托给 systemctl；
- 安装器写的是 Type=simple 的 unit，而不是会递归调用脚本的 Type=forking。
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


class LauncherSyntaxTests(unittest.TestCase):
    def test_shell_syntax(self):
        for name in ("serve.sh", "scheduler.sh", "install-autostart.sh", "daemon.sh"):
            with self.subTest(script=name):
                proc = subprocess.run(
                    ["bash", "-n", str(SCRIPTS / name)],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_launchers_are_executable(self):
        for name in ("serve.sh", "scheduler.sh", "install-autostart.sh"):
            with self.subTest(script=name):
                self.assertTrue(os.access(SCRIPTS / name, os.X_OK), name)


class ServeLauncherTests(unittest.TestCase):
    def test_adopts_healthy_listener_when_pidfile_is_stale(self):
        text = read("serve.sh")
        self.assertIn("listener_pid()", text)
        self.assertIn("adopt_healthy()", text)
        self.assertIn("ss -ltnp", text)
        # 防 PID 复用：不能只看 kill -0。
        self.assertIn("ps -p \"$pid\" -o args=", text)
        self.assertIn("fundana.*serve", text)

    def test_delegates_to_systemd_when_unit_exists(self):
        text = read("serve.sh")
        self.assertIn("systemd_unit_exists()", text)
        self.assertIn("systemd_unit_active()", text)
        self.assertIn("systemctl --user stop fundana-web.service", text)
        self.assertIn("systemctl --user start fundana-web.service", text)
        self.assertIn("systemctl --user restart fundana-web.service", text)

    def test_status_reports_systemd_ownership(self):
        text = read("serve.sh")
        self.assertIn("systemd 托管", text)
        self.assertIn("systemd：active", text)


class SchedulerLauncherTests(unittest.TestCase):
    def test_delegates_to_systemd_when_unit_exists(self):
        text = read("scheduler.sh")
        self.assertIn("systemd_unit_exists()", text)
        self.assertIn("systemctl --user stop fundana-scheduler.service", text)
        self.assertIn("systemctl --user start fundana-scheduler.service", text)
        self.assertIn("systemctl --user restart fundana-scheduler.service", text)

    def test_heartbeat_can_prove_process_is_alive(self):
        text = read("scheduler.sh")
        self.assertIn("心跳新鲜；pidfile 未记录", text)


class AutostartInstallerTests(unittest.TestCase):
    def test_units_are_simple_and_do_not_call_scripts(self):
        text = read("install-autostart.sh")
        self.assertIn("Type=simple", text)
        self.assertNotIn("Type=forking", text)
        self.assertIn("ExecStart=$PY -u -m fundana serve --port 8091", text)
        self.assertIn("ExecStart=$PY -u -m fundana scheduler", text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("systemctl --user enable --now", text)
        # 重新安装时要让已运行的 unit 重载新文件。
        self.assertIn("systemctl --user restart fundana-web.service fundana-scheduler.service", text)

    def test_desktop_fallback_and_linger(self):
        text = read("install-autostart.sh")
        self.assertIn("/autostart", text)
        self.assertIn("fundana.desktop", text)
        self.assertIn("loginctl enable-linger", text)
        self.assertIn("--linger", text)


if __name__ == "__main__":
    unittest.main()
