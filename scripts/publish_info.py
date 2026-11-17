"""读当期发布信息：publication_state.json 缺 nav_as_of，revision_path 又只是相对 reports/ 的路径。

workflow 里的 Release 归档与保活都要用这两项，统一从这里取，避免每处各写一遍 jq。
缺库或缺状态文件时不报错，只输出空值——上层据此跳过归档，不阻断主流程。
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT_DEFAULT = Path(__file__).resolve().parent.parent
FIELDS = ("nav_as_of", "revision_path", "generation", "publication_id")


def _revision_path(value: str) -> str:
    if not value:
        return ""
    return value if value.startswith("reports/") else "reports/" + value


def load(root: Path) -> dict:
    out = {k: "" for k in FIELDS}
    state = root / "reports/publication_state.json"
    if state.exists():
        try:
            doc = json.loads(state.read_text(encoding="utf-8"))
        except ValueError as exc:
            print("warning: publication_state.json unreadable: %s" % exc, file=sys.stderr)
            doc = {}
        out["generation"] = doc.get("generation", "")
        out["publication_id"] = doc.get("publication_id", "") or ""
        out["revision_path"] = _revision_path(str(doc.get("revision_path") or ""))
    db_path = root / "var/db/fundana.db"
    if db_path.exists():
        row = None
        try:
            conn = sqlite3.connect(str(db_path), timeout=60)
            try:
                row = conn.execute(
                    "SELECT nav_as_of, revision_path, generation, publication_id"
                    " FROM publication ORDER BY generation DESC LIMIT 1").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            # WAL 库、半截库都可能在这里报错：归档能跳过，流水线不能因此中断
            print("warning: publication lookup failed: %s" % exc, file=sys.stderr)
        if row:
            out["nav_as_of"] = row[0] or ""
            out["revision_path"] = _revision_path(str(row[1] or "")) or out["revision_path"]
            if row[2] is not None:
                out["generation"] = row[2]
            out["publication_id"] = row[3] or out["publication_id"]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="读当期发布信息")
    ap.add_argument("--root", default=str(ROOT_DEFAULT))
    ap.add_argument("--field", choices=FIELDS)
    ap.add_argument("--github-output", action="store_true",
                    help="按 GITHUB_OUTPUT 格式输出 nav 与 rev 两行")
    args = ap.parse_args(argv)
    info = load(Path(args.root))
    if args.field:
        print(info[args.field])
        return 0
    if args.github_output:
        print("nav=%s" % info["nav_as_of"])
        print("rev=%s" % info["revision_path"])
        return 0
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
