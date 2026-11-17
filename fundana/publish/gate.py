"""发布门禁（§16.8）：12 条规则，命中 block 就不切换 .publication_current。

设计要点：
- 门禁是**发布前**的最后一道业务判定，不替代 verify（verify 管文件完整性）；
- 每条规则都返回可读证据（evidence），前端与 summary.md 直接展示；
- 规则可以 not_applicable（例如没有指数插件时的 index_repair_pending），
  但不得静默跳过：not_applicable 也要出现在 checks 里，证明"检查过"。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import db, freshness, fundtypes
from ..data_health import provenance_audit

FORMAL_BOARDS = ("bond_stable", "bond_enhanced")
REQUIRED_SOURCES = ("em_directory", "em_status_snapshot", "em_nav_snapshot")
SEVERITY = {"block": "block", "warn": "warn", "info": "info"}


class PublicationBlocked(RuntimeError):
    """门禁阻断：不允许切换 current。"""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        blocks = [c["code"] for c in result.get("checks", []) if c["status"] == "block"]
        super().__init__("publication_blocked: " + ", ".join(blocks))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check(code: str, severity: str, status: str, detail: str,
           evidence: Any = None) -> dict[str, Any]:
    return {"code": code, "severity": severity, "status": status,
            "detail": detail, "evidence": evidence}


def formal_board_drift(data: dict[str, Any]) -> float | None:
    """正式榜与上一批的对称换手率（按基金去重，不把跨榜移动重复计数）。

    旧实现把每个榜的 added+removed 相加再除以 2n，基金从稳健榜移到增强榜
    会被同时记一次“移出”和一次“加入”，把一次风格重分类放大成两次换手。
    这里用并集：changed / (prev_n + cur_n)。
    """
    boards = (data or {}).get("boards") or {}
    added: set[str] = set()
    removed: set[str] = set()
    cur_n = prev_n = 0
    for board in FORMAL_BOARDS:
        b = boards.get(board) or {}
        added.update(b.get("added") or [])
        removed.update(b.get("removed") or [])
        cur_n += int(b.get("n") or 0)
        prev_n += int(b.get("prev_n") or 0)
    if not cur_n and not prev_n:
        return None
    # 跨榜移动会同时出现在 added/removed；交集是“同一批内换榜”，不算进出。
    changed = (added | removed) - (added & removed)
    return len(changed) / float(cur_n + prev_n)


def _screen_run_counts(conn, run_id: str) -> dict[str, Any]:
    """读 screen_run 计数列；旧库/测试夹具没有 0037 新列时自动降级。

    直接 SELECT l9_pass_count 会在未迁移的库上抛 OperationalError，把门禁
    变成异常而不是可读的 block/warn。这里按实际列名取。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(screen_run)")}
    wanted = [c for c in ("formal_count", "l9_pass_count", "published_count",
                          "board_trim_json") if c in cols]
    if not wanted:
        return {}
    row = conn.execute("SELECT %s FROM screen_run WHERE run_id=?"
                       % ",".join(wanted), (run_id,)).fetchone()
    return {k: row[k] for k in wanted} if row else {}


def boundary_share(conn, run_id: str, batch_id: str | None,
                   tol: float = 1.0) -> tuple[float | None, int, float | None]:
    """正式榜里综合分距动态综合线 <= tol 分的只数占比（ADR-063）。

    占比高说明这批榜单是被阈值抖动决定的，而不是质量差异决定的：同一批数据下
    分位线移动 1 分就能换掉一批人。返回 (share, near, threshold)。
    """
    if not batch_id:
        return None, 0, None
    row = conn.execute(
        "SELECT enter_value FROM screen_threshold"
        " WHERE batch_id=? AND scope='global' AND metric IN ('composite_min','composite')",
        (batch_id,)).fetchone()
    if not row or row["enter_value"] is None:
        return None, 0, None
    threshold = float(row["enter_value"])
    rows = conn.execute(
        "SELECT d.scores_json FROM screen_pick p"
        " JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
        " WHERE p.run_id=? AND p.board IN (?,?)", (run_id,) + FORMAL_BOARDS).fetchall()
    near = 0
    total = 0
    for r in rows:
        try:
            scores = json.loads(r["scores_json"] or "{}")
        except ValueError:
            continue
        comp = scores.get("composite")
        if comp is None:
            continue
        total += 1
        if abs(float(comp) - threshold) <= tol:
            near += 1
    if not total:
        return None, 0, threshold
    return round(near / total, 4), near, threshold


def company_cap_stats(conn, run_id: str, cap: int = 2) -> dict[str, Any]:
    """正式榜单公司只数（P0-6）：每个榜单独计数，超过 cap 即违约。

    旧实现按榜单规模 5% 浮动（100 只→5），合同写的是固定 2；这里用发布结果
    反查，避免排序代码改了但报告仍按旧口径解释。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fund)")}
    if "company_name" not in cols:
        return {"checked": False, "cap": cap, "violations": [], "max_count": 0}
    rows = conn.execute(
        "SELECT p.board AS board, f.company_name AS company, COUNT(*) AS n"
        " FROM screen_pick p JOIN fund f ON f.fcode=p.fcode"
        " WHERE p.run_id=? AND p.board IN (?,?)"
        "  AND f.company_name IS NOT NULL AND TRIM(f.company_name)<>''"
        " GROUP BY p.board, f.company_name ORDER BY n DESC",
        (run_id,) + FORMAL_BOARDS).fetchall()
    by_board: dict[str, int] = {}
    violations = []
    for r in rows:
        n = int(r["n"])
        by_board[r["board"]] = max(by_board.get(r["board"], 0), n)
        if n > cap:
            violations.append({"board": r["board"], "company": r["company"], "n": n})
    return {"checked": True, "cap": cap, "violations": violations,
            "max_count": max(by_board.values()) if by_board else 0,
            "by_board": by_board}


def board_ratio_stats(conn, run_id: str, min_ratio: float = 0.25) -> dict[str, Any]:
    """正式双榜较小一侧占发布数的比例（P0-6）。

    合同要求较小榜至少 25%；一侧为空时不做比例约束（由 no_formal_members /
    enhanced_empty 单独判定），避免样本不足时把另一侧也清零。
    """
    counts = {r["board"]: int(r["n"]) for r in conn.execute(
        "SELECT board, COUNT(*) AS n FROM screen_pick"
        " WHERE run_id=? AND board IN (?,?) GROUP BY board",
        (run_id,) + FORMAL_BOARDS).fetchall()}
    stable = counts.get("bond_stable", 0)
    enhanced = counts.get("bond_enhanced", 0)
    total = stable + enhanced
    if not stable or not enhanced:
        return {"applicable": False, "stable": stable, "enhanced": enhanced,
                "total": total, "min_ratio": min_ratio}
    small = min(stable, enhanced)
    ratio = small / total if total else 0.0
    return {"applicable": True, "stable": stable, "enhanced": enhanced,
            "total": total, "small": small, "ratio": round(ratio, 4),
            "min_ratio": min_ratio, "pass": ratio + 1e-9 >= min_ratio}


def _average_ranks(values: list[float]) -> list[float]:
    """平均秩（处理并列），用于不依赖 scipy 的 Spearman。"""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(values[order[j + 1]] - values[order[i]]) <= 1e-12:
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    rx, ry = _average_ranks(xs), _average_ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx)
    dy = sum((b - my) ** 2 for b in ry)
    den = (dx * dy) ** 0.5
    return None if den == 0 else num / den


def rank_order_stats(conn, run_id: str) -> dict[str, Any]:
    """正式榜名次是否严格按 rank_score 降序（P0-6）。

    rank 由 _rank 用 rank_score 主键生成；若二者出现严格逆序，说明排序/落库不一致，
    报告展示的“名次”就不可信。并列分数不算逆序，因为次级键决定先后。
    """
    rows = conn.execute(
        "SELECT board, rank, rank_score FROM screen_pick"
        " WHERE run_id=? AND board IN (?,?) ORDER BY board, rank",
        (run_id,) + FORMAL_BOARDS).fetchall()
    by_board: dict[str, list[Any]] = {}
    for r in rows:
        by_board.setdefault(r["board"], []).append(r)
    out = {"boards": {}, "n": 0, "inversions": 0, "missing": 0, "spearman": None}
    rhos: list[float] = []
    for board, items in by_board.items():
        scores = [None if r["rank_score"] is None else float(r["rank_score"]) for r in items]
        missing = sum(1 for s in scores if s is None)
        inversions = 0
        for i in range(len(scores)):
            for j in range(i + 1, len(scores)):
                if scores[i] is not None and scores[j] is not None and scores[i] + 1e-9 < scores[j]:
                    inversions += 1
        rho = None
        if not missing:
            rho = spearman([float(r["rank"]) for r in items], [float(s) for s in scores])
            if rho is not None:
                rhos.append(rho)
        out["boards"][board] = {"n": len(items), "inversions": inversions,
                                "missing": missing,
                                "spearman": None if rho is None else round(rho, 6)}
        out["n"] += len(items)
        out["inversions"] += inversions
        out["missing"] += missing
    out["spearman"] = None if not rhos else round(min(rhos), 6)
    return out


def board_turnover(conn, run_id: str, max_share: float = 0.25) -> dict[str, Any]:
    """规则 18：正式榜换手率（ADR-067）。

    换手 = (进入 + 离开) / 并集。只比同一 formula_contract 的上一份发布；契约变更
    标 not_applicable，避免把口径升级读成榜单不稳。阈值 gate.turnover_max。
    """
    from ..audit import turnover as turnover_mod
    prev_pub = conn.execute(
        "SELECT p.run_id, p.generation FROM publication p"
        " WHERE p.status='committed' AND p.run_id<>?"
        " ORDER BY p.generation DESC LIMIT 1", (run_id,)).fetchone()
    if not prev_pub:
        return _check("board_turnover", "warn", "not_applicable",
                      "没有上一份已提交发布可比", {"prev_run_id": None})
    try:
        cmp = turnover_mod.compare(conn, run_id, prev_pub["run_id"])
    except Exception as exc:  # 表缺失/结构不符时不让门禁整体失败
        return _check("board_turnover", "warn", "not_applicable",
                      "换手不可计算：%s" % exc, {"prev_run_id": prev_pub["run_id"]})
    if not cmp["comparable"]:
        return _check("board_turnover", "warn", "not_applicable",
                      "契约 %s → %s 且不同批次，换手不可比"
                      % (cmp["prev_contract"] or "未记录", cmp["contract"] or "未记录"),
                      {"prev_run_id": prev_pub["run_id"], "generation": prev_pub["generation"]})
    formal = [(b, cmp["boards"][b]) for b in turnover_mod.FORMAL_BOARDS]
    worst_board, worst = max(formal, key=lambda kv: kv[1]["turnover"] or 0.0)
    share = worst["turnover"]
    evidence = {"prev_run_id": prev_pub["run_id"], "generation": prev_pub["generation"],
                "boards": {b: {"turnover": v["turnover"], "entered": v["entered"],
                               "left": v["left"]} for b, v in formal}}
    if share is not None and share > max_share:
        return _check("board_turnover", "warn", "warn",
                      "%s榜换手 %.1f%% > %.0f%%（进入 %d / 离开 %d）"
                      % (worst["label"], 100 * share, 100 * max_share,
                         worst["entered"], worst["left"]), evidence)
    return _check("board_turnover", "warn", "pass",
                  "正式榜换手 %s（上限 %.0f%%）"
                  % ("-" if share is None else "%.1f%%" % (100 * share), 100 * max_share),
                  evidence)


def cross_board_family_stats(conn, run_id: str) -> dict[str, Any]:
    """P1-6：跨榜 family 重叠统计。

    core/core10 是 stable 的集中视图，天然会重叠；这里不强行改榜成员，只把
    “视图重复”与“独立代码数”如实报出来，避免把 10 只 core10 读成 10 个独立 bet。
    """
    boards = ("bond_stable", "bond_enhanced", "bond_core", "bond_core10", "bond_long")
    rows = conn.execute(
        "SELECT p.board, p.fcode, sc.family_id FROM screen_pick p"
        " LEFT JOIN fund_share_class sc ON sc.fcode=p.fcode"
        " WHERE p.run_id=? AND p.board IN (?,?,?,?,?)",
        (run_id,) + boards).fetchall()
    counts: dict[str, int] = {}
    per_board: dict[str, set] = {b: set() for b in boards}
    for r in rows:
        per_board[r["board"]].add(r["fcode"])
        if r["family_id"]:
            counts[r["family_id"]] = counts.get(r["family_id"], 0) + 1
    dup = {k: v for k, v in counts.items() if v > 1}
    return {"rows": len(rows), "unique_codes": len({r["fcode"] for r in rows}),
            "unique_families": len(counts), "duplicate_families": len(dup),
            "max_family_rows": max(counts.values()) if counts else 0,
            "per_board": {b: len(v) for b, v in per_board.items()},
            "duplicate_examples": sorted(dup.items(), key=lambda kv: (-kv[1], kv[0]))[:5]}


def lifecycle_stats(conn) -> dict[str, Any]:
    """P2-4：清盘/合并样本覆盖。没有样本时只警告，不能假装已校正幸存者偏差。"""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM fund_lifecycle GROUP BY status").fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    try:
        with_nav = conn.execute(
            "SELECT COUNT(DISTINCT l.fcode) FROM fund_lifecycle l"
            " WHERE EXISTS (SELECT 1 FROM fund_nav_daily n WHERE n.fcode=l.fcode)"
        ).fetchone()[0]
    except Exception:
        with_nav = 0
    return {"funds": conn.execute(
        "SELECT COUNT(DISTINCT fcode) FROM fund_lifecycle").fetchone()[0],
        "by_status": by_status, "with_nav": with_nav}


def dsr_skill_check(cfg) -> dict[str, Any]:
    """P2-5：DSR 是多重试验后的门槛，只有 >0.95 才能对外说“有选基技能”。"""
    ev = cfg.get("evidence", default={}) or {}
    dsr = ev.get("dsr")
    dsr_min = float(ev.get("dsr_threshold", 0.95))
    if dsr is None:
        return _check("dsr_skill", "warn", "warn",
                      "未登记 DSR：不能声明有选基技能", {"dsr": None})
    if float(dsr) < dsr_min:
        return _check("dsr_skill", "warn", "warn",
                      "DSR %.3f < %.2f：不能拒绝无技能原假设，报告只能写候选池"
                      % (float(dsr), dsr_min),
                      {"dsr": float(dsr), "threshold": dsr_min,
                       "n_trials": ev.get("n_trials")})
    return _check("dsr_skill", "warn", "pass",
                  "DSR %.3f ≥ %.2f" % (float(dsr), dsr_min),
                  {"dsr": float(dsr), "threshold": dsr_min})


def evaluate(conn, cfg, run_id: str, built: dict[str, Any]) -> dict[str, Any]:
    gcfg = (cfg.get("gate", default={}) or {})
    mode = str(gcfg.get("mode", "strict"))
    require_cross = bool(gcfg.get("require_cross_check", True))
    max_age = int(gcfg.get("source_max_age_days", 3))
    cov_min = float(gcfg.get("metric_coverage_min", 0.90))
    mismatch_max = float(gcfg.get("mismatch_ratio_max", 0.01))
    nav_age_limit = int((cfg.get("quality", default={}) or {}).get("nav_age_limit_days", 10))

    checks: list[dict[str, Any]] = []

    # 1 数据源不完整
    missing = [s for s in REQUIRED_SOURCES if s not in cfg.sources]
    stale: list[str] = []
    now = datetime.now(db.CN)
    for sid in REQUIRED_SOURCES:
        if sid in missing:
            continue
        row = conn.execute("SELECT MAX(last_success_at) AS t FROM source_state WHERE source_id=?",
                           (sid,)).fetchone()
        t = row["t"] if row else None
        if not t:
            stale.append(sid + ":never")
            continue
        ts = db.parse_ts(t)
        if ts is None:
            stale.append(sid + ":bad_ts")
            continue
        age = (now - ts).days
        if age > max_age:
            stale.append("%s:%dd" % (sid, age))
    if missing or stale:
        checks.append(_check("source_incomplete", "block", "block",
                             "必需数据源缺失或过期", {"missing": missing, "stale": stale}))
    else:
        checks.append(_check("source_incomplete", "block", "pass", "三个必需源均在 %dd 内成功" % max_age))

    # 2 风险指标未就绪
    formal = conn.execute(
        "SELECT COUNT(*) AS n FROM screen_pick WHERE run_id=? AND board IN (?,?)",
        (run_id,) + FORMAL_BOARDS).fetchone()["n"]
    with_metric = conn.execute(
        "SELECT COUNT(*) AS n FROM screen_pick p JOIN fund_metric m"
        " ON m.fcode=p.fcode AND m.period_days=365 AND m.formula_contract=("
        "   SELECT formula_contract FROM screen_run WHERE run_id=?)"
        " WHERE p.run_id=? AND p.board IN (?,?) AND m.annual_dec IS NOT NULL",
        (run_id, run_id) + FORMAL_BOARDS).fetchone()["n"]
    cov = (with_metric / formal) if formal else None
    if formal and (cov is None or cov < cov_min):
        checks.append(_check("risk_unready", "block", "block",
                             "正式候选 365 天指标覆盖率 %.1f%% < %.0f%%"
                             % (100.0 * (cov or 0), 100.0 * cov_min),
                             {"formal": formal, "with_metric": with_metric}))
    else:
        checks.append(_check("risk_unready", "block", "pass",
                             "365 天指标覆盖 %s/%s" % (with_metric, formal),
                             {"formal": formal, "with_metric": with_metric}))

    # 3 无正式成员
    checks.append(_check("no_formal_members", "block", "block" if formal == 0 else "pass",
                         "正式入榜 0 只" if formal == 0 else "正式入榜 %d 只" % formal,
                         {"formal": formal}))

    # 3b L9 通过数、落库发布数、实际正式榜条数三者必须能对上。
    # 差额可能来自 family/company、增强风格/总量配额、双榜目标/比例截断，
    # 不再统称“family/company 折叠”。
    run_formal_row = _screen_run_counts(conn, run_id)
    run_formal = run_formal_row.get("formal_count")
    l9 = (run_formal_row.get("l9_pass_count")
          if run_formal_row.get("l9_pass_count") is not None else run_formal)
    published_stored = run_formal_row.get("published_count")
    trim = {}
    if run_formal_row.get("board_trim_json"):
        try:
            trim = json.loads(run_formal_row["board_trim_json"]) or {}
        except (TypeError, ValueError):
            trim = {}
    if l9 is None:
        checks.append(_check("formal_count_fold", "warn", "warn",
                             "screen_run 计数缺失，无法核对 L9 与正式榜条数",
                             {"l9_pass": None, "published": formal}))
    elif int(l9) < formal:
        checks.append(_check("formal_count_fold", "block", "block",
                             "L9 通过 %s 少于正式榜 %s，口径不可能成立"
                             % (l9, formal),
                             {"l9_pass": int(l9), "published": formal}))
    elif published_stored is not None and int(published_stored) != formal:
        checks.append(_check("formal_count_fold", "block", "block",
                             "screen_run.published_count=%s 与实际正式榜 %s 不一致"
                             % (published_stored, formal),
                             {"l9_pass": int(l9), "published": formal,
                              "published_stored": int(published_stored)}))
    else:
        folded = int(l9) - formal
        reasons = "、".join("%s %s" % (k, (v or {}).get("n", 0))
                           for k, v in sorted(trim.items())) or "无"
        checks.append(_check("formal_count_fold", "block", "pass",
                             "L9 通过 %s；发布 %s；折叠 %s（%s）"
                             % (l9, formal, folded, reasons),
                             {"l9_pass": int(l9), "published": formal,
                              "folded": folded, "trim": trim}))

    # 4 增强榜为空（可发布，但必须解释）
    enhanced = conn.execute(
        "SELECT COUNT(*) AS n FROM screen_pick WHERE run_id=? AND board='bond_enhanced'",
        (run_id,)).fetchone()["n"]
    checks.append(_check("enhanced_empty", "warn", "warn" if enhanced == 0 else "pass",
                         "增强榜为空（逐只差项见 diagnostics/enhanced_near_miss.csv）"
                         if enhanced == 0 else "增强榜 %d 只" % enhanced, {"enhanced": enhanced}))

    # 5 跨源校验
    from ..validate import cross as cross_mod
    summary = cross_mod.summarize(conn)
    ratio = summary.get("mismatch_ratio")
    if summary.get("total", 0) == 0:
        checks.append(_check("validation_hard_block", "block",
                             "block" if require_cross else "warn",
                             "缺少跨源校验记录（当日未执行 validate --cross）",
                             {"degraded": True}))
    elif ratio is not None and ratio > mismatch_max:
        checks.append(_check("validation_hard_block", "block", "block",
                             "跨源不一致比例 %.2f%% > %.2f%%"
                             % (100.0 * ratio, 100.0 * mismatch_max),
                             {"total": summary["total"], "by_status": summary["by_status"]}))
    else:
        checks.append(_check("validation_hard_block", "block", "pass",
                             "跨源校验 %s 条，不一致 %.2f%%，同日可比 %s 只（%.1f%%）"
                             % (summary["total"], 100.0 * (ratio or 0),
                                summary.get("same_day", 0),
                                100.0 * (summary.get("same_day_ratio") or 0)),
                             {"total": summary["total"], "by_status": summary["by_status"],
                              "same_day": summary.get("same_day"),
                              "same_day_ratio": summary.get("same_day_ratio")}))

    # 6 产物与 manifest 不一致
    rev = Path(built["revision_path"])
    files = (built.get("manifest") or {}).get("files") or []
    bad: list[str] = []
    for entry in files:
        rel = entry.get("path") or ""
        path = rev / rel
        if not path.exists():
            bad.append(rel + ":missing")
            continue
        if path.stat().st_size != entry.get("size"):
            bad.append(rel + ":size")
        elif _sha256(path) != entry.get("sha256"):
            bad.append(rel + ":sha")
    checks.append(_check("artifact_mismatch", "block", "block" if bad else "pass",
                         "产物校验失败 %d 个" % len(bad) if bad else "产物 %d 个与 manifest 一致" % len(files),
                         {"bad": bad[:20]}))

    # 7 revision 未校验
    manifest = built.get("manifest") or {}
    need = ("schema_version", "publication_id", "run_id", "files")
    lacking = [k for k in need if k not in manifest]
    escaped = [e.get("path") for e in files
               if str(e.get("path", "")).startswith("/") or ".." in Path(str(e.get("path", ""))).parts]
    if lacking or escaped or manifest.get("schema_version") != 1:
        checks.append(_check("revision_unvalidated", "block", "block", "manifest 不合法",
                             {"lacking": lacking, "escaped": escaped[:5]}))
    else:
        checks.append(_check("revision_unvalidated", "block", "pass", "manifest 结构合法"))

    # 8 数据源被禁用（注册表里没有 = 不可用）
    checks.append(_check("source_disabled", "block", "block" if missing else "pass",
                         "缺失注册源 %s" % missing if missing else "所有必需源均已注册",
                         {"missing": missing}))

    # 9 研究类产物降级
    paths = {e.get("path") for e in files}
    research_need = ("diagnostics/enhanced_near_miss.csv",)
    lacking_research = [p for p in research_need if p not in paths]
    checks.append(_check("research_degraded", "warn", "warn" if lacking_research else "pass",
                         "研究类产物缺失：%s" % lacking_research if lacking_research else "研究类产物齐全",
                         {"missing": lacking_research}))

    # 10 前瞻未就绪
    matured = conn.execute("SELECT COUNT(*) AS n FROM forward_observation WHERE status='matured'"
                           ).fetchone()["n"]
    checks.append(_check("forward_not_ready", "warn", "warn" if matured == 0 else "pass",
                         "尚无成熟前瞻 cohort（not_ready 不是 0 收益）" if matured == 0
                         else "成熟前瞻观测 %d 条" % matured, {"matured": matured}))

    # 11 盘中估值不可用（源已下线，固定记 warn）
    checks.append(_check("valuation_unavailable", "warn", "warn",
                         "盘中估值源（fundgz）已下线，系统不提供实时估值，只发布已确认净值",
                         {"source": "fundgz", "state": "offline"}))

    # 12 发布数据过期：用「覆盖率 >=90% 的有效净值日」，不是 MAX(nav_date)。
    # 2026-11-03 只有 185 只披露 11-02，MAX 会把发布误判成新鲜；有效日仍是 10-30。
    run_row = conn.execute("SELECT * FROM screen_run WHERE run_id=?", (run_id,)).fetchone()
    run = dict(run_row) if run_row else {}
    biz = run.get("business_date") or conn.execute(
        "SELECT MAX(business_date) AS d FROM fund_buyable_daily").fetchone()["d"]
    nav_as_of = run.get("nav_as_of") or biz
    fresh = freshness.assess(conn, business_date=nav_as_of,
                             types=fundtypes.resolve(cfg=cfg),
                             formula_contract=run.get("formula_contract"))
    nav = fresh.get("nav") or {}
    metric = fresh.get("metric") or {}
    metric_as_of = metric.get("metric_as_of")
    current_pct = metric.get("current_pct")
    evidence = {"business_date": biz, "nav_as_of": nav_as_of,
                "effective_nav_date": nav.get("effective_date"),
                "max_nav_date": nav.get("max_date"),
                "nav_coverage_pct": nav.get("effective_coverage_pct"),
                "metric_as_of": metric_as_of, "metric_current_pct": current_pct,
                "freshness_status": fresh.get("status"),
                "warnings": fresh.get("warnings")}
    block_reasons = []
    if nav.get("effective_date") is None:
        block_reasons.append("无有效净值日")
    if fresh.get("status") == "blocked":
        block_reasons.append("指标未跟上有效日")
    if metric_as_of and nav_as_of and metric_as_of < nav_as_of:
        block_reasons.append("指标截止 %s 早于净值日 %s" % (metric_as_of, nav_as_of))
    if block_reasons:
        checks.append(_check("stale_publication", "block", "block",
                             "；".join(block_reasons), evidence))
    elif fresh.get("status") == "warning" or (current_pct is not None and current_pct < 100.0 * cov_min):
        checks.append(_check("stale_publication", "block", "warn",
                             "有效净值日 %s / 指标 %s（覆盖 %.2f%%）"
                             % (nav.get("effective_date"), metric_as_of, current_pct or 0.0), evidence))
    else:
        checks.append(_check("stale_publication", "block", "pass",
                             "有效净值日 %s（覆盖 %.2f%%）/ 指标 %s（当前 %.2f%%）"
                             % (nav.get("effective_date"), nav.get("effective_coverage_pct") or 0.0,
                                metric_as_of, current_pct or 0.0), evidence))

    # 12b 季度/事件数据陈旧度：持仓、费率、规模不是每日更新，必须显式披露
    def _latest_date(sql: str):
        try:
            row = conn.execute(sql).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def _to_date(value):
        text = str(value)
        try:
            if "Q" in text:
                year, quarter = text.split("Q", 1)
                month = int(quarter) * 3
                return date(int(year), month, {3: 31, 6: 30, 9: 30, 12: 31}[month])
            return date.fromisoformat(text[:10])
        except (TypeError, ValueError, KeyError):
            return None

    def _age(value):
        ref = metric_as_of or biz
        parsed = _to_date(value) if value else None
        if parsed is None or not ref:
            return None
        try:
            return max(0, (date.fromisoformat(ref[:10]) - parsed).days)
        except (TypeError, ValueError):
            return None

    holding_date = _latest_date("SELECT MAX(report_period) FROM fund_holding_bond")
    fee_date = _latest_date("SELECT MAX(observed_at) FROM fund_fee_meta")
    scale_date = _latest_date("SELECT MAX(report_period) FROM fund_scale_history")
    profile_date = _latest_date("SELECT MAX(updated_at) FROM fund_profile")
    ages = {"holding": _age(holding_date), "fee": _age(fee_date),
            "scale": _age(scale_date), "profile": _age(profile_date)}
    stale = []
    if ages["holding"] is not None and ages["holding"] > 540:
        stale.append(("block", "持仓报告期 %s 已 %s 天，超过 540 天" % (holding_date, ages["holding"])))
    elif ages["holding"] is not None and ages["holding"] > 270:
        stale.append(("warn", "持仓报告期 %s 已 %s 天，超过 270 天" % (holding_date, ages["holding"])))
    if ages["fee"] is not None and ages["fee"] > 180:
        stale.append(("warn", "费率观测日 %s 已 %s 天，超过 180 天" % (fee_date, ages["fee"])))
    if ages["scale"] is not None and ages["scale"] > 180:
        stale.append(("warn", "规模报告期 %s 已 %s 天，超过 180 天" % (scale_date, ages["scale"])))
    if ages["profile"] is not None and ages["profile"] > 7:
        stale.append(("warn", "profile 更新时间 %s 已 %s 天，超过 7 天" % (profile_date, ages["profile"])))
    if any(x[0] == "block" for x in stale):
        checks.append(_check("data_staleness", "block", "block",
                             "；".join(x[1] for x in stale), ages))
    elif stale:
        checks.append(_check("data_staleness", "warn", "warn",
                             "；".join(x[1] for x in stale), ages))
    else:
        checks.append(_check("data_staleness", "warn", "pass",
                             "持仓 %s / 费率 %s / 规模 %s / profile %s"
                             % (holding_date, fee_date, scale_date, profile_date), ages))

    # 12b2 真实债券基准新鲜度：报告第 6 节引用 CBC_CFZS_00；缺失时只能用 ETF 参考，
    # 不能把 peer median 冒充基准。这里只告警，不阻断发布，但必须让人看见。
    try:
        ref = metric_as_of or biz
        brow = conn.execute(
            "SELECT MAX(trade_date) AS d FROM market_index_daily"
            " WHERE index_code=? AND close IS NOT NULL", ("CBC_CFZS_00",)).fetchone()
        bdate = brow["d"] if brow else None
        bage = None
        if bdate and ref:
            try:
                bage = (date.fromisoformat(str(ref)[:10])
                        - date.fromisoformat(str(bdate)[:10])).days
            except ValueError:
                bage = None
        if not bdate:
            checks.append(_check("benchmark_index", "warn", "warn",
                                 "没有 CBC_CFZS_00 真实债券指数，报告只能标“无合适基准/ETF参考”",
                                 {"index": "CBC_CFZS_00"}))
        elif bage is not None and bage > 10:
            checks.append(_check("benchmark_index", "warn", "warn",
                                 "CBC_CFZS_00 最新交易日 %s，距 %s 已 %d 天"
                                 % (bdate, ref, bage), {"date": bdate, "age_days": bage}))
        else:
            checks.append(_check("benchmark_index", "warn", "pass",
                                 "CBC_CFZS_00 最新交易日 %s（距 %s %s 天）"
                                 % (bdate, ref, "—" if bage is None else bage),
                                 {"date": bdate, "age_days": bage}))
    except Exception as exc:                                 # noqa: BLE001
        checks.append(_check("benchmark_index", "warn", "warn",
                             "真实债券基准检查失败：%s" % str(exc)[:200], None))

    # 12c 溯源完整性：每条源事实的 raw_sha256 必须能在 raw_object 找到。
    # 这不是“数据正确”的证明，而是证明数据确实来自一次可复核的公开 HTTP 响应。
    try:
        prov_since = None
        if nav_as_of:
            try:
                prov_since = (date.fromisoformat(str(nav_as_of)[:10])
                              - timedelta(days=90)).isoformat()
            except ValueError:
                prov_since = None
        prov = provenance_audit(conn, nav_since=prov_since,
                                allowed_sources=set(cfg.sources or {}))
        if prov.get("status") == "not_applicable":
            checks.append(_check("provenance_integrity", "warn", "warn",
                                 "raw_object 不存在，无法核对源事实溯源（旧库）", prov))
        elif prov.get("ok"):
            checks.append(_check("provenance_integrity", "block", "pass",
                                 "源事实 raw_sha256 全部可回查 raw_object（%d 张表）"
                                 % len(prov.get("tables") or []), prov))
        else:
            bad = [t["table"] for t in (prov.get("tables") or []) if not t.get("ok")]
            checks.append(_check("provenance_integrity", "block", "block",
                                 "源事实溯源不完整：%s；缺 raw=%d，格式错=%d，未注册来源=%d"
                                 % (", ".join(bad[:6]) or "—", prov.get("missing_total", 0),
                                    prov.get("bad_format_total", 0),
                                    len(prov.get("unregistered_sources") or [])), prov))
    except Exception as exc:                                 # noqa: BLE001
        checks.append(_check("provenance_integrity", "warn", "warn",
                             "溯源审计执行失败：%s" % str(exc)[:200], None))

    # 12d 可比性旗标必须与指标同批：旧 as_of/旧契约会让 P0-2 的 3 年门槛静默失效。
    comp = conn.execute(
        "SELECT as_of, formula_contract, COUNT(*) AS n,"
        " SUM(CASE WHEN flags_json IS NOT NULL AND flags_json<>'[]' THEN 1 ELSE 0 END) AS flagged"
        " FROM fund_metric_comparability WHERE period_days=365"
        " GROUP BY as_of, formula_contract ORDER BY as_of DESC LIMIT 1").fetchone()
    if not comp:
        checks.append(_check("comparability_freshness", "block", "warn",
                             "没有 365 天可比性旗标，P0-2 门槛无法核对", None))
    else:
        comp_as_of, comp_contract = comp["as_of"], comp["formula_contract"]
        comp_n, comp_flagged = int(comp["n"] or 0), int(comp["flagged"] or 0)
        problems: list[str] = []
        level = "pass"
        age = None
        if metric_as_of and comp_as_of:
            try:
                age = (date.fromisoformat(str(metric_as_of)[:10])
                       - date.fromisoformat(str(comp_as_of)[:10])).days
            except ValueError:
                age = None
        if age is not None and age > 1:
            problems.append("旗标 as_of %s 比指标 %s 旧 %d 天" % (comp_as_of, metric_as_of, age))
            level = "block"
        elif age == 1:
            problems.append("旗标 as_of %s 比指标 %s 旧 1 天" % (comp_as_of, metric_as_of))
            level = "warn"
        if run.get("formula_contract") and comp_contract != run.get("formula_contract"):
            problems.append("旗标契约 %s 与筛选契约 %s 不一致"
                            % (comp_contract, run.get("formula_contract")))
            level = "block" if level == "block" else "warn"
        metric_n = conn.execute(
            "SELECT COUNT(*) AS n FROM fund_metric WHERE period_days=365"
            " AND formula_contract=?", (run.get("formula_contract"),)).fetchone()["n"]
        if metric_n and comp_n < 0.90 * int(metric_n):
            problems.append("旗标覆盖 %d/%d 低于 90%%" % (comp_n, int(metric_n)))
            level = "block" if level == "block" else "warn"
        formal_flagged = conn.execute(
            "SELECT COUNT(*) AS n FROM screen_pick p"
            " JOIN fund_metric_comparability mc ON mc.fcode=p.fcode"
            "  AND mc.period_days=365 AND mc.as_of=? AND mc.formula_contract=?"
            " WHERE p.run_id=? AND p.board IN (?,?)"
            "  AND mc.flags_json IS NOT NULL AND mc.flags_json<>'[]'",
            (comp_as_of, comp_contract, run_id, *FORMAL_BOARDS)).fetchone()["n"]
        if formal_flagged:
            problems.append("正式榜有 %d 只命中不可比旗标" % int(formal_flagged))
            level = "block"
        evidence = {"as_of": comp_as_of, "formula_contract": comp_contract,
                    "rows": comp_n, "flagged": comp_flagged, "age_days": age,
                    "formal_flagged": int(formal_flagged), "metric_rows": int(metric_n or 0)}
        if level == "block":
            checks.append(_check("comparability_freshness", "block", "block",
                                 "；".join(problems), evidence))
        elif level == "warn":
            checks.append(_check("comparability_freshness", "warn", "warn",
                                 "；".join(problems), evidence))
        else:
            checks.append(_check("comparability_freshness", "block", "pass",
                                 "旗标 %s/%s；%d 行，命中 %d；与指标同批"
                                 % (comp_as_of, comp_contract, comp_n, comp_flagged), evidence))

    # 13 指数修复待处理（本期无指数插件，明确记录 not_applicable）
    checks.append(_check("index_repair_pending", "info", "not_applicable",
                         "本期只做债券型，无指数修复流程", None))

    # 14 批次阈值是否冻结（ADR-050）：同一批次的多次跑批必须共用一套分位阈值，
    # 否则"同一交易日、同一数据、26 分钟后重跑就换 16 只"会重现。
    run_row = conn.execute("SELECT batch_id, board_diff_path FROM screen_run WHERE run_id=?",
                           (run_id,)).fetchone()
    batch_id = run_row["batch_id"] if run_row else None
    frozen_n = 0
    if batch_id:
        frozen_n = conn.execute("SELECT COUNT(*) AS n FROM screen_threshold WHERE batch_id=?",
                                (batch_id,)).fetchone()["n"]
    if not batch_id:
        checks.append(_check("batch_not_frozen", "warn", "warn",
                             "该 run 没有 batch_id（v1 遗留批次），分位阈值不可复现",
                             {"run_id": run_id}))
    elif frozen_n == 0:
        checks.append(_check("batch_not_frozen", "block", "block",
                             "批次 %s 未冻结分位阈值" % batch_id,
                             {"batch_id": batch_id, "frozen_rows": 0}))
    else:
        checks.append(_check("batch_not_frozen", "block", "pass",
                             "批次 %s 已冻结 %d 条阈值" % (batch_id, frozen_n),
                             {"batch_id": batch_id, "frozen_rows": frozen_n}))

    # 15 名单漂移（ADR-050）：与上一批相比的进出比例。抖动过大说明阈值不稳或口径在变，
    # 可发布但必须让人看见，避免"静默换榜"。
    drift_path = run_row["board_diff_path"] if run_row else None
    drift = None
    if drift_path:
        try:
            data = json.loads(Path(drift_path).read_text(encoding="utf-8"))
            drift = formal_board_drift(data)
        except (OSError, ValueError):
            drift = None
    if drift_path is None:
        checks.append(_check("board_drift", "warn", "warn",
                             "缺少 board_diff（无法判断与上一批的进出）", {"path": None}))
    elif drift is None:
        checks.append(_check("board_drift", "warn", "warn",
                             "board_diff 不可读或正式榜为空", {"path": drift_path}))
    elif drift > 0.30:
        checks.append(_check("board_drift", "warn", "warn",
                             "正式榜进出比例 %.1f%% > 30%%" % (100 * drift),
                             {"path": drift_path, "drift": round(drift, 4)}))
    else:
        checks.append(_check("board_drift", "warn", "pass",
                             "正式榜进出比例 %.1f%%" % (100 * drift),
                             {"path": drift_path, "drift": round(drift, 4)}))

    # 16 同一只基金多份额重复上榜（ADR-051）：正式榜按 family 折叠后必须为 0。
    dup = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT sc.family_id FROM screen_pick p"
        " JOIN fund_share_class sc ON sc.fcode=p.fcode"
        " WHERE p.run_id=? AND p.board IN (?,?) AND sc.family_id IS NOT NULL"
        " GROUP BY sc.family_id HAVING COUNT(*)>1)", (run_id,) + FORMAL_BOARDS).fetchone()["n"]
    checks.append(_check("family_duplicate", "block", "block" if dup else "pass",
                         "正式榜存在 %d 个多份额家族" % dup if dup else "正式榜无多份额重复",
                         {"duplicate_families": dup}))

    # 16b 公司上限（P0-6）：合同固定每榜每公司 <= 2，不再按榜单规模 5% 浮动。
    cap = int((cfg.get("screen", default={}) or {}).get("company_cap", 2))
    comp = company_cap_stats(conn, run_id, cap)
    if not comp["checked"]:
        checks.append(_check("company_cap", "block", "not_applicable",
                             "库表缺 company_name，无法核对公司上限", comp))
    elif comp["violations"]:
        checks.append(_check("company_cap", "block", "block",
                             "正式榜有 %d 个公司超过每榜 %d 只"
                             % (len(comp["violations"]), cap), comp))
    else:
        checks.append(_check("company_cap", "block", "pass",
                             "每榜单公司最多 %d 只（上限 %d）"
                             % (comp["max_count"], cap), comp))

    # 16c 双榜最小占比（P0-6）：较小榜必须 >= 发布数的 25%，防止增强榜被挤成点缀。
    min_ratio = float((cfg.get("screen", default={}) or {}).get("group_min_ratio", 0.25))
    ratio_stats = board_ratio_stats(conn, run_id, min_ratio)
    if not ratio_stats["applicable"]:
        checks.append(_check("board_ratio", "block", "not_applicable",
                             "正式双榜有一侧为空，比例约束不适用", ratio_stats))
    elif not ratio_stats["pass"]:
        checks.append(_check("board_ratio", "block", "block",
                             "较小榜占发布数 %.1f%% < %.0f%%（稳健 %d / 增强 %d）"
                             % (100 * ratio_stats["ratio"], 100 * min_ratio,
                                ratio_stats["stable"], ratio_stats["enhanced"]),
                             ratio_stats))
    else:
        checks.append(_check("board_ratio", "block", "pass",
                             "较小榜占发布数 %.1f%% ≥ %.0f%%"
                             % (100 * ratio_stats["ratio"], 100 * min_ratio),
                             ratio_stats))

    # 17 名次与排序分一致（P0-6）：报告按 rank 展示，必须保证 rank 是 rank_score 的降序；
    # 正式分是另一套权重，不参与名次，不能拿 formal_score 反过来质疑 rank。
    order = rank_order_stats(conn, run_id)
    if order["n"] < 2:
        checks.append(_check("rank_score_order", "block", "not_applicable",
                             "正式榜不足 2 只，无法校验名次与排序分", order))
    elif order["missing"]:
        checks.append(_check("rank_score_order", "block", "block",
                             "正式榜有 %d 只缺 rank_score" % order["missing"], order))
    elif order["inversions"]:
        checks.append(_check("rank_score_order", "block", "block",
                             "正式榜名次与排序分存在 %d 处逆序（Spearman %s）"
                             % (order["inversions"], order["spearman"]), order))
    else:
        checks.append(_check("rank_score_order", "block", "pass",
                             "名次与排序分方向一致（Spearman(rank,score)=%s；rank 1 为最高分）"
                             % order["spearman"], order))

    # 18 边界占比（ADR-063）：正式榜里综合分贴着动态综合线的比例。占比高说明榜单由
    # 阈值抖动决定而不是质量差异决定，可发布但必须让人看见。
    tol = float(gcfg.get("boundary_tolerance", 1.0))
    share, near, threshold = boundary_share(conn, run_id, batch_id, tol)
    share_max = float(gcfg.get("boundary_share_max", 0.10))
    if share is None:
        checks.append(_check("dynamic_boundary", "warn", "warn",
                             "无法计算边界占比（缺批次或综合线阈值）",
                             {"batch_id": batch_id, "threshold": threshold}))
    elif share > share_max:
        checks.append(_check("dynamic_boundary", "warn", "warn",
                             "正式榜 %.1f%% 的综合分在动态线 ±%.1f 分内（> %.0f%%）"
                             % (100 * share, tol, 100 * share_max),
                             {"share": share, "near": near, "threshold": threshold}))
    else:
        checks.append(_check("dynamic_boundary", "warn", "pass",
                             "正式榜 %.1f%% 的综合分在动态线 ±%.1f 分内"
                             % (100 * share, tol),
                             {"share": share, "near": near, "threshold": threshold}))

    # 19 榜单换手（ADR-067）：与上一份已提交发布比，正式榜换手率过高说明榜单被
    # 批次分位与配额主导。契约变更时明确 not_applicable —— 口径升级不是"榜单不稳"。
    checks.append(board_turnover(conn, run_id, float(gcfg.get("turnover_max", 0.25))))

    # 20 跨榜 family 重叠（P1-6）：只警告，不阻断。core/core10 是 stable 的集中视图，
    # 硬去重会让 core 空掉；但必须把重复家族数写出来，防止把视图重复当独立标的。
    cross_fam = cross_board_family_stats(conn, run_id)
    if cross_fam["duplicate_families"]:
        checks.append(_check("cross_board_family", "warn", "warn",
                             "跨榜视图有 %d 个家族出现多次（最多 %d 行）；core/core10 与 stable 重叠是视图重复，不是独立标的"
                             % (cross_fam["duplicate_families"], cross_fam["max_family_rows"]),
                             cross_fam))
    else:
        checks.append(_check("cross_board_family", "warn", "pass",
                             "跨榜没有重复家族", cross_fam))

    # 21 长期核心榜的 5 年证据（P1-7）：入榜者必须 coverage_ratio_5y>=0.60，
    # 且 5 年窗口的 annual_dec 非空；只有 years 跨了 5 年、指标因点数不足为 NULL 不算证据。
    long_rows = conn.execute(
        "SELECT p.fcode, d.scores_json FROM screen_pick p"
        " JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
        " WHERE p.run_id=? AND p.board='bond_long'", (run_id,)).fetchall()
    bad_long = []
    for r in long_rows:
        try:
            cov = json.loads(r["scores_json"] or "{}").get("coverage") or {}
        except ValueError:
            cov = {}
        cov5 = cov.get("coverage_ratio_5y")
        has_annual = bool(cov.get("has_5y_annual"))
        if cov5 is None or float(cov5) < 0.60 or not has_annual:
            bad_long.append({"fcode": r["fcode"], "coverage_ratio_5y": cov5,
                             "has_5y_annual": has_annual})
    if bad_long:
        checks.append(_check("long_board_5y", "block", "block",
                             "长期核心榜有 %d 只 5 年覆盖不足 60%% 或缺少 5 年年化" % len(bad_long),
                             {"bad": bad_long[:10]}))
    else:
        checks.append(_check("long_board_5y", "block", "pass",
                             "长期核心榜 5 年覆盖均≥60% 且 5 年年化非空（或榜单为空）",
                             {"n": len(long_rows)}))

    # 22 生命周期覆盖（P2-4）：清盘/合并名单必须外部导入；0 行时回测/前瞻仍有
    # 幸存者偏差，报告和门禁都要明说，不能把"当前库只有存续基金"当已校正。
    lc = lifecycle_stats(conn)
    try:
        from .. import survivorship as sv_mod
        sv = sv_mod.summary(conn, lookback_days=30, limit=1)
    except Exception as exc:  # 诊断不能阻断发布
        sv = {"status": "error", "error": str(exc)}
    sv_txt = ""
    if sv.get("status") == "ok":
        sv_txt = ("；公开目录快照 %s 天，消失候选 %s 只（债券，不等于清盘）"
                  % (sv.get("coverage_days"), sv.get("bond_missing_count")))
    lc["survivorship"] = sv
    if lc["funds"] == 0:
        checks.append(_check("lifecycle_coverage", "warn", "warn",
                             "生命周期表 0 行：未导入清盘/合并样本，回测与前瞻仍含幸存者偏差"
                             + sv_txt, lc))
    else:
        checks.append(_check("lifecycle_coverage", "warn", "pass",
                             "生命周期样本 %d 只（清盘 %d、合并 %d；其中 %d 只有历史净值；"
                             "walkforward 仍从当前 fund 表构造，无法纳入已移除的清盘样本；历史 IC/DSR 的幸存者偏差未校正）%s"
                             % (lc["funds"], lc["by_status"].get("liquidated", 0),
                                lc["by_status"].get("merged", 0), lc.get("with_nav", 0), sv_txt), lc))

    # 23 统计显著性协议（P2-5）
    checks.append(dsr_skill_check(cfg))

    blocks = [c for c in checks if c["status"] == "block"]
    warnings = [c for c in checks if c["status"] == "warn"]
    status = "blocked" if blocks else ("warning" if warnings else "pass")
    downgraded = False
    if mode == "warn" and status == "blocked":
        # warn 模式下阻断项降级为警告：blocks 必须清空，否则调用方会以为"仍然被拦"，
        # 但证据不能丢 —— 降级项以 downgraded_from 标注后并入 warnings。
        status = "warning"
        downgraded = True
        warnings = warnings + [dict(c, severity="warn", downgraded_from="block") for c in blocks]
        blocks = []

    result = {"publication_id": built.get("revision_id"), "run_id": run_id,
              "status": status, "checks": checks, "downgraded": downgraded,
              "blocks": [c["code"] for c in blocks],
              "warnings": [c["code"] for c in warnings],
              "generated_at": db.now_iso()}
    conn.execute(
        "INSERT OR REPLACE INTO publication_gate(publication_id, run_id, status, blocks_json,"
        " warnings_json, checks_json, created_at) VALUES(?,?,?,?,?,?,?)",
        (built.get("revision_id"), run_id, status,
         json.dumps(result["blocks"], ensure_ascii=False),
         json.dumps(result["warnings"], ensure_ascii=False),
         json.dumps(checks, ensure_ascii=False), result["generated_at"]))
    conn.commit()
    return result


def attach_report_consistency(conn, cfg, run_id: str, result: dict[str, Any],
                              report: dict[str, Any], boards: dict[str, Any]) -> dict[str, Any]:
    """P2-13：报告 facts 与数据库/榜单对账；不一致直接阻断，避免报告说一套、库里另一套。"""
    facts = report.get("facts") or {}
    run_row = _screen_run_counts(conn, run_id)
    # 报告 facts.l9_pass 是 L9 口径；旧行没有 l9_pass_count 时回退 formal_count。
    run_formal = int((run_row.get("l9_pass_count")
                      if run_row.get("l9_pass_count") is not None
                      else run_row.get("formal_count", 0)) or 0)
    pick_counts = {r["board"]: int(r["n"]) for r in conn.execute(
        "SELECT board, COUNT(*) AS n FROM screen_pick WHERE run_id=? GROUP BY board",
        (run_id,))}
    published_db = pick_counts.get("bond_stable", 0) + pick_counts.get("bond_enhanced", 0)
    from .. import forward as forward_mod
    fwd = forward_mod.summary(conn)
    expected = {
        "l9_pass": run_formal,
        "published": published_db,
        "folded": max(0, run_formal - published_db),
        "formal_by_board": {"bond_stable": pick_counts.get("bond_stable", 0),
                            "bond_enhanced": pick_counts.get("bond_enhanced", 0)},
        "cohorts": fwd.get("cohorts", 0),
        "matured_formal_observations": fwd.get("matured_formal_observations", 0),
    }
    problems = []
    for key, want in expected.items():
        got = facts.get(key)
        if got != want:
            problems.append("%s 报告=%r 数据库=%r" % (key, got, want))
    for board, rows in boards.items():
        got = (facts.get("board_counts") or {}).get(board)
        if got != len(rows):
            problems.append("board_counts.%s 报告=%r 榜单=%r" % (board, got, len(rows)))
    if facts.get("published") != ((facts.get("formal_by_board") or {}).get("bond_stable", 0)
                                  + (facts.get("formal_by_board") or {}).get("bond_enhanced", 0)):
        problems.append("published 与 formal_by_board 之和不一致")

    check = _check("report_consistency", "block", "block" if problems else "pass",
                   ("报告数字与数据库不一致：" + "；".join(problems)) if problems
                   else ("报告数字与数据库一致：L9 %d、发布 %d、cohort %d、成熟观测 %d"
                         % (run_formal, published_db, fwd.get("cohorts", 0),
                            fwd.get("matured_formal_observations", 0))),
                   {"facts": facts, "expected": expected, "problems": problems})
    checks = [c for c in (result.get("checks") or []) if c.get("code") != "report_consistency"]
    checks.append(check)
    blocks = [c for c in checks if c["status"] == "block"]
    warnings = [c for c in checks if c["status"] == "warn"]
    status = "blocked" if blocks else ("warning" if warnings else "pass")
    mode = str((cfg.get("gate") or {}).get("mode") or "strict")
    if mode == "warn" and status == "blocked":
        status = "warning"
        warnings = warnings + [dict(c, severity="warn", downgraded_from="block") for c in blocks]
        blocks = []
    result.update({"status": status, "checks": checks,
                   "blocks": [c["code"] for c in blocks],
                   "warnings": [c["code"] for c in warnings],
                   "downgraded": bool(warnings and not blocks and mode == "warn")})
    conn.execute(
        "INSERT OR REPLACE INTO publication_gate(publication_id, run_id, status, blocks_json,"
        " warnings_json, checks_json, created_at) VALUES(?,?,?,?,?,?,?)",
        (result.get("publication_id"), run_id, status,
         json.dumps(result["blocks"], ensure_ascii=False),
         json.dumps(result["warnings"], ensure_ascii=False),
         json.dumps(checks, ensure_ascii=False), db.now_iso()))
    conn.commit()
    return result

