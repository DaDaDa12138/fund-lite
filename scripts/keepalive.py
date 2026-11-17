#!/usr/bin/env python3
"""把最近一次运行记录写到 status 分支，制造仓库活动。

为什么需要：公开仓库的定时工作流在 60 天没有仓库活动后会被 GitHub 自动停用；
每日一次 status 分支提交即可重置计时，又不污染 main 的历史与 CI（ci.yml 只跟 main）。
只用标准库 + REST API，失败不阻断主流程（打印警告后退出 0）。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
PATH = ".ci/last_run.json"
BRANCH = "status"


def _req(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") or "{}"
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"message": body}
        return exc.code, parsed


def main() -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("keepalive: 缺 GH_TOKEN / GITHUB_REPOSITORY，跳过")
        return 0

    status, info = _req("GET", API + "/repos/" + repo, token)
    if status != 200:
        print("keepalive: 读仓库信息失败 " + str(status))
        return 0
    default_branch = info.get("default_branch") or "main"

    status, ref = _req("GET", API + "/repos/" + repo + "/git/ref/heads/" + BRANCH, token)
    if status != 200:
        status, head = _req("GET", API + "/repos/" + repo + "/git/ref/heads/" + default_branch, token)
        if status != 200:
            print("keepalive: 读默认分支失败 " + str(status))
            return 0
        _req("POST", API + "/repos/" + repo + "/git/refs", token,
             {"ref": "refs/heads/" + BRANCH, "sha": head["object"]["sha"]})

    status, cur = _req("GET", API + "/repos/" + repo + "/contents/" + PATH + "?ref=" + BRANCH, token)
    payload = {
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "status": os.environ.get("FUNDANA_STATUS", ""),
        "nav_as_of": os.environ.get("FUNDANA_NAV_AS_OF", ""),
        "at": os.environ.get("GITHUB_RUN_STARTED_AT", ""),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    body = {
        "message": "chore: 记录流水线运行 " + payload["run_id"],
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if status == 200 and cur.get("sha"):
        body["sha"] = cur["sha"]
    status, out = _req("PUT", API + "/repos/" + repo + "/contents/" + PATH, token, body)
    if status in (200, 201):
        print("keepalive: ok")
        return 0
    print("keepalive: 写入失败 " + str(status) + " " + str(out.get("message", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
