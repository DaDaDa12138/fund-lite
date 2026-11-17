"""SQLite 连接、迁移、run/fetch_log/source_state 记账。"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CN = ZoneInfo("Asia/Shanghai")
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def now_iso() -> str:
    return datetime.now(CN).isoformat(timespec="seconds")


def today_cn() -> str:
    return datetime.now(CN).date().isoformat()


def parse_ts(value: str | None) -> datetime | None:
    """把库内时间戳解析成上海时区的 aware datetime。

    历史数据/测试夹具里可能有没有偏移量的裸时间（例如 2026-11-01T00:00:00）。
    本项目全程按 Asia/Shanghai 解释，绝不能把裸时间当 UTC 或本机时区；
    带偏移量的时间统一换算到 +08:00 再比较。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN)
    return dt.astimezone(CN)


def connect(cfg) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path, timeout=cfg.app["db"].get("busy_timeout_ms", 15000) / 1000)
    conn.row_factory = sqlite3.Row
    if cfg.app["db"].get("wal", True):
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(cfg.app['db'].get('busy_timeout_ms', 15000))}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, sha256 TEXT NOT NULL)"
    )
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migration")}
    done: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migration(version, applied_at, sha256) VALUES(?,?,?)",
            (path.name, now_iso(), hashlib.sha256(sql.encode()).hexdigest()),
        )
        conn.commit()
        done.append(path.name)
    return done


# ── run 生命周期 ──────────────────────────────────────────────
def start_run(conn: sqlite3.Connection, mode: str, config_hash: str, code_version: str, notes: str | None = None) -> str:
    run_id = f"{datetime.now(CN).strftime('%Y%m%dT%H%M%S')}-{mode}-{uuid.uuid4().hex[:6]}"
    conn.execute(
        "INSERT INTO run(run_id, mode, started_at, status, config_hash, code_version, notes) VALUES(?,?,?,?,?,?,?)",
        (run_id, mode, now_iso(), "running", config_hash, code_version, notes),
    )
    conn.commit()
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: str, status: str, notes: str | None = None) -> None:
    conn.execute(
        "UPDATE run SET finished_at=?, status=?, notes=COALESCE(?, notes) WHERE run_id=?",
        (now_iso(), status, notes, run_id),
    )
    conn.commit()


class RunStages(list):
    """按顺序记录 run 的阶段，并逐条落库。

    过去 daily 的 stages 只活在内存里：进程被 OOM/重启杀掉后，run 表只剩
    running，journal 也拿不到结构化阶段。这里保持 list 接口，append 时写
    run_stage 并提交，调用方无需改动。
    """

    def __init__(self, conn: sqlite3.Connection, run_id: str):
        super().__init__()
        self._conn = conn
        self._run_id = run_id
        self._seq = 0

    def append(self, item: dict) -> None:  # type: ignore[override]
        super().append(item)
        self._seq += 1
        ok = item.get("ok")
        status = "ok" if ok is True else ("failed" if ok is False else "info")
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO run_stage(run_id, seq, stage, status, recorded_at, detail_json)"
                " VALUES(?,?,?,?,?,?)",
                (self._run_id, self._seq, str(item.get("stage") or "unknown"), status,
                 now_iso(), json.dumps(item, ensure_ascii=False, default=str, separators=(",", ":"))),
            )
            self._conn.commit()
        except sqlite3.Error:
            # 观测信息不能反过来把日更弄挂；内存里的 stages 仍然返回给调用方。
            pass


# ── L0 原始对象 ───────────────────────────────────────────────
def put_raw(conn: sqlite3.Connection, cfg, body: bytes, url: str, http_status: int | None,
            content_type: str | None, encoding: str) -> str:
    sha = hashlib.sha256(body).hexdigest()
    exists = conn.execute("SELECT 1 FROM raw_object WHERE sha256=?", (sha,)).fetchone()
    if not exists:
        blob = gzip.compress(body, compresslevel=6)
        conn.execute(
            "INSERT INTO raw_object(sha256, bytes_gz, raw_size, content_type, encoding, url, http_status, fetched_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (sha, blob, len(body), content_type, encoding, url, http_status, now_iso()),
        )
        target = cfg.raw_dir / sha[:2] / f"{sha}.gz"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(blob)
    return sha


def get_raw(conn: sqlite3.Connection, sha: str) -> bytes:
    row = conn.execute("SELECT bytes_gz FROM raw_object WHERE sha256=?", (sha,)).fetchone()
    if row is None:
        raise KeyError(f"raw_object 不存在: {sha}")
    return gzip.decompress(row["bytes_gz"])


def log_fetch(conn: sqlite3.Connection, run_id: str, source_id: str, key: str | None, url: str,
              started_at: str, duration_ms: int, http_status: int | None, business_ok: bool,
              status: str, error_msg: str | None, raw_sha: str | None, attempt: int = 1) -> None:
    conn.execute(
        "INSERT INTO fetch_log(run_id, source_id, key, url, started_at, duration_ms, http_status,"
        " business_ok, status, error_msg, raw_sha256, attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, source_id, key, url, started_at, duration_ms, http_status, 1 if business_ok else 0,
         status, error_msg, raw_sha, attempt),
    )


def get_source_state(conn: sqlite3.Connection, source_id: str, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM source_state WHERE source_id=? AND key=?", (source_id, key)).fetchone()


def update_source_state(conn: sqlite3.Connection, source_id: str, key: str, status: str,
                        raw_sha: str | None, error: str | None, success: bool,
                        cooldown_until: str | None = None) -> None:
    prev = get_source_state(conn, source_id, key)
    fails = 0 if success else (prev["consecutive_failures"] if prev else 0) + 1
    conn.execute(
        "INSERT INTO source_state(source_id, key, last_attempt_at, last_success_at, last_status,"
        " consecutive_failures, cooldown_until, last_raw_sha256, last_error)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(source_id, key) DO UPDATE SET"
        " last_attempt_at=excluded.last_attempt_at,"
        " last_success_at=COALESCE(excluded.last_success_at, source_state.last_success_at),"
        " last_status=excluded.last_status,"
        " consecutive_failures=excluded.consecutive_failures,"
        " cooldown_until=excluded.cooldown_until,"
        " last_raw_sha256=COALESCE(excluded.last_raw_sha256, source_state.last_raw_sha256),"
        " last_error=excluded.last_error",
        (source_id, key, now_iso(), now_iso() if success else None, status, fails,
         cooldown_until, raw_sha, error),
    )


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    out: dict[str, int] = {}
    for t in tables:
        out[t] = conn.execute(f'SELECT COUNT(*) AS c FROM "{t}"').fetchone()["c"]
    return out
