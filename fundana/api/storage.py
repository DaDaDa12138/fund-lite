"""项目信息：数据库文件体量与整个项目目录的硬盘占用。

按使用者视角只回答两个问题：数据库多大、这个项目一共占了多少硬盘。
表级占用（dbstat）与逐表行数对使用没有帮助，反而要扫一遍全库，已移除；
需要逐表行数时用 `fundana stats` 或 SQL 控制台。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path


def _dir_size(path: Path) -> tuple[int, int]:
    total = files = 0
    if not path.exists():
        return 0, 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                continue
    return total, files


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def storage(cfg) -> dict:
    """数据库文件 + 项目目录 + 所在磁盘的体量（不含表级明细）。"""
    db_path = Path(cfg.db_path)
    wal, shm = Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")
    raw_bytes, raw_files = _dir_size(Path(cfg.raw_dir))
    freeze_bytes, freeze_files = _dir_size(Path(cfg.freeze_dir))
    report_bytes, report_files = _dir_size(Path(cfg.report_root))
    project_root = Path(getattr(cfg, "root", None) or db_path.parent)
    project_bytes, project_files = _dir_size(project_root)

    try:
        usage = shutil.disk_usage(project_root)
        disk_total, disk_free = usage.total, usage.free
    except OSError:
        disk_total = disk_free = 0

    db_bytes = _file_size(db_path)
    return {
        "db": {
            "path": str(db_path),
            "bytes": db_bytes,
            "wal_bytes": _file_size(wal),
            "shm_bytes": _file_size(shm),
        },
        "disk": {
            "project_dir": str(project_root),
            "project_bytes": project_bytes,
            "project_files": project_files,
            "db_bytes": db_bytes,
            "raw_bytes": raw_bytes,
            "raw_files": raw_files,
            "reports_bytes": report_bytes,
            "reports_files": report_files,
            "freeze_bytes": freeze_bytes,
            "freeze_files": freeze_files,
            "host_total_bytes": disk_total,
            "host_free_bytes": disk_free,
        },
        "total_bytes": project_bytes,
    }


def schema(cfg) -> list[dict]:
    """表名 + 列定义，供查询控制台自动补全与浏览。"""
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        out = []
        for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            cols = [dict(c) for c in conn.execute(f'PRAGMA table_info("{row["name"]}")')]
            out.append({"table": row["name"],
                        "columns": [{"name": c["name"], "type": c["type"], "pk": bool(c["pk"])}
                                    for c in cols]})
        return out
    finally:
        conn.close()
