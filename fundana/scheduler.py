"""调度器：工作日 07:30 跑一次日更（单进程单例，心跳可观测）。

为什么不常驻 cron：日更包含采集/指标/筛选/发布四个阶段，需要"一次只跑一个采集任务"
（SQLite 单写者）。这里用文件锁 + 心跳，避免两个进程同时写库。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import cn_calendar, config, db, notify

CN = db.CN
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _state_dir(cfg) -> Path:
    path = Path(cfg.get("paths", "state_dir", default="var/state"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lock(cfg):
    path = _state_dir(cfg) / "scheduler.lock"
    if path.exists():
        pid = path.read_text(encoding="utf-8").strip()
        if pid.isdigit() and Path(f"/proc/{pid}").exists():
            return None
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


def _sd_notify(state: str) -> None:
    """systemd Type=notify/Watchdog 的最小实现；手工运行时静默跳过。"""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode("utf-8"), addr)
    except OSError:
        pass


def _watchdog_loop(stop: threading.Event) -> None:
    while not stop.wait(30):
        _sd_notify("WATCHDOG=1")


def heartbeat(cfg, payload: dict) -> None:
    path = _state_dir(cfg) / "scheduler_heartbeat.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    # P1-10：保留历史，才能回答“过去 30 天失败几次”；按大小轮转，避免无限增长。
    hist = _state_dir(cfg) / "scheduler_heartbeat.jsonl"
    line = json.dumps({"at": db.now_iso(), **payload}, ensure_ascii=False)
    try:
        with hist.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if hist.stat().st_size > 2_000_000:
            os.replace(hist, _state_dir(cfg) / "scheduler_heartbeat.1.jsonl")
    except OSError:
        pass


def read_heartbeat_history(cfg, limit: int = 50) -> list[dict]:
    hist = _state_dir(cfg) / "scheduler_heartbeat.jsonl"
    try:
        lines = hist.read_text(encoding="utf-8").splitlines()[-max(1, limit):]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def read_heartbeat(cfg) -> dict:
    path = _state_dir(cfg) / "scheduler_heartbeat.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def next_run_at(now: datetime, at: str = "07:30") -> datetime:
    hour, minute = (int(x) for x in at.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # 恰好 07:30 时应当今天跑，不能因为 <= 直接跳到明天。
    if target < now:
        target += timedelta(days=1)
    # 周末与法定节假日都不跑：基金不披露净值，空跑只会把当天标记成
    # controlled_skip，掩盖真正的数据延迟。
    while not cn_calendar.is_trading_day(target.date()):
        target += timedelta(days=1)
    return target


def needs_catchup(conn, now: datetime, at: str = "07:30") -> bool:
    """工作日已过执行时刻、但今天还没有成功/跳过的日更时，补跑一次。

    2026-11-02 22:39 主机重启，11-03 07:30 的日更没有执行；旧 loop 启动时
    只算下一个 07:30，直接跳到 11-04，导致榜单停在 10-30。这里用 run 表
    判断“今天是否已经跑过”，避免重复补跑，也避免 failed 后永不重试。
    """
    if not cn_calendar.is_trading_day(now.date()):
        return False
    hour, minute = (int(x) for x in at.split(":"))
    if now < now.replace(hour=hour, minute=minute, second=0, microsecond=0):
        return False
    row = conn.execute(
        "SELECT status FROM run WHERE mode='daily' AND substr(started_at,1,10)=?"
        " ORDER BY started_at DESC LIMIT 1", (now.date().isoformat(),)).fetchone()
    return not row or (row[0] not in ("success", "skipped"))


def daily_command(force: bool = False) -> list[str]:
    """调度器不直接 import daily.run，避免长驻进程把旧代码/旧配置带过夜。"""
    cmd = [sys.executable, "-m", "fundana", "daily", "--quiet"]
    if force:
        cmd.append("--force")
    return cmd


def _run_daily_subprocess(force: bool = False, cfg=None) -> dict:
    """在独立进程中跑日更；新进程会重新加载 config、迁移和当前公式契约。

    2026-11-04 的真实事故：scheduler 21:08 启动后一直用进程内旧模块，07:30
    仍按 v10/formula 11.4 跑，发布出 11-03 的旧契约榜单。子进程是这里的
    隔离边界：即使 scheduler 自己长驻，也不会把旧代码带进下一次日更。
    """
    try:
        proc = subprocess.run(
            daily_command(force=force),
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=7200,
        )
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "daily_timeout_7200s"}
    out: dict = {}
    stdout = (proc.stdout or "").strip()
    if stdout:
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                out = parsed
        except json.JSONDecodeError:
            # 兼容 daily.run 的进度输出：取最后一行 JSON。
            for line in reversed(stdout.splitlines()):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    out = parsed
                    break
    if not out:
        out = {"status": "ok" if proc.returncode == 0 else "failed"}
    out.setdefault("status", "ok" if proc.returncode == 0 else "failed")
    out["exit_code"] = proc.returncode
    if proc.returncode != 0 and out.get("status") == "ok":
        out["status"] = "failed"
    if proc.stderr:
        out["stderr_tail"] = proc.stderr[-1000:]
    return out


def _alert_failure(cfg, out: dict) -> None:
    # cfg=None 只出现在单测/手工调用：不能偷偷加载生产配置写生产告警。
    # 2026-11-10 的事故就是 tests/test_scheduler.py 直接调用失败分支，
    # 把一条假告警写进了生产 alerts。
    if cfg is None:
        return
    try:
        notify.send(cfg, "Fund-ANA 日更失败", str(out.get("error") or out.get("status")),
                    unit="fundana-scheduler.service",
                    detail={k: out.get(k) for k in ("status", "error", "exit_code", "stderr_tail")})
    except Exception:
        pass


def _log_result(out: dict) -> None:
    """把子进程结果压成一行写 stdout；systemd 会收进 journal。

    daily --quiet 只输出最终 JSON，调度器不解析就等于 journal 里只有
    “下一次日更”。这里只记状态、阶段、run_id、generation 与错误，避免把
    整份 stages 重复打一遍。
    """
    keys = ("status", "stage", "run_id", "generation", "attempts",
            "retry_stopped", "error", "detail", "exit_code")
    payload = {k: out.get(k) for k in keys if out.get(k) is not None}
    try:
        print("[scheduler] " + json.dumps(payload, ensure_ascii=False, default=str),
              flush=True)
    except (TypeError, ValueError):
        print("[scheduler] status=%s stage=%s" % (out.get("status"), out.get("stage")),
              flush=True)


def _run_with_retries(cfg, *, force: bool = False, sleep=time.sleep) -> dict:
    """日更失败时有限重试；publish 阶段失败不重试。

    重试针对的是网络抖动/数据源短暂不可用；publish 已经可能写了半套产物，
    再跑一次会把 generation 推高，留给人工判断更安全。只在最终失败时告警，
    避免三次尝试发三条通知。
    """
    attempts = max(1, int(cfg.get("scheduler", "max_attempts", default=3)))
    delay = max(0, int(cfg.get("scheduler", "retry_delay_s", default=900)))
    out: dict = {}
    for attempt in range(1, attempts + 1):
        out = _run_daily_subprocess(force=force, cfg=cfg)
        out["attempts"] = attempt
        if out.get("status") != "failed":
            _log_result(out)
            return out
        if out.get("stage") == "publish":
            out["retry_stopped"] = "publish_stage"
            _log_result(out)
            break
        _log_result(out)
        if attempt < attempts and delay:
            print("[scheduler] %ds 后重试（第 %d/%d 次）"
                  % (delay, attempt + 1, attempts), flush=True)
            sleep(delay)
    _alert_failure(cfg, out)
    return out


def _catchup_if_needed(cfg, at: str) -> dict | None:
    conn = db.connect(cfg)
    try:
        if not needs_catchup(conn, datetime.now(CN), at):
            return None
    finally:
        conn.close()
    print("检测到今天还没有成功日更，立即补跑一次", flush=True)
    return _run_with_retries(cfg)


def run_once(at: str = "07:30", force: bool = False) -> dict:
    cfg = config.load_config()
    out = _run_with_retries(cfg, force=force)
    heartbeat(cfg, {"last_run_at": db.now_iso(), "last_status": out.get("status"),
                    "next_run_at": next_run_at(datetime.now(CN), at).isoformat()})
    return out


def loop(at: str = "07:30") -> int:
    cfg = config.load_config()
    lock = _lock(cfg)
    if lock is None:
        print("已有调度器在运行（var/state/scheduler.lock），本次退出")
        return 3
    prev = read_heartbeat(cfg)
    last_info: dict = {k: prev[k] for k in ("last_run_at", "last_status") if prev.get(k)}
    _sd_notify("READY=1")
    stop_wd = threading.Event()
    wd = threading.Thread(target=_watchdog_loop, args=(stop_wd,), daemon=True)
    wd.start()
    try:
        while True:
            now = datetime.now(CN)
            catch = _catchup_if_needed(cfg, at)
            if catch is not None:
                last_info = {"last_run_at": db.now_iso(), "last_status": catch.get("status"),
                             "catchup": True}
                heartbeat(cfg, {"pid": os.getpid(), **last_info, "at": at})
            now = datetime.now(CN)
            target = next_run_at(now, at)
            heartbeat(cfg, {"pid": os.getpid(), "started_at": now.isoformat(),
                            "next_run_at": target.isoformat(), "at": at, **last_info})
            print(f"下一次日更：{target.isoformat()}", flush=True)
            while datetime.now(CN) < target:
                heartbeat(cfg, {"pid": os.getpid(), "next_run_at": target.isoformat(),
                                "at": at, "sleeping_since": datetime.now(CN).isoformat(),
                                **last_info})
                time.sleep(min(60, max(1, (target - datetime.now(CN)).total_seconds())))
            out = _run_with_retries(cfg)
            last_info = {"last_run_at": db.now_iso(), "last_status": out.get("status")}
            heartbeat(cfg, {"pid": os.getpid(), **last_info,
                            "next_run_at": next_run_at(datetime.now(CN), at).isoformat()})
    finally:
        stop_wd.set()
        wd.join(timeout=2)
        lock.unlink(missing_ok=True)
    return 0
