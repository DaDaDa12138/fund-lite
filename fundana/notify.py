"""P1-10：运维告警出口。

不引入外部依赖：告警先落 var/state/alerts.jsonl（可审计），如果 config/app.yaml 配了
ops.webhook_url，再 POST 一份 JSON。webhook 失败不影响主流程；本模块绝不吞掉主流程异常。
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

from . import db


def _state_dir(cfg) -> Path:
    path = Path(cfg.get("paths", "state_dir", default="var/state"))
    if not path.is_absolute():
        path = cfg.root / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def send(cfg, title: str, message: str, *, unit: str | None = None,
         detail: dict | None = None) -> dict:
    """记录并可选推送一条告警；返回 {logged, pushed, error?}。"""
    payload = {"at": db.now_iso(), "title": title, "message": message,
               "unit": unit, "detail": detail or {}}
    logged = False
    try:
        path = _state_dir(cfg) / "alerts.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        logged = True
    except OSError as exc:
        return {"logged": False, "pushed": False, "error": f"log_failed:{exc}"}

    url = str(cfg.get("ops", "webhook_url", default="") or "").strip()
    if not url:
        return {"logged": logged, "pushed": False}
    try:
        resp = requests.post(url, json=payload, timeout=8)
        return {"logged": logged, "pushed": resp.ok, "status_code": resp.status_code}
    except requests.RequestException as exc:
        return {"logged": logged, "pushed": False, "error": str(exc)[:200]}
