"""榜单换手率与进出原因（P2-1b 月度稳定性监控，ADR-067）。

回答的问题：两次发布之间榜单换了多少人？换掉的人是因为"门槛没过"（可解释、
可复盘），还是因为"排序/配额挤掉"（同一批数据下换个截止点就会变）？换手率长期
偏高，说明榜单由批次分位与配额主导，而不是由基金质量差异主导。

口径：
- 只比同一 formula_contract 的两次发布。契约变更（口径升级）时直接标 not_applicable，
  否则会把"我们改了规则"误读成"榜单不稳"；
- 换手率 = (进入 + 离开) / 并集，等价于 1 - Jaccard，取值 0–1；
- 离开者的原因取当次 screen_decision：passed=1 却没进榜记 rank_out（被排序/配额挤掉），
  其余取前三条原因码；
- 只做描述，不做判定：阈值放在发布门禁（gate.turnover_max，默认 0.25）。
"""
from __future__ import annotations

import json
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
FORMAL_BOARDS = ("bond_stable", "bond_enhanced")
MAX_CODES = 20


def _picks(conn, run_id: str, board: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT fcode, rank, formal_score, rank_score FROM screen_pick"
        " WHERE run_id=? AND board=?", (run_id, board)).fetchall()
    return {r["fcode"]: {"rank": r["rank"], "formal_score": r["formal_score"],
                         "rank_score": r["rank_score"]} for r in rows}


def _contract(conn, run_id: str) -> str | None:
    """筛选契约优先（ADR-067）；老库没有该列时回退 metrics 契约。"""
    try:
        row = conn.execute("SELECT screen_contract, formula_contract FROM screen_run"
                           " WHERE run_id=?", (run_id,)).fetchone()
    except Exception:
        row = conn.execute("SELECT formula_contract FROM screen_run WHERE run_id=?",
                           (run_id,)).fetchone()
    if not row:
        return None
    keys = row.keys()
    if "screen_contract" not in keys:
        return row["formula_contract"]  # 老库：只能拿 metrics 契约代替，且会被标为不可比
    return row["screen_contract"]


def _reasons(conn, run_id: str, codes: Sequence[str]) -> dict[str, list[str]]:
    """离开者的原因：passed 但没进榜 → rank_out；否则取原因码前三条。"""
    out: dict[str, list[str]] = {}
    if not codes:
        return out
    marks = ",".join("?" * len(codes))
    rows = conn.execute(
        "SELECT fcode, layer_reached, passed, reason_codes_json FROM screen_decision"
        " WHERE run_id=? AND fcode IN (%s)" % marks, (run_id, *codes)).fetchall()
    for r in rows:
        try:
            reasons = json.loads(r["reason_codes_json"] or "[]")
        except ValueError:
            reasons = []
        if r["passed"]:
            out[r["fcode"]] = ["rank_out"]
        else:
            out[r["fcode"]] = [str(x) for x in reasons][:3] or ["no_reason_code"]
    for code in codes:
        out.setdefault(code, ["missing_decision"])
    return out


def _batch(conn, run_id: str) -> str | None:
    row = conn.execute("SELECT batch_id FROM screen_run WHERE run_id=?", (run_id,)).fetchone()
    return row["batch_id"] if row else None


def compare(conn, run_id: str, prev_run_id: str) -> dict[str, Any]:
    """两次运行之间的逐榜换手。返回可直接 JSON 化的 dict。

    可比性：契约相同，或同一 batch（同批次共用一套冻结阈值，等价于同一口径）。
    迁移前的旧运行没有 screen_contract，只靠"同批次"兜底。
    """
    contract, prev_contract = _contract(conn, run_id), _contract(conn, prev_run_id)
    batch, prev_batch = _batch(conn, run_id), _batch(conn, prev_run_id)
    boards: dict[str, Any] = {}
    for board, label in BOARDS:
        cur, prev = _picks(conn, run_id, board), _picks(conn, prev_run_id, board)
        entered = sorted(set(cur) - set(prev))
        left = sorted(set(prev) - set(cur))
        union = len(set(cur) | set(prev))
        moves = [abs(int(cur[c]["rank"]) - int(prev[c]["rank"]))
                 for c in set(cur) & set(prev)
                 if cur[c].get("rank") is not None and prev[c].get("rank") is not None]
        boards[board] = {
            "label": label,
            "prev": len(prev),
            "cur": len(cur),
            "entered": len(entered),
            "left": len(left),
            "turnover": round((len(entered) + len(left)) / union, 4) if union else None,
            "mean_rank_move": round(sum(moves) / len(moves), 2) if moves else None,
            "entered_codes": entered[:MAX_CODES],
            "left_codes": left[:MAX_CODES],
            "left_reasons": _reasons(conn, run_id, left),
        }
    same_contract = contract is not None and contract == prev_contract
    same_batch = bool(batch) and batch == prev_batch
    return {"run_id": run_id, "prev_run_id": prev_run_id,
            "contract": contract, "prev_contract": prev_contract,
            "batch_id": batch, "prev_batch_id": prev_batch,
            "same_contract": same_contract, "same_batch": same_batch,
            # 任一侧没记录契约且不是同批次 → 不可比：宁可不给结论，也不把口径升级
            # 读成榜单换手。
            "comparable": same_contract or same_batch,
            "boards": boards}


def _distinct_batches(conn) -> list[dict[str, Any]]:
    """按业务日去重，保留当日最后一次。

    同一天的 v3…v7 是开发期的口径迭代，不是新的观察点：把它们当相邻批次会把
    "改代码"算成"榜单换手"。业务日相同只留最后一条。
    """
    rows = conn.execute(
        "SELECT run_id, business_date, batch_id, formula_contract, screen_contract,"
        " created_at FROM screen_run WHERE formal_count>0 ORDER BY created_at").fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["business_date"] or r["run_id"]
        latest[key] = dict(r)
    return sorted(latest.values(), key=lambda r: (r["business_date"] or "", r["created_at"]))


def history(conn, limit: int = 24) -> dict[str, Any]:
    """逐 batch 的换手序列（只保留同一契约的相邻对）。"""
    runs = _distinct_batches(conn)
    pairs: list[dict[str, Any]] = []
    for prev, cur in zip(runs, runs[1:]):
        same_contract = (prev.get("screen_contract") and
                         prev["screen_contract"] == cur.get("screen_contract"))
        same_batch = bool(prev.get("batch_id")) and prev["batch_id"] == cur.get("batch_id")
        if not (same_contract or same_batch):
            continue
        cmp = compare(conn, cur["run_id"], prev["run_id"])
        pairs.append({
            "prev_run_id": prev["run_id"], "run_id": cur["run_id"],
            "business_date": cur["business_date"],
            "stable": cmp["boards"]["bond_stable"]["turnover"],
            "enhanced": cmp["boards"]["bond_enhanced"]["turnover"],
            "stable_in": cmp["boards"]["bond_stable"]["entered"],
            "stable_out": cmp["boards"]["bond_stable"]["left"],
        })
    return {"pairs": pairs[-limit:], "runs": len(runs), "contract": runs[-1]["formula_contract"] if runs else None}


def _pct(value: float | None) -> str:
    return "-" if value is None else "%.1f%%" % (100.0 * value)


def render(result: dict[str, Any]) -> str:
    if "boards" not in result:
        lines = ["换手序列（只含同一契约的相邻批次）",
                 "批次        稳健   增强   稳健进/出   契约"]
        for p in result["pairs"]:
            lines.append("%-10s %6s %6s   %2d/%-2d      %s"
                         % (p["business_date"] or p["run_id"][:8], _pct(p["stable"]),
                            _pct(p["enhanced"]), p["stable_in"], p["stable_out"],
                            result["contract"]))
        lines.append("")
        lines.append("共 %d 个批次、%d 个可比相邻对" % (result["runs"], len(result["pairs"])))
        if not result["pairs"]:
            lines.append("提示：筛选契约自 ADR-067 起才落库，之前的批次无法判定是否同口径；"
                         "再跑一次同契约的日更即可产生第一个可比对。")
        return "\n".join(lines)
    lines = ["换手：%s → %s（契约 %s）" % (result["prev_run_id"], result["run_id"],
                                       result["contract"]),
             "榜单   前次  本次  进入  离开  换手    平均名次变动"]
    for board, _label in BOARDS:
        b = result["boards"][board]
        lines.append("%-6s %4d  %4d  %4d  %4d  %6s  %s"
                     % (b["label"], b["prev"], b["cur"], b["entered"], b["left"],
                        _pct(b["turnover"]), "-" if b["mean_rank_move"] is None else "%.1f" % b["mean_rank_move"]))
    if not result.get("comparable", result["same_contract"]):
        lines.append("")
        lines.append("契约 %s → %s 且不同批次：换手不可比，仅作记录"
                     % (result["prev_contract"] or "未记录", result["contract"] or "未记录"))
    elif result.get("same_batch") and not result["same_contract"]:
        lines.append("")
        lines.append("前次运行未记录契约（迁移前），但两次同批次（共用冻结阈值），按同批次比较")
    left = result["boards"]["bond_stable"]["left_reasons"]
    if left:
        lines.append("")
        lines.append("稳健榜离开者原因（前 5）：")
        for code, reasons in list(left.items())[:5]:
            lines.append("  %s  %s" % (code, ", ".join(reasons)))
    return "\n".join(lines)
