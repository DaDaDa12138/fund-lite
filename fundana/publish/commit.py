"""唯一提交点：先写 revision + validated.json，最后 os.replace 原子切换 .publication_current。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .. import db
from .verify import PublicationError


def check_nav_asof_monotonic(conn, run: dict, allow_older: bool = False):
    """禁止旧 as-of 覆盖新 as-of；返回当前 publication 行（可能为 None）。

    2026-11-04 的真实事故：07:30 daily 已发布 11-03，之后手动用 11-02 跑 P2
    并连续 publish，把 generation 45 的 11-03 榜单覆盖成 11-02。发布层必须有
    单调性门禁，不能依赖操作者记得“不要用旧日期重发”。
    """
    current = conn.execute(
        "SELECT nav_as_of, metric_as_of, run_id, generation FROM publication"
        " ORDER BY generation DESC LIMIT 1").fetchone()
    if current is None or allow_older:
        return current
    cur_nav, cur_metric = current[0], current[1]
    new_nav, new_metric = run.get("nav_as_of"), run.get("metric_as_of")
    if not cur_nav or not new_nav:
        return current
    cur_key = (str(cur_nav)[:10], str(cur_metric or cur_nav)[:10])
    new_key = (str(new_nav)[:10], str(new_metric or new_nav)[:10])
    if new_key < cur_key:
        raise PublicationError(
            "publication_older_than_current: new run %s nav_as_of=%s metric_as_of=%s"
            " < current %s nav_as_of=%s metric_as_of=%s; 如需回滚请显式 --allow-older"
            % (run.get("run_id") or "?", new_key[0], new_key[1],
               current[2], cur_key[0], cur_key[1]))
    return current


def commit(cfg, built: dict[str, Any], run_id: str, conn,
           allow_older: bool = False) -> dict[str, Any]:
    # 门禁阻断时绝不切换 current：旧 publication 继续对外提供服务（失败保旧）
    gate = (built.get("manifest") or {}).get("gate") or {}
    if gate.get("status") == "blocked":
        from .gate import PublicationBlocked
        raise PublicationBlocked(gate)
    root = Path(cfg.report_root).resolve()
    rev = Path(built["revision_path"]).resolve()
    if root not in rev.parents:
        raise ValueError("revision_path 逃逸 report_root: %s" % rev)
    pointer = root / cfg.get("publish", "commit_pointer", default=".publication_current")
    state_path = root / "publication_state.json"

    run_row = conn.execute("SELECT * FROM screen_run WHERE run_id=?", (run_id,)).fetchone()
    run = dict(run_row) if run_row else {}
    check_nav_asof_monotonic(conn, run, allow_older=allow_older)

    generation = 0
    if state_path.exists():
        try:
            generation = int(json.loads(state_path.read_text(encoding="utf-8")).get("generation", 0))
        except (json.JSONDecodeError, ValueError):
            generation = 0
    generation += 1

    rel = os.path.relpath(rev, root)
    tmp_link = root / (pointer.name + ".tmp")
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    os.symlink(rel, tmp_link, target_is_directory=True)
    os.replace(tmp_link, pointer)          # 原子提交点

    manifest = built["manifest"]
    state = {"generation": generation, "publication_id": built["revision_id"],
             "run_id": run_id, "revision_path": rel, "schema_version": 1,
             "committed_at": db.now_iso(), "file_count": len(manifest["files"])}
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    with state_path.open("rb") as fh:
        os.fsync(fh.fileno())

    conn.execute(
        "INSERT INTO publication(publication_id, run_id, generation, created_at, revision_path,"
        " manifest_sha256, status, screen_contract, formula_contract, nav_as_of, metric_as_of)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(publication_id) DO UPDATE SET status=excluded.status,"
        " screen_contract=excluded.screen_contract, formula_contract=excluded.formula_contract,"
        " nav_as_of=excluded.nav_as_of, metric_as_of=excluded.metric_as_of",
        (built["revision_id"], run_id, generation, state["committed_at"], rel,
         _manifest_sha(rev / "validated.json"), "committed",
         run.get("screen_contract"), run.get("formula_contract"),
         run.get("nav_as_of"), run.get("metric_as_of")))
    conn.commit()
    return {"pointer": str(pointer), "generation": generation, "state": state}


def _manifest_sha(path: Path) -> str | None:
    import hashlib
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()
