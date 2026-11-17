#!/usr/bin/env python3
"""状态包：把 SQLite 状态 + 发布指针 + 最近若干期 revision 打成可分片的 tar.zst。

为什么需要这一层
----------------
GitHub 托管 runner 每天销毁，`var/db/fundana.db`（约 9 GB）必须每天搬运一次。
Release 单资产上限 2 GiB，因此切成 <=1.9 GiB 的分片；`manifest.json` 最后上传并覆盖，
实现「先传新片 -> 原子切指针 -> 删旧片」：上传中断时旧指针仍指向一套完整分片。

子命令
------
    slim    裁掉对下一次发布没有作用的行（只用于导出副本，不碰线上库）
    export  打包 + zstd + 分片 + 写 manifest.json
    import  校验 sha256 + 流式解压回工作目录
    inspect 打印 manifest 摘要

设计约束
--------
* 只用标准库 + `zstd` 命令行；不引入第三方包。
* provenance 保留集直接复用 `fundana.data_health.PROVENANCE_TABLES`，避免两处口径漂移。
* 打包/解压全程流式，峰值磁盘 = 剩余分片 + 解压出的库，不是两倍。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT_DEFAULT = Path(__file__).resolve().parent.parent
if str(ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(ROOT_DEFAULT))

PART_SIZE_DEFAULT = 1_900 * 1024 * 1024  # < 2 GiB 的 Release 单资产上限
CHUNK = 8 * 1024 * 1024
CN = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------- 工具

def _now_cn() -> datetime:
    return datetime.now(CN)


def _nav_as_of_from_db(db_path: Path, publication_id: str | None) -> str | None:
    """publication_state.json 里没有 nav_as_of，只能按 publication_id 回查 publication 表。"""
    if not publication_id:
        return None
    # 不要用 mode=ro：WAL 库在上一个连接干净关闭后 -wal/-shm 已被删，只读打开会在
    # 首次查询时失败（实测 CI 上 nav_as_of 因此静默变 null）。这里只做一次 SELECT，
    # 用普通连接最稳，代价是可能重建空的 -wal/-shm，关闭即清。
    try:
        conn = sqlite3.connect(str(db_path), timeout=60)
        try:
            row = conn.execute("SELECT nav_as_of FROM publication WHERE publication_id = ?",
                               (publication_id,)).fetchone()
            return str(row[0]) if row and row[0] else None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print("warning: nav_as_of lookup failed: %s" % exc, file=sys.stderr)
        return None


def _db_stats(conn: sqlite3.Connection, db_path: Path) -> dict[str, Any]:
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    return {
        "db_bytes": page_count * page_size,
        "freelist_count": int(conn.execute("PRAGMA freelist_count").fetchone()[0]),
        "db_file_bytes": db_path.stat().st_size if db_path.exists() else 0,
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _provenance_tables() -> tuple[tuple[str, str | None, str | None], ...]:
    from fundana.data_health import PROVENANCE_TABLES
    return tuple(PROVENANCE_TABLES)


def _part_suffix(i: int) -> str:
    """0 -> aa, 1 -> ab, 26 -> ba ... 分片数极少，两位字母足够。"""
    first = chr(ord("a") + (i // 26) % 26)
    second = chr(ord("a") + i % 26)
    return first + second


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- slim

# 状态包会作为 Release 资产公开发布，导出前必须清掉个人使用痕迹。
# fund_watchlist 是网页「自选」表（fcode + 手写备注），不属于项目数据。
PERSONAL_TABLES = ("fund_watchlist",)


def redact(db_path: Path, tables: Iterable[str] = PERSONAL_TABLES) -> dict[str, int]:
    """清空个人痕迹表，返回 {表名: 删除行数}。

    只删行不删表：schema 必须与导入方一致，迁移才能照常跑。
    删完由 slim 的 VACUUM（或导出的 checkpoint）回收空闲页，避免残留字节被人 strings 出来。
    """
    conn = sqlite3.connect(str(db_path), timeout=120)
    try:
        removed: dict[str, int] = {}
        for table in tables:
            if not _table_exists(conn, table):
                continue
            n = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            if n:
                conn.execute("DELETE FROM %s" % table)
            removed[table] = int(n)
        conn.commit()
        return removed
    finally:
        conn.close()


def slim(
    db_path: Path,
    keep_publications: int = 20,
    keep_raw_days: int = 120,
    vacuum: bool = False,
    keep_runs: int = 5,
) -> dict[str, Any]:
    """裁掉旧 screen 明细与没有被 provenance 引用/超窗的 raw_object。

    安全性依据：
    * `fundana/tracker.py` 的 _rebuild 只重放自己的 screening_tracker_event，不读 screen_decision；
    * 发布门禁的 provenance 只核对 `nav_as_of - 90d` 窗口，窗口外的事实不再被复核，
      因此窗口外没人引用的 raw 可以直接删；
    * src_zqcc_holding / src_manager_history 没有 business_date 列，门禁对它们全表核对，
      所以这两张表的 raw_sha256 必须整表保留。
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path), timeout=120)
    conn.isolation_level = None
    out: dict[str, Any] = {"db_path": str(db_path)}
    try:
        before = _db_stats(conn, db_path)
        out["before"] = before
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        row = conn.execute("SELECT MAX(nav_as_of) FROM publication").fetchone()
        as_of_str = (row[0] if row else None) or date.today().isoformat()
        since = (date.fromisoformat(as_of_str) - timedelta(days=keep_raw_days)).isoformat()
        out["as_of"] = as_of_str
        out["raw_since"] = since

        conn.execute("BEGIN IMMEDIATE")
        # 1) screen 明细：最近 N 期 publication + 最近 M 次 run
        conn.execute("CREATE TEMP TABLE keep_run(run_id TEXT PRIMARY KEY)")
        conn.execute(
            "INSERT OR IGNORE INTO keep_run "
            "SELECT run_id FROM publication ORDER BY generation DESC LIMIT ?",
            (keep_publications,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO keep_run "
            "SELECT run_id FROM screen_run ORDER BY created_at DESC LIMIT ?",
            (keep_runs,),
        )
        keep_run_n = int(conn.execute("SELECT COUNT(*) FROM keep_run").fetchone()[0])
        cur = conn.execute(
            "DELETE FROM screen_run_input WHERE run_id NOT IN (SELECT run_id FROM keep_run)"
        )
        deleted_input = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        cur = conn.execute(
            "DELETE FROM screen_decision WHERE run_id NOT IN (SELECT run_id FROM keep_run)"
        )
        deleted_decision = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        out["keep_run"] = keep_run_n
        out["deleted_screen_run_input"] = deleted_input
        out["deleted_screen_decision"] = deleted_decision

        # 2) raw 保留集：各 provenance 表在窗口内引用到的 sha（无日期列的表整表保留）
        conn.execute("CREATE TEMP TABLE keep_raw(sha TEXT PRIMARY KEY)")
        per_table: dict[str, int] = {}
        for table, date_col, _source_col in _provenance_tables():
            if not _table_exists(conn, table):
                continue
            cols = _columns(conn, table)
            if "raw_sha256" not in cols:
                continue
            if date_col and date_col in cols:
                sql = (
                    "INSERT OR IGNORE INTO keep_raw "
                    "SELECT DISTINCT raw_sha256 FROM %s "
                    "WHERE %s >= ? AND raw_sha256 IS NOT NULL AND length(raw_sha256)=64"
                    % (table, date_col)
                )
                params: tuple[Any, ...] = (since,)
            else:
                sql = (
                    "INSERT OR IGNORE INTO keep_raw "
                    "SELECT DISTINCT raw_sha256 FROM %s "
                    "WHERE raw_sha256 IS NOT NULL AND length(raw_sha256)=64" % table
                )
                params = ()
            try:
                conn.execute(sql, params)
                per_table[table] = int(conn.execute("SELECT changes()").fetchone()[0])
            except sqlite3.Error as exc:  # 表结构漂移不该阻断瘦身
                per_table[table] = -1
                out.setdefault("warnings", []).append("%s: %s" % (table, exc))
        keep_raw_n = int(conn.execute("SELECT COUNT(*) FROM keep_raw").fetchone()[0])
        total_raw_n = int(conn.execute("SELECT COUNT(*) FROM raw_object").fetchone()[0])             if _table_exists(conn, "raw_object") else 0
        cur = conn.execute("DELETE FROM raw_object WHERE sha256 NOT IN (SELECT sha FROM keep_raw)")
        deleted_raw = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        out["keep_raw"] = keep_raw_n
        out["raw_rows_before"] = total_raw_n
        out["deleted_raw_object"] = deleted_raw
        out["per_table_inserted"] = per_table
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise

    try:
        if vacuum:
            conn.execute("VACUUM")
        conn.execute("PRAGMA optimize")
        out["after"] = _db_stats(conn, db_path)
    finally:
        conn.close()
    out["ok"] = True
    return out


# --------------------------------------------------------------------------- export

def _collect_members(root: Path, keep_publications: int, keep_screens: int = 5,
                     db_path: Path | None = None) -> list[tuple[Path, str, bool]]:
    """返回 [(绝对路径, tar 内路径, 是否递归)]。

    db_path 只影响「打包哪个库文件」，归档里一律叫 var/db/fundana.db：
    bootstrap 时从副本导包，解到别处就还是标准路径。
    """
    members: list[tuple[Path, str, bool]] = []

    def add(path: Path, arc: str, recursive: bool = False) -> None:
        if path.exists() or path.is_symlink():
            members.append((path, arc, recursive))

    add(Path(db_path) if db_path else root / "var/db/fundana.db", "var/db/fundana.db")
    add(root / "reports/publication_state.json", "reports/publication_state.json")
    add(root / "reports/.publication_current", "reports/.publication_current")

    revisions: list[Path] = []
    runs_root = root / "reports/runs"
    if runs_root.is_dir():
        for run_dir in sorted((p for p in runs_root.iterdir() if p.is_dir()), reverse=True):
            rev_root = run_dir / "revisions"
            if not rev_root.is_dir():
                continue
            for pub in sorted((p for p in rev_root.iterdir() if p.is_dir()), reverse=True):
                revisions.append(pub)
                if len(revisions) >= keep_publications:
                    break
            if len(revisions) >= keep_publications:
                break
    for pub in revisions:
        add(pub, pub.relative_to(root).as_posix(), True)

    screen_root = root / "reports/screen"
    if screen_root.is_dir():
        for sd in sorted((p for p in screen_root.iterdir() if p.is_dir()), reverse=True)[:keep_screens]:
            add(sd, sd.relative_to(root).as_posix(), True)
    return members


def _feed_tar(members: list[tuple[Path, str, bool]], stdin, errors: list[str]) -> None:
    try:
        with tarfile.open(fileobj=stdin, mode="w|") as tf:
            for path, arc, recursive in members:
                tf.add(str(path), arcname=arc, recursive=recursive)
    except BaseException as exc:  # 线程里不能静默失败
        errors.append("%s: %s" % (type(exc).__name__, exc))
    finally:
        try:
            stdin.close()
        except Exception:
            pass


def export(
    root: Path,
    out_dir: Path,
    db_path: Path | None = None,
    do_slim: bool = False,
    keep_publications: int = 20,
    keep_raw_days: int = 120,
    part_size: int = PART_SIZE_DEFAULT,
    do_redact: bool = True,
) -> dict[str, Any]:
    root = Path(root)
    out_dir = Path(out_dir)
    db_path = Path(db_path) if db_path else root / "var/db/fundana.db"
    if not db_path.exists():
        raise SystemExit("db not found: %s" % db_path)

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*"):
        if stale.is_file():
            stale.unlink()

    result: dict[str, Any] = {"db_path": str(db_path)}
    if do_redact:
        result["redacted"] = redact(db_path)
        if any(result["redacted"].values()):
            print("redacted: %s" % result["redacted"], file=sys.stderr)
    if do_slim:
        result["slim"] = slim(db_path, keep_publications=keep_publications,
                              keep_raw_days=keep_raw_days, vacuum=True)

    # 导出前确认库没坏；坏库不上传，避免污染滚动 Release
    conn = sqlite3.connect(str(db_path), timeout=120)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        check = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
    finally:
        conn.close()
    if str(check).lower() != "ok":
        raise SystemExit("quick_check failed: %s" % check)

    state_path = root / "reports/publication_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    nav_as_of = state.get("nav_as_of") or _nav_as_of_from_db(db_path, state.get("publication_id"))
    stamp = _now_cn().strftime("%Y%m%dT%H%M%S")
    members = _collect_members(root, keep_publications, db_path=db_path)

    zproc = subprocess.Popen(
        ["zstd", "-3", "-T0", "-q", "-c", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    assert zproc.stdin is not None and zproc.stdout is not None
    errors: list[str] = []
    feeder = threading.Thread(target=_feed_tar, args=(members, zproc.stdin, errors), daemon=True)
    feeder.start()

    parts: list[dict[str, Any]] = []
    idx = 0
    fh = None
    hasher = None
    written = 0
    part_path = None
    total = 0

    def _open_part() -> None:
        nonlocal idx, fh, hasher, written, part_path
        part_path = out_dir / ("state-%s.part.%s" % (stamp, _part_suffix(idx)))
        idx += 1  # 分片名必须递增，否则多片会互相覆盖
        fh = part_path.open("wb")
        hasher = hashlib.sha256()
        written = 0

    def _close_part() -> None:
        nonlocal fh, part_path
        if fh is None or part_path is None:
            return
        fh.close()
        parts.append({"name": part_path.name, "sha256": hasher.hexdigest(),  # type: ignore[union-attr]
                      "size": part_path.stat().st_size})
        fh = None

    try:
        read = zproc.stdout.read
        while True:
            chunk = read(CHUNK)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                if fh is None:
                    _open_part()
                room = part_size - written
                piece = chunk[offset:offset + room]
                fh.write(piece)  # type: ignore[union-attr]
                hasher.update(piece)  # type: ignore[union-attr]
                written += len(piece)
                total += len(piece)
                offset += len(piece)
                if written >= part_size:
                    _close_part()
        _close_part()
    finally:
        try:
            zproc.stdout.close()
        except Exception:
            pass

    feeder.join(timeout=600)
    rc = zproc.wait()
    if errors:
        raise SystemExit("tar failed: %s" % errors[0])
    if rc != 0:
        raise SystemExit("zstd exited %s" % rc)

    manifest = {
        "format": 1,
        "stamp": stamp,
        "created_at": _now_cn().isoformat(timespec="seconds"),
        "as_of": state.get("committed_at"),
        "nav_as_of": nav_as_of,
        "generation": state.get("generation"),
        "publication_id": state.get("publication_id"),
        "db_bytes": db_path.stat().st_size,
        "total_bytes": total,
        "member_count": len(members),
        "parts": parts,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["manifest"] = manifest
    return result


# --------------------------------------------------------------------------- import

def _verify_parts(src: Path, manifest: dict[str, Any]) -> None:
    for part in manifest["parts"]:
        path = src / part["name"]
        if not path.exists():
            raise SystemExit("missing part: %s" % path)
        size = path.stat().st_size
        if size != part["size"]:
            raise SystemExit("size mismatch: %s (%s != %s)" % (path.name, size, part["size"]))
        digest = _sha256_file(path)
        if digest != part["sha256"]:
            raise SystemExit("sha256 mismatch: %s" % path.name)


def _feed_parts(parts: list[Path], stdin, errors: list[str]) -> None:
    try:
        for path in parts:
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    stdin.write(chunk)
            try:
                path.unlink()  # 边解边删，降低峰值磁盘占用
            except OSError:
                pass
    except BaseException as exc:
        errors.append("%s: %s" % (type(exc).__name__, exc))
    finally:
        try:
            stdin.close()
        except Exception:
            pass


def import_(root: Path, src: Path) -> dict[str, Any]:
    root = Path(root)
    src = Path(src)
    manifest_path = src / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit("manifest.json not found in %s" % src)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _verify_parts(src, manifest)

    zproc = subprocess.Popen(["zstd", "-d", "-q", "-c"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert zproc.stdin is not None and zproc.stdout is not None
    errors: list[str] = []
    feeder = threading.Thread(
        target=_feed_parts,
        args=([src / p["name"] for p in manifest["parts"]], zproc.stdin, errors),
        daemon=True,
    )
    feeder.start()

    count = 0
    try:
        with tarfile.open(fileobj=zproc.stdout, mode="r|") as tf:
            try:
                tf.extractall(path=str(root), filter="data")
                count = len(tf.getmembers())
            except TypeError:  # Python < 3.12 没有 filter 参数
                for member in tf:
                    target = (root / member.name).resolve()
                    if not str(target).startswith(str(root.resolve())):
                        raise SystemExit("unsafe member: %s" % member.name)
                    tf.extract(member, path=str(root))
                    count += 1
    finally:
        try:
            zproc.stdout.close()
        except Exception:
            pass
    feeder.join(timeout=1800)
    rc = zproc.wait()
    if errors:
        raise SystemExit("extract failed: %s" % errors[0])
    if rc != 0:
        raise SystemExit("zstd exited %s" % rc)

    db_path = root / "var/db/fundana.db"
    return {"ok": True, "members": count, "manifest": manifest,
            "db_bytes": db_path.stat().st_size if db_path.exists() else 0}


# --------------------------------------------------------------------------- CLI

def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fundana state bundle")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_slim = sub.add_parser("slim", help="裁掉旧 screen 明细与无引用 raw")
    p_slim.add_argument("--db", default=str(ROOT_DEFAULT / "var/db/fundana.db"))
    p_slim.add_argument("--keep-publications", type=int, default=20)
    p_slim.add_argument("--keep-raw-days", type=int, default=120)
    p_slim.add_argument("--keep-runs", type=int, default=5)
    p_slim.add_argument("--vacuum", action="store_true")

    p_exp = sub.add_parser("export", help="打包 + 压缩 + 分片 + manifest")
    p_exp.add_argument("--root", default=str(ROOT_DEFAULT))
    p_exp.add_argument("--db", default=None)
    p_exp.add_argument("--out", required=True)
    p_exp.add_argument("--slim", action="store_true")
    p_exp.add_argument("--keep-publications", type=int, default=20)
    p_exp.add_argument("--keep-raw-days", type=int, default=120)
    p_exp.add_argument("--part-size", type=int, default=PART_SIZE_DEFAULT)
    p_exp.add_argument("--keep-personal", action="store_true",
                       help="保留自选/备注等个人表（只用于本机私有备份，不要推到公开 Release）")

    p_imp = sub.add_parser("import", help="校验 + 解压回工作目录")
    p_imp.add_argument("--root", default=str(ROOT_DEFAULT))
    p_imp.add_argument("--src", required=True)

    p_ins = sub.add_parser("inspect", help="打印 manifest 摘要")
    p_ins.add_argument("--src", required=True)

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.cmd == "slim":
        out = slim(Path(args.db), keep_publications=args.keep_publications,
                   keep_raw_days=args.keep_raw_days, keep_runs=args.keep_runs,
                   vacuum=args.vacuum)
        _print(out)
        return 0
    if args.cmd == "export":
        out = export(Path(args.root), Path(args.out),
                     db_path=Path(args.db) if args.db else None,
                     do_slim=args.slim,
                     keep_publications=args.keep_publications,
                     keep_raw_days=args.keep_raw_days,
                     part_size=args.part_size,
                      do_redact=not args.keep_personal)
        _print({k: out[k] for k in ("db_path", "redacted", "manifest") if k in out})
        return 0
    if args.cmd == "import":
        _print(import_(Path(args.root), Path(args.src)))
        return 0
    if args.cmd == "inspect":
        src = Path(args.src)
        manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
        _print(manifest)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
