#!/usr/bin/env python3
"""连通性冒烟：对 config/source_registry.yaml 里每个域名抽一个 URL 实测一次。

用途：GitHub 托管 runner 是海外 IP，eastmoney / csrc / chinabond 是否可达、
是否被 UA/Referer 拦，必须在把定时任务交给 CI 之前先量一遍。
只发 HEAD 式的少量字节请求，不落库、不解析业务。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT_DEFAULT = Path(__file__).resolve().parent.parent
SAMPLE_CODE = "013492"  # 任意一只在用基金，只用于替换 {code} 占位符
READ_BYTES = 64 * 1024


def _load_sources(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = data.get("sources") or {}
    return [dict(v, source_id=k) for k, v in sources.items() if isinstance(v, dict) and v.get("url")]


def _pick_per_host(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每个 host 取一个请求最轻的源：优先非快照、非 pdf 的页面。"""
    def weight(src: dict[str, Any]) -> tuple[int, int]:
        url = str(src.get("url", ""))
        heavy = 1 if ("Fund_JJJZ_Data.aspx" in url or url.endswith(".pdf")) else 0
        return (heavy, len(url))
    best: dict[str, dict[str, Any]] = {}
    for src in sources:
        url = str(src["url"])
        host = url.split("//", 1)[-1].split("/", 1)[0]
        cur = best.get(host)
        if cur is None or weight(src) < weight(cur):
            best[host] = src
    return sorted(best.values(), key=lambda s: str(s["url"]))


def probe(source: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    import requests

    url = str(source["url"]).replace("{code}", SAMPLE_CODE)
    headers = dict(source.get("headers") or {})
    headers.setdefault("User-Agent", "fundana-smoke/1.0")
    out: dict[str, Any] = {"source_id": source.get("source_id"), "host": url.split("//", 1)[-1].split("/", 1)[0],
                           "url": url}
    try:
        resp = requests.get(url, headers=headers, timeout=(5, timeout), stream=True)
        chunk = next(resp.iter_content(READ_BYTES), b"") or b""
        status = resp.status_code
        # 405/403/429 说明域名可达、只是不接受这种 GET 或限流，和「连不上」是两回事
        rejected = status in (401, 403, 405, 429)
        out.update({"status": status, "bytes": len(chunk),
                    "content_type": resp.headers.get("Content-Type", ""),
                    "reachable": True, "rejected": rejected,
                    "ok": (200 <= status < 400) or rejected})
        resp.close()
    except Exception as exc:  # 网络错误就是本函数要测的东西
        out.update({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="source connectivity smoke test")
    parser.add_argument("--root", default=str(ROOT_DEFAULT))
    parser.add_argument("--registry", default=None)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    registry = Path(args.registry) if args.registry else Path(args.root) / "config/source_registry.yaml"
    sources = _load_sources(registry)
    if not sources:
        print(json.dumps({"ok": False, "error": "no sources in %s" % registry}, ensure_ascii=False))
        return 1

    results = [probe(src, timeout=args.timeout) for src in _pick_per_host(sources)]
    ok = [r for r in results if r.get("ok")]
    rejected = [r for r in ok if r.get("rejected")]
    print(json.dumps({"registry": str(registry), "hosts": len(results), "ok": len(ok),
                      "rejected": [r["host"] for r in rejected],
                      "results": results}, ensure_ascii=False, indent=2))
    # 至少一个域名可用就不算致命：单源不可达由主流程的 non-blocking 阶段兜住
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
