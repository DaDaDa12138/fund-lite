"""日更管线：一次跑完「当日快照 → 增量指标 → 融合 → 筛选 → 发布 → 跟踪记账」。

设计要点（对应 ADR-020）：
- 净值当日增量走 B1 全市场快照（一次请求），不再逐只抓 pzd（7.4k 只 ≈ 3.7GB/天）；
- **没有新业务日就不发布**：返回 status=controlled_skip，绝不生成一份"看起来更新了"的快照；
- 每个阶段各自一个 run_id，失败时旧发布仍然有效（发布是唯一提交点，见 ADR-010）；
- 跟踪榜记账发生在发布成功之后，幂等键是 publication_id。
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from typing import Any

from . import (collect, db, factors as factors_mod, freshness, fundtypes, maintenance,
               publish, quality as quality_mod, tracker)
from .fetch.http import Fetcher
from .fuse import run_fuse
from .metrics import engine as metrics_engine
from .screen import engine as screen_engine


def batch_id_for(as_of: str) -> str:
    """日更批次号：带契约版本，换口径时自然生成新批次。

    不能只用 as_of：2026-11-02 已有一个冻结在 v8 的批次，v10 日更复用同名
    批次会触发 batch_screen_contract_mismatch，整个 screen 阶段直接失败。
    """
    return "daily-%s-%s" % (as_of, screen_engine.T.SCREEN_CONTRACT.split("-")[2])


def _maybe_collect_lifecycle(conn, fetcher, *, today: str, stages: list[dict[str, Any]]) -> None:
    """生命周期公告按月增量采集；失败只记 stage，不阻断日更。"""
    try:
        last = conn.execute(
            "SELECT MAX(created_at) FROM fund_lifecycle WHERE source LIKE 'csrc_eid:%'"
        ).fetchone()[0]
        if last and str(last)[:10] >= (date.fromisoformat(today) - timedelta(days=30)).isoformat():
            return
        from . import lifecycle_csrc
        start = (date.fromisoformat(today) - timedelta(days=45)).isoformat()
        out = lifecycle_csrc.collect(fetcher, start=start, end=today, parse_pdfs=True)
        stages.append({"stage": "lifecycle", "ok": bool(out.get("ok")),
                       "rows": out.get("rows"), "funds": out.get("funds"),
                       "pdf_ok": out.get("pdf_ok"), "pdf_fail": out.get("pdf_fail")})
    except Exception as exc:                                 # noqa: BLE001
        stages.append({"stage": "lifecycle", "ok": False, "error": str(exc)[:200]})


def _codes(conn, types=None) -> list[str]:
    clause, params = fundtypes.where(types, column="type_group")
    # 已清盘/合并基金只服务历史回测，不参与当日指标计算（其净值停在清盘日）。
    active = freshness._active_clause(conn)
    return [r["fcode"] for r in conn.execute(
        f"SELECT f.fcode FROM fund f WHERE {clause}{active} ORDER BY f.fcode", params)]


def run(cfg, conn, *, types: str | None = None, force: bool = False, skip_publish: bool = False,
        verbose: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    type_codes = fundtypes.resolve(types, cfg)
    types = ",".join(type_codes)              # 采集层按逗号分隔的前缀族工作
    business_date = db.today_cn()
    formula = cfg.get("risk", "formula_contract", default="fundana-risk-2026.11.3")
    prev_max = conn.execute("SELECT MAX(nav_date) FROM fund_nav_daily").fetchone()[0]
    prev_effective = (freshness.effective_nav_date(conn, types=type_codes, as_of=prev_max)
                      if prev_max else None)
    def say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    run_id = db.start_run(conn, "daily", cfg.config_hash, cfg.code_version,
                          notes=f"types={types} force={force}")
    stages = db.RunStages(conn, run_id)
    fetcher = Fetcher(cfg, conn, run_id)
    # 名录与申赎状态都是"当日观测"，必须每天刷新：名录用于类型命名空间与新增基金，
    # 状态用于 L2 可投性门禁（否则 fuse 找不到当天的 business_date，universe 会变 0）。
    dir_res = collect.collect_directory(fetcher, business_date)
    stages.append({"stage": "directory", "ok": dir_res.get("ok"),
                   "rows": dir_res.get("rows")})
    if not dir_res.get("ok"):
        db.finish_run(conn, run_id, "failed", notes="directory_failed")
        return {"status": "failed", "stage": "directory", "run_id": run_id,
                "detail": dir_res, "stages": stages}
    st_res = collect.collect_status(fetcher, business_date)
    stages.append({"stage": "status", "ok": st_res.get("ok"), "rows": st_res.get("rows")})
    if not st_res.get("ok"):
        db.finish_run(conn, run_id, "failed", notes="status_failed")
        return {"status": "failed", "stage": "status", "run_id": run_id,
                "detail": st_res, "stages": stages}
    say(f"名录 {dir_res.get('rows')} 行，申赎状态 {st_res.get('rows')} 行")
    # 指数日线很小（三只共约 1.5 万行），每天整段覆盖；失败不阻断主流程，
    # 因子页会显示「没有因子数据」而不是让整次日更失败。
    try:
        idx_res = collect.collect_index(fetcher)
        stages.append({"stage": "index", "ok": idx_res.get("ok"),
                       "rows": idx_res.get("rows"),
                       "chinabond": idx_res.get("chinabond")})
    except Exception as exc:                                 # noqa: BLE001
        stages.append({"stage": "index", "ok": False, "error": str(exc)[:200]})
        say(f"指数日线采集失败（不阻断）：{exc}")
    snap = collect.collect_nav_snapshot(fetcher, business_date, types)
    stages.append({"stage": "snapshot", "ok": snap.get("ok"), "detail": snap})
    if not snap.get("ok"):
        db.finish_run(conn, run_id, "failed")
        return {"status": "failed", "stage": "snapshot", "run_id": run_id,
                "detail": snap, "stages": stages}
    nav_date = snap["nav_date"]
    # B1 的 showday 只有两日：短停机由 showday[1] 补前一交易日，跨多日停机时
    # 中间日仍缺失。这里只对候选池逐只回补，不阻断主流程；缺口本身会继续
    # 通过 data_quality/门禁暴露，不会被当成完整历史。
    if prev_max:
        try:
            bf = collect.backfill_nav_gaps(
                fetcher, business_date, prev_max, nav_date, types=types,
                max_funds=int(cfg.get("collect", "nav_backfill_max_funds", default=200)),
                min_ratio=float(cfg.get("collect", "nav_backfill_min_ratio", default=0.5)))
            stages.append({"stage": "nav_backfill",
                           "ok": bf.get("status") in ("ok", "not_needed"),
                           "detail": bf})
            if bf.get("attempted"):
                say(f"净值回补：缺失 {len(bf.get('missing') or [])} 个交易日，"
                    f"候选 {bf.get('attempted')} 只，成功 {bf.get('succeeded')}，"
                    f"失败 {bf.get('failed')}")
        except Exception as exc:                             # noqa: BLE001
            stages.append({"stage": "nav_backfill", "ok": False,
                           "error": str(exc)[:200]})
            say(f"净值回补失败（不阻断）：{exc}")
    # 有效净值日：不是 MAX(nav_date)。11-03 只有 185 只披露 11-02 时，
    # 有效日仍是 10-30；等 11-02 覆盖到 90% 才把 as_of 推到 11-02。
    as_of = freshness.effective_nav_date(conn, types=type_codes, as_of=nav_date) or nav_date
    say(f"快照 {nav_date}：本类型写入 {snap['written']} 行（全市场 {snap['snapshot_rows']} 行），"
        f"有效净值日 {as_of}")
    if prev_max and as_of <= (prev_effective or "") and not force:
        # 有效日没前进时，只有指标还没跟上才需要继续跑；否则控制性跳过。
        cov = freshness.metric_coverage(conn, as_of, formula_contract=formula, types=type_codes)
        current = cov.get("current_pct")
        if current is not None and current >= 90.0:
            db.finish_run(conn, run_id, "skipped")
            return {"status": "controlled_skip", "run_id": run_id, "nav_date": nav_date,
                    "as_of": as_of, "prev_max_nav_date": prev_max,
                    "prev_effective_date": prev_effective,
                    "reason": "no_new_effective_date", "stages": stages}
    # 注意：这里不能提前把 run 标 success。2026-11-03 的日更在 screen 阶段
    # 因批次契约冲突失败，但 run 仍显示 success，掩盖了真实故障。

    # §16.1 跨源核对：E3 状态快照 vs B1 净值快照，同一事实两个端点各取一遍。
    # 失败不阻断（第二来源不可用属可降级），但门禁会据此记 warning 甚至 block。
    try:
        from .validate import cross as cross_mod
        cc = cross_mod.check_daily(conn, business_date, cfg)
        stages.append({"stage": "cross_check", "ok": True, "written": cc.get("written"),
                       "mismatch": cc.get("mismatch"), "missing": cc.get("missing")})
        say(f"跨源核对：{cc.get('written')} 行，不一致 {cc.get('mismatch')}，"
            f"缺一侧 {cc.get('missing')}")
    except Exception as exc:                                 # noqa: BLE001
        stages.append({"stage": "cross_check", "ok": False, "error": str(exc)[:200]})
        say(f"跨源核对失败（不阻断，门禁会记录）：{exc}")

    codes = _codes(conn, type_codes)
    t0 = time.monotonic()
    ok = fail = 0
    for code in codes:
        try:
            res = metrics_engine.compute_fund(conn, cfg, code, as_of)
            ok += 1 if res.get("ok") else 0
            fail += 0 if res.get("ok") else 1
        except Exception:                                   # noqa: BLE001
            fail += 1
    conn.commit()
    stages.append({"stage": "metrics", "ok": fail == 0, "funds": len(codes), "computed": ok,
                   "failed": fail, "elapsed_s": round(time.monotonic() - t0, 1)})
    say(f"指标：{ok} 只完成 / {fail} 只失败，{time.monotonic() - t0:.1f}s")
    if codes and ok == 0:
        db.finish_run(conn, run_id, "failed", notes="metrics_all_failed")
        return {"status": "failed", "stage": "metrics", "run_id": run_id,
                "detail": {"funds": len(codes), "failed": fail}, "stages": stages}

    # 可比性旗标必须与指标同批刷新。此前只有 CLI 会刷新，日更筛选读到的是
    # 旧 as_of/旧契约的快照，P0-2 的 3 年门槛会悄悄失效。
    try:
        from .metrics import comparability
        t0 = time.monotonic()
        comp = comparability.refresh(conn, as_of=as_of, formula_contract=formula)
        stages.append({"stage": "comparability", "ok": True, "as_of": comp["as_of"],
                       "rows": comp["rows"], "comparable": comp["comparable"],
                       "flagged": comp["flagged"],
                       "elapsed_s": round(time.monotonic() - t0, 1)})
        say(f"可比性：{comp['rows']} 只，可比 {comp['comparable']}，"
            f"{time.monotonic() - t0:.1f}s")
    except Exception as exc:                                 # noqa: BLE001
        stages.append({"stage": "comparability", "ok": False, "error": str(exc)[:200]})
        db.finish_run(conn, run_id, "failed", notes="comparability_failed")
        return {"status": "failed", "stage": "comparability", "run_id": run_id,
                "detail": {"error": str(exc)[:200]}, "stages": stages}

    # 风格因子归因：只读净值+指数，不参与筛选/发布；失败不阻断日更。
    try:
        t0 = time.monotonic()
        fac = factors_mod.compute(conn, cfg, as_of=as_of, types=type_codes)
        stages.append({"stage": "factors", "ok": True, "computed": fac.get("computed"),
                       "reliable": fac.get("reliable"), "skipped": fac.get("skipped"),
                       "elapsed_s": round(time.monotonic() - t0, 1)})
        say(f"因子：{fac.get('computed')} 只（可靠 {fac.get('reliable')}），"
            f"{time.monotonic() - t0:.1f}s")
    except Exception as exc:                                 # noqa: BLE001
        stages.append({"stage": "factors", "ok": False, "error": str(exc)[:200]})
        say(f"因子计算失败（不阻断）：{exc}")

    # 长期质量指标：36 个月 MRAR(2)，只写质量表，不参与筛选/发布；失败不阻断日更。
    try:
        t0 = time.monotonic()
        q = quality_mod.compute_mrar(conn, cfg, as_of=as_of, types=type_codes)
        stages.append({"stage": "mrar", "ok": True, "computed": q.get("computed"),
                       "skipped": q.get("skipped"),
                       "elapsed_s": round(time.monotonic() - t0, 1)})
        say(f"MRAR：{q.get('computed')} 只（跳过 {q.get('skipped')}），"
            f"{time.monotonic() - t0:.1f}s")
    except Exception as exc:                                 # noqa: BLE001
        stages.append({"stage": "mrar", "ok": False, "error": str(exc)[:200]})
        say(f"MRAR 计算失败（不阻断）：{exc}")

    fuse_run = db.start_run(conn, "fuse", cfg.config_hash, cfg.code_version, notes="daily")
    try:
        fused = run_fuse(conn, cfg, fuse_run, business_date, types=set(type_codes))
    except Exception as exc:                                 # noqa: BLE001
        db.finish_run(conn, fuse_run, "failed", notes="fuse_exception")
        db.finish_run(conn, run_id, "failed", notes="fuse_exception")
        stages.append({"stage": "fuse", "ok": False, "error": str(exc)[:200]})
        return {"status": "failed", "stage": "fuse", "run_id": run_id,
                "detail": str(exc)[:200], "stages": stages}
    db.finish_run(conn, fuse_run, "success")
    prof = (fused.get("stages") or {}).get("profile") or {}
    cov = fused.get("stages", {}).get("coverage") or {}
    stages.append({"stage": "fuse", "ok": True, "run_id": fuse_run,
                   "funds": fused.get("funds"), "profiles": prof.get("written"),
                   "conflicts": prof.get("conflicts"), "coverage": cov.get("funds")})
    say(f"融合：基金 {fused.get('funds')} 只，画像 {prof.get('written')} 条，"
        f"冲突 {prof.get('conflicts')}，覆盖 {cov.get('funds')} 只")

    screen_run = db.start_run(conn, "screen", cfg.config_hash, cfg.code_version,
                              notes="daily")
    # 批次号必须带契约版本：2026-11-02 已有一个冻结在 v8 的同名批次，
    # 直接复用会触发 batch_screen_contract_mismatch；带口径后换口径自然生成新批次。
    batch_id = batch_id_for(as_of)
    try:
        result = screen_engine.run_screen(conn, cfg, screen_run, business_date, formula,
                                          batch_id=batch_id)
    except Exception as exc:                                 # noqa: BLE001
        db.finish_run(conn, screen_run, "failed", notes="screen_exception")
        db.finish_run(conn, run_id, "failed", notes="screen_exception")
        stages.append({"stage": "screen", "ok": False, "error": str(exc)[:200]})
        return {"status": "failed", "stage": "screen", "run_id": run_id,
                "detail": str(exc)[:200], "stages": stages}
    try:
        from .cli import _write_freeze
        _write_freeze(cfg, conn, screen_run, as_of)
    except Exception as exc:                                 # noqa: BLE001
        db.finish_run(conn, screen_run, "failed", notes="freeze_failed")
        db.finish_run(conn, run_id, "failed", notes="freeze_failed")
        stages.append({"stage": "freeze", "ok": False, "error": str(exc)[:200]})
        return {"status": "failed", "stage": "freeze", "run_id": run_id,
                "detail": str(exc)[:200], "stages": stages}
    db.finish_run(conn, screen_run, "success")
    stages.append({"stage": "screen", "ok": True, "run_id": screen_run,
                   "universe": result.get("universe"), "formal": result.get("formal"),
                   "locked": result.get("locked_qualified"),
                   "locked_l3": result.get("locked")})
    say(f"筛选：universe={result.get('universe')} formal={result.get('formal')}"
        f" locked={result.get('locked_qualified')}")
    out: dict[str, Any] = {"status": "ok", "nav_date": nav_date, "as_of": as_of,
                           "run_id": screen_run, "screen": result, "stages": stages}
    if skip_publish:
        db.finish_run(conn, run_id, "success")
        return out

    try:
        built = publish.build(conn, cfg, screen_run)
        committed = publish.commit(cfg, built, screen_run, conn)
        picks = [dict(r) for r in conn.execute(
            "SELECT p.fcode, p.board, p.rank, p.formal_score, p.confidence, d.style_code"
            " FROM screen_pick p LEFT JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
            " WHERE p.run_id=?", (screen_run,))]
        # 跟踪榜按“已实现净值日”记账，而不是墙钟日：早盘发布时 business_date 是今天、
        # nav_as_of 还是昨天，用今天会把尚未实现的行情提前记进 first_seen_date，
        # 也会让同日修正报告因 nav_as_of 更旧而被 tracker 拒绝。
        trk = tracker.update_after_publish(conn, built["revision_id"], as_of, screen_run,
                                           picks, generation=committed["generation"])
    except Exception as exc:                                 # noqa: BLE001
        db.finish_run(conn, run_id, "failed", notes="publish_exception")
        stages.append({"stage": "publish", "ok": False, "error": str(exc)[:200]})
        return {"status": "failed", "stage": "publish", "run_id": run_id,
                "detail": str(exc)[:200], "stages": stages}
    stages.append({"stage": "publish", "ok": True, "generation": committed["generation"],
                   "publication_id": built["revision_id"], "tracker": trk})
    db.finish_run(conn, run_id, "success")
    out.update({"generation": committed["generation"],
                "publication_id": built["revision_id"], "tracker": trk,
                "elapsed_s": round(time.monotonic() - started, 1)})
    say(f"发布 generation {committed['generation']}（{built['revision_id'][:12]}），"
        f"跟踪：新增 {trk.get('entered')} 保持 {trk.get('held')} 退出 {trk.get('exited')}"
        + (f"，替换同日快照 {len(trk.get('superseded') or [])} 份" if trk.get("superseded") else ""))

    # P1-5：维护采集按到期触发（持仓/规模季度，档案/费率/经理月度）。
    # 放在发布之后：它更新的是下一轮筛选/风险门的事实，不应回滚已发布报告；
    # 失败只记 stage，不阻断 forward/shadow。
    try:
        mt = maintenance.run(cfg, conn, verbose=verbose)
        stages.append({"stage": "maintenance", "ok": bool(mt.get("ok")),
                       "status": mt.get("status"), "tasks": mt.get("tasks"),
                       "codes": mt.get("codes")})
        if mt.get("status") != "not_due":
            say(f"维护采集：{mt.get('status')}，目标 {mt.get('codes')} 只，"
                f"任务 {','.join(mt.get('tasks', {}).keys()) or '无'}")
    except Exception as exc:                                 # noqa: BLE001
        stages.append({"stage": "maintenance", "ok": False, "error": str(exc)[:200]})
        say(f"维护采集失败（不阻断）：{exc}")

    # 生命周期公告按月增量：清盘/合并是“是否可投资”的事实，必须定期刷新；
    # 失败只记 stage，不影响已发布报告。
    _maybe_collect_lifecycle(conn, fetcher, today=db.today_cn(), stages=stages)

    # §15 前瞻：发布成功后冻结 cohort（正式 vs 同风格对照 vs 风险匹配对照）。
    # 评估只取 cohort.business_date 之后的净值，所以今天必然是 not_ready —— 这正是设计。
    try:
        from . import forward as forward_mod
        co = forward_mod.create_cohort(conn, cfg, screen_run, business_date=as_of,
                                       publication_id=built["revision_id"], source="live")
        ev = forward_mod.evaluate(conn, cfg, cohort_id=co.get("cohort_id"))
        stages.append({"stage": "forward", "ok": True, "cohort": co.get("cohort_id"),
                       "members": co.get("members"), "matured": ev.get("matured"),
                       "not_ready": ev.get("not_ready")})
        say(f"前瞻 cohort {co.get('cohort_id')}：成员 {co.get('members')} 只，"
            f"已到期 {ev.get('matured')} 条，未到期 {ev.get('not_ready')} 条")
    except Exception as exc:                                 # noqa: BLE001
        stages.append({"stage": "forward", "ok": False, "error": str(exc)[:200]})
        say(f"前瞻 cohort 建立失败（不阻断）：{exc}")

    # v9 同池影子排序：只读复算，不改变发布；每日留一份 core10/core50 差异。
    shadow_result = None
    try:
        from .screen import shadow_rank
        shadow_result = shadow_rank.run(conn, cfg, screen_run)
        meta = shadow_result["meta"]
        stages.append({"stage": "shadow_rank", "ok": True,
                       "core10_overlap": meta["core10"]["overlap"],
                       "core50_overlap": meta["core50"]["overlap"]})
        say(f"v9 shadow：core10 重叠 {meta['core10']['overlap']}/10，"
            f"core50 重叠 {meta['core50']['overlap']}/50")
    except Exception as exc:                                 # noqa: BLE001
        stages.append({"stage": "shadow_rank", "ok": False, "error": str(exc)[:200]})
        say(f"v9 shadow 失败（不阻断）：{exc}")

    # 影子前瞻：与正式 forward_* 分表，只观察同池 v9/v10 的 core10/core50。
    try:
        from . import shadow_forward
        sf = shadow_forward.record(conn, cfg, screen_run, shadow=shadow_result,
                                   publication_id=built["revision_id"], business_date=as_of)
        se = shadow_forward.evaluate(conn, cfg, cohort_id=sf["cohort_id"])
        stages.append({"stage": "shadow_forward", "ok": True,
                       "cohort": sf["cohort_id"], "members": sf["members"],
                       "matured": se.get("matured"), "not_ready": se.get("not_ready")})
        say(f"影子前瞻 cohort {sf['cohort_id']}：成员 {sf['members']} 条，"
            f"已到期 {se.get('matured')} 条，未到期 {se.get('not_ready')} 条")
    except Exception as exc:                                 # noqa: BLE001
        stages.append({"stage": "shadow_forward", "ok": False, "error": str(exc)[:200]})
        say(f"影子前瞻失败（不阻断）：{exc}")

    # SQLite 规划器统计信息收尾；很轻，不做 VACUUM（重写 8GB 库必须人工低峰执行）。
    try:
        from . import db_maint
        db_maint.optimize(conn)
        stages.append({"stage": "db_optimize", "ok": True})
    except Exception as exc:                                 # noqa: BLE001
        stages.append({"stage": "db_optimize", "ok": False, "error": str(exc)[:200]})
        say(f"SQLite optimize 失败（不阻断）：{exc}")
    return out


def main(cfg=None, conn=None, **kw) -> dict[str, Any]:
    from . import config as config_mod
    cfg = cfg or config_mod.load_config()
    conn = conn or db.connect(cfg)
    db.migrate(conn)
    return run(cfg, conn, **kw)
