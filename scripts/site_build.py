#!/usr/bin/env python3
"""把发布 revision 渲染成静态站点（GitHub Pages 用）。

输入：reports/runs/<run>/revisions/<pub>/{report.json, report.md, summary.md, *.csv}
输出：site/index.html、site/report.html、site/files/*.csv、site/archive/*.html、site/manifest.json

为什么只做静态站：交互式 UI（fundana/api/server.py + web/）依赖活的 SQLite，
Pages 上没有后端，只能托管渲染后的报告；本机 python -m fundana serve 不受影响。
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DEFAULT = Path(__file__).resolve().parent.parent
MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "toc"]

CSS = """
:root { color-scheme: light dark; --fg:#1b1f23; --muted:#5b6570; --line:#d8dee4; --bg:#fff; --accent:#0b6bcb; --soft:#f6f8fa; }
@media (prefers-color-scheme: dark) { :root { --fg:#e6edf3; --muted:#9aa7b4; --line:#30363d; --bg:#0d1117; --accent:#58a6ff; --soft:#161b22; } }
* { box-sizing: border-box; }
body { margin:0; padding:0 16px 64px; background:var(--bg); color:var(--fg); font:16px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
main, header.top, footer { max-width: 940px; margin-left:auto; margin-right:auto; }
header.top { padding:20px 0 10px; border-bottom:1px solid var(--line); }
header.top a { color:var(--accent); text-decoration:none; margin-right:18px; font-size:.95rem; }
h1,h2,h3 { line-height:1.3; margin:1.4em 0 .6em; }
h1 { font-size:1.6rem; } h2 { font-size:1.22rem; border-bottom:1px solid var(--line); padding-bottom:.3em; } h3 { font-size:1.05rem; }
a { color: var(--accent); }
table { border-collapse: collapse; width:100%; margin:1em 0; font-size:.93rem; display:block; overflow-x:auto; }
th,td { border:1px solid var(--line); padding:6px 10px; text-align:left; white-space:nowrap; }
th { background: var(--soft); }
blockquote { margin:1em 0; padding:.6em 1em; border-left:3px solid var(--accent); background:var(--soft); color:var(--muted); }
code { background:var(--soft); padding:.1em .35em; border-radius:4px; font-size:.9em; }
.meta { color:var(--muted); font-size:.9rem; }
.cards { display:flex; flex-wrap:wrap; gap:12px; margin:18px 0 10px; }
.card { border:1px solid var(--line); border-radius:10px; padding:10px 14px; min-width:118px; background:var(--soft); }
.card b { display:block; font-size:1.35rem; } .card span { color:var(--muted); font-size:.85rem; }
ul.files { columns:2; } @media (max-width:640px){ ul.files{columns:1;} }
footer { margin-top:48px; padding-top:16px; border-top:1px solid var(--line); color:var(--muted); font-size:.85rem; }
"""

BOARD_LABELS = {
    "bond_stable": "稳健榜",
    "bond_enhanced": "增强榜",
    "bond_core": "核心精选",
    "bond_core10": "收益精选 10",
    "bond_long": "长期榜",
    "bond_watch": "观察榜",
    "bond_locked": "持有期受限",
    "bond_rookie": "新锐榜",
}


def _md():
    try:
        import markdown  # type: ignore
    except ImportError:
        raise SystemExit("需要 markdown 包：pip install -r requirements-ci.txt")

    def render(text: str) -> str:
        return markdown.markdown(text, extensions=MD_EXTENSIONS, output_format="html5")

    return render


def _clean_md(text: str) -> str:
    """去掉 summary 自带的 H1（页面自己已有标题），并把 report.md 链接改成 report.html。"""
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return chr(10).join(lines).strip()


def _fix_links(fragment: str) -> str:
    return (fragment.replace('href="report.md"', 'href="report.html"')
            .replace('href="./report.md"', 'href="report.html"'))


def _page(title: str, body: str, home: str = "index.html", archive: str = "archive/index.html",
          report: str = "", extra: str = "") -> str:
    links = ['<a href="' + home + '">首页</a>']
    if report:
        links.append('<a href="' + report + '">完整报告</a>')
    links.append('<a href="' + archive + '">历史归档</a>')
    head = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">')
    return (head + "<title>" + html.escape(title) + "</title><style>" + CSS + "</style></head><body>"
            + '<header class="top">' + "".join(links) + "</header><main>" + extra + body + "</main>"
            + "<footer>由 GitHub Actions 每日自动生成。榜单是规则筛出的候选池，不构成投资建议。</footer>"
            + "</body></html>")


def _nav_as_of_map(root: Path) -> dict[str, str]:
    """从库里取 publication_id -> nav_as_of；库不在或表缺失时返回空。"""
    db = Path(root) / "var/db/fundana.db"
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect("file:" + str(db) + "?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT publication_id, nav_as_of FROM publication").fetchall()
        finally:
            conn.close()
        return {str(r[0]): str(r[1]) for r in rows if r[0]}
    except sqlite3.Error:
        return {}


def _load_revisions(root: Path, limit: int = 20) -> list[dict[str, Any]]:
    nav_map = _nav_as_of_map(root)
    out: list[dict[str, Any]] = []
    runs_root = Path(root) / "reports/runs"
    if not runs_root.is_dir():
        return out
    for run_dir in sorted((p for p in runs_root.iterdir() if p.is_dir()), reverse=True):
        rev_root = run_dir / "revisions"
        if not rev_root.is_dir():
            continue
        for pub_dir in sorted((p for p in rev_root.iterdir() if p.is_dir()), reverse=True):
            report_json = pub_dir / "report.json"
            if not report_json.exists():
                continue
            try:
                data = json.loads(report_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            pub_id = pub_dir.name
            generated_at = str(data.get("generated_at") or "")
            day = generated_at[:10] or datetime.fromtimestamp(pub_dir.stat().st_mtime).strftime("%Y-%m-%d")
            out.append({
                "dir": pub_dir,
                "publication_id": pub_id,
                "run_id": str(data.get("run_id") or run_dir.name),
                "generated_at": generated_at,
                "day": day,
                "nav_as_of": nav_map.get(pub_id, ""),
                "counts": data.get("counts") or {},
                "facts": data.get("facts") or {},
            })
            if len(out) >= limit:
                return out
    return out


def _cards(rev: dict[str, Any]) -> str:
    counts = rev.get("counts") or {}
    facts = rev.get("facts") or {}
    items: list[tuple[str, Any]] = [
        ("正式入榜", facts.get("published", "-")),
        ("通过准入 L9", facts.get("l9_pass", "-")),
    ]
    for key in ("bond_stable", "bond_enhanced", "bond_core", "bond_core10"):
        if key in counts:
            items.append((BOARD_LABELS.get(key, key), counts[key]))
    cells = "".join('<div class="card"><b>' + html.escape(str(v)) + "</b><span>" + html.escape(k)
                    + "</span></div>" for k, v in items)
    return '<div class="cards">' + cells + "</div>"


def _archive_list(revisions: list[dict[str, Any]], prefix: str = "archive/") -> str:
    rows = []
    for rev in revisions:
        href = rev.get("archive_href") or ("archive/" + rev["day"] + ".html")
        if not prefix:
            href = href.split("/", 1)[-1]
        counts = rev.get("counts") or {}
        rows.append("<tr><td>" + html.escape(rev["day"]) + "</td><td>"
                    + html.escape(rev.get("nav_as_of") or "-") + "</td><td>"
                    + html.escape(str(counts.get("bond_stable", "-"))) + "</td><td>"
                    + '<a href="' + html.escape(href) + '">查看</a></td></tr>')
    head = "<table><thead><tr><th>发布日</th><th>净值日期</th><th>稳健榜</th><th>链接</th></tr></thead><tbody>"
    return head + "".join(rows) + "</tbody></table>"


def build(root: Path, out: Path, limit: int = 20) -> dict[str, Any]:
    render = _md()
    root = Path(root)
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "archive").mkdir(parents=True)
    (out / "files").mkdir(parents=True)

    revisions = _load_revisions(root, limit=limit)
    if not revisions:
        body = '<h1>暂无发布</h1><p class="meta">流水线尚未产出发布。</p>'
        (out / "index.html").write_text(_page("基金筛选日报", body), encoding="utf-8")
        (out / "archive/index.html").write_text(
            _page("历史归档", "<h1>历史归档</h1><p>暂无。</p>", archive="index.html"), encoding="utf-8")
        return {"ok": True, "revisions": 0, "out": str(out)}

    latest = revisions[0]
    for rev in revisions:
        summary_path = rev["dir"] / "summary.md"
        if summary_path.exists():
            text = summary_path.read_text(encoding="utf-8")
        else:
            text = "# 缺 summary.md"
        title = "债券型基金筛选报告 " + (rev.get("nav_as_of") or rev["day"])
        meta = ('<p class="meta">发布日 ' + html.escape(rev["day"]) + " ｜ 净值日期 "
                + html.escape(rev.get("nav_as_of") or "-") + " ｜ run " + html.escape(rev["run_id"])
                + " ｜ publication " + html.escape(rev["publication_id"][:12]) + "</p>")
        target = out / "archive" / (rev["day"] + ".html")
        suffix = 1
        while target.exists():
            suffix += 1
            target = out / "archive" / (rev["day"] + "-" + str(suffix) + ".html")
        target.write_text(_page(title, meta + _cards(rev) + _fix_links(render(_clean_md(text))),
                               archive="index.html"),
                          encoding="utf-8")
        rev["archive_href"] = "archive/" + target.name

    report_md = latest["dir"] / "report.md"
    if report_md.exists():
        (out / "report.html").write_text(
            _page("完整报告 " + latest["day"],
                  render(_clean_md(report_md.read_text(encoding="utf-8"))),
                  archive="archive/index.html"),
            encoding="utf-8")

    files: list[str] = []
    for csv_path in sorted(latest["dir"].glob("*.csv")):
        shutil.copy2(csv_path, out / "files" / csv_path.name)
        files.append(csv_path.name)

    summary_text = ""
    if (latest["dir"] / "summary.md").exists():
        summary_text = (latest["dir"] / "summary.md").read_text(encoding="utf-8")
    file_list = "".join('<li><a href="files/' + html.escape(n) + '">' + html.escape(n) + "</a></li>"
                        for n in files) or "<li>本期无 CSV</li>"
    body = ("<h1>债券型基金筛选日报</h1>"
            + '<p class="meta">最新一期：发布日 ' + html.escape(latest["day"])
            + " ｜ 净值日期 " + html.escape(latest.get("nav_as_of") or "-") + "</p>"
            + _cards(latest)
            + _fix_links(render(_clean_md(summary_text)))
            + '<h2>当期 CSV</h2><ul class="files">' + file_list + "</ul>"
            + "<h2>历史归档</h2>" + _archive_list(revisions))
    (out / "index.html").write_text(_page("债券型基金筛选日报", body, report="report.html"),
                                    encoding="utf-8")
    (out / "archive/index.html").write_text(
        _page("历史归档", "<h1>历史归档</h1>" + _archive_list(revisions, prefix=""), archive="index.html"),
        encoding="utf-8")

    manifest = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "revisions": len(revisions),
        "latest": {
            "day": latest["day"],
            "nav_as_of": latest.get("nav_as_of") or "",
            "run_id": latest["run_id"],
            "publication_id": latest["publication_id"],
        },
        "files": files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "revisions": len(revisions), "out": str(out), "latest": manifest["latest"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build static report site")
    parser.add_argument("--root", default=str(ROOT_DEFAULT))
    parser.add_argument("--out", default=str(ROOT_DEFAULT / "site"))
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    out = build(Path(args.root), Path(args.out), limit=args.limit)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
