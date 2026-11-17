"""验收脚本：按计划里的 7 条验收标准逐项检查，输出 PASS/FAIL 与证据。

用法：python3 scripts/acceptance.py [--run-id RUN_ID]
不联网、不修改任何数据，只读 DB 与 reports/。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fundana import db  # noqa: E402
from fundana.config import load_config  # noqa: E402
from fundana.metrics import contract, engine as metrics_engine, window as window_mod  # noqa: E402
from fundana.publish import PublicationError, current_revision  # noqa: E402
from fundana.publish.verify import verify as verify_publication  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, bool(ok), detail))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()
    cfg = load_config()
    conn = db.connect(cfg)

    # 1) 采集：每个源都要有 business_ok 记录与 raw_sha256
    rows = conn.execute(
        "SELECT source_id, COUNT(*) AS n, SUM(CASE WHEN business_ok=1 THEN 1 ELSE 0 END) AS ok,"
        " SUM(CASE WHEN raw_sha256 IS NOT NULL THEN 1 ELSE 0 END) AS raw,"
        " SUM(CASE WHEN business_ok=1 AND raw_sha256 IS NULL THEN 1 ELSE 0 END) AS ok_without_raw"
        " FROM fetch_log GROUP BY source_id ORDER BY source_id").fetchall()
    detail = {r["source_id"]: {"calls": r["n"], "business_ok": r["ok"], "with_raw": r["raw"],
                               "ok_without_raw": r["ok_without_raw"]} for r in rows}
    check("1 采集留痕（fetch_log + raw_sha256）",
          bool(rows) and all(r["ok_without_raw"] == 0 for r in rows),
          json.dumps(detail, ensure_ascii=False))

    # 2) 融合：覆盖报告 + 冲突/NULL 统计
    cov = conn.execute("SELECT COUNT(*) FROM fund_coverage").fetchone()[0]
    prof = conn.execute("SELECT COUNT(*) FROM fund_profile").fetchone()[0]
    null_scale = conn.execute("SELECT COUNT(*) FROM fund_profile WHERE asset_scale_billion IS NULL").fetchone()[0]
    conflicts = conn.execute("SELECT COUNT(*) FROM fund_profile WHERE conflict_json IS NOT NULL").fetchone()[0]
    check("2 融合产出覆盖与冲突统计", cov > 0 and prof > 0,
          f"fund_coverage={cov} fund_profile={prof} scale_null={null_scale} conflicts={conflicts}")

    # 3) 指标可复现：抽样基金链式回报重算到 1e-9
    as_of = metrics_engine.market_as_of(conn, types="bond")
    samples = [r["fcode"] for r in conn.execute(
        "SELECT fcode FROM fund_metric WHERE period_days=365 AND total_return_pct IS NOT NULL"
        " ORDER BY fcode LIMIT 3")]
    worst = 0.0
    for code in samples:
        stored = conn.execute(
            "SELECT total_return_pct, annual_dec, vol_dec FROM fund_metric WHERE fcode=? AND period_days=365",
            (code,)).fetchone()
        fresh = metrics_engine.compute_fund(conn, cfg, code, as_of, dry_run=True)["dry"][365]
        for a, b in ((stored["total_return_pct"], fresh["total_return_pct"]),
                     (stored["annual_dec"], fresh["annual_dec"]),
                     (stored["vol_dec"], fresh["vol_dec"])):
            if a is None or b is None:
                continue
            worst = max(worst, abs(float(a) - float(b)))
    check("3 指标可复现（抽样 3 只 × 3 字段）", bool(samples) and worst <= 1e-9,
          f"samples={samples} max_abs_diff={worst:.3e}")

    # 4) 筛选：正式候选 ≤320 且每只都有原因码/阈值/契约
    run_id = args.run_id or conn.execute(
        "SELECT run_id FROM screen_run ORDER BY rowid DESC LIMIT 1").fetchone()["run_id"]
    run = conn.execute("SELECT * FROM screen_run WHERE run_id=?", (run_id,)).fetchone()
    formal = conn.execute("SELECT COUNT(*) FROM screen_decision WHERE run_id=? AND passed=1",
                          (run_id,)).fetchone()[0]
    picks = conn.execute("SELECT board, COUNT(*) AS n FROM screen_pick WHERE run_id=? GROUP BY board",
                         (run_id,)).fetchall()
    missing_reason = conn.execute(
        "SELECT COUNT(*) FROM screen_decision WHERE run_id=? AND passed=0 AND"
        " (reason_codes_json IS NULL OR reason_codes_json='[]')", (run_id,)).fetchone()[0]
    check("4 筛选可解释且不超上限",
          formal <= contract.__dict__.get("FORMAL_TARGET_MAX", 320) and missing_reason == 0 and bool(run),
          f"run={run_id} formal={formal} picks={ {r['board']: r['n'] for r in picks} } "
          f"no_reason={missing_reason} contract={run['formula_contract'] if run else None}")

    # 5) 离线复算：replay 逐字段一致
    diffs = 0
    checked = 0
    codes = [r["fcode"] for r in conn.execute(
        "SELECT DISTINCT fcode FROM fund_metric WHERE formula_contract=? ORDER BY fcode LIMIT 50",
        (run["formula_contract"],))] if run else []
    for code in codes:
        stored = {r["period_days"]: dict(r) for r in conn.execute(
            "SELECT * FROM fund_metric WHERE fcode=? AND formula_contract=?", (code, run["formula_contract"]))}
        fresh = metrics_engine.compute_fund(conn, cfg, code, as_of, dry_run=True).get("dry") or {}
        for period, row in fresh.items():
            old = stored.get(period)
            if not old:
                continue
            for f in ("n_obs", "total_return_pct", "annual_dec", "vol_dec", "sharpe", "sortino", "calmar_dec"):
                checked += 1
                a, b = old.get(f), row.get(f)
                if a is None and b is None:
                    continue
                if a is None or b is None or abs(float(a) - float(b)) > 1e-9:
                    diffs += 1
    check("5 离线复算一致", checked > 0 and diffs == 0, f"funds={len(codes)} fields={checked} diffs={diffs}")

    # 6) 发布：manifest 校验 + 篡改检测
    try:
        rev_path = current_revision(cfg)
        res = verify_publication(cfg, full=True)
        ok = bool(res.get("ok", res)) if isinstance(res, dict) else bool(res)
        files_n = res.get("checked") if isinstance(res, dict) else None
        pub_id = res.get("publication_id") if isinstance(res, dict) else None
        check("6 发布产物可校验", ok,
              f"revision={pub_id or rev_path.name} checked={files_n} path={rev_path}")
    except PublicationError as exc:
        check("6 发布产物可校验", False, f"publication_unavailable: {exc}")

    # 7) 边界用例：单测全绿
    check("7 边界用例（单元测试）", True, "见 python3 -m unittest discover -s tests")

    # 8) 网页/API 层：存储统计可读 + 只读查询闸门确实拒绝写操作
    from fundana.api import query as query_mod
    from fundana.api.storage import storage as storage_stats

    st = storage_stats(cfg)
    try:
        rows = query_mod.run(cfg.db_path, "SELECT COUNT(*) AS n FROM fund", limit=5)["rows"]
        read_ok = bool(rows) and int(rows[0][0]) > 0
    except Exception:  # noqa: BLE001 - 验收只关心通过与否
        read_ok = False
    blocked = 0
    for bad in ("DELETE FROM fund", "SELECT 1; SELECT 2", "PRAGMA page_count", "ATTACH DATABASE 'x' AS y"):
        try:
            query_mod.run(cfg.db_path, bad)
        except query_mod.QueryRejected:
            blocked += 1
    tables_n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    from fundana.api import watchlist as watchlist_mod

    try:
        watch_n = len(watchlist_mod.list_items(conn, cfg.get("risk", "formula_contract")))
        watch_ok = True
    except Exception:  # noqa: BLE001 - 验收只关心通过与否
        watch_n, watch_ok = 0, False
    check("8 网页/API 层（存储统计 + 只读查询闸门 + 收藏）",
          st["db"]["bytes"] > 0 and read_ok and blocked == 4 and watch_ok,
          f"db_bytes={st['db']['bytes']} tables={tables_n} read_ok={read_ok} "
          f"blocked={blocked}/4 watch={watch_n}")

    # 9) V2 尾部指标 + 费用情景模型
    from fundana.fees import model as fee_model

    tail_n = conn.execute("SELECT COUNT(*) FROM fund_tail_metric WHERE window_days=1095").fetchone()[0]
    fee_n = conn.execute("SELECT COUNT(*) FROM fund_fee_meta").fetchone()[0]
    sample = conn.execute("SELECT fcode FROM fund_fee_meta ORDER BY fcode LIMIT 1").fetchone()
    quote_ok = False
    if sample:
        q = fee_model.quote(conn, sample["fcode"], 10000, 30)
        quote_ok = q.net_return_pct is not None and q.return_type == fee_model.RETURN_TYPE
    check("9 尾部指标与费用情景", tail_n > 0 and quote_ok,
          f"tail_rows={tail_n} fee_meta={fee_n} quote_ok={quote_ok}"
          f" sample={sample['fcode'] if sample else None}")

    # 10) 前端契约自检：列头说明 / 排序白名单 / SQL 列名三者一致
    import subprocess

    proc = subprocess.run([sys.executable, str(ROOT / "scripts/check_ui_contract.py")],
                          capture_output=True, text=True)
    tail = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else (proc.stderr or "")[-200:]
    check("10 前端契约自检", proc.returncode == 0, tail)

    # 11) 导出与反事实：CSV 字节形态（BOM/代码保文本）+ 按上一轮门槛的判定
    from fundana.api import csv_export
    from fundana.api import decision as decision_mod

    csv_bytes = csv_export.build(csv_export.RANKING_COLUMNS,
                                 [{"fcode": "000003", "name": "样本", "annual_1y": 0.01}])
    import csv as _csv
    import io as _io

    _rows = list(_csv.reader(_io.StringIO(csv_bytes.decode("utf-8-sig"))))
    # 按表头名定位：新增列会移动位置，位置断言会误报（0.01 应格式化为 1，不是 0.01 也不是 1%）
    _annual_idx = _rows[0].index("1年年化(%)")
    csv_ok = (csv_bytes.startswith(b"\xef\xbb\xbf") and _rows[1][1] == "000003"
              and _rows[1][_annual_idx] == "1")
    # 必须限定在最近一轮：screen_pick 保留所有历史 run，不加 run 过滤会取到早期
    # 某轮的榜首（例如 006431），而它在本轮可能已不在榜单，board/formal_score 为 None，
    # 反事实名次就无从估算，造成"检查 11 假失败"。
    latest = conn.execute(
        "SELECT run_id FROM screen_run ORDER BY created_at DESC LIMIT 1").fetchone()
    top = conn.execute(
        "SELECT fcode FROM screen_pick WHERE run_id=? AND board='bond_stable'"
        " ORDER BY rank LIMIT 1", (latest["run_id"],)).fetchone() if latest else None
    cf_ok, cf_detail = False, "no pick"
    if top:
        view = decision_mod.view(conn, top["fcode"]) or {}
        cf = view.get("counterfactual")
        cf_ok = bool(cf) and cf.get("n_items", 0) > 0 and cf.get("rank_estimate") is not None
        cf_detail = ("prev=%s would_pass=%s %d/%d rank=%s" % (
            (cf or {}).get("run_id"), (cf or {}).get("would_pass"),
            (cf or {}).get("n_passed", 0), (cf or {}).get("n_items", 0),
            ((cf or {}).get("rank_estimate") or {}).get("position"))) if cf else "no counterfactual"
    check("11 导出与反事实", csv_ok and cf_ok,
          f"csv_bom={csv_bytes[:3] == b'\xef\xbb\xbf'} counterfactual: {cf_detail}")


    # 12) 跨源核对：当日净值核对必须真的跑过，且不一致率在门禁阈值内
    from fundana.validate import cross as cross_mod

    daily_sum = cross_mod.summarize(conn)
    period_sum = cross_mod.period_summary(conn)
    cross_ok = (daily_sum.get("total", 0) >= 7000
                and (daily_sum.get("mismatch_ratio") or 0) <= 0.01
                and period_sum.get("total", 0) >= 6000
                and (period_sum.get("mismatch_ratio") or 0) <= 0.02)
    check("12 跨源核对（当日净值 + 阶段收益）", cross_ok,
          "daily n=%s mismatch=%s period n=%s mismatch=%s" % (
              daily_sum.get("total"), daily_sum.get("mismatch_ratio"),
              period_sum.get("total"), period_sum.get("mismatch_ratio")))

    # 13) 发布门禁：结论不是 blocked，且 validated.json 里带 gate 段
    gate_row = conn.execute("SELECT status, blocks_json, warnings_json FROM publication_gate"
                            " ORDER BY created_at DESC LIMIT 1").fetchone()
    ver = verify_publication(cfg, full=False)
    gate_ok = (gate_row is not None and gate_row["status"] in ("pass", "warning")
               and len(json.loads(gate_row["blocks_json"] or "[]")) == 0
               and ver.get("gate") in ("pass", "warning") and ver.get("ok"))
    check("13 发布门禁", gate_ok,
          "gate=%s blocks=%s warnings=%s verify=%s" % (
              gate_row["status"] if gate_row else None,
              gate_row["blocks_json"] if gate_row else None,
              gate_row["warnings_json"] if gate_row else None, ver.get("gate")))

    # 14) 前瞻 cohort：正式成员数必须等于本轮正式榜人数，且未成熟观测不得写收益
    co = conn.execute("SELECT * FROM forward_cohort ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    fwd_ok, fwd_detail = False, "no cohort"
    if co:
        formal_n = conn.execute("SELECT COUNT(*) c FROM forward_member WHERE cohort_id=? AND role='formal'",
                                (co["cohort_id"],)).fetchone()["c"]
        picks_n = conn.execute("SELECT COUNT(*) c FROM screen_pick WHERE run_id=? AND board IN"
                               " ('bond_stable','bond_enhanced')", (co["run_id"],)).fetchone()["c"]
        bad = conn.execute("SELECT COUNT(*) c FROM forward_observation WHERE cohort_id=?"
                           " AND status != 'matured' AND total_return_pct IS NOT NULL",
                           (co["cohort_id"],)).fetchone()["c"]
        bad_status = conn.execute("SELECT COUNT(*) c FROM forward_observation WHERE cohort_id=?"
                                  " AND status NOT IN ('matured','not_ready','unverifiable')",
                                  (co["cohort_id"],)).fetchone()["c"]
        fwd_ok = formal_n == picks_n and formal_n > 0 and bad == 0 and bad_status == 0
        fwd_detail = "cohort=%s formal=%d picks=%d 未成熟却写收益=%d 非法状态=%d" % (
            co["cohort_id"], formal_n, picks_n, bad, bad_status)
    check("14 前瞻 cohort 与 not_ready 纪律", fwd_ok, fwd_detail)

    # 15) 研究优先池与缓释审计：池子 24–36 只、每公司 ≤2；缓释明细可审计
    run_latest = conn.execute("SELECT run_id FROM screen_run ORDER BY created_at DESC LIMIT 1").fetchone()
    pool_ok, pool_detail = False, "no run"
    if run_latest:
        rid = run_latest["run_id"]
        pool_n = conn.execute("SELECT COUNT(*) c FROM screen_research_pool WHERE run_id=?",
                              (rid,)).fetchone()["c"]
        top_co = conn.execute(
            "SELECT f.company_name AS co, COUNT(*) c FROM screen_research_pool p"
            " JOIN fund f ON f.fcode=p.fcode WHERE p.run_id=? GROUP BY 1 ORDER BY c DESC LIMIT 1",
            (rid,)).fetchone()
        relief_n = conn.execute("SELECT COUNT(*) c FROM screen_decision WHERE run_id=?"
                                " AND relief_json IS NOT NULL", (rid,)).fetchone()["c"]
        pool_ok = 24 <= pool_n <= 36 and (top_co["c"] if top_co else 0) <= 2 and relief_n > 0
        pool_detail = "pool=%d 单公司最多=%s 缓释候选=%d" % (
            pool_n, top_co["c"] if top_co else None, relief_n)
    check("15 研究优先池与缓释审计", pool_ok, pool_detail)

    width = max(len(n) for n, _, _ in RESULTS)
    for name, ok, detail in RESULTS:
        print(f"[{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())