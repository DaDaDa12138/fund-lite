"""产物构建：把一次 run 的决策写成可审计目录（CSV/MD/JSON + 逐文件 SHA256）。"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .. import db, explain, quality as quality_mod
from ..fees.share_class import compare as share_class_compare
from ..labels import label_board, label_profile, label_style
from ..metrics.tail import TAIL_CONTRACT, TAIL_WINDOW_DAYS
from ..screen import thresholds as T

SCHEMA_VERSION = 1
CSV_COLUMNS = ["fcode", "name", "company_name", "style_code", "style_label", "grade",
               "formal_score", "rank_score",
               "admission_path", "manager_tenure_coverage_3y",
               "composite", "short_formal", "long_formal", "confidence", "confidence_tier",
               "annual_1y_pct", "sharpe_1y", "sortino_1y", "calmar_1y", "mdd_1y", "vol_1y",
               "positive_1y_pct", "flat_1y_pct", "non_negative_1y_pct",
               "recurring_fee_pct", "cost_1y_pct", "cost_3y_pct", "cost_5y_pct",
               "long_score",
               "annual_3y_pct", "mdd_3y_pct", "vol_3y_pct", "sharpe_3y", "calmar_3y",
               "annual_5y_pct", "mdd_5y_pct", "vol_5y_pct", "sharpe_5y", "calmar_5y",
               "effective_years_3y", "coverage_ratio_3y", "is_full_3y",
               "effective_years_5y", "coverage_ratio_5y", "is_full_5y",
               "long_core_eligible", "long_formal_eligible",
               "asset_scale_billion", "share_scale_billion", "estab_date",
               "buyable_state", "effective_days", "rank",
               "net_annual_1y_pct", "mrar_3y_pct", "mrar_peer_pct", "mrar_n_obs",
               "alpha_annual_pct", "alpha_t", "alpha_r2", "alpha_reliable",
               "cvar5_pct", "worst180_pct", "max_underwater_obs", "ongoing_drawdown",
               "inst_ratio_pct", "stock_pct", "top10_bond_pct", "manager_tenure_days",
               "manager_tenure_min_days",
               "scale_change_1y_pct", "quality_status", "quality_watch_n",
               "quality_unknown_n", "quality_flags", "risk_status", "risk_flags",
               "why_selected", "caveat", "recommended_share_class",
               "share_class_advantage_3y_pct", "share_class_break_even_years"]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def collect_rows(conn, run_id: str, cfg=None) -> dict[str, list[dict[str, Any]]]:
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return _collect_rows(conn, run_id, cfg)
    finally:
        conn.row_factory = old_factory


def _collect_rows(conn, run_id: str, cfg=None) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT p.board, p.rank, p.fcode, p.formal_score, p.rank_score, p.confidence, f.name,"
        " f.company_name, f.estab_date,"
        " d.style_code, d.scores_json, d.thresholds_json, d.reason_codes_json, d.risk_json, d.passed,"
        " d.layer_reached, pr.asset_scale_billion, pr.recurring_fee_pct,"
        " pr.max_sub_fee_pct, pr.field_src_json, fm.has_discount, h.effective_days,"
        " (SELECT MIN(t.days) FROM fund_manager_tenure t"
        "  WHERE t.fcode=p.fcode AND t.is_current=1) AS manager_tenure_min_days,"
        " b.buyable_state, sc.family_id"
        " FROM screen_pick p"
        " JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
        " JOIN fund f ON f.fcode=p.fcode"
        " LEFT JOIN fund_profile pr ON pr.fcode=p.fcode"
        " LEFT JOIN fund_fee_meta fm ON fm.fcode=p.fcode"
        " LEFT JOIN fund_holding_policy h ON h.fcode=p.fcode"
        " LEFT JOIN fund_share_class sc ON sc.fcode=p.fcode"
        " LEFT JOIN fund_buyable_daily b ON b.fcode=p.fcode AND b.business_date=("
        "   SELECT MAX(business_date) FROM fund_buyable_daily)"
        " WHERE p.run_id=? ORDER BY p.board, p.rank", (run_id,)).fetchall()
    metrics = {}
    for m in conn.execute("SELECT fcode, period_days, annual_dec, sharpe, sortino, calmar_dec,"
                          " mdd_dec, vol_dec, positive_ratio_pct, flat_ratio_pct,"
                          " non_negative_ratio_pct, years"
                          " FROM fund_metric WHERE period_days IN (365,1095,1825) AND"
                          " formula_contract=(SELECT formula_contract FROM screen_run WHERE run_id=?)",
                          (run_id,)):
        metrics[(m["fcode"], m["period_days"])] = dict(m)
    qmap: dict[str, dict[str, Any]] = {}
    if cfg is not None:
        codes = sorted({r["fcode"] for r in rows})
        qmap = quality_mod.build_map(conn, cfg, codes)
    boards: dict[str, list[dict[str, Any]]] = {"bond_stable": [], "bond_enhanced": [],
                                                "bond_core": [], "bond_core10": [],
                                                "bond_long": [],
                                                "bond_watch": [], "bond_locked": [],
                                                "bond_rookie": []}
    for r in rows:
        sc = json.loads(r["scores_json"] or "{}")
        m1 = metrics.get((r["fcode"], 365), {})
        m3 = metrics.get((r["fcode"], 1095), {})
        m5 = metrics.get((r["fcode"], 1825), {})
        conf = r["confidence"]
        if conf is None:
            conf_tier = None
        elif conf >= 80.0:   # 数据质量分 0–100（v2，见 thresholds.DATA_QUALITY_HIGH）
            conf_tier = "high"
        elif conf >= 60.0:
            conf_tier = "medium"
        else:
            conf_tier = "low"
        q = qmap.get(r["fcode"]) or {}
        q_mrar = q.get("mrar") or {}
        q_alpha = q.get("alpha") or {}
        q_tail = q.get("tail") or {}
        q_risk = q.get("risk") or {}
        q_sum = q.get("summary") or {}
        flags = q.get("flags") or []
        risk = json.loads(r["risk_json"] or "{}")
        risk_summary = risk.get("summary") or {}
        risk_flags = ";".join(str(x) for x in (risk_summary.get("blocked_flags") or [])
                             + (risk_summary.get("watch_flags") or [])
                             + (risk_summary.get("unknown_flags") or []))
        annual = _pct(m1.get("annual_dec"))
        share_scale = _share_scale(r["field_src_json"])
        cost_1y = _cost(r, 365)
        recurring = r["recurring_fee_pct"]
        amortized = (None if cost_1y is None or recurring is None
                     else round(cost_1y - recurring, 4))
        net_annual = (None if annual is None or amortized is None
                      else round(annual - amortized, 4))
        if r["style_code"] in T.ENHANCED_STYLES:
            admission_path = "enhanced_relative"
        elif r["board"] == "bond_stable":
            admission_path = "l6c_fixed"
        else:
            admission_path = "observation"
        tenure_days = r["manager_tenure_min_days"]
        tenure_coverage = (None if tenure_days is None
                           else round(min(1.0, float(tenure_days) / 1095.0), 4))
        item = {
            "board": r["board"],
            "fcode": r["fcode"], "name": r["name"],
            "company_name": r["company_name"], "rank": r["rank"],
            "family_id": r["family_id"],
            "style_code": r["style_code"], "style_label": label_style(r["style_code"]),
            "admission_path": admission_path,
            "manager_tenure_coverage_3y": tenure_coverage,
            "grade": _grade(r["formal_score"] or 0),
            "formal_score": r["formal_score"], "rank_score": r["rank_score"],
            "core10_risk_budget": sc.get("core10_risk_budget"),
            "long_score": sc.get("long_score"),
            "composite": sc.get("composite"),
            "short_formal": sc.get("short_formal"), "long_formal": sc.get("long_formal"),
            "confidence": conf,
            "confidence_tier": conf_tier,
            "annual_1y_pct": annual,
            "sharpe_1y": m1.get("sharpe"), "sortino_1y": m1.get("sortino"),
            "calmar_1y": m1.get("calmar_dec"), "mdd_1y": m1.get("mdd_dec"),
            "vol_1y": m1.get("vol_dec"), "positive_1y_pct": m1.get("positive_ratio_pct"),
            "flat_1y_pct": m1.get("flat_ratio_pct"),
            "non_negative_1y_pct": m1.get("non_negative_ratio_pct"),
            "recurring_fee_pct": recurring,
            "cost_1y_pct": cost_1y, "cost_3y_pct": _cost(r, 1095),
            "cost_5y_pct": _cost(r, 1825),
            "annual_3y_pct": _pct(m3.get("annual_dec")),
            "mdd_3y_pct": _pct(m3.get("mdd_dec")),
            "vol_3y_pct": _pct(m3.get("vol_dec")),
            "sharpe_3y": m3.get("sharpe"), "calmar_3y": m3.get("calmar_dec"),
            "annual_5y_pct": _pct(m5.get("annual_dec")),
            "mdd_5y_pct": _pct(m5.get("mdd_dec")),
            "vol_5y_pct": _pct(m5.get("vol_dec")),
            "sharpe_5y": m5.get("sharpe"), "calmar_5y": m5.get("calmar_dec"),
            "effective_years_3y": (sc.get("coverage") or {}).get("effective_years_3y"),
            "coverage_ratio_3y": (sc.get("coverage") or {}).get("coverage_ratio_3y"),
            "is_full_3y": int(bool((sc.get("coverage") or {}).get("is_full_3y"))),
            "effective_years_5y": (sc.get("coverage") or {}).get("effective_years_5y"),
            "coverage_ratio_5y": (sc.get("coverage") or {}).get("coverage_ratio_5y"),
            "is_full_5y": int(bool((sc.get("coverage") or {}).get("is_full_5y"))),
            "long_core_eligible": int(bool((sc.get("coverage") or {}).get("long_core_eligible"))),
            "long_formal_eligible": int(bool((sc.get("coverage") or {}).get("long_formal_eligible"))),
            "asset_scale_billion": r["asset_scale_billion"],
            "share_scale_billion": share_scale,
            "manager_tenure_min_days": r["manager_tenure_min_days"],
            "estab_date": r["estab_date"],
            "buyable_state": r["buyable_state"], "effective_days": r["effective_days"],
            "net_annual_1y_pct": net_annual,
            "mrar_3y_pct": q_mrar.get("value_pct"), "mrar_peer_pct": q_mrar.get("peer_pct"),
            "mrar_n_obs": q_mrar.get("n_obs"),
            "alpha_annual_pct": q_alpha.get("annual_pct"), "alpha_t": q_alpha.get("t"),
            "alpha_r2": q_alpha.get("r2"), "alpha_reliable": int(bool(q_alpha.get("reliable"))),
            "cvar5_pct": q_tail.get("cvar5_pct"), "worst180_pct": q_tail.get("worst180_pct"),
            "max_underwater_obs": q_tail.get("max_underwater_obs"),
            "ongoing_drawdown": q_tail.get("ongoing_drawdown"),
            "inst_ratio_pct": q_risk.get("inst_ratio_pct"), "stock_pct": q_risk.get("stock_pct"),
            "top10_bond_pct": q_risk.get("top10_bond_pct"),
            "manager_tenure_days": q_risk.get("manager_tenure_days"),
            "scale_change_1y_pct": q_risk.get("scale_change_1y_pct"),
            "quality_status": q_sum.get("status"), "quality_watch_n": q_sum.get("watch"),
            "quality_unknown_n": q_sum.get("unknown"),
            "quality_flags": ";".join("%s:%s" % (f.get("code"), f.get("status"))
                                      for f in flags if f.get("status") != "ok"),
            "risk_status": risk_summary.get("status"),
            "risk_flags": risk_flags,
        }
        exp = explain.build(item)
        item["why_selected"] = explain.as_text(exp["why"])
        item["caveat"] = explain.as_text(exp["caveat"])
        item["why_selected_items"] = exp["why"]
        item["caveat_items"] = exp["caveat"]
        sc_cmp = share_class_compare(conn, r["fcode"])
        rec = sc_cmp.get("recommended") or {}
        item["recommended_share_class"] = rec.get("class_letter")
        item["share_class_advantage_3y_pct"] = sc_cmp.get("advantage_3y_pct")
        item["share_class_break_even_years"] = sc_cmp.get("break_even_years")
        item["share_class_compare"] = sc_cmp
        boards[r["board"]].append(item)
    return boards


def _pct(value) -> float | None:
    return None if value is None else round(value * 100, 4)


def _share_scale(field_src_json: str | None) -> float | None:
    try:
        src = json.loads(field_src_json or "{}")
    except (TypeError, ValueError):
        return None
    value = src.get("share_class_scale_billion")
    return None if value is None else float(value)


def _cost(row, horizon_days: int) -> float | None:
    """持有期总成本（ADR-059），口径与 fundana.fees.holding 完全一致。"""
    from ..fees.holding import holding_cost
    out = holding_cost(row["recurring_fee_pct"], row["max_sub_fee_pct"],
                       row["has_discount"], horizon_days)
    return None if out is None else out["total_pct"]


def _grade(score: float) -> str:
    for label, floor in [("A", 75.0), ("B+", 68.0), ("B", 60.0), ("C", 52.0)]:
        if score >= floor:
            return label
    return "D"


def build(conn, cfg, run_id: str) -> dict[str, Any]:
    root = Path(cfg.report_root)
    revision_id = uuid.uuid4().hex
    rev = root / "runs" / run_id / "revisions" / revision_id
    (rev / "diagnostics").mkdir(parents=True, exist_ok=True)
    (rev / "decisions").mkdir(parents=True, exist_ok=True)

    boards = collect_rows(conn, run_id, cfg)
    for board, rows in boards.items():
        _write_csv(rev / f"{board}.csv", rows, CSV_COLUMNS)
    _write_csv(rev / "all_formal_candidates.csv",
               boards["bond_stable"] + boards["bond_enhanced"], CSV_COLUMNS)

    run = conn.execute("SELECT * FROM screen_run WHERE run_id=?", (run_id,)).fetchone()

    # 诊断：拒绝原因分布、NULL 比例
    reason_dist: dict[str, int] = {}
    for r in conn.execute("SELECT reason_codes_json FROM screen_decision WHERE run_id=?", (run_id,)):
        for code in json.loads(r["reason_codes_json"] or "[]"):
            key = code.split(":")[0]
            reason_dist[key] = reason_dist.get(key, 0) + 1
    (rev / "diagnostics" / "rejections.json").write_text(
        json.dumps(reason_dist, ensure_ascii=False, indent=2), encoding="utf-8")
    layer_dist = {r["layer_reached"]: r["c"] for r in conn.execute(
        "SELECT layer_reached, COUNT(*) AS c FROM screen_decision WHERE run_id=? GROUP BY 1", (run_id,))}
    (rev / "diagnostics" / "layers.json").write_text(
        json.dumps(layer_dist, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_diagnostics(conn, rev, run_id)
    _write_decisions(conn, rev, run_id, cfg)

    files = []
    for path in sorted(rev.rglob("*")):
        if path.is_file():
            files.append({"path": str(path.relative_to(rev)).replace("\\", "/"),
                          "sha256": _sha256(path), "size": path.stat().st_size})
    # §16.8 发布门禁：必须在写 validated.json 之前评估，结果随 manifest 一起冻结
    from . import gate as gate_mod
    gate_result = gate_mod.evaluate(conn, cfg, run_id, {
        "revision_id": revision_id, "revision_path": str(rev),
        "manifest": {"schema_version": SCHEMA_VERSION, "publication_id": revision_id,
                     "run_id": run_id, "files": files}})
    # 门禁明细单独落盘（供 verify 与网页读取），随后重算文件清单把它纳入 manifest
    (rev / "diagnostics" / "publication_gate.json").write_text(
        json.dumps(gate_result, ensure_ascii=False, indent=2), encoding="utf-8")
    # 报告 v2 在门禁之后落盘；summary 头部会链接到 report.md
    from . import report_v2 as report_v2_mod
    report = report_v2_mod.build(conn, cfg, run_id, run, boards, gate_result)
    # P2-13：报告 facts 与数据库对账；重建一次让报告的风险节看到最终门禁结论。
    gate_result = gate_mod.attach_report_consistency(conn, cfg, run_id, gate_result,
                                                     report, boards)
    (rev / "diagnostics" / "publication_gate.json").write_text(
        json.dumps(gate_result, ensure_ascii=False, indent=2), encoding="utf-8")
    report = report_v2_mod.build(conn, cfg, run_id, run, boards, gate_result)
    report_v2_mod.write(rev, report)
    # summary 在门禁之后落盘，才能写上门禁结论；随后重算文件清单把 summary 的哈希纳入 manifest
    (rev / "summary.md").write_text(_summary_md(conn, run_id, run, boards, cfg), encoding="utf-8")
    files = [{"path": str(x.relative_to(rev)).replace("\\", "/"),
              "sha256": _sha256(x), "size": x.stat().st_size}
             for x in sorted(rev.rglob("*")) if x.is_file()]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "publication_id": revision_id,
        "gate": gate_result,
        "run_id": run_id,
        "generated_at": db.now_iso(),
        "formula_contract": run["formula_contract"] if run else None,
        "screen_contract": run["screen_contract"] if run else None,
        "profile": run["profile"] if run else None,
        "counts": {b: len(rows) for b, rows in boards.items()},
        "files": files,
    }
    (rev / "validated.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return {"revision_id": revision_id, "revision_path": str(rev), "manifest": manifest}


def _write_diagnostics(conn, rev: Path, run_id: str) -> None:
    """覆盖 / 冲突 / 缺失三张诊断表：只描述证据状态，不改变任何决策。"""
    cov = conn.execute(
        "SELECT c.fcode, c.nav_points, c.nav_first_date, c.nav_last_date, c.gap_max_days,"
        " c.freshness_days, c.flags_json FROM fund_coverage c ORDER BY c.fcode").fetchall()
    _write_csv(rev / "diagnostics" / "coverage.csv", [dict(r) for r in cov],
               ["fcode", "nav_points", "nav_first_date", "nav_last_date", "gap_max_days",
                "freshness_days", "flags_json"])

    conf = conn.execute(
        "SELECT fcode, conflict_json FROM fund_profile WHERE conflict_json IS NOT NULL"
        " ORDER BY fcode").fetchall()
    _write_csv(rev / "diagnostics" / "conflicts.csv", [dict(r) for r in conf],
               ["fcode", "conflict_json"])

    fields = ["asset_scale_billion", "mgr_fee_pct", "trust_fee_pct", "sales_fee_pct",
              "recurring_fee_pct", "estab_date", "benchmark_text"]
    nulls = []
    total = conn.execute("SELECT COUNT(*) FROM fund").fetchone()[0] or 1
    for field in fields:
        if field == "estab_date":
            n = conn.execute("SELECT COUNT(*) FROM fund WHERE estab_date IS NULL").fetchone()[0]
        else:
            n = conn.execute(f"SELECT COUNT(*) FROM fund_profile WHERE {field} IS NULL").fetchone()[0]
        nulls.append({"field": field, "null_count": n, "null_ratio_pct": round(100.0 * n / total, 2)})
    for field in ("annual_dec", "vol_dec", "sharpe", "sortino", "calmar_dec"):
        n = conn.execute(
            "SELECT COUNT(*) FROM fund_metric WHERE period_days=365 AND formula_contract=?"
            " AND " + field + " IS NULL",
            (conn.execute("SELECT formula_contract FROM screen_run WHERE run_id=?",
                          (run_id,)).fetchone()[0],)).fetchone()[0]
        nulls.append({"field": "metric_1y." + field, "null_count": n,
                      "null_ratio_pct": round(100.0 * n / total, 2)})
    _write_csv(rev / "diagnostics" / "nulls.csv", nulls,
               ["field", "null_count", "null_ratio_pct"])

    # 增强债"差一点"清单：逐只列出准入项通过几项、差在哪几项，并带上该风格
    # 当时的相对门槛（ADR-031 之后门槛是按同风格分位算的，必须一起给出才可解释）。
    near = []
    for r in conn.execute(
            "SELECT d.fcode, f.name, d.style_code, d.reason_codes_json, d.thresholds_json"
            " FROM screen_decision d JOIN fund f ON f.fcode=d.fcode"
            " WHERE d.run_id=? AND d.layer_reached='L6E' ORDER BY d.fcode", (run_id,)):
        payload = json.loads(r["thresholds_json"] or "{}") or {}
        checks = payload.get("fixed_or_admission") or {}
        if not checks:
            continue
        meta = payload.get("enhanced_relative_meta") or {}
        ok_n = sum(1 for v in checks.values() if isinstance(v, dict) and v.get("pass"))
        near.append({"fcode": r["fcode"], "name": r["name"], "style_code": r["style_code"],
                     "checks_passed": ok_n, "checks_total": len(checks),
                     "failed": ";".join(sorted(k for k, v in checks.items()
                                               if isinstance(v, dict) and not v.get("pass"))),
                     "peers": meta.get("n"),
                     "annual_min_pct": meta.get("annual_min_pct"),
                     "annual_1y_pct": (checks.get("annual_1y") or {}).get("value"),
                     "sharpe": (checks.get("sharpe") or {}).get("value"),
                     "calmar": (checks.get("calmar") or {}).get("value"),
                     "mdd": (checks.get("mdd") or {}).get("value"),
                     "positive_pct": (checks.get("positive") or {}).get("value")})
    near.sort(key=lambda x: (-x["checks_passed"], x["fcode"]))
    _write_csv(rev / "diagnostics" / "enhanced_near_miss.csv", near,
               ["fcode", "name", "style_code", "checks_passed", "checks_total", "failed",
                "peers", "annual_min_pct", "annual_1y_pct", "sharpe", "calmar", "mdd",
                "positive_pct"])

    # 同家族重复入榜（A/C 份额各自独立入榜是合规的，但会让榜面前排被同一只基金占满）：
    # 只做诊断，不改变排序——份额不合并是硬规则，去重会改变"同一口径"承诺。
    dup = []
    for r in conn.execute(
            "SELECT sc.family_id, f.company_name, COUNT(*) AS n, GROUP_CONCAT(p.fcode) AS codes,"
            " MIN(p.rank) AS best_rank FROM screen_pick p"
            " JOIN fund_share_class sc ON sc.fcode=p.fcode JOIN fund f ON f.fcode=p.fcode"
            " WHERE p.run_id=? GROUP BY sc.family_id HAVING COUNT(*)>1"
            " ORDER BY n DESC, best_rank", (run_id,)):
        dup.append({"family_id": r["family_id"], "company": r["company_name"], "members": r["n"],
                    "codes": r["codes"], "best_rank": r["best_rank"]})
    _write_csv(rev / "diagnostics" / "family_duplicates.csv", dup,
               ["family_id", "company", "members", "codes", "best_rank"])

    # L8 经理后过滤清单：因"现任经理最短职业年限不足 365 天"而未入选的基金。
    # 这是准入失败，不是数据缺失；缺值时不会出现在本表（缺失不阻断，见 ADR-019）。
    mgr_blocked = []
    for r in conn.execute(
            "SELECT d.fcode, f.name, d.style_code, d.reason_codes_json, p.manager_count,"
            " p.manager_career_min_days, p.manager_career_max_days, p.manager_power_score"
            " FROM screen_decision d JOIN fund f ON f.fcode=d.fcode"
            " LEFT JOIN fund_profile p ON p.fcode=d.fcode"
            " WHERE d.run_id=? AND d.layer_reached='L8'"
            " ORDER BY p.manager_career_min_days, d.fcode", (run_id,)):
        mgr_blocked.append({"fcode": r["fcode"], "name": r["name"], "style_code": r["style_code"],
                            "manager_count": r["manager_count"],
                            "career_min_days": r["manager_career_min_days"],
                            "career_max_days": r["manager_career_max_days"],
                            "power_score_em": r["manager_power_score"],
                            "reason_codes": r["reason_codes_json"]})
    _write_csv(rev / "diagnostics" / "manager_blocked.csv", mgr_blocked,
               ["fcode", "name", "style_code", "manager_count", "career_min_days",
                "career_max_days", "power_score_em", "reason_codes"])

    # 风格证据覆盖：多少只基金用到了结构化持仓证据（zqcc 是季报、按需增量采集）。
    style_rows = []
    for r in conn.execute(
            "SELECT s.style_code, s.confidence, COUNT(*) AS n,"
            " SUM(CASE WHEN s.evidence_json LIKE '%holding_total_pct%'"
            " AND json_extract(s.evidence_json, '$.holding_total_pct') > 0 THEN 1 ELSE 0 END) AS with_holdings"
            " FROM fund_style s JOIN fund f ON f.fcode=s.fcode"
            " GROUP BY 1,2 ORDER BY n DESC"):
        style_rows.append({"style_code": r["style_code"], "confidence": r["confidence"],
                           "n": r["n"], "with_holdings": r["with_holdings"]})
    _write_csv(rev / "diagnostics" / "style_evidence.csv", style_rows,
               ["style_code", "confidence", "n", "with_holdings"])

    # 家族去重视图（只做诊断，不替代榜单）：A/C/E 份额各自独立入榜是硬规则，
    # 但"到底有多少只不同的基金"要看家族维度，否则 149 个名额只对应 111 只基金。
    fam = []
    for r in conn.execute(
            "SELECT sc.family_id, f.company_name, COUNT(*) AS members,"
            " MIN(p.rank) AS best_rank, MIN(p.fcode) AS representative,"
            " GROUP_CONCAT(p.fcode) AS codes, MAX(p.formal_score) AS best_score,"
            " d.style_code FROM screen_pick p"
            " JOIN fund_share_class sc ON sc.fcode=p.fcode JOIN fund f ON f.fcode=p.fcode"
            " LEFT JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
            " WHERE p.run_id=? AND p.board='bond_stable'"
            " GROUP BY sc.family_id ORDER BY best_rank", (run_id,)):
        fam.append({"best_rank": r["best_rank"], "family_id": r["family_id"],
                    "company": r["company_name"], "members": r["members"],
                    "representative": r["representative"], "codes": r["codes"],
                    "style_code": r["style_code"], "best_score": r["best_score"]})
    _write_csv(rev / "diagnostics" / "bond_stable_by_family.csv", fam,
               ["best_rank", "family_id", "company", "members", "representative", "codes",
                "style_code", "best_score"])

    # 尾部指标覆盖（V2 影子，不参与筛选）：完整度分布与主要缺失原因。
    tail_rows = []
    for r in conn.execute(
            "SELECT s.style_code, COUNT(*) AS n,"
            " SUM(CASE WHEN t.completeness = 1.0 THEN 1 ELSE 0 END) AS full,"
            " ROUND(AVG(t.completeness), 4) AS avg_completeness,"
            " ROUND(AVG(t.mdd_dec), 4) AS avg_mdd_3y"
            " FROM fund_style s LEFT JOIN fund_tail_metric t"
            " ON t.fcode=s.fcode AND t.window_days=? AND t.formula_contract=?"
            " GROUP BY 1 ORDER BY n DESC", (TAIL_WINDOW_DAYS, TAIL_CONTRACT)):
        tail_rows.append(dict(r))
    _write_csv(rev / "diagnostics" / "tail_coverage.csv", tail_rows,
               ["style_code", "n", "full", "avg_completeness", "avg_mdd_3y"])

    # 费用阶梯覆盖：只有采到 jjfl 的基金才能算"到手收益"，此处显式暴露缺口。
    fee_rows = []
    for r in conn.execute(
            "SELECT f.fcode, f.name,"
            " CASE WHEN m.fcode IS NULL THEN 0 ELSE 1 END AS has_meta,"
            " (SELECT COUNT(*) FROM fund_fee_tier t WHERE t.fcode=f.fcode AND t.kind='purchase') AS purchase_tiers,"
            " (SELECT COUNT(*) FROM fund_fee_tier t WHERE t.fcode=f.fcode AND t.kind='redemption') AS redemption_tiers,"
            " m.mgr_fee_pct, m.trust_fee_pct, m.sales_fee_pct"
            " FROM fund f LEFT JOIN fund_fee_meta m ON m.fcode=f.fcode"
            " ORDER BY has_meta DESC, f.fcode"):
        fee_rows.append(dict(r))
    _write_csv(rev / "diagnostics" / "fee_coverage.csv", fee_rows,
               ["fcode", "name", "has_meta", "purchase_tiers", "redemption_tiers",
                "mgr_fee_pct", "trust_fee_pct", "sales_fee_pct"])

    # 缓释证据（ADR-047）：单一原因落榜者逐只留痕。放行 0 只也是结论，
    # 所以"检查过但被哪一条挡住"必须进产物，而不是只在内存里算一次。
    relief = []
    for r in conn.execute(
            "SELECT d.fcode, f.name, d.style_code, d.scores_json, d.relief_json"
            " FROM screen_decision d JOIN fund f ON f.fcode = d.fcode"
            " WHERE d.run_id = ? AND d.relief_json IS NOT NULL ORDER BY d.fcode", (run_id,)):
        rj = json.loads(r["relief_json"] or "{}") or {}
        det = rj.get("detail") or {}
        sc = json.loads(r["scores_json"] or "{}") or {}
        relief.append({"fcode": r["fcode"], "name": r["name"], "style_code": r["style_code"],
                       "relief_type": rj.get("type"), "allowed": rj.get("allowed"),
                       "blocked": det.get("blocked"), "value": det.get("value"),
                       "threshold": det.get("threshold"), "gap": det.get("gap"),
                       "composite": sc.get("composite"), "rule": det.get("rule")})
    _write_csv(rev / "diagnostics" / "relief_candidates.csv", relief,
               ["fcode", "name", "style_code", "relief_type", "allowed", "blocked",
                "value", "threshold", "gap", "composite", "rule"])

    # 跨源校验汇总（§16.1）：把当日核对结果冻结进发布，事后可复核"当时对不对得上"
    from ..validate import cross as cross_mod
    cross_summary = cross_mod.summarize(conn)
    (rev / "diagnostics" / "cross_check_summary.json").write_text(
        json.dumps(cross_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 前瞻状态（§15）：cohort 数量、成熟/未成熟观测、证据等级
    from .. import forward as forward_mod
    fwd = forward_mod.summary(conn)
    (rev / "diagnostics" / "forward_status.json").write_text(
        json.dumps(fwd, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_decisions(conn, rev: Path, run_id: str, cfg=None) -> None:
    """每只入选基金一份决策快照：阈值、分数、原因码、指标，可独立审计。"""
    run = conn.execute("SELECT * FROM screen_run WHERE run_id=?", (run_id,)).fetchone()
    contract = run["formula_contract"] if run else None
    rows = conn.execute(
        "SELECT p.board, p.rank, p.fcode, p.formal_score, p.stability_rank, p.confidence,"
        " d.layer_reached, d.passed, d.style_code, d.reason_codes_json, d.thresholds_json,"
        " d.scores_json, f.name"
        " FROM screen_pick p JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
        " JOIN fund f ON f.fcode=p.fcode WHERE p.run_id=? ORDER BY p.board, p.rank",
        (run_id,)).fetchall()
    for r in rows:
        metrics = {}
        for m in conn.execute(
                "SELECT period_days, n_obs, total_return_pct, annual_dec, vol_dec, sharpe, sortino,"
                " calmar_dec, mdd_dec, positive_ratio_pct, undefined_reasons_json"
                " FROM fund_metric WHERE fcode=? AND formula_contract=?", (r["fcode"], contract)):
            metrics[str(m["period_days"])] = {k: m[k] for k in m.keys() if k != "period_days"}
        mgr = conn.execute(
            "SELECT p.manager_count, p.manager_career_min_days, p.manager_career_max_days,"
            " p.manager_power_score FROM fund_profile p WHERE p.fcode=?", (r["fcode"],)).fetchone()
        members = [dict(m) for m in conn.execute(
            "SELECT mgr_id, mgr_name, is_lead, career_days, tenure_return_pct, power_avr, star"
            " FROM fund_manager_evidence WHERE fcode=? ORDER BY is_lead DESC, career_days DESC",
            (r["fcode"],))]
        payload = {
            "fcode": r["fcode"], "name": r["name"], "board": r["board"], "rank": r["rank"],
            "formal_score": r["formal_score"], "stability_rank": r["stability_rank"],
            "confidence": r["confidence"], "layer_reached": r["layer_reached"],
            "passed": bool(r["passed"]), "style_code": r["style_code"],
            "reason_codes": json.loads(r["reason_codes_json"] or "[]"),
            "thresholds": json.loads(r["thresholds_json"] or "{}"),
            "scores": json.loads(r["scores_json"] or "{}"),
            "metrics": metrics,
            "manager": {
                "count": mgr["manager_count"] if mgr else None,
                "career_min_days": mgr["manager_career_min_days"] if mgr else None,
                "career_max_days": mgr["manager_career_max_days"] if mgr else None,
                # 东财 power.avr（0–100），不是 MGOLD 10 分制，禁止映射
                "power_score_em": mgr["manager_power_score"] if mgr else None,
                "members": members,
            },
            "run_id": run_id,
            "formula_contract": contract,
            "standard_metric_claim": False,
            "quality": quality_mod.profile(conn, cfg, r["fcode"]) if cfg is not None else None,
        }
        (rev / "decisions" / (r["fcode"] + ".json")).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _evidence_sentence(run, ev: dict[str, Any]) -> str:
    """首屏证据披露：只在证据登记的 as_of 与本次 run 一致时引用数字。"""
    as_of = None
    if run is not None:
        try:
            as_of = run["nav_as_of"]
        except (KeyError, IndexError):
            as_of = None
    if not ev or ev.get("as_of") != as_of:
        return ("未登记与本次 as_of 匹配的历史 IC/DSR 证据；在证据补齐前，"
                "不应把榜单当作“经过验证的选基能力”。")
    return ("%s 队列回测（as_of %s）：排序分对前瞻年化 IC %+.3f、对 MDD IC %+.3f；"
            "DSR %.3f（n_trials=%s，<0.95），不能拒绝“无技能”。来源：%s"
            % (ev.get("cohorts", "—"), as_of, float(ev.get("rank_ic_annual", 0.0)),
               float(ev.get("rank_ic_mdd", 0.0)), float(ev.get("dsr", 0.0)),
               ev.get("n_trials", "—"), ev.get("source", "—")))


def _summary_md(conn, run_id: str, run, boards: dict[str, list[dict[str, Any]]],
                cfg=None) -> str:
    from .. import forward as _fwd
    _fs = _fwd.summary(conn)
    ev = (cfg.get("evidence", default={}) if cfg is not None else {}) or {}
    lines = ["# 债券型基金筛选报告 %s" % run_id, ""]
    lines += ["> **这不是买入建议。** 榜单是规则筛出的候选池，不是收益保证，也不构成对未来"
              "业绩的预测；请按自己的持有期、回撤承受力和税费情况独立判断。",
              ">",
              "> **证据等级：%s**（前瞻 cohort %s 个、成熟观测 %s 条）。%s"
              % (_fs.get("evidence_label") or "—", _fs.get("cohorts", 0),
                 _fs.get("matured_formal_observations", 0), _evidence_sentence(run, ev)),
              ">",
              "> 完整十节报告（长期表、组合体检、费用与反面证据）：[report.md](report.md)",
              ""]
    if run:
        stable_n = len(boards.get("bond_stable") or [])
        enhanced_n = len(boards.get("bond_enhanced") or [])
        picked_n = stable_n + enhanced_n
        def _run_val(key):
            try:
                return run[key]
            except (KeyError, IndexError):
                return None
        l9_raw = _run_val("l9_pass_count")
        l9_pass = int(l9_raw if l9_raw is not None else (run["formal_count"] or 0))
        lines += ["- 风格档案：%s" % label_profile(run["profile"]),
                  "- 公式契约：%s" % run["formula_contract"],
                  "- 候选池：%s 只；基础门禁通过：%s 只；通过准入门槛（L9）：%s 只；"
                  "正式入榜：%s 只（稳健 %s + 增强 %s）；核心精选：%s 只；收益精选 10：%s 只；"
                  "持有期受限：%s 只"
                  % (run["universe_count"], run["base_pass_count"], l9_pass,
                     picked_n, stable_n, enhanced_n, len(boards.get("bond_core") or []),
                     len(boards.get("bond_core10") or []), run["locked_count"]), ""]
        if l9_pass > picked_n:
            try:
                trim = json.loads(_run_val("board_trim_json") or "{}") or {}
            except (TypeError, ValueError):
                trim = {}
            labels = {
                "family_cap": "同基金多份额折叠",
                "company_cap": "公司上限",
                "enhanced_style_quota": "增强每风格配额",
                "enhanced_total_quota": "增强总量配额",
                "formal_target_trim": "双榜目标/比例截断",
            }
            desc = "、".join("%s %s" % (labels.get(k, k), (v or {}).get("n", 0))
                            for k, v in sorted(trim.items())) if trim else "折叠明细缺失"
            lines += ["- 通过准入但未入榜：%d 只（%s；逐只见 decisions/<fcode>.json 的 passed=true）"
                      % (l9_pass - picked_n, desc), ""]
    hold_period = conn.execute("SELECT MAX(report_period) FROM src_zqcc_holding").fetchone()[0]
    scope_n = conn.execute(
        "SELECT COUNT(*) FROM fund_buyable_daily WHERE buyable_state='confirmed_yes'"
        " AND business_date=(SELECT MAX(business_date) FROM fund_buyable_daily)").fetchone()[0]
    # 分子必须与分母同期同域：最新报告期 ∩ 当日可申购。旧实现用全历史持仓数
    # 除以当日可申购数，覆盖率可 >100%，80% 闸门名存实亡。
    hold_n = conn.execute(
        "SELECT COUNT(DISTINCT h.code) FROM src_zqcc_holding h"
        " WHERE h.report_period=?"
        " AND h.code IN (SELECT fcode FROM fund_buyable_daily"
        "   WHERE buyable_state='confirmed_yes'"
        "   AND business_date=(SELECT MAX(business_date) FROM fund_buyable_daily))",
        (hold_period,)).fetchone()[0]
    if run:
        cov = (hold_n / scope_n) if scope_n else 0.0
        lines += ["- 结构化持仓证据（zqcc）：%s 只 / 采集范围 %s 只（%.0f%%，报告期 %s）；覆盖率低于 80%% 时"
                  "整体不用持仓证据，风格统一由类型+名称判定"
                  % (hold_n, scope_n, 100.0 * cov, hold_period or "—"), ""]
    tail_n = conn.execute("SELECT COUNT(*) FROM fund_tail_metric WHERE window_days=?"
                          " AND formula_contract=?",
                          (TAIL_WINDOW_DAYS, TAIL_CONTRACT)).fetchone()[0]
    tail_full = conn.execute("SELECT COUNT(*) FROM fund_tail_metric WHERE window_days=?"
                             " AND formula_contract=? AND completeness=1.0",
                             (TAIL_WINDOW_DAYS, TAIL_CONTRACT)).fetchone()[0]
    fee_n = conn.execute("SELECT COUNT(*) FROM fund_fee_meta").fetchone()[0]
    fee_tiers = conn.execute("SELECT COUNT(DISTINCT fcode) FROM fund_fee_tier").fetchone()[0]
    lines += ["- V2 尾部指标（3 年，影子口径）：%s 只，其中 8/8 组件完整 %s 只；不参与正式分与榜单排序"
              % (tail_n, tail_full),
              "- 交易费用阶梯（jjfl）：%s 只可算到手收益（%s 只有档位）；缺口见 diagnostics/fee_coverage.csv"
              % (fee_n, fee_tiers), ""]
    # ── 长期质量与风险观察（ADR-083，只做报告，不进入 L1–L10） ─────────────
    formal_rows = (boards.get("bond_stable") or []) + (boards.get("bond_enhanced") or [])
    mrar_n = sum(1 for r in formal_rows if r.get("mrar_3y_pct") is not None)
    alpha_n = sum(1 for r in formal_rows if r.get("alpha_reliable"))
    watch_n = sum(1 for r in formal_rows if (r.get("quality_watch_n") or 0) > 0)
    unknown_n = sum(1 for r in formal_rows if (r.get("quality_unknown_n") or 0) > 0)
    lines += ["## 长期质量与风险观察（ADR-083）", "",
              "质量指标只用于观察清单与报告，不改变正式分、准入或榜单排序。",
              "",
              "- MRAR(2) 三年评级覆盖：%d / %d 只（不足 36 个月或月度收益异常者留空）"
              % (mrar_n, len(formal_rows)),
              "- 因子 alpha 可信覆盖：%d / %d 只（R²≥5%%、样本≥200 天且 HAC t 可算）"
              % (alpha_n, len(formal_rows)),
              "- 质量观察：有 watch 项 %d 只，有 unknown 项 %d 只；逐只见 CSV 的 quality_* 列"
              % (watch_n, unknown_n), ""]
    if formal_rows:
        lines += ["### 稳健榜前十的净费后收益（按榜单顺序）", "",
                  "净费后 = 1 年年化 − 申购费摊销；管理费/托管费/销售服务费已在每日净值计提，"
                  "不重复扣减。本表按榜单顺序，不按净费后收益排序；未扣赎回费。", "",
                  "| 排名 | 代码 | 名称 | 1 年年化 | 持有成本 | 净费后 |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for r in (boards.get("bond_stable") or [])[:10]:
            def _pct(v):
                return "—" if v is None else "%.2f%%" % v
            lines.append("| %s | %s | %s | %s | %s | %s |" % (
                r.get("rank"), r.get("fcode"), r.get("name"), _pct(r.get("annual_1y_pct")),
                _pct(r.get("cost_1y_pct")), _pct(r.get("net_annual_1y_pct"))))
        lines.append("")
    watch_rows = [r for r in formal_rows if (r.get("quality_watch_n") or 0) > 0]
    watch_rows.sort(key=lambda r: (-(r.get("quality_watch_n") or 0), r.get("rank") or 9999))
    if watch_rows:
        lines += ["### 质量观察清单（前 10 条）", "",
                  "| 代码 | 名称 | 榜单 | 状态 | 未通过项 |",
                  "| --- | --- | --- | --- | --- |"]
        for r in watch_rows[:10]:
            lines.append("| %s | %s | %s | %s | %s |" % (
                r.get("fcode"), r.get("name"), label_board(r.get("board") or ""),
                r.get("quality_status"), r.get("quality_flags") or "—"))
        lines.append("")
    # ── 可信度层（Batch A）：跨源校验 / 缓释 / 前瞻 ────────────────────────
    from ..validate import cross as _cross
    _cs = _cross.summarize(conn)
    _rj_n = conn.execute("SELECT COUNT(*) FROM screen_decision WHERE run_id=? AND relief_json"
                         " IS NOT NULL", (run_id,)).fetchone()[0]
    _rj_ok = conn.execute("SELECT COUNT(*) FROM screen_decision WHERE run_id=?"
                          " AND relief_json LIKE ?",
                          (run_id, '%' + '"allowed": true' + '%')).fetchone()[0]
    _gate_note = conn.execute("SELECT status FROM publication_gate WHERE run_id=?",
                              (run_id,)).fetchone()
    lines += ["## 可信度层", "",
              "- 跨源校验：%s 行核对（%s），不一致率 %.4f%%，降级=%s；明细见"
              " diagnostics/cross_check_summary.json"
              % (_cs.get("total", 0),
                 "、".join("%s %s" % (k, v) for k, v in (_cs.get("by_status") or {}).items()) or "—",
                 100.0 * float(_cs.get("mismatch_ratio") or 0.0),
                 "是" if _cs.get("degraded") else "否"),
              "- 单一原因缓释：检查 %s 只，放行 %s 只（ADR-047；逐只见 diagnostics/relief_candidates.csv）"
              % (_rj_n, _rj_ok),
              "- 前瞻验证：cohort %s 个、成熟观测 %s 条、决策月份 %s 个、证据等级 %s"
              % (_fs.get("cohorts", 0), _fs.get("matured_formal_observations", 0),
                 _fs.get("distinct_decision_months", 0), _fs.get("evidence_label")),
              "- 发布门禁：%s（明细见 validated.json 的 gate 段与 diagnostics/publication_gate.json）"
              % ("—" if not _gate_note else _gate_note["status"]), ""]
    lines += ["## 榜单规模", "", "| 榜单 | 数量 |", "| --- | --- |"]
    for board, rows in boards.items():
        lines.append("| %s | %d |" % (label_board(board), len(rows)))
    style_rows = conn.execute(
        "SELECT d.style_code, COUNT(*) AS n FROM screen_pick p"
        " JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
        " WHERE p.run_id=? AND p.board IN ('bond_stable','bond_enhanced')"
        " GROUP BY 1 ORDER BY n DESC", (run_id,)).fetchall()
    if style_rows:
        lines += ["", "## 正式入选的风格分布", "", "| 风格 | 数量 |", "| --- | --- |"]
        for r in style_rows:
            lines.append("| %s | %d |" % (label_style(r["style_code"]), r["n"]))
    lines += ["", "## 稳健榜前十", "",
              "排名按**排序分**（0.85×综合分 + 0.15×6 个月收益分位）降序；**正式分**"
              "（0.45×短期正式分 + 0.55×长期正式分）用于准入与质量展示，不是排序主键。"
              "两者权重不同，名次与正式分不单调是预期行为，不是数据错误。", "",
              "| 排名 | 代码 | 名称 | 风格 | 排序分 | 正式分 | 1 年年化 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in boards["bond_stable"][:10]:
        def _num(v, fmt="%.2f"):
            return "—" if v is None else fmt % v
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            row["rank"], row["fcode"], row["name"], label_style(row["style_code"]),
            _num(row.get("rank_score")), _num(row.get("formal_score")),
            _num(row.get("annual_1y_pct"), "%.2f%%")))
    lines += ["", "## 增强榜前十", ""]
    lines.append("增强债口径：同风格分位（同类样本≥30 才估分位，否则用绝对地板）"
                 " + 正式分≥%.0f + 每风格配额 %d 只（ADR-031）；严格绝对口径仅作诊断，"
                 "逐只差项见 diagnostics/enhanced_near_miss.csv。"
                 % (T.ENHANCED_RELATIVE["formal_score_min"],
                    T.ENHANCED_RELATIVE["quota_per_style"]))
    lines.append("")
    if not boards["bond_enhanced"]:
        lines.append("本轮无增强债入选（一级/二级/转债增强独立准入未达门槛，逐只差项见"
                     " diagnostics/enhanced_near_miss.csv）。")
    for row in boards["bond_enhanced"][:10]:
        lines.append("%d. %s %s（%s，正式分 %.2f，信心 %.2f）"
                     % (row["rank"], row["fcode"], row["name"], label_style(row["style_code"]),
                        row["formal_score"] or 0, row["confidence"] or 0))
    dup_n = conn.execute(
        "SELECT COUNT(*) FROM (SELECT sc.family_id FROM screen_pick p"
        " JOIN fund_share_class sc ON sc.fcode=p.fcode WHERE p.run_id=?"
        " GROUP BY sc.family_id HAVING COUNT(*)>1)", (run_id,)).fetchone()[0]
    if dup_n:
        lines += ["", "## 同家族重复入榜", "",
                  "共 %d 个家族有 2 个及以上份额同时入榜（份额不合并是硬规则，明细见"
                  " diagnostics/family_duplicates.csv）。" % dup_n]
    lines += ["", "> 口径声明：收益为日增长率链式复利的复投总收益；夏普/索提诺/卡玛为策略口径"
              "（standard_metric_claim=false），不可与公开标准值直接比较。", ""]
    return "\n".join(lines)