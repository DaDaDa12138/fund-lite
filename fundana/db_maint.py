"""SQLite 日常维护：checkpoint / optimize / quick_check / vacuum。

为什么单独一层：
- 日更每天新增约 30MB（screen_run_input + screen_decision），WAL 只会在
  自动 checkpoint 后回收；长跑进程退出时若不 checkpoint，-wal 会一直留着。
- VACUUM 会重写整个库并需要约 2 倍磁盘，不能在每日发布路径里自动跑；
  只提供显式 CLI，由运维在低峰执行。
- 所有函数只做 SQLite 自身维护，不删业务数据；删数据的保留策略必须单独评审。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _sidecar(path: Path, suffix: str) -> int:
    p = Path(str(path) + suffix)
    try:
        return p.stat().st_size
    except OSError:
        return 0


def stats(conn, db_path: str | Path) -> dict[str, Any]:
    """页数/空闲页/文件与 WAL 大小；用于判断是否需要 VACUUM。"""
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    path = Path(db_path)
    size = path.stat().st_size if path.exists() else 0
    return {
        "path": str(path),
        "page_count": page_count,
        "page_size": page_size,
        "freelist_count": freelist,
        "free_ratio": round(freelist / page_count, 4) if page_count else 0.0,
        "db_bytes": size,
        "wal_bytes": _sidecar(path, "-wal"),
        "shm_bytes": _sidecar(path, "-shm"),
    }


def optimize(conn) -> dict[str, Any]:
    """让查询规划器统计信息保持新鲜；很轻，可放在每日收尾。"""
    conn.execute("PRAGMA optimize")
    conn.commit()
    return {"ok": True}


def checkpoint(conn, mode: str = "TRUNCATE") -> dict[str, Any]:
    """把 WAL 合并回主库。TRUNCATE 会把 -wal 截到 0；有读者时会 busy。"""
    mode = mode.upper()
    if mode not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
        raise ValueError("unknown checkpoint mode: %s" % mode)
    conn.commit()  # checkpoint 不能在未提交的写事务里执行
    row = conn.execute("PRAGMA wal_checkpoint(%s)" % mode).fetchone()
    # 返回 (busy, log, checkpointed)
    return {"ok": not bool(row[0]), "busy": int(row[0]),
            "log_pages": int(row[1]), "checkpointed_pages": int(row[2]),
            "mode": mode}


def quick_check(conn) -> dict[str, Any]:
    rows = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
    return {"ok": rows == ["ok"], "result": rows[:20]}


def vacuum(conn) -> dict[str, Any]:
    """重写主库回收空闲页。必须在无事务状态执行，且需要约 2 倍磁盘。"""
    conn.commit()
    conn.execute("VACUUM")
    conn.commit()
    return {"ok": True}


def run(conn, db_path: str | Path, *, do_checkpoint: bool = True,
        do_vacuum: bool = False, do_integrity: bool = False,
        checkpoint_mode: str = "TRUNCATE") -> dict[str, Any]:
    """执行一次维护并返回维护前后的可核对数字。"""
    before = stats(conn, db_path)
    out: dict[str, Any] = {"before": before}
    if do_integrity:
        out["quick_check"] = quick_check(conn)
    out["optimize"] = optimize(conn)
    if do_checkpoint:
        out["checkpoint"] = checkpoint(conn, checkpoint_mode)
    if do_vacuum:
        out["vacuum"] = vacuum(conn)
    out["after"] = stats(conn, db_path)
    out["reclaimed_bytes"] = max(0, before["db_bytes"] - out["after"]["db_bytes"])
    return out
