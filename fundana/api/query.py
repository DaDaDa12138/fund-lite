"""只读 SQL 查询闸门：单语句 SELECT/WITH、禁写操作、强制行数上限、超时中断。

设计原则（与 docs/decisions.md ADR-016 一致）：
1. 连接用 URI `file:...?mode=ro` 打开，即使闸门被绕过也无法落盘写入；
2. 语法层只放行以 SELECT / WITH 开头的单条语句，出现分号即拒绝（防多语句注入）；
3. 关键字黑名单覆盖 DDL/DML/PRAGMA/ATTACH/扩展加载；
4. 结果集按 limit+1 取，多出的那条用于标记 truncated，绝不无界返回；
5. set_progress_handler 每 10k 条 VM 指令检查一次墙钟，超过 timeout_s 主动 abort。
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

MAX_LIMIT = 1000
DEFAULT_LIMIT = 200

# 整词匹配；顺序无关。sqlite_master / sqlite_schema 允许读（只读元数据）。
FORBIDDEN_WORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "attach", "detach", "pragma", "vacuum", "reindex", "analyze",
    "begin", "commit", "rollback", "savepoint", "release",
    "load_extension", "writefile", "readfile", "fts3_tokenizer",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_WORDS) + r")\b", re.IGNORECASE)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


class QueryRejected(ValueError):
    """SQL 未通过闸门。message 直接展示给前端。"""


def sanitize(sql: str) -> str:
    """去注释、去尾分号、压空白；保留原始大小写供展示。"""
    if not isinstance(sql, str):
        raise QueryRejected("SQL 必须是字符串")
    text = _BLOCK_COMMENT.sub(" ", sql)
    text = _LINE_COMMENT.sub(" ", text)
    text = text.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    return text


def check(sql: str) -> str:
    """校验并返回可执行 SQL；不合法抛 QueryRejected。"""
    text = sanitize(sql)
    if not text:
        raise QueryRejected("SQL 为空")
    if ";" in text:
        raise QueryRejected("只允许单条语句（检测到分号）")
    head = text.lstrip("( \n\t").lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise QueryRejected("只允许 SELECT 或 WITH 查询")
    hit = _FORBIDDEN_RE.search(text)
    if hit:
        raise QueryRejected(f"包含被禁止的关键字：{hit.group(1).upper()}")
    return text


def clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def run(db_path, sql: str, limit: Any = DEFAULT_LIMIT, timeout_s: float = 3.0) -> dict:
    """执行只读查询，返回 {columns, rows, row_count, truncated, elapsed_ms, sql}。"""
    text = check(sql)
    n = clamp_limit(limit)
    started = time.monotonic()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=1")

        def _guard() -> int:
            return 1 if (time.monotonic() - started) > timeout_s else 0

        conn.set_progress_handler(_guard, 10_000)
        cur = conn.execute(text)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(n + 1)
        truncated = len(rows) > n
        rows = rows[:n]
        elapsed = round((time.monotonic() - started) * 1000, 1)
        return {
            "columns": columns,
            "rows": [[_jsonable(v) for v in tuple(r)] for r in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": elapsed,
            "sql": text,
            "limit": n,
        }
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "interrupted" in msg.lower():
            raise QueryRejected(f"查询超过 {timeout_s:g} 秒被中断，请加 WHERE 或 LIMIT") from exc
        raise QueryRejected(msg) from exc
    finally:
        conn.close()


def _jsonable(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<blob {len(bytes(value))}B>"
    return value
