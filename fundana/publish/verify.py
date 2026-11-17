"""发布校验：symlink 不逃逸、manifest 逐文件 SHA256 与 size 一致、schema_version 合法。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class PublicationError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def current_revision(cfg) -> Path:
    root = Path(cfg.report_root).resolve()
    pointer = root / cfg.get("publish", "commit_pointer", default=".publication_current")
    if not pointer.is_symlink():
        raise PublicationError("publication_unavailable: %s 不是符号链接" % pointer)
    target = pointer.resolve()
    if root not in target.parents and target != root:
        raise PublicationError("revision 逃逸 report_root: %s" % target)
    if not target.is_dir():
        raise PublicationError("revision 目录不存在: %s" % target)
    return target


def verify(cfg, full: bool = True) -> dict[str, Any]:
    rev = current_revision(cfg)
    manifest_path = rev / "validated.json"
    if not manifest_path.exists():
        raise PublicationError("validated.json 缺失")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise PublicationError("schema_version 非法: %r" % manifest.get("schema_version"))
    problems: list[str] = []
    checked = 0
    # full=True 校验 SHA256；full=False（--fast）仍逐文件校验存在性与大小，
    # 只是跳过哈希，避免"fast 模式 checked=0"这种假通过。
    for entry in manifest.get("files", []):
        rel = entry["path"]
        if rel.startswith("/") or ".." in Path(rel).parts:
            problems.append("路径非法: %s" % rel)
            continue
        path = rev / rel
        if not path.exists():
            problems.append("文件缺失: %s" % rel)
            continue
        if path.stat().st_size != entry["size"]:
            problems.append("大小不符: %s" % rel)
        if full and _sha256(path) != entry["sha256"]:
            problems.append("SHA256 不符: %s" % rel)
        checked += 1
    # §16.8 门禁结果必须随 revision 一起冻结；blocked 的 revision 不允许成为 current
    gate = manifest.get("gate")
    if not isinstance(gate, dict):
        problems.append("validated.json 缺少 gate 段")
    else:
        if gate.get("status") not in ("pass", "warning"):
            problems.append("gate 状态非法: %r" % gate.get("status"))
        if gate.get("status") == "blocked":
            problems.append("gate 判定为 blocked，不应成为 current")
    return {"ok": not problems, "revision": str(rev), "checked": checked,
            "publication_id": manifest.get("publication_id"), "problems": problems,
            "gate": (gate or {}).get("status"), "counts": manifest.get("counts")}
