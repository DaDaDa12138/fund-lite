"""发布诊断 CSV 的只读访问：白名单文件名 + 当前 revision 内解析。

诊断文件由 publish/builder.py 写入 `revisions/<id>/diagnostics/*.csv`，是"为什么某只基金
没过"的唯一可追溯证据。这里只读当前发布、只允许 `[a-z0-9_]+\.csv`，不接受路径分隔符。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from ..publish import PublicationError, current_revision

NAME_RE = re.compile(r"^[a-z0-9_]+\.csv$")
DEFAULT_ROWS = 500
MAX_ROWS = 2000


class DiagnosticError(Exception):
    """诊断文件不可用（不存在 / 名称非法 / 无当前发布）。"""


def diagnostics_dir(cfg) -> Path:
    try:
        rev = current_revision(cfg)
    except PublicationError as exc:
        raise DiagnosticError(f"publication_unavailable: {exc}") from exc
    return Path(rev) / "diagnostics"


def list_files(cfg) -> list[dict]:
    base = diagnostics_dir(cfg)
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.glob("*.csv")):
        if not NAME_RE.match(p.name):
            continue
        out.append({"name": p.name, "bytes": p.stat().st_size})
    return out


def load(cfg, name: str, limit: int = DEFAULT_ROWS) -> dict:
    if not NAME_RE.match(name or ""):
        raise DiagnosticError("invalid diagnostic name")
    base = diagnostics_dir(cfg).resolve()
    path = (base / name).resolve()
    if base not in path.parents or not path.is_file():
        raise DiagnosticError(f"diagnostic not found: {name}")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        columns = list(reader.fieldnames or [])
    n = max(1, min(int(limit), MAX_ROWS))
    return {
        "name": name,
        "columns": columns,
        "rows": rows[:n],
        "total": len(rows),
        "truncated": len(rows) > n,
    }
