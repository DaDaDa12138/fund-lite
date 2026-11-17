"""命令行入口：bootstrap / probe / collect / stats / fuse / metrics / screen / publish / verify / serve / replay。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import collect as collect_mod
from . import db, db_maint, fundtypes, maintenance
from .config import load_config
from .fetch import parsers
from .fetch.http import Fetcher


def cmd_bootstrap(args: argparse.Namespace) -> int:
    cfg = load_config()
    print(f"config_hash={cfg.config_hash}")
    print(f"code_version={cfg.code_version}  db={cfg.db_path}")
    if args.verify_config:
        missing = [p for p in (cfg.root / "config").glob("*.yaml")]
        print(f"配置文件: {[p.name for p in missing]}")
        for sid, spec in cfg.sources.items():
            print(f"  source {sid:22s} domain={spec.domain:9s} ttl={spec.ttl_days}d qps={spec.rate_qps} parser={spec.parser}")
    conn = db.connect(cfg)
    applied = db.migrate(conn)
    print(f"迁移: {applied or '无新增'}")
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    print(f"表数量: {len(rows)}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    run_id = db.start_run(conn, "probe", cfg.config_hash, cfg.code_version)
    fetcher = Fetcher(cfg, conn, run_id)
    samples = {"em_jbgk": "000032", "em_pzd": "000032", "em_zqcc": "000032",
               "dj_nav": "000032", "em_lsjz": "000032",
               "em_gmbd": "000003", "em_jjjl": "000003"}
    parsers_map = {
        "em_directory": parsers.parse_directory,
        "em_status_snapshot": parsers.parse_status,
        "em_rank": parsers.parse_rank,
        "em_jbgk": parsers.parse_jbgk,
        "em_pzd": parsers.parse_pzd,
        "em_zqcc": parsers.parse_zqcc,
        "em_gmbd": parsers.parse_gmbd,
        "em_jjjl": parsers.parse_jjjl,
        "dj_nav": _probe_dj_nav,
        "em_lsjz": _probe_lsjz,
    }
    results = []
    failures = 0
    for sid in (list(cfg.sources) if args.sources == "all" else args.sources.split(",")):
        key = samples.get(sid, "-")
        # validation 域是"第二来源"，不可用属可降级情形（门禁记 warning），不计入硬失败
        warn_only = sid in cfg.sources and cfg.sources[sid].domain == "validation"
        params = None
        if sid == "em_rank":
            from datetime import date, timedelta
            today = date.fromisoformat(db.today_cn())
            params = {"op": "ph", "dt": "kf", "ft": "zq", "rs": "", "gs": "0", "sc": "1nzf", "st": "desc",
                      "sd": (today - timedelta(days=365 * 3)).isoformat(), "ed": today.isoformat(),
                      "qdii": "", "tabSubtype": ",,,,", "dx": "1", "pi": "1", "pn": "50"}
        t0 = time.monotonic()
        res = fetcher.get(sid, key=key, params=params, allow_cache=False, force=True)
        elapsed = int((time.monotonic() - t0) * 1000)
        if not res.business_ok:
            if warn_only:
                results.append((sid, "WARN", res.status, 0, elapsed, res.error_msg or ""))
                print(f"[WARN] {sid:22s} {res.status:14s} {elapsed}ms {res.error_msg or ''}（第二来源不可用，门禁降级）")
                continue
            failures += 1
            results.append((sid, "FAIL", res.status, 0, elapsed, res.error_msg or ""))
            print(f"[FAIL] {sid:22s} {res.status:14s} {elapsed}ms {res.error_msg or ''}")
            continue
        try:
            parsed = parsers_map[sid](res.text)
        except parsers.ParseError as exc:
            if warn_only:
                results.append((sid, "WARN", "parse_failed", 0, elapsed, str(exc)))
                print(f"[WARN] {sid:22s} parse_failed {elapsed}ms {exc}")
                continue
            failures += 1
            results.append((sid, "PARSE_FAIL", "parse_failed", 0, elapsed, str(exc)))
            print(f"[PARSE_FAIL] {sid:22s} {elapsed}ms {exc}")
            continue
        if isinstance(parsed, list):
            n = len(parsed)
            extra = f"groups={_count_groups(parsed)}" if sid == "em_directory" else ""
        elif sid == "em_pzd":
            n = len(parsed["nav"])
            extra = f"acc={len(parsed['acc'])} syl_1n={parsed['syl_1n_pct']}"
        elif sid == "em_zqcc":
            n = len(parsed["rows"])
            extra = f"period={parsed['period']}"
        elif sid == "em_jbgk":
            # jbgk 是单只基金的档案字典，不是行列表：用"非空字段数"衡量解析是否真的拿到内容
            n = sum(1 for k, v in parsed.items()
                    if k != "raw_pairs" and v not in (None, "", [], {}))
            extra = f"estab={parsed.get('estab_date')} scale={parsed.get('asset_scale_text')}"
        else:
            n = len(parsed.get("rows") or [])
            extra = ""
        # 业务下限：HTTP 200 + 解析成功不等于"拿到内容"，用最小体量做二次判定
        floor = {"em_directory": 10000, "em_status_snapshot": 10000, "em_rank": 1,
                 "em_pzd": 100, "em_zqcc": 1, "em_jbgk": 5,
                 "dj_nav": 100, "em_lsjz": 5}.get(sid, 1)
        if n < floor:
            failures += 1
            results.append((sid, "TOO_SMALL", "business_empty", n, elapsed, f"n={n} < {floor}"))
            print(f"[TOO_SMALL] {sid:20s} rows={n} < {floor} {elapsed}ms {extra}")
            continue
        print(f"[ok]   {sid:22s} rows={n:<7d} {elapsed:>5d}ms sha={str(res.raw_sha256)[:12]} {extra}")
        results.append((sid, "ok", "ok", n, elapsed, ""))
    db.finish_run(conn, run_id, "success" if failures == 0 else "partial")
    print(f"\nprobe 汇总: {len(results) - failures}/{len(results)} 通过  run_id={run_id}")
    if args.strict and failures:
        return 1
    return 0


def _probe_dj_nav(text: str) -> list[dict]:
    """蛋卷净值探针：复用跨源校验的解析器，返回逐日行。"""
    from .validate.cross import _parse_dj
    rows = _parse_dj(text)
    if not rows:
        raise parsers.ParseError("蛋卷净值 items 为空")
    return [{"date": d} for d in rows]


def _probe_lsjz(text: str) -> list[dict]:
    from .validate.cross import _parse_lsjz
    rows = _parse_lsjz(text)
    if not rows:
        raise parsers.ParseError("lsjz 净值为空")
    return [{"date": d} for d in rows]


def _count_groups(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        g = collect_mod.type_group(r.get("type_text"))
        out[g] = out.get(g, 0) + 1
    return out


def cmd_collect(args: argparse.Namespace) -> int:
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    args.types = ",".join(fundtypes.resolve(args.types, cfg))
    run_id = db.start_run(conn, args.mode, cfg.config_hash, cfg.code_version,
                          notes=f"domain={args.domain} types={args.types}")
    fetcher = Fetcher(cfg, conn, run_id)
    business_date = db.today_cn()
    ok = True
    if args.domain == "directory":
        out = collect_mod.collect_directory(fetcher, business_date)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        ok = out["ok"]
    elif args.domain == "status":
        out = collect_mod.collect_status(fetcher, business_date)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        ok = out["ok"]
    elif args.domain == "index":
        out = collect_mod.collect_index(fetcher)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        ok = out["ok"]
    elif args.domain == "rank":
        out = collect_mod.collect_rank(fetcher, business_date, page_size=args.page_size,
                                       max_pages=args.max_pages)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        ok = out["ok"]
    elif args.domain in {"nav", "profile", "holding", "fee", "scale", "manager"}:
        codes = collect_mod.select_codes(conn, args.types)
        if getattr(args, "codes", None):
            wanted = {c.strip() for c in str(args.codes).split(",") if c.strip()}
            codes = [c for c in codes if c in wanted]
        if getattr(args, "only_buyable", False):
            # 只采集"当前可申购"的基金：L2 之后才可能进入正式筛选，其余基金在此层已被淘汰，
            # 采集它们的管理费/规模不会改变任何决策（未来转可申购时增量采集即可）。
            buyable = {r["fcode"] for r in conn.execute(
                "SELECT fcode FROM fund_buyable_daily WHERE buyable_state='confirmed_yes'"
                " AND business_date=(SELECT MAX(business_date) FROM fund_buyable_daily)")}
            codes = [c for c in codes if c in buyable]
        if getattr(args, "skip_collected", False):
            skip_sql = {
                "profile": "SELECT DISTINCT code FROM src_jbgk_profile",
                "holding": "SELECT DISTINCT code FROM src_zqcc_holding",
                "fee": "SELECT fcode AS code FROM fund_fee_meta",
                # nav 用 fund_nav_daily 判"已采过"：新增 universe（如指数债基）只补缺的，
                # 不会把 7,000+ 只已有全历史的基金重拉一遍。
                "nav": "SELECT DISTINCT fcode AS code FROM fund_nav_daily",
                "scale": "SELECT DISTINCT fcode AS code FROM fund_scale_history",
                "manager": "SELECT DISTINCT fcode AS code FROM fund_manager_tenure",
            }[args.domain]
            done = {r["code"] for r in conn.execute(skip_sql)}
            codes = [c for c in codes if c not in done]
        if getattr(args, "boards", None):
            boards = [b.strip() for b in str(args.boards).split(",") if b.strip()]
            run_row = conn.execute(
                "SELECT run_id FROM screen_run ORDER BY created_at DESC, run_id DESC LIMIT 1"
            ).fetchone()
            if run_row is None:
                print("没有可用的 screen_run，无法按榜单过滤")
                return 2
            marks = ",".join("?" for _ in boards)
            picked = {r["fcode"] for r in conn.execute(
                f"SELECT DISTINCT fcode FROM screen_pick WHERE run_id=? AND board IN ({marks})",
                (run_row["run_id"], *boards))}
            codes = [c for c in codes if c in picked]
        if args.limit:
            codes = codes[:args.limit]
        years: list[int] = []
        if getattr(args, "years", None):
            if args.domain != "holding":
                print("--years 只适用于 holding 域")
                return 2
            for part in str(args.years).split(","):
                part = part.strip()
                if "-" in part:
                    lo, hi = part.split("-", 1)
                    years.extend(range(int(lo), int(hi) + 1))
                elif part:
                    years.append(int(part))
        print(f"目标 {args.domain}: {len(codes)} 只（types={args.types}"
              + (f"，years={years}" if years else "") + "）")
        counters = {"ok": 0, "fail": 0, "nav_rows": 0, "revisions": 0,
                    "periods": 0, "tenures": 0, "holding_rows": 0}
        failures: dict[str, int] = {}
        for i, code in enumerate(codes, 1):
            if args.domain == "nav":
                out = collect_mod.collect_pzd_one(fetcher, code, business_date,
                                                  force=getattr(args, "force", False))
                counters["nav_rows"] += out.get("nav_rows", 0)
                counters["revisions"] += out.get("revisions", 0)
            elif args.domain == "profile":
                out = collect_mod.collect_jbgk_one(fetcher, code, business_date)
            elif args.domain == "fee":
                out = collect_mod.collect_fee_one(fetcher, code)
            elif args.domain == "scale":
                out = collect_mod.collect_scale_one(fetcher, code,
                                                    force=getattr(args, "force", False))
                counters["periods"] += out.get("periods", 0)
            elif args.domain == "manager":
                out = collect_mod.collect_manager_one(fetcher, code, business_date,
                                                      force=getattr(args, "force", False))
                counters["tenures"] += out.get("tenures", 0)
            elif years:
                out = {"ok": True, "status": "ok", "code": code, "periods": 0, "rows": 0}
                for year in years:
                    one = collect_mod.collect_zqcc_year(
                        fetcher, code, year, force=getattr(args, "force", False))
                    out["periods"] += one.get("periods", 0)
                    out["rows"] += one.get("rows", 0)
                    if not one.get("ok"):
                        out.update(ok=False, status=one.get("status", "?"))
                        break
                counters["holding_rows"] += out.get("rows", 0)
                counters["periods"] += out.get("periods", 0)
            else:
                out = collect_mod.collect_zqcc_one(fetcher, code)
                counters["holding_rows"] += out.get("rows", 0)
                counters["periods"] += 1 if out.get("period") else 0
            if out.get("ok"):
                counters["ok"] += 1
            else:
                counters["fail"] += 1
                failures[out.get("status", "?")] = failures.get(out.get("status", "?"), 0) + 1
            if i % 100 == 0 or i == len(codes):
                print(f"  [{i}/{len(codes)}] ok={counters['ok']} fail={counters['fail']} "
                      f"nav_rows={counters['nav_rows']} rev={counters['revisions']}")
        print(json.dumps({**counters, "failures": failures}, ensure_ascii=False, indent=2))
        ok = counters["fail"] == 0
    else:
        print(f"未知 domain: {args.domain}")
        return 2
    db.finish_run(conn, run_id, "success" if ok else "partial")
    print(f"run_id={run_id}")
    return 0 if ok else 1


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = load_config()
    conn = db.connect(cfg)
    for table, count in db.stats(conn).items():
        print(f"{table:28s} {count:>10d}")
    return 0


def cmd_types(args: argparse.Namespace) -> int:
    """列出类型插件注册表：哪些能跑、跑的是哪个 profile、还差什么（ADR-077）。"""
    cfg = load_config()
    rows = fundtypes.describe()
    print(f"当前 universe.types = {list(fundtypes.resolve(cfg=cfg))}"
          f"（config/app.yaml）")
    print()
    print(f"{'code':8s} {'label':8s} {'状态':6s} {'profile':10s} {'风格':4s} {'榜单':4s} 前缀")
    for r in rows:
        state = "已实现" if r["implemented"] else "未实现"
        print(f"{r['code']:8s} {r['label']:8s} {state:6s} {str(r['screen_profile'] or '-'):10s}"
              f" {r['styles']:>4d} {r['boards']:>4d} {'/'.join(r['prefixes'])}")
    gaps = [r for r in rows if r["gaps"]]
    if gaps:
        print("\n缺口：")
        for r in gaps:
            print(f"  {r['code']}（{r['label']}）")
            for g in r["gaps"]:
                print(f"    - {g}")
    return 0


def cmd_fuse(args: argparse.Namespace) -> int:
    from . import fuse
    from .fuse import coverage as coverage_mod
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    business_date = args.business_date or db.today_cn()
    run_id = db.start_run(conn, "fuse", cfg.config_hash, cfg.code_version,
                          notes=f"business_date={business_date}")
    types = set(fundtypes.resolve(args.types, cfg))
    report = fuse.run_fuse(conn, cfg, run_id, business_date, types)
    out_path = Path(cfg.get("paths", "state_dir", default="var/state")) / f"coverage_{run_id}.json"
    coverage_mod.write_report(report, out_path)
    db.finish_run(conn, run_id, "success")
    print(json.dumps({k: report[k] for k in ("funds_ok", "funds_quarantined", "by_style",
                                             "style_confidence", "scale_source",
                                             "profile_conflicts", "buyable_state",
                                             "lockup_type", "nav_coverage")},
                     ensure_ascii=False, indent=2))
    print(f"stages={json.dumps(report['stages'], ensure_ascii=False)}")
    print(f"report={out_path}")
    print(f"run_id={run_id}")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    from .metrics import engine as metrics_engine
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    from .metrics import contract as metric_contract
    formula = args.formula or cfg.get("risk", "formula_contract", default=metric_contract.FORMULA_CONTRACT)
    # 契约即公式定义：配置与代码里的契约版本必须一致，否则会把新公式写成旧版本号（gen12 的真实事故）。
    if formula != metric_contract.FORMULA_CONTRACT and not getattr(args, "allow_contract_mismatch", False):
        print(f"契约不一致：配置/参数={formula} 代码={metric_contract.FORMULA_CONTRACT}；"
              f"先改 config/app.yaml 的 risk.formula_contract，或加 --allow-contract-mismatch 显式覆盖")
        return 2
    clause, tparams = fundtypes.where(cfg=cfg, column="f.type_group")
    as_of = args.as_of or metrics_engine.market_as_of(conn, types=fundtypes.resolve(cfg=cfg)) \
        or db.today_cn()
    codes = [r["fcode"] for r in conn.execute(
        "SELECT DISTINCT f.fcode FROM fund f JOIN fund_nav_daily n ON n.fcode=f.fcode"
        f" WHERE {clause} ORDER BY f.fcode", tparams)]
    if args.limit:
        codes = codes[: args.limit]
    print(f"目标 {len(codes)} 只，as_of={as_of} formula={formula}")
    t0 = time.monotonic()
    ok = fail = 0
    reasons: dict[str, int] = {}
    for i, code in enumerate(codes, 1):
        try:
            res = metrics_engine.compute_fund(conn, cfg, code, as_of, only_missing=args.changed_only)
        except Exception as exc:                      # noqa: BLE001 - 单只失败不中断全量
            fail += 1
            reasons[type(exc).__name__] = reasons.get(type(exc).__name__, 0) + 1
            continue
        if res.get("ok"):
            ok += 1
        else:
            fail += 1
            reasons[res.get("reason", "unknown")] = reasons.get(res.get("reason", "unknown"), 0) + 1
        for r in res.get("undefined") or []:
            key = r.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
        if i % 200 == 0 or i == len(codes):
            print(f"  [{i}/{len(codes)}] ok={ok} fail={fail} {time.monotonic() - t0:.1f}s")
    conn.commit()
    print(json.dumps({"computed": ok, "failed": fail, "undefined_reasons": reasons},
                     ensure_ascii=False, indent=2))
    # P0-2：指标重算后同步刷新可比性旗标；失败不阻断指标本身。
    try:
        from .metrics import comparability
        comp = comparability.refresh(conn, as_of=as_of, formula_contract=formula)
        print(json.dumps({"comparability": comp}, ensure_ascii=False, indent=2))
    except Exception as exc:                          # noqa: BLE001 - 旗标失败不阻断指标
        print(f"comparability refresh failed: {type(exc).__name__}: {exc}")
    _warn_stale_contracts(conn, "fund_metric", formula)
    return 0


def cmd_snapshot_metrics(args: argparse.Namespace) -> int:
    from .audit import metric_snapshot
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    as_of = args.as_of or db.today_cn()
    if args.quarters:
        out = metric_snapshot.snapshot_quarters(conn, cfg, as_of, args.quarters,
                                                limit=args.limit)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        out = metric_snapshot.snapshot_as_of(conn, cfg, as_of, limit=args.limit)
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_comparability(args: argparse.Namespace) -> int:
    """P0-2：只重算指标可比性旗标，不重算指标本身（回填/排障用）。"""
    from .metrics import comparability
    from .metrics import contract as metric_contract
    from .metrics import engine as metrics_engine
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    formula = args.formula or cfg.get("risk", "formula_contract", default=metric_contract.FORMULA_CONTRACT)
    as_of = args.as_of or metrics_engine.market_as_of(conn, types=fundtypes.resolve(cfg=cfg)) \
        or db.today_cn()
    res = comparability.refresh(conn, as_of=as_of, formula_contract=formula)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    from .metrics import engine as metrics_engine
    from .metrics import tail as tail_mod
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    clause, tparams = fundtypes.where(cfg=cfg, column="f.type_group")
    as_of = args.as_of or metrics_engine.market_as_of(conn, types=fundtypes.resolve(cfg=cfg)) \
        or db.today_cn()
    codes = [r["fcode"] for r in conn.execute(
        "SELECT DISTINCT f.fcode FROM fund f JOIN fund_nav_daily n ON n.fcode=f.fcode"
        f" WHERE {clause} ORDER BY f.fcode", tparams)]
    if args.limit:
        codes = codes[: args.limit]
    print(f"目标 {len(codes)} 只，as_of={as_of} window=1095d contract={tail_mod.TAIL_CONTRACT}")
    t0 = time.monotonic()
    ok = fail = 0
    reasons: dict[str, int] = {}
    completeness: list[float] = []
    for i, code in enumerate(codes, 1):
        try:
            res = tail_mod.compute_fund(conn, cfg, code, as_of)
        except Exception as exc:                      # noqa: BLE001 - 单只失败不中断全量
            fail += 1
            reasons[type(exc).__name__] = reasons.get(type(exc).__name__, 0) + 1
            continue
        if res.get("ok"):
            ok += 1
            completeness.append(float(res.get("completeness") or 0.0))
        else:
            fail += 1
            reasons[res.get("reason", "unknown")] = reasons.get(res.get("reason", "unknown"), 0) + 1
        for r in res.get("undefined") or []:
            key = r.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
        if i % 200 == 0 or i == len(codes):
            print(f"  [{i}/{len(codes)}] ok={ok} fail={fail} {time.monotonic() - t0:.1f}s")
    conn.commit()
    avg = round(sum(completeness) / len(completeness), 4) if completeness else 0.0
    print(json.dumps({"computed": ok, "failed": fail, "avg_completeness": avg,
                      "reasons": reasons}, ensure_ascii=False, indent=2))
    _warn_stale_contracts(conn, "fund_tail_metric", tail_mod.TAIL_CONTRACT)
    return 0


def _warn_stale_contracts(conn, table: str, contract: str) -> None:
    """换契约后旧行不会自动消失：这里主动提示，避免漏写契约条件的 JOIN 又出重复行。"""
    row = conn.execute("SELECT COUNT(*) FROM " + table + " WHERE formula_contract IS NOT ?",
                       (contract,)).fetchone()
    if row and row[0]:
        print(f"注意：{table} 里还有 {row[0]} 行非当前契约（{contract}）的历史行；"
              f"漏写契约条件的 JOIN 会让同一只基金出两行。"
              f"运行 fundana prune-contracts --apply 清理（ADR-027 决策三）")


def cmd_prune_contracts(args: argparse.Namespace) -> int:
    """删除非当前公式契约的历史指标行（ADR-027 决策三）。

    fund_metric / fund_tail_metric 都按 (fcode, 周期, formula_contract) 多版本共存，
    换契约后旧行不会自动消失；只要有一处 JOIN 漏写契约条件，同一只基金就会出两行
    （gen12 的 1、1、2、2 重复行事故）。历史口径靠 freeze/ 与发布 revision 复现，
    因此库里只保留当前契约。默认只报告，加 --apply 才真删。
    """
    from .metrics import contract as metric_contract
    from .metrics import tail as tail_mod
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    risk = cfg.get("risk", "formula_contract", default=metric_contract.FORMULA_CONTRACT)
    keep = {"fund_metric": risk, "fund_tail_metric": tail_mod.TAIL_CONTRACT}
    report: dict = {}
    for table, contract in keep.items():
        before = [dict(r) for r in conn.execute(
            "SELECT formula_contract, COUNT(*) AS n FROM " + table + " GROUP BY 1 ORDER BY 1")]
        stale = sum(r["n"] for r in before if r["formula_contract"] != contract)
        entry = {"keep": contract, "before": before, "stale_rows": stale}
        if args.apply and stale:
            conn.execute("DELETE FROM " + table + " WHERE formula_contract IS NOT ?", (contract,))
            conn.commit()
            entry["after"] = [dict(r) for r in conn.execute(
                "SELECT formula_contract, COUNT(*) AS n FROM " + table + " GROUP BY 1 ORDER BY 1")]
        report[table] = entry
    print(json.dumps({"applied": bool(args.apply), "tables": report},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_replay_screen(args: argparse.Namespace) -> int:
    """P0-1：按 screen_run_input 重放 base_gate 与评分，不写库。"""
    from .screen import pit
    cfg = load_config()
    conn = db.connect(cfg)
    run_id = args.run_id
    if not run_id:
        row = conn.execute("SELECT run_id FROM screen_run WHERE status='done'"
                           " ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            print("没有可重放的 screen_run")
            return 2
        run_id = row["run_id"]
    nav_age = int((cfg.get("quality", default={}) or {}).get("nav_age_limit_days", 10))
    out = pit.replay(conn, run_id, nav_age_limit=nav_age)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not out.get("score_mismatches") and not out.get("gate_mismatches") else 1


def cmd_lifecycle_import(args: argparse.Namespace) -> int:
    """P0-1：导入外部清盘/合并名单；缺失字段直接报错，不猜。"""
    from . import lifecycle
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    out = lifecycle.import_csv(conn, args.path)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not out["errors"] else 1


def cmd_lifecycle_collect(args: argparse.Namespace) -> int:
    """P0-1：从证监会 EID 公开公告采集清盘/合并生命周期。"""
    from . import lifecycle_csrc
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    run_id = db.start_run(conn, "lifecycle", cfg.config_hash, cfg.code_version,
                          notes=f"csrc_eid {args.start}~{args.end or 'today'}")
    fetcher = Fetcher(cfg, conn, run_id)
    try:
        out = lifecycle_csrc.collect(
            fetcher, start=args.start, end=args.end, types=args.types or None,
            parse_pdfs=not args.no_pdf, force=args.force, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        db.finish_run(conn, run_id, "failed", notes=f"lifecycle_exception: {exc}")
        raise
    db.finish_run(conn, run_id, "success" if out.get("ok") else "partial",
                  notes=f"rows={out.get('rows')} announcements={out.get('announcements')}")
    out["run_id"] = run_id
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_lifecycle_status(args: argparse.Namespace) -> int:
    from . import lifecycle
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    as_of = args.as_of or db.today_cn()
    out = {"fcode": args.fcode, "as_of": as_of,
           "status": lifecycle.status_as_of(conn, args.fcode, as_of),
           "investable": lifecycle.is_investable(conn, args.fcode, as_of),
           "counts": lifecycle.counts(conn)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    from .screen import engine as screen_engine
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    formula = cfg.get("risk", "formula_contract", default="fundana-risk-2026.11.1")
    business_date = args.business_date or db.today_cn()
    run_id = db.start_run(conn, "screen", cfg.config_hash, cfg.code_version,
                          notes=f"profile={args.profile}")
    batch_id = getattr(args, "batch", None) or business_date
    result = screen_engine.run_screen(conn, cfg, run_id, business_date, formula,
                                      batch_id=batch_id)
    _write_freeze(cfg, conn, run_id, business_date)
    db.finish_run(conn, run_id, "success")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"run_id={run_id}")
    return 0


def cmd_shadow(args: argparse.Namespace) -> int:
    from .screen import shadow
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    formula = cfg.get("risk", "formula_contract", default="fundana-risk-2026.11.1")
    out = shadow.run_shadow(conn, cfg, formula, window_days=args.window, limit=args.limit)
    print(json.dumps(out["meta"], ensure_ascii=False, indent=2))
    print(f"预览 {len(out['rows'])} 行（research_only，不进正式发布）")
    return 0


def cmd_shadow_screen(args: argparse.Namespace) -> int:
    """固定线风格化/动态层影子对比：只读 screen_decision，不写榜单。"""
    from .screen import style_shadow
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    run_id = args.run_id
    if not run_id:
        row = conn.execute(
            "SELECT run_id FROM screen_run WHERE status='done' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            print("没有 status=done 的 screen_run", file=sys.stderr)
            return 2
        run_id = row["run_id"]
    out = {
        "run_id": run_id,
        "fixed_style": style_shadow.fixed_style_shadow(conn, run_id),
        "dynamic": style_shadow.dynamic_shadow(
            conn, run_id, quantile=args.quantile, k=args.k),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_shadow_rank(args: argparse.Namespace) -> int:
    from .screen import shadow_rank
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    out = shadow_rank.run(conn, cfg, args.run_id)
    meta = out["meta"]
    print(json.dumps({k: meta[k] for k in ("run_id", "business_date", "core10", "core50")},
                     ensure_ascii=False, indent=2))
    print(f"shadow_rank rows={len(out['rows'])} research_only，不进正式发布")
    return 0


def cmd_shadow_forward(args: argparse.Namespace) -> int:
    """冻结并评估 v9/v10 core10/core50 的前瞻观察（research_only）。"""
    from . import shadow_forward
    from .screen import shadow_rank
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    out: dict = {}
    cohort_id = args.cohort
    if not cohort_id:
        shadow = shadow_rank.run(conn, cfg, args.run_id)
        out["recorded"] = shadow_forward.record(
            conn, cfg, args.run_id, shadow=shadow,
            publication_id=args.publication_id, business_date=args.business_date)
        cohort_id = out["recorded"]["cohort_id"]
    out["evaluated"] = shadow_forward.evaluate(conn, cfg, cohort_id=cohort_id)
    summary = shadow_forward.summary(conn, cohort_id)
    if not args.rows:
        summary.pop("rows", None)
        summary.pop("comparisons", None)
    out["summary"] = summary
    out["cohort_list"] = shadow_forward.cohort_list(conn)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def _write_freeze(cfg, conn, run_id: str, business_date: str) -> str:
    """固化本轮归一化输入，保证 replay --offline 可逐字段复算。"""
    import gzip
    import hashlib
    freeze_dir = Path(cfg.freeze_dir) / run_id
    freeze_dir.mkdir(parents=True, exist_ok=True)
    path = freeze_dir / "inputs.jsonl.gz"
    rows = conn.execute(
        "SELECT d.fcode, d.style_code, d.layer_reached, d.passed, d.reason_codes_json,"
        " d.scores_json, d.confidence, f.name, f.estab_date, p.asset_scale_billion,"
        " p.recurring_fee_pct FROM screen_decision d JOIN fund f ON f.fcode=d.fcode"
        " LEFT JOIN fund_profile p ON p.fcode=d.fcode WHERE d.run_id=? ORDER BY d.fcode",
        (run_id,))
    h = hashlib.sha256()
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            line = json.dumps(dict(r), ensure_ascii=False, sort_keys=True)
            fh.write(line + "\n")
            h.update(line.encode("utf-8"))
    # P0-1：完整 PIT 输入单独冻结一份（含 metric/pcts/scores），供 replay-screen 逐字段重放。
    pit_path = freeze_dir / "inputs_pit.jsonl.gz"
    pit_rows = conn.execute(
        "SELECT * FROM screen_run_input WHERE run_id=? ORDER BY fcode", (run_id,))
    ph = hashlib.sha256()
    pit_n = 0
    with gzip.open(pit_path, "wt", encoding="utf-8") as fh:
        for r in pit_rows:
            line = json.dumps(dict(r), ensure_ascii=False, sort_keys=True)
            fh.write(line + "\n")
            ph.update(line.encode("utf-8"))
            pit_n += 1
    manifest = {"run_id": run_id, "business_date": business_date,
                "inputs_sha256": h.hexdigest(), "path": str(path),
                "pit_sha256": ph.hexdigest(), "pit_path": str(pit_path), "pit_rows": pit_n,
                "created_at": db.now_iso(), "rows": conn.execute(
                    "SELECT COUNT(*) AS c FROM screen_decision WHERE run_id=?", (run_id,)).fetchone()["c"]}
    (freeze_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    return str(path)


def tracker_business_date(conn, run_id: str) -> str:
    """P2-0：tracker 用本次 run 的 nav_as_of，而不是最新 buyable 快照日。

    buyable 快照可能比指标日更新（11-04 已抓、指标只到 11-02）。若用
    MAX(fund_buyable_daily.business_date)，tracker 会把未来一天的行情
    当成已实现。run 缺日期时才退回 buyable 快照/今天。
    """
    run_row = conn.execute(
        "SELECT business_date, nav_as_of FROM screen_run WHERE run_id=?", (run_id,)).fetchone()
    if run_row:
        value = run_row["nav_as_of"] or run_row["business_date"]
        if value:
            return str(value)
    row = conn.execute(
        "SELECT MAX(business_date) AS d FROM fund_buyable_daily").fetchone()
    return str((row["d"] if row else None) or db.today_cn())


def cmd_publish(args: argparse.Namespace) -> int:
    from . import publish
    cfg = load_config()
    conn = db.connect(cfg)
    run_id = args.run_id
    if not run_id:
        row = conn.execute("SELECT run_id FROM screen_run ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            print("没有可发布的 screen_run")
            return 2
        run_id = row["run_id"]
    # P1-10：同一 screen_run 重复发布必须显式 --allow-republish，避免误触
    # 生成一个内容相同、generation 递增的“新发布”，把跟踪榜/换手统计搅乱。
    if not getattr(args, "allow_republish", False):
        cur = conn.execute(
            "SELECT run_id, generation FROM publication ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        if cur is not None and cur["run_id"] == run_id:
            print(f"[FAIL] screen_run {run_id} 已是当前发布（generation {cur['generation']}）；"
                  "如确需重发，请加 --allow-republish")
            return 1
    built = publish.build(conn, cfg, run_id)
    try:
        out = publish.commit(cfg, built, run_id, conn, allow_older=args.allow_older)
    except publish.PublicationError as exc:
        print(f"[FAIL] {exc}")
        return 1
    from . import tracker as tracker_mod
    picks = [dict(r) for r in conn.execute(
        "SELECT p.fcode, p.board, p.rank, p.formal_score, p.confidence, d.style_code"
        " FROM screen_pick p LEFT JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
        " WHERE p.run_id=?", (run_id,))]
    business_date = tracker_business_date(conn, run_id)
    trk = tracker_mod.update_after_publish(conn, built["revision_id"], business_date, run_id,
                                           picks, generation=out["generation"])
    print(json.dumps({"run_id": run_id, "revision": built["revision_path"],
                      "generation": out["generation"], "counts": built["manifest"]["counts"],
                      "tracker": trk}, ensure_ascii=False, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from . import publish
    cfg = load_config()
    try:
        res = publish.verify(cfg, full=not args.fast)
    except publish.PublicationError as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


def cmd_replay(args: argparse.Namespace) -> int:
    """离线复算：不触网，从 fund_nav_daily 重算指标并与已存 fund_metric 逐字段比对。"""
    from .metrics import engine as metrics_engine
    cfg = load_config()
    conn = db.connect(cfg)
    from .metrics import contract as metric_contract
    formula = args.formula or cfg.get("risk", "formula_contract", default=metric_contract.FORMULA_CONTRACT)
    # 契约即公式定义：配置与代码里的契约版本必须一致，否则会把新公式写成旧版本号（gen12 的真实事故）。
    if formula != metric_contract.FORMULA_CONTRACT and not getattr(args, "allow_contract_mismatch", False):
        print(f"契约不一致：配置/参数={formula} 代码={metric_contract.FORMULA_CONTRACT}；"
              f"先改 config/app.yaml 的 risk.formula_contract，或加 --allow-contract-mismatch 显式覆盖")
        return 2
    as_of = args.as_of or metrics_engine.market_as_of(conn, types=fundtypes.resolve(cfg=cfg))
    codes = [r["fcode"] for r in conn.execute(
        "SELECT DISTINCT fcode FROM fund_metric WHERE formula_contract=? ORDER BY fcode", (formula,))]
    if args.limit:
        codes = codes[: args.limit]
    fields = ("n_obs", "total_return_pct", "annual_dec", "vol_dec", "positive_ratio_pct",
              "mdd_dec", "sharpe", "sortino", "calmar_dec", "var95_dec", "cvar5_dec")
    diffs = checked = 0
    for code in codes:
        stored = {r["period_days"]: dict(r) for r in conn.execute(
            "SELECT * FROM fund_metric WHERE fcode=? AND formula_contract=?", (code, formula))}
        fresh = metrics_engine.compute_fund(conn, cfg, code, as_of, dry_run=True)
        for period, row in (fresh.get("dry") or {}).items():
            old = stored.get(period)
            if not old:
                continue
            for f in fields:
                checked += 1
                a, b = old.get(f), row.get(f)
                if a is None and b is None:
                    continue
                if a is None or b is None or abs(float(a) - float(b)) > 1e-9:
                    diffs += 1
                    if diffs <= 10:
                        print(f"  DIFF {code} {period}d {f}: stored={a} recomputed={b}")
    print(json.dumps({"offline": True, "funds": len(codes), "fields_checked": checked,
                      "diffs": diffs, "as_of": as_of, "formula": formula},
                     ensure_ascii=False, indent=2))
    return 0 if diffs == 0 else 1


def cmd_daily(args: argparse.Namespace) -> int:
    from . import daily as daily_mod
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    out = daily_mod.run(cfg, conn, types=args.types, force=args.force,
                        skip_publish=args.skip_publish, verbose=not args.quiet)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if out.get("status") in ("ok", "controlled_skip") else 1


def cmd_notify(args: argparse.Namespace) -> int:
    from . import notify
    cfg = load_config()
    title = args.title or (f"systemd 单元失败: {args.unit}" if args.unit else "Fund-ANA 告警")
    message = args.message or (f"unit {args.unit} 进入 failed 状态" if args.unit else "")
    out = notify.send(cfg, title, message, unit=args.unit)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("logged") else 1


def cmd_maintenance(args: argparse.Namespace) -> int:
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    tasks = [t.strip() for t in (args.tasks or "").split(",") if t.strip()] or None
    out = maintenance.run(cfg, conn, tasks=tasks, force=args.force, limit=args.limit,
                          run_id=args.run_id)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def cmd_db_maintain(args: argparse.Namespace) -> int:
    cfg = load_config()
    conn = db.connect(cfg)
    out = db_maint.run(conn, cfg.db_path,
                       do_checkpoint=not args.no_checkpoint,
                       do_vacuum=args.vacuum,
                       do_integrity=args.integrity_check,
                       checkpoint_mode=args.checkpoint_mode)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    ok = out["optimize"].get("ok") and out.get("checkpoint", {}).get("ok", True)
    if args.integrity_check:
        ok = ok and out["quick_check"].get("ok")
    return 0 if ok else 1


def cmd_scheduler(args: argparse.Namespace) -> int:
    from . import scheduler as scheduler_mod
    if args.once:
        print(json.dumps(scheduler_mod.run_once(args.at, force=args.force),
                         ensure_ascii=False, indent=2, default=str))
        return 0
    return scheduler_mod.loop(args.at)


def cmd_tracker(args: argparse.Namespace) -> int:
    from . import tracker as tracker_mod
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    if args.rebuild_from_picks:
        # 从历史 screen_run 重建（仅用于把已有发布补记进跟踪榜；仍然按 publication_id 去重）
        rows = conn.execute(
            "SELECT publication_id, run_id, generation, created_at, nav_as_of FROM publication"
            " ORDER BY generation").fetchall()
        done = []
        for r in rows:
            picks = [dict(x) for x in conn.execute(
                "SELECT p.fcode, p.board, p.rank, p.formal_score, p.confidence, d.style_code"
                " FROM screen_pick p LEFT JOIN screen_decision d"
                " ON d.run_id=p.run_id AND d.fcode=p.fcode WHERE p.run_id=?", (r["run_id"],))]
            bd = r["nav_as_of"]
            if not bd:
                bd = conn.execute("SELECT MAX(business_date) AS d FROM fund_buyable_daily").fetchone()["d"]
            done.append(tracker_mod.update_after_publish(
                conn, r["publication_id"], bd, r["run_id"], picks,
                generation=r["generation"], created_at=r["created_at"]))
        print(json.dumps(done, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.rebuild:
        # 按业务日重算：同日多份快照只留最后一份，出现次数不再被同日重发放大
        print(json.dumps(tracker_mod.rebuild(conn), ensure_ascii=False, indent=2, default=str))
        return 0
    if args.fcode:
        print(json.dumps(tracker_mod.history(conn, args.fcode), ensure_ascii=False,
                         indent=2, default=str))
        return 0
    print(json.dumps(tracker_mod.current(conn, board=args.board, limit=args.limit),
                     ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """跨源核对（§16.1）：同一事实用第二个端点再取一遍，不一致就留双值。"""
    from .validate import cross as cross_mod
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    if not args.cross:
        print("validate 目前只实现 --cross（跨源核对）", file=sys.stderr)
        return 2
    business_date = args.business_date or db.today_cn()
    run_id = db.start_run(conn, "validate", cfg.config_hash, cfg.code_version, notes="cross")
    out: dict = {"daily": cross_mod.check_daily(conn, business_date, cfg)}
    if args.sample:
        clause, tparams = fundtypes.where(cfg=cfg, column="type_group")
        codes = [r[0] for r in conn.execute(
            f"SELECT fcode FROM fund WHERE {clause} AND fcode IN"
            " (SELECT DISTINCT fcode FROM fund_nav_daily) ORDER BY RANDOM() LIMIT ?",
            (*tparams, args.sample))]
        out["sample"] = cross_mod.check_sample(conn, cfg, codes, run_id)
    out["period"] = cross_mod.check_period_returns(conn, run_id, cfg)
    out["summary"] = cross_mod.summarize(conn, business_date)
    db.finish_run(conn, run_id, "success")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_walkforward(args: argparse.Namespace) -> int:
    """Walkforward：历史 as-of 重建榜单，检验评分与排序键的前瞻表现（P2-1）。"""
    from .audit import walkforward as wf
    cfg = load_config()
    conn = db.connect(cfg)
    years = [int(y) for y in str(args.years).split(",") if y.strip()]
    result = wf.run(conn, cfg, years, top=args.top, horizon_days=args.horizon,
                    style_asof=args.style_asof, include_enhanced=args.enhanced,
                    include_portfolio=args.portfolio,
                    portfolio_weighting=args.portfolio_weighting,
                    include_liquidated=args.include_liquidated)
    print(wf.render(result))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细写入 {path}")
    return 0


def cmd_phase2(args: argparse.Namespace) -> int:
    """Phase 2 影子研究：长期权重、暴露、杠杆与基准匹配（只读）。"""
    from .audit import phase2
    cfg = load_config()
    conn = db.connect(cfg)
    result = phase2.diagnose(conn, cfg, args.run_id)
    print(phase2.render(result))
    if not args.no_write:
        json_path, md_path = phase2.write_outputs(result, cfg.report_root)
        print("\n明细写入 %s" % json_path)
        print("报告写入 %s" % md_path)
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    """产品告警：对当前发布做净值/经理/规模/回撤/风格漂移扫描。"""
    from . import alerts as alerts_mod
    cfg = load_config()
    conn = db.connect(cfg)
    boards = [args.board] if args.board else alerts_mod.DEFAULT_BOARDS
    result = alerts_mod.scan(conn, as_of=args.as_of, boards=boards)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(alerts_mod.render(result))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细写入 {path}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    """经理/规模事件研究：事件分桶后的前瞻收益。"""
    from .audit import events as events_mod
    cfg = load_config()
    conn = db.connect(cfg)
    years = [int(y) for y in str(args.years).split(",") if y.strip()]
    result = events_mod.run(conn, cfg, years, horizon_days=args.horizon)
    print(events_mod.render(result))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细写入 {path}")
    return 0


def cmd_turnover(args: argparse.Namespace) -> int:
    """榜单换手（P2-1b）：两次发布之间进了谁、出了谁、为什么出。"""
    from .audit import turnover as turnover_mod
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    if args.history:
        result = turnover_mod.history(conn, limit=args.limit)
    else:
        cur = args.run_id
        if not cur:
            row = conn.execute("SELECT run_id FROM screen_run WHERE formal_count>0"
                               " ORDER BY created_at DESC LIMIT 1").fetchone()
            if not row:
                print("没有可用的 screen_run，先跑 screen", file=sys.stderr)
                return 2
            cur = row[0]
        prev = args.prev_run_id
        if not prev:
            row = conn.execute(
                "SELECT p.run_id FROM publication p JOIN screen_run r ON r.run_id=p.run_id"
                " WHERE p.status='committed' AND p.run_id<>? ORDER BY p.generation DESC LIMIT 1",
                (cur,)).fetchone()
            if not row:
                print("没有上一份发布可比", file=sys.stderr)
                return 2
            prev = row[0]
        result = turnover_mod.compare(conn, cur, prev)
    print(turnover_mod.render(result))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细写入 {path}")
    return 0


def cmd_exposure(args: argparse.Namespace) -> int:
    """持仓风格归因：各榜单的券种暴露画像（P2-2）。"""
    from .audit import exposure as exposure_mod
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    run_id = args.run_id
    if not run_id:
        row = conn.execute("SELECT run_id FROM screen_run WHERE formal_count>0"
                           " ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            print("没有可用的 screen_run，先跑 screen", file=sys.stderr)
            return 2
        run_id = row[0]
    result = exposure_mod.run(conn, run_id, exposure_mod.BOARDS)
    print(exposure_mod.render(result))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细写入 {path}")
    return 0


def cmd_forward(args: argparse.Namespace) -> int:
    """前瞻验证（§15）：冻结 cohort → 用 cohort 之后才发生的净值评估。"""
    from . import forward as fwd
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    out: dict = {}
    if args.create:
        row = conn.execute("SELECT run_id FROM screen_run ORDER BY created_at DESC, rowid DESC"
                           " LIMIT 1").fetchone()
        if not row:
            print("没有可用的 screen_run，先跑 screen", file=sys.stderr)
            return 2
        pub = conn.execute("SELECT publication_id FROM publication ORDER BY rowid DESC LIMIT 1").fetchone()
        out["created"] = fwd.create_cohort(
            conn, cfg, args.run_id or row[0], business_date=args.business_date,
            publication_id=(pub[0] if pub else None), source=args.source)
    out["evaluated"] = fwd.evaluate(conn, cfg, cohort_id=args.cohort)
    summary = fwd.summary(conn, args.cohort)
    if not args.rows:
        summary.pop("rows", None)
    out["summary"] = summary
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_fees(args: argparse.Namespace) -> int:
    """费用阶梯回放：从 raw_object 重解析，不重新联网。"""
    from .fees import rebuild
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    out = rebuild.rebuild_from_raw(conn, cfg, limit=args.limit)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["fail"] == 0 else 1


def cmd_quality(args: argparse.Namespace) -> int:
    """质量看板（A5）：源成功率 / 字段 NULL 率 / 跨源不一致 / 门禁历史。"""
    from .api import quality
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    print(json.dumps(quality.build(conn, cfg, days=args.days), ensure_ascii=False, indent=2))
    return 0


def cmd_survivorship(args: argparse.Namespace) -> int:
    """幸存者偏差诊断：公开目录快照里的消失候选（不等于清盘）。"""
    from . import survivorship
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    out = survivorship.summary(conn, lookback_days=args.lookback_days, limit=args.limit)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_oos(args: argparse.Namespace) -> int:
    """季度 OOS 研究模型（P2-15，research_only）：只读，不改变筛选。"""
    from .screen import oos
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    out = oos.run(conn, cfg, contract=args.contract, test_quarters=args.test_quarters)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细写入 {path}")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_capacity(args: argparse.Namespace) -> int:
    """规模容量曲线（P2-11，研究）：只读，不改变筛选。"""
    from . import capacity
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    out = capacity.summary(conn, cfg, as_of=args.as_of)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细写入 {path}")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_factors(args: argparse.Namespace) -> int:
    """风格因子归因（ADR-078）：久期/信用/转债 beta，按 as_of 留存。"""
    from . import factors as factors_mod
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    out = factors_mod.compute(conn, cfg, as_of=args.as_of, types=args.types,
                              window_days=args.window_days, min_obs=args.min_obs)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_mrar(args: argparse.Namespace) -> int:
    """长期质量指标（ADR-083）：36 个月 MRAR(2)，按 as_of 留存。"""
    from . import quality as quality_mod
    cfg = load_config()
    conn = db.connect(cfg)
    db.migrate(conn)
    out = quality_mod.compute_mrar(conn, cfg, as_of=args.as_of, types=args.types)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_portfolio_diagnostics(args: argparse.Namespace) -> int:
    """组合相关性诊断：榜单是不是"十只基金、一个风险"（P1-1，只读）。"""
    from datetime import date, timedelta
    from . import portfolio_diagnostics as diag
    from .audit import portfolio as port
    cfg = load_config()
    conn = db.connect(cfg)
    run = None
    if args.run_id:
        run = conn.execute("SELECT * FROM screen_run WHERE run_id=?", (args.run_id,)).fetchone()
    else:
        run = conn.execute(
            "SELECT * FROM screen_run WHERE status='done' ORDER BY rowid DESC LIMIT 1").fetchone()
    if run is None:
        print("没有可用的 screen_run", file=sys.stderr)
        return 1
    rows = conn.execute(
        "SELECT p.fcode, f.name, f.company_name,"
        " (SELECT s.family_id FROM fund_share_class s WHERE s.fcode=p.fcode LIMIT 1) AS family_id,"
        " (SELECT st.style_code FROM fund_style st WHERE st.fcode=p.fcode"
        "  ORDER BY st.updated_at DESC LIMIT 1) AS style_code"
        " FROM screen_pick p JOIN fund f ON f.fcode=p.fcode"
        " WHERE p.run_id=? AND p.board=? ORDER BY p.rank", (run["run_id"], args.board)).fetchall()
    if not rows:
        print(f"run {run['run_id']} 的 {args.board} 为空", file=sys.stderr)
        return 1
    as_of = run["business_date"]
    start = (date.fromisoformat(as_of) - timedelta(days=args.window_days)).isoformat()

    def load(codes):
        series: dict[str, list[tuple[str, float]]] = {}
        meta: dict[str, dict[str, str | None]] = {}
        for code in codes:
            r = conn.execute(
                "SELECT f.name, f.company_name,"
                " (SELECT s.family_id FROM fund_share_class s WHERE s.fcode=f.fcode LIMIT 1) AS family_id,"
                " (SELECT st.style_code FROM fund_style st WHERE st.fcode=f.fcode"
                "  ORDER BY st.updated_at DESC LIMIT 1) AS style_code"
                " FROM fund f WHERE f.fcode=?", (code,)).fetchone()
            if r is None:
                continue
            s = port._load_series(conn, code, start, as_of)
            if s:
                series[code] = s
            meta[code] = {"company": r["company_name"], "family": r["family_id"],
                          "style": r["style_code"]}
        return series, meta

    codes = [r["fcode"] for r in rows]
    series, meta = load(codes)
    result = diag.diagnose(series, meta, min_pair_obs=args.min_obs)
    result.update({
        "run_id": run["run_id"],
        "board": args.board,
        "as_of": as_of,
        "start": start,
        "window_days": args.window_days,
        "requested": len(rows),
        "series_used": len(series),
        "codes": codes,
        "series_missing": [c for c in codes if c not in series],
    })
    if args.baseline == "formal":
        base_codes = [r[0] for r in conn.execute(
            "SELECT fcode FROM screen_decision WHERE run_id=? AND passed=1 ORDER BY fcode",
            (run["run_id"],))]
        if base_codes:
            base_series, base_meta = load(base_codes)
            base = diag.diagnose(base_series, base_meta, min_pair_obs=args.min_obs)
            result["baseline"] = {"label": "formal", **diag.summarize(base),
                                  "requested": len(base_codes),
                                  "series_used": len(base_series)}
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细写入 {path}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_stat_validation(args: argparse.Namespace) -> int:
    """统计验证：块自举超额 + Deflated Sharpe（P1-3，只读）。"""
    from datetime import date, timedelta
    from .audit import portfolio as port
    from .audit import stat_validation as sv
    cfg = load_config()
    conn = db.connect(cfg)
    run = None
    if args.run_id:
        run = conn.execute("SELECT * FROM screen_run WHERE run_id=?", (args.run_id,)).fetchone()
    else:
        run = conn.execute(
            "SELECT * FROM screen_run WHERE status='done' ORDER BY rowid DESC LIMIT 1").fetchone()
    if run is None:
        print("没有可用的 screen_run", file=sys.stderr)
        return 1
    codes = [r[0] for r in conn.execute(
        "SELECT fcode FROM screen_pick WHERE run_id=? AND board=? ORDER BY rank",
        (run["run_id"], args.board))]
    if not codes:
        print(f"run {run['run_id']} 的 {args.board} 为空", file=sys.stderr)
        return 1
    as_of = run["business_date"]
    start = (date.fromisoformat(as_of) - timedelta(days=args.window_days)).isoformat()
    board = port._stats_for(conn, codes, start, as_of)
    formal_codes = [r[0] for r in conn.execute(
        "SELECT fcode FROM screen_decision WHERE run_id=? AND passed=1 ORDER BY fcode",
        (run["run_id"],))]
    bench = port._stats_for(conn, formal_codes, start, as_of)
    if not board.get("curve") or not bench.get("curve"):
        print("组合或基准没有足够净值序列", file=sys.stderr)
        return 1
    pr = sv.daily_returns(board["curve"])
    br = sv.daily_returns(bench["curve"])
    excess = sv.aligned_excess(pr, br)
    ex = sv.summarize_excess(excess, block_size=args.block, n_boot=args.boot, seed=args.seed)
    rf = float(cfg.get("risk", "risk_free_rate", default=0.0164))
    rf_daily = (1.0 + rf) ** (1.0 / 252.0) - 1.0
    raw = sv.moments([r for _, r in pr])
    excess_rf = [r - rf_daily for _, r in pr]
    m = sv.moments(excess_rf)
    sr_daily = m["mean"] / m["std"] if m["std"] > 0 else 0.0
    dsr = sv.deflated_sharpe(sr_daily, args.n_trials, len(pr), m["skew"], m["kurt"])
    def stats_view(s):
        out = {k: s.get(k) for k in ("n", "n_used", "annual_pct", "vol_pct", "mdd_pct", "days", "obs")}
        out["raw_sharpe_no_rf"] = s.get("sharpe")
        return out

    result = {
        "run_id": run["run_id"],
        "board": args.board,
        "as_of": as_of,
        "start": start,
        "window_days": args.window_days,
        "n_board": len(codes),
        "n_benchmark": len(formal_codes),
        "board_stats": stats_view(board),
        "benchmark_stats": stats_view(bench),
        "excess": ex,
        "risk_free": {"annual": rf, "daily": rf_daily},
        "portfolio_daily": {"raw_mean": raw["mean"], "raw_std": raw["std"],
                            "mean_excess_rf": m["mean"], "std": m["std"],
                            "skew": m["skew"], "kurt": m["kurt"],
                            "sharpe_daily_rf": sr_daily,
                            "sharpe_annual_rf": sr_daily * (252 ** 0.5)},
        "deflated_sharpe": dsr,
        "note": "基准=本轮正式通过基金等权；DSR 的 n_trials 是近似有效试验数，不是精确配置计数",
    }
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细写入 {path}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_data_health(args: argparse.Namespace) -> int:
    """数据健康快照：DB/迁移/净值/指标/采集/发布（P2，只读）。"""
    from . import data_health
    cfg = load_config()
    conn = db.connect(cfg)
    result = data_health.build(conn, days=args.days,
                               business_date=args.business_date,
                               integrity=not args.skip_integrity,
                               sources=cfg.sources,
                               state_dir=cfg.root / cfg.app["paths"]["state_dir"])
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"明细写入 {path}")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        checks = result["checks"]
        print("状态: %s%s" % ("OK" if result["ok"] else "FAIL",
              ("" if result["ok"] else "  失败: " + ", ".join(result["failed_checks"]))))
        print("DB: quick_check=%s  schema=%s (%d/%d, missing=%s)" % (
            checks["db_integrity"]["detail"], "ok" if checks["schema"]["ok"] else "drift",
            checks["schema"]["applied"], checks["schema"]["files"],
            checks["schema"]["missing"] or "—"))
        nav = result["nav"]
        print("NAV: effective=%s (覆盖 %s%%) max=%s age=%s天 funds=%s rows=%s stale>%d天=%s" % (
            nav["effective"], nav["coverage_pct"], nav["max"], nav["age_days"],
            nav["funds"], nav["rows"], data_health.NAV_STALE_DAYS, nav["stale_funds"]))
        print("指标: %s 周期=%d 行=%d" % (
            result["metrics"]["contract"], len(result["metrics"]["periods"]),
            sum(x["n"] for x in result["metrics"]["periods"])))
        cov = result.get("coverage") or {}
        if cov:
            print("覆盖: 当前=%s 历史清盘=%s profile=%s nav=%s fee=%s scale=%s manager=%s holding=%s(%s %s) lifecycle=%s(净值%s)" % (
                cov.get("funds_active"), cov.get("funds_liquidated"),
                cov.get("profile"), cov.get("nav"), cov.get("fee"),
                cov.get("scale"), cov.get("manager_current"), cov.get("holding"),
                cov.get("holding_latest_period") or "—", cov.get("holding_latest_funds"),
                cov.get("lifecycle"), cov.get("lifecycle_with_nav")))
        fetch = result["fetch"]
        print("采集: 近%d天 calls=%d business_ok=%d (%s%%)" % (
            fetch["window_days"], fetch["calls"], fetch["business_ok"],
            fetch["success_rate_pct"] if fetch["success_rate_pct"] is not None else "—"))
        src = result["source_health"]
        active = sum(1 for s in src["sources"] if s["calls"])
        print("来源: 注册=%d 近%d天活跃=%d 单源域=%s" % (
            len(src["sources"]), args.days, active,
            ", ".join(src["single_source_domains"]) or "—"))
        prov = checks.get("provenance") or {}
        if prov.get("status") == "not_applicable":
            print("溯源: 未检查（raw_object 不存在）")
        else:
            print("溯源: %s 表=%d 缺 raw=%d 格式错=%d 未注册来源=%d" % (
                prov.get("status"), len(prov.get("tables") or []),
                prov.get("missing_total", 0), prov.get("bad_format_total", 0),
                len(prov.get("unregistered_sources") or [])))
        sched = checks.get("scheduler") or {}
        print("调度: status=%s next=%s last=%s/%s" % (
            sched.get("status"), sched.get("next_run_at") or "—",
            sched.get("last_run_at") or "—", sched.get("last_status") or "—"))
        if result["warnings"]:
            print("告警: " + ", ".join(result["warnings"]))
        pub = result["publication"] or {}
        print("发布: generation=%s contract=%s gate=%s" % (
            pub.get("generation"), pub.get("screen_contract"),
            (result["gate"] or {}).get("status")))
    return 0 if result["ok"] else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from .api.server import serve
    cfg = load_config()
    serve(cfg, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fundana", description="Fund-ANA V2 基金筛选（首期债券型）")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bootstrap", help="初始化配置与数据库")
    b.add_argument("--verify-config", action="store_true")
    b.add_argument("--migrate", action="store_true")
    b.set_defaults(func=cmd_bootstrap)

    pr = sub.add_parser("probe", help="逐源业务解析探针（HTTP 200 不算通过）")
    pr.add_argument("--sources", default="all")
    pr.add_argument("--strict", action="store_true")
    pr.set_defaults(func=cmd_probe)

    c = sub.add_parser("collect", help="采集")
    c.add_argument("--domain", required=True,
                   choices=["directory", "status", "rank", "index", "nav", "profile",
                            "holding", "fee", "scale", "manager"])
    c.add_argument("--types", default=None,
                   help="采集类型（逗号分隔），默认取 config universe.types")
    c.add_argument("--mode", default="initial", choices=["initial", "daily", "research"])
    c.add_argument("--limit", type=int, default=None)
    c.add_argument("--page-size", type=int, default=500)
    c.add_argument("--max-pages", type=int, default=40)
    c.add_argument("--only-buyable", action="store_true",
                   help="只采集当日可申购的基金（profile/holding/fee 域）")
    c.add_argument("--skip-collected", action="store_true",
                   help="跳过已采集过该域的基金（nav/profile/holding/fee/scale/manager 域）")
    c.add_argument("--force", action="store_true",
                   help="绕过当日缓存（nav/scale/manager 域；历史回补时用）")
    c.add_argument("--years", default=None,
                   help="holding 域按年回补历史，如 2020-2025 或 2020,2021（每年一次请求返回四个季度）")
    c.add_argument("--boards", default=None,
                   help="只采集最新一次筛选榜单中这些榜的基金（逗号分隔，如 bond_core10,bond_stable）")
    c.add_argument("--codes", default=None,
                   help="只采集指定代码（逗号分隔）；用于补齐个别公开序列缺口")
    c.set_defaults(func=cmd_collect)

    s = sub.add_parser("stats", help="各表行数")
    s.set_defaults(func=cmd_stats)

    ty = sub.add_parser("types", help="基金类型插件注册表（能跑哪些类型/还差什么）")
    ty.set_defaults(func=cmd_types)

    fz = sub.add_parser("fuse", help="L2 融合：身份/份额/画像/费率/持有期/可购/风格/覆盖")
    fz.add_argument("--business-date", default=None)
    fz.add_argument("--types", default=None,
                    help="只融合这些类型（逗号分隔），默认取 config universe.types")
    fz.set_defaults(func=cmd_fuse)

    mt = sub.add_parser("metrics", help="计算风险指标（九周期）")
    mt.add_argument("--formula", default=None)
    mt.add_argument("--as-of", default=None)
    mt.add_argument("--changed-only", action="store_true", help="只补缺失周期")
    mt.add_argument("--limit", type=int, default=None)
    mt.set_defaults(func=cmd_metrics)

    sm = sub.add_parser("snapshot-metrics", help="把 as-of 指标写成季度快照（P2-3，不覆盖生产指标）")
    sm.add_argument("--as-of", default=None, help="截止日；--quarters 时作为最后一个季末")
    sm.add_argument("--quarters", type=int, default=0, help=">0 时回填最近 N 个季末")
    sm.add_argument("--limit", type=int, default=None)
    sm.set_defaults(func=cmd_snapshot_metrics)

    cp = sub.add_parser("comparability", help="重算指标可比性旗标（P0-2，不改指标）")
    cp.add_argument("--as-of", default=None)
    cp.add_argument("--formula", default=None)
    cp.set_defaults(func=cmd_comparability)

    tl = sub.add_parser("tail", help="计算 V2 尾部风险指标（3 年窗口，影子）")
    tl.add_argument("--as-of", default=None)
    tl.add_argument("--limit", type=int, default=None)
    tl.set_defaults(func=cmd_tail)

    pc = sub.add_parser("prune-contracts", help="清理非当前公式契约的历史指标行（ADR-027）")
    pc.add_argument("--apply", action="store_true", help="真正删除；默认只报告")
    pc.set_defaults(func=cmd_prune_contracts)

    sc = sub.add_parser("screen", help="L1–L10 筛选")
    sc.add_argument("--batch", help="批次号（默认取业务日）；同批次共用一套冻结分位阈值")
    sc.add_argument("--profile", default="bond_v1")
    sc.add_argument("--business-date", default=None)
    sc.set_defaults(func=cmd_screen)

    rps = sub.add_parser("replay-screen", help="按 PIT 快照重放 base_gate 与评分（只读）")
    rps.add_argument("--run-id", default=None, help="默认取最近一次 status=done 的 screen_run")
    rps.set_defaults(func=cmd_replay_screen)

    li = sub.add_parser("lifecycle-import", help="导入清盘/合并/暂停名单（P0-1）")
    li.add_argument("path", help="CSV：fcode,status,effective_date[,last_nav_date,reason,source]")
    li.set_defaults(func=cmd_lifecycle_import)

    lc = sub.add_parser("lifecycle-collect", help="从证监会 EID 采集清盘/合并生命周期（公开公告）")
    lc.add_argument("--start", default="2015-01-01")
    lc.add_argument("--end", default=None)
    lc.add_argument("--types", nargs="*", default=None,
                    choices=["FB060010", "FC030010", "FC030030", "FC050050", "FC050060"])
    lc.add_argument("--no-pdf", action="store_true", help="只取公告日，不解析清算报告 PDF")
    lc.add_argument("--force", action="store_true")
    lc.add_argument("--limit", type=int, default=None, help="最多写入多少只（调试用）")
    lc.set_defaults(func=cmd_lifecycle_collect)

    ls = sub.add_parser("lifecycle-status", help="查询某只基金在 as-of 日的生命周期状态")
    ls.add_argument("--fcode", required=True)
    ls.add_argument("--as-of", default=None)
    ls.set_defaults(func=cmd_lifecycle_status)

    sh = sub.add_parser("shadow", help="增强债影子榜（换窗口复算，research_only）")
    sh.add_argument("--window", type=int, default=1095, choices=[365, 730, 1095, 1825])
    sh.add_argument("--limit", type=int, default=60)
    sh.set_defaults(func=cmd_shadow)

    ss = sub.add_parser("shadow-screen", help="固定线风格化/动态层影子（只读，research_only）")
    ss.add_argument("--run-id", default=None, help="默认取最近一次 status=done 的 screen_run")
    ss.add_argument("--quantile", type=float, default=0.60, help="composite 分位，默认 0.60")
    ss.add_argument("--k", type=int, default=4, help="6 个动态维度至少通过数，默认 4")
    ss.set_defaults(func=cmd_shadow_screen)

    sr = sub.add_parser("shadow-rank", help="v9 影子排序（只读，research_only）")
    sr.add_argument("--run-id", default=None, help="默认取最近一次 status=done 的 screen_run")
    sr.set_defaults(func=cmd_shadow_rank)

    sf = sub.add_parser("shadow-forward", help="v9/v10 core10/core50 前瞻观察（research_only）")
    sf.add_argument("--run-id", default=None, help="默认取最近一次 status=done 的 screen_run")
    sf.add_argument("--cohort", default=None, help="只评估/汇总已有影子 cohort，不再冻结")
    sf.add_argument("--publication-id", default=None)
    sf.add_argument("--business-date", default=None)
    sf.add_argument("--rows", action="store_true", help="输出逐榜/逐策略明细")
    sf.set_defaults(func=cmd_shadow_forward)

    pb = sub.add_parser("publish", help="构建产物并原子提交")
    pb.add_argument("--run-id", default=None)
    pb.add_argument("--allow-republish", action="store_true",
                    help="允许同一 screen_run 重复发布（默认拒绝）")
    pb.add_argument("--allow-older", action="store_true",
                    help="允许用更旧 nav_as_of 的 run 覆盖当前发布（仅回滚用）")
    pb.set_defaults(func=cmd_publish)

    vf = sub.add_parser("verify", help="校验当前发布")
    vf.add_argument("--fast", action="store_true", help="跳过逐文件 SHA256")
    vf.set_defaults(func=cmd_verify)

    rp = sub.add_parser("replay", help="离线复算并与已存指标比对")
    rp.add_argument("--run-id", default=None)
    rp.add_argument("--formula", default=None)
    rp.add_argument("--as-of", default=None)
    rp.add_argument("--limit", type=int, default=None)
    rp.add_argument("--offline", action="store_true", default=True)
    rp.set_defaults(func=cmd_replay)

    dl = sub.add_parser("daily", help="日更管线：快照→指标→融合→筛选→发布→跟踪")
    dl.add_argument("--types", default=None,
                    help="类型（逗号分隔），默认取 config universe.types")
    dl.add_argument("--force", action="store_true", help="即使没有新业务日也重算并发布")
    dl.add_argument("--skip-publish", action="store_true")
    dl.add_argument("--quiet", action="store_true", help="不打印进度，只输出最终 JSON")
    dl.set_defaults(func=cmd_daily)

    nt = sub.add_parser("notify", help="P1-10 写运维告警（可选 webhook）")
    nt.add_argument("--unit", default=None)
    nt.add_argument("--title", default=None)
    nt.add_argument("--message", default=None)
    nt.set_defaults(func=cmd_notify)

    mt = sub.add_parser("maintenance", help="P1-5 季度/月度维护采集（按到期触发）")
    mt.add_argument("--tasks", default=None, help="profile,fee,scale,manager,holding；默认按到期")
    mt.add_argument("--force", action="store_true", help="忽略到期状态，强制全部执行")
    mt.add_argument("--limit", type=int, default=None)
    mt.add_argument("--run-id", default=None, help="以某次 screen 的榜单为目标")
    mt.set_defaults(func=cmd_maintenance)

    dm = sub.add_parser("db-maintain", help="SQLite 维护：optimize/checkpoint，可选 vacuum")
    dm.add_argument("--vacuum", action="store_true", help="重写主库回收空闲页（需约 2 倍磁盘，低峰执行）")
    dm.add_argument("--integrity-check", action="store_true", help="先跑 PRAGMA quick_check")
    dm.add_argument("--no-checkpoint", action="store_true", help="跳过 WAL checkpoint")
    dm.add_argument("--checkpoint-mode", default="TRUNCATE",
                    choices=["PASSIVE", "FULL", "RESTART", "TRUNCATE"])
    dm.set_defaults(func=cmd_db_maintain)

    sch = sub.add_parser("scheduler", help="工作日定时日更（默认 07:30）")
    sch.add_argument("--once", action="store_true", help="只跑一次就退出")
    sch.add_argument("--at", default="07:30")
    sch.add_argument("--force", action="store_true")
    sch.set_defaults(func=cmd_scheduler)

    tk = sub.add_parser("tracker", help="长期跟踪榜（累积出现次数/频率/进出）")
    tk.add_argument("--board", default=None)
    tk.add_argument("--fcode", default=None, help="查看单只基金的跟踪历史")
    tk.add_argument("--limit", type=int, default=20)
    tk.add_argument("--rebuild", action="store_true",
                    help="按业务日重算跟踪表（同一交易日重发只算一次；修数据用）")
    tk.add_argument("--rebuild-from-picks", action="store_true",
                    help="把历史已发布快照补记进跟踪榜（同一交易日重发以最后一次为准）")
    tk.set_defaults(func=cmd_tracker)

    va = sub.add_parser("validate", help="跨源核对：同一事实用第二个端点复核")
    va.add_argument("--cross", action="store_true")
    va.add_argument("--sample", type=int, default=0, help="抽样深检的基金数（蛋卷/东财净值历史）")
    va.add_argument("--business-date")
    va.set_defaults(func=cmd_validate)

    fw = sub.add_parser("forward", help="前瞻验证：冻结 cohort 并用后续净值评估")
    fw.add_argument("--create", action="store_true", help="为最近一次 screen_run 建 cohort")
    fw.add_argument("--run-id")
    fw.add_argument("--business-date")
    fw.add_argument("--source", default="live", choices=["live", "retro"])
    fw.add_argument("--cohort", help="只看某个 cohort_id")
    fw.add_argument("--rows", action="store_true", help="输出逐 horizon 明细")
    fw.set_defaults(func=cmd_forward)

    p2_p = sub.add_parser("phase2", help="Phase 2 影子研究：5年倾斜/暴露/杠杆/基准匹配（只读）")
    p2_p.add_argument("--run-id", help="screen_run.run_id；默认最新成功运行")
    p2_p.add_argument("--no-write", action="store_true", help="只打印，不写 reports/analysis/phase2")
    p2_p.set_defaults(func=cmd_phase2)

    wf_p = sub.add_parser("walkforward", help="历史 as-of 回测：评分与排序键的前瞻表现")
    wf_p.add_argument("--years", default="2020,2021,2022,2023,2024,2025",
                      help="逗号分隔的建仓年份（6-30）；2019 及更早固定线通过者不足 top，"
                           "六键会退化为同一组，不建议用于排序键比较")
    wf_p.add_argument("--top", type=int, default=118)
    wf_p.add_argument("--horizon", type=int, default=365)
    wf_p.add_argument("--style-asof", action=argparse.BooleanOptionalAction, default=True,
                      help="用 fund_style_history 的 as-of 标签（默认开；历史不足时回退当前）")
    wf_p.add_argument("--enhanced", action="store_true",
                      help="追加增强债 strict（7项绝对）与 relative（当前相对口径）准入回测")
    wf_p.add_argument("--portfolio", action="store_true",
                      help="追加组合层回测：等权买入持有，对比全市场可投债基等权基准")
    wf_p.add_argument("--portfolio-weighting", choices=("equal", "inverse_vol"),
                      default="equal",
                      help="组合层权重：equal 等权（默认）/ inverse_vol 逆波动率（风险预算实验）")
    wf_p.add_argument("--include-liquidated", action=argparse.BooleanOptionalAction, default=True,
                      help="纳入已确认清盘/合并基金（默认开；关闭可复现回填前基线，量化幸存者偏差）")
    wf_p.add_argument("--out", help="把逐队列明细写成 JSON")
    wf_p.set_defaults(func=cmd_walkforward)

    al = sub.add_parser("alerts", help="产品告警：已入选基金的净值/经理/规模/回撤/风格漂移")
    al.add_argument("--board", help="只扫描一个榜单（默认核心10/核心50/稳健/增强）")
    al.add_argument("--as-of", help="业务日（默认最新发布的 business_date）")
    al.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    al.add_argument("--out", help="把明细写成 JSON")
    al.set_defaults(func=cmd_alerts)

    ev = sub.add_parser("events", help="经理/规模事件研究：换帅、任期、规模变化后的前瞻收益")
    ev.add_argument("--years", default="2020,2021,2022,2023,2024,2025",
                    help="逗号分隔的建仓年份（6-30）")
    ev.add_argument("--horizon", type=int, default=365)
    ev.add_argument("--out", help="把逐队列明细写成 JSON")
    ev.set_defaults(func=cmd_events)

    ex = sub.add_parser("exposure", help="持仓风格归因：各榜单的券种暴露")
    ex.add_argument("--run-id", help="运行 ID（默认最近一次有正式成员的）")
    ex.add_argument("--out", help="把明细写成 JSON")
    ex.set_defaults(func=cmd_exposure)

    to = sub.add_parser("turnover", help="榜单换手：两次发布之间的进出与原因")
    to.add_argument("--run-id", help="本次运行（默认最近一次有正式成员的）")
    to.add_argument("--prev-run-id", help="对比基准（默认上一份已提交发布）")
    to.add_argument("--history", action="store_true", help="输出逐批次换手序列")
    to.add_argument("--limit", type=int, default=24)
    to.add_argument("--out", help="把明细写成 JSON")
    to.set_defaults(func=cmd_turnover)

    fee = sub.add_parser("fees", help="费用阶梯回放：从 raw_object 重解析，不联网")
    fee.add_argument("--limit", type=int, default=None)
    fee.set_defaults(func=cmd_fees)

    ql = sub.add_parser("quality", help="质量看板：源成功率 / NULL 率 / 跨源不一致 / 门禁")
    ql.add_argument("--days", type=int, default=7)
    ql.set_defaults(func=cmd_quality)

    svp = sub.add_parser("survivorship", help="幸存者偏差诊断：公开目录消失候选（不等于清盘）")
    svp.add_argument("--lookback-days", type=int, default=30)
    svp.add_argument("--limit", type=int, default=50)
    svp.set_defaults(func=cmd_survivorship)

    cap = sub.add_parser("capacity", help="规模容量曲线（研究，不设硬上限）")
    cap.add_argument("--as-of", default=None)
    cap.add_argument("--out", default=None)
    cap.set_defaults(func=cmd_capacity)

    oosp = sub.add_parser("oos", help="季度 OOS 研究模型（research_only）")
    oosp.add_argument("--contract", default=None)
    oosp.add_argument("--test-quarters", type=int, default=3)
    oosp.add_argument("--out", default=None)
    oosp.set_defaults(func=cmd_oos)

    fx = sub.add_parser("factors", help="风格因子归因（久期/信用/转债 beta）")
    fx.add_argument("--as-of", default=None, help="截止交易日，默认取指数日线最新一天")
    fx.add_argument("--types", default=None, help="基金类型，默认取 config universe.types")
    fx.add_argument("--window-days", type=int, default=365)
    fx.add_argument("--min-obs", type=int, default=60)
    fx.set_defaults(func=cmd_factors)

    mr = sub.add_parser("mrar", help="长期质量指标：36 个月 MRAR(2) 评级")
    mr.add_argument("--as-of", default=None, help="截止净值日，默认取最新净值日")
    mr.add_argument("--types", default=None, help="基金类型，默认取 config universe.types")
    mr.set_defaults(func=cmd_mrar)

    pd = sub.add_parser("portfolio-diagnostics", help="组合相关性/集中度诊断（P1-1，只读）")
    pd.add_argument("--run-id", default=None)
    pd.add_argument("--board", default="bond_core10")
    pd.add_argument("--window-days", type=int, default=365)
    pd.add_argument("--min-obs", type=int, default=40)
    pd.add_argument("--baseline", choices=("none", "formal"), default="formal",
                    help="对照基线：formal=本轮正式通过的全部基金")
    pd.add_argument("--out", default=None)
    pd.set_defaults(func=cmd_portfolio_diagnostics)

    stv = sub.add_parser("stat-validation", help="统计验证：块自举超额 + Deflated Sharpe（P1-3，只读）")
    stv.add_argument("--run-id", default=None)
    stv.add_argument("--board", default="bond_core10")
    stv.add_argument("--window-days", type=int, default=365)
    stv.add_argument("--block", type=int, default=20)
    stv.add_argument("--boot", type=int, default=5000)
    stv.add_argument("--seed", type=int, default=42)
    stv.add_argument("--n-trials", type=int, default=256)
    stv.add_argument("--out", default=None)
    stv.set_defaults(func=cmd_stat_validation)

    dh = sub.add_parser("data-health", help="数据健康快照：DB/迁移/净值/指标/采集/发布（P2，只读）")
    dh.add_argument("--days", type=int, default=7)
    dh.add_argument("--business-date", default=None)
    dh.add_argument("--skip-integrity", action="store_true", help="跳过 PRAGMA quick_check（大库更快）")
    dh.add_argument("--json", action="store_true")
    dh.add_argument("--out", default=None)
    dh.set_defaults(func=cmd_data_health)

    sv = sub.add_parser("serve", help="本地只读 API")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sv.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())