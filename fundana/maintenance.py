"""P1-5：季度/月度维护采集。

背景：日更只采目录/状态/净值/排名，profile/holding/fee/scale/manager 过去只能手工
`fundana collect --domain ...`。结果是持仓停在某一季度、费率和规模可能半年不更新，
筛选与风险门读到的都是旧事实。

本模块按"到期才跑"的方式，把维护采集接到 daily 末尾：
* 只采最新发布榜/研究池涉及的基金（约 174 只），不做全市场重拉；
* 每类数据有独立周期（持仓/规模 80 天，档案/费率/经理 30 天），状态落 maintenance_state；
* 单只失败不阻断其他基金，但会记录 fail 数；整类失败记为 failed，daily 只记 warning，
  不因为维护采集失败而回滚已经发布的报告。

数据仍然全部来自公开基金 API（东财 F10 / pingzhongdata），不写任何模拟值。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Callable

from . import collect as collect_mod
from . import db
from .fetch.http import Fetcher

# task -> (默认周期天数, 采集函数名, 说明)
TASKS: dict[str, dict[str, Any]] = {
    "profile": {"interval_days": 30, "fn": "jbgk", "label": "档案/成立日/规模文本"},
    "fee": {"interval_days": 30, "fn": "fee", "label": "费率阶梯"},
    "scale": {"interval_days": 80, "fn": "scale", "label": "规模历史（季度）"},
    "manager": {"interval_days": 30, "fn": "manager", "label": "经理任职"},
    "holding": {"interval_days": 80, "fn": "holding", "label": "债券持仓（季度）"},
}


def _cfg_interval(cfg, task: str) -> int:
    raw = (cfg.get("maintenance", "tasks", task, "interval_days", default=None))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = TASKS[task]["interval_days"]
    return max(1, value)


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _last_state(conn, task: str):
    return conn.execute(
        "SELECT task, last_run_at, last_status, last_run_id, detail_json"
        " FROM maintenance_state WHERE task=?", (task,)).fetchone()


def due_tasks(conn, cfg, *, today: str | None = None, force: bool = False,
              target_count: int | None = None) -> list[str]:
    today_d = _parse_day(today or db.today_cn())
    out: list[str] = []
    for task in TASKS:
        if force:
            out.append(task)
            continue
        row = _last_state(conn, task)
        last = _parse_day(row["last_run_at"] if row else None)
        if last is None or (today_d - last).days >= _cfg_interval(cfg, task):
            out.append(task)
            continue
        # 上次只跑了 --limit 的少量样本时，不能把整类数据当成已维护。
        if target_count is not None and row is not None:
            try:
                detail = json.loads(row["detail_json"] or "{}")
                if int(detail.get("codes") or 0) < target_count:
                    out.append(task)
            except (TypeError, ValueError, json.JSONDecodeError):
                out.append(task)
    return out


def target_codes(conn, run_id: str | None = None, *, include_research: bool = True) -> list[str]:
    """维护采集目标：最新一次成功 screen 的全部榜单 + 研究池。

    只采这些基金，是因为它们才可能影响当前报告/榜单；全市场 8,000+ 只的档案/费率/持仓
    重拉成本高且不会改变任何决策。新基金进入候选池时，下一次 screen 后自动纳入。
    """
    if run_id is None:
        row = conn.execute(
            "SELECT run_id FROM screen_run WHERE status='done'"
            " ORDER BY created_at DESC, run_id DESC LIMIT 1").fetchone()
        run_id = row["run_id"] if row else None
    if not run_id:
        return []
    codes = {r["fcode"] for r in conn.execute(
        "SELECT DISTINCT fcode FROM screen_pick WHERE run_id=?", (run_id,))}
    if include_research:
        try:
            codes |= {r["fcode"] for r in conn.execute(
                "SELECT DISTINCT fcode FROM screen_research_pool WHERE run_id=?", (run_id,))}
        except Exception:
            pass
    return sorted(codes)


def _call(fetcher: Fetcher, task: str, code: str, business_date: str) -> dict[str, Any]:
    if task == "profile":
        return collect_mod.collect_jbgk_one(fetcher, code, business_date)
    if task == "fee":
        return collect_mod.collect_fee_one(fetcher, code)
    if task == "scale":
        return collect_mod.collect_scale_one(fetcher, code, force=True)
    if task == "manager":
        return collect_mod.collect_manager_one(fetcher, code, business_date, force=True)
    if task == "holding":
        return collect_mod.collect_zqcc_one(fetcher, code)
    raise ValueError(f"unknown maintenance task: {task}")


def run(cfg, conn, *, tasks: list[str] | None = None, force: bool = False,
        limit: int | None = None, run_id: str | None = None,
        verbose: bool = True) -> dict[str, Any]:
    """执行到期维护采集；返回每个 task 的 ok/fail/rows 统计。"""
    codes = target_codes(conn, run_id)
    todo = [t for t in (tasks or due_tasks(conn, cfg, force=force,
                                           target_count=len(codes))) if t in TASKS]
    if limit:
        codes = codes[:limit]
    if not todo:
        return {"ok": True, "status": "not_due", "tasks": {}, "codes": len(codes)}
    if not codes:
        return {"ok": False, "status": "no_targets", "tasks": {}, "codes": 0}

    run_db_id = db.start_run(conn, "maintenance", cfg.config_hash, cfg.code_version,
                             notes="tasks=" + ",".join(todo))
    fetcher = Fetcher(cfg, conn, run_db_id)
    business_date = db.today_cn()
    out: dict[str, Any] = {}
    all_ok = True
    for task in todo:
        ok = fail = rows = periods = 0
        failures: dict[str, int] = {}
        for i, code in enumerate(codes, 1):
            try:
                one = _call(fetcher, task, code, business_date)
            except Exception as exc:  # 单只异常不能中断整类
                one = {"ok": False, "status": f"exception:{type(exc).__name__}"}
            if one.get("ok"):
                ok += 1
                rows += int(one.get("rows") or one.get("tiers") or one.get("tenures") or 0)
                periods += int(one.get("periods") or 0)
            else:
                fail += 1
                failures[str(one.get("status", "?"))] = failures.get(str(one.get("status", "?")), 0) + 1
            if verbose and (i % 50 == 0 or i == len(codes)):
                print(f"  [{task} {i}/{len(codes)}] ok={ok} fail={fail}")
        status = "success" if fail == 0 else ("failed" if ok == 0 else "partial")
        if fail:
            all_ok = False
        detail = {"task": task, "codes": len(codes), "ok": ok, "fail": fail,
                  "rows": rows, "periods": periods, "failures": failures,
                  "status": status, "business_date": business_date}
        out[task] = detail
        conn.execute(
            "INSERT INTO maintenance_state(task,last_run_at,last_status,last_run_id,detail_json)"
            " VALUES(?,?,?,?,?) ON CONFLICT(task) DO UPDATE SET"
            " last_run_at=excluded.last_run_at, last_status=excluded.last_status,"
            " last_run_id=excluded.last_run_id, detail_json=excluded.detail_json",
            (task, db.now_iso(), status, run_db_id, json.dumps(detail, ensure_ascii=False)))
        conn.commit()
    db.finish_run(conn, run_db_id, "success" if all_ok else "partial",
                  notes="tasks=" + ",".join(todo))
    return {"ok": all_ok, "status": "success" if all_ok else "partial",
            "run_id": run_db_id, "tasks": out, "codes": len(codes)}
