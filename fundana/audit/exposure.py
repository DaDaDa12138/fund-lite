"""榜单持仓风格归因：用债券持仓名称做券种暴露画像（P2-2，ADR-069）。

回答的问题：稳健榜是不是真的"纯债"？增强榜的收益是不是主要来自转债暴露？
新锐榜的券种结构跟稳健榜差多少？——把"风格标签"从名称启发式落到持仓证据上。

口径与局限：
- 数据源 `src_zqcc_holding`（基金季报前五大债券），只覆盖前五大持仓，不是完整组合；
- 券种按债券名称关键词分类：可转债（含可交换债）、利率债（国债/国开/农发/进出口/地方债/央票）、
  同业存单、信用债（其余），"其他"包括无法识别的名称；
- 每只基金先算"各类市值占前五大合计"的比例，再对榜单取中位数，避免大额持仓主导；
- 没有久期与信用等级字段，利率债占比只能当作"久期暴露"的粗代理，不能替代组合久期。
"""
from __future__ import annotations

import re
from statistics import median
from typing import Any, Iterable, Sequence

BOARDS: tuple[tuple[str, str], ...] = (
    ("bond_stable", "稳健"),
    ("bond_enhanced", "增强"),
    ("bond_core", "精选"),
    ("bond_core10", "收益10"),
    ("bond_rookie", "新锐"),
    ("bond_watch", "观察"),
    ("bond_locked", "受限"),
)

CLASSES = ("convertible", "rate", "cd", "credit", "other")
CLASS_LABELS = {
    "convertible": "可转债/可交换债",
    "rate": "利率债",
    "cd": "同业存单",
    "credit": "信用债",
    "other": "其他/未识别",
}

_RATE_WORDS = ("国债", "国开", "农发", "进出口", "口行", "政策", "地方债", "央票", "政府债")
_CD_WORDS = ("存单", "NCD")
_CONV_WORDS = ("转债", "转股", "交换债", "可交换")
_CONV_PREFIXES = ("110", "111", "113", "118", "123", "127", "128", "132")


def classify_bond(name: str, code: str = "") -> str:
    """按名称/代码把一只债券归到券种类别。"""
    text = (name or "").strip()
    for word in _CONV_WORDS:
        if word in text:
            return "convertible"
    if re.search(r"转\d*$", text):   # 希望转2 / 柳工转2 一类简称
        return "convertible"
    if code[:3] in _CONV_PREFIXES:
        return "convertible"
    for word in _CD_WORDS:
        if word in text:
            return "cd"
    for word in _RATE_WORDS:
        if word in text:
            return "rate"
    if not text:
        return "other"
    return "credit"


def _latest_holdings(conn, codes: Sequence[str]) -> dict[str, list[tuple[str, str, float]]]:
    """取每只基金最新报告期的持仓 [(bond_name, bond_code, pct_nav)]。"""
    if not codes:
        return {}
    marks = ",".join("?" * len(codes))
    rows = conn.execute(
        "SELECT code, bond_name, bond_code, pct_nav, report_period FROM src_zqcc_holding"
        " WHERE code IN (%s)" % marks, tuple(codes)).fetchall()
    latest: dict[str, str] = {}
    for r in rows:
        period = str(r["report_period"] or "")
        if r["code"] not in latest or period > latest[r["code"]]:
            latest[r["code"]] = period
    out: dict[str, list[tuple[str, str, float]]] = {}
    for r in rows:
        if str(r["report_period"] or "") != latest.get(r["code"]):
            continue
        out.setdefault(r["code"], []).append(
            (r["bond_name"] or "", r["bond_code"] or "", float(r["pct_nav"] or 0)))
    return out


def fund_shares(holdings: Iterable[tuple]) -> dict[str, float] | None:
    """一只基金各类券种占前五大合计的比例。条目为 (name, pct) 或 (name, code, pct)。"""
    items = []
    for item in holdings:
        name, code, pct = (item[0], item[1], item[2]) if len(item) == 3 else (item[0], "", item[1])
        items.append((classify_bond(name, code), max(0.0, pct)))
    total = sum(pct for _, pct in items)
    if total <= 0:
        return None
    return {cls: round(sum(p for c, p in items if c == cls) / total, 4) for cls in CLASSES}


def fund_history(conn, fcode: str, limit: int = 12) -> list[dict[str, Any]]:
    """一只基金最近 N 个报告期的券种结构（用于详情页“持仓历史”）。

    数据来自 src_zqcc_holding 的全部已采集季度；每期只按前五大债券市值
    计算占比，因此是“重仓券结构”而不是完整组合。
    """
    rows = conn.execute(
        "SELECT report_period, bond_code, bond_name, pct_nav FROM src_zqcc_holding"
        " WHERE code=? ORDER BY report_period DESC, pct_nav DESC", (fcode,)).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        period = str(r["report_period"] or "")
        grouped.setdefault(period, []).append({
            "bond_code": r["bond_code"], "bond_name": r["bond_name"],
            "pct_nav": float(r["pct_nav"] or 0.0)})
    out: list[dict[str, Any]] = []
    for period in sorted(grouped, reverse=True)[:limit]:
        items = grouped[period]
        shares = fund_shares([(x["bond_name"] or "", x["bond_code"] or "", x["pct_nav"])
                              for x in items])
        if not shares:
            continue
        out.append({
            "period": period,
            "shares": shares,
            "total_pct": round(sum(x["pct_nav"] for x in items), 4),
            "top": sorted(items, key=lambda x: -x["pct_nav"])[:3],
        })
    return out


def board_exposure(conn, codes: Sequence[str]) -> dict[str, Any]:
    """一个榜单（或任意基金集合）的券种暴露中位数与覆盖率。"""
    holdings = _latest_holdings(conn, list(codes))
    per_fund = {code: fund_shares(rows) for code, rows in holdings.items()}
    per_fund = {k: v for k, v in per_fund.items() if v}
    out: dict[str, Any] = {
        "n": len(list(codes)),
        "covered": len(per_fund),
        "coverage": round(len(per_fund) / len(list(codes)), 4) if codes else None,
    }
    for cls in CLASSES:
        vals = [v[cls] for v in per_fund.values()]
        out[cls] = round(median(vals), 4) if vals else None
    return out


def run(conn, run_id: str, boards: Sequence[tuple[str, str]]) -> dict[str, Any]:
    picks: dict[str, list[str]] = {}
    for board, _label in boards:
        rows = conn.execute("SELECT fcode FROM screen_pick WHERE run_id=? AND board=?",
                            (run_id, board)).fetchall()
        picks[board] = [r["fcode"] for r in rows]
    universe = sorted({code for codes in picks.values() for code in codes})
    return {
        "run_id": run_id,
        "boards": {board: board_exposure(conn, picks[board]) for board, _ in boards},
        "all_picks": board_exposure(conn, universe),
    }


def render(result: dict[str, Any]) -> str:
    boards = result.get("boards", {})
    lines = ["榜单持仓券种暴露（中位数，占前五大债券市值）", ""]
    header = f"{'榜单':<8}{'只数':>5}{'覆盖':>7}" + "".join(f"{CLASS_LABELS[c]:>12}" for c in CLASSES)
    lines.append(header)
    for board, label in (("bond_stable", "稳健"), ("bond_enhanced", "增强"),
                         ("bond_core", "精选"), ("bond_core10", "收益10"),
                         ("bond_rookie", "新锐"), ("bond_watch", "观察"),
                         ("bond_locked", "受限")):
        item = boards.get(board)
        if not item:
            continue
        cov = f"{item['covered']}/{item['n']}"
        cells = "".join(f"{('%.1f%%' % (item[c] * 100)) if item[c] is not None else '-':>12}" for c in CLASSES)
        lines.append(f"{label:<8}{item['n']:>5}{cov:>10}{cells}")
    allp = result.get("all_picks") or {}
    if allp:
        cov = f"{allp['covered']}/{allp['n']}"
        cells = "".join(f"{('%.1f%%' % (allp[c] * 100)) if allp[c] is not None else '-':>12}" for c in CLASSES)
        lines.append(f"{'全部':<8}{allp['n']:>5}{cov:>10}{cells}")
    lines.append("")
    lines.append("注：仅前五大债券持仓；按名称关键词分类，信用债含未识别名称；无久期/评级字段。")
    return "\n".join(lines)
