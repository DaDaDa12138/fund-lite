"""P1-1 目标函数：把“优质”写成可核对的维度，而不是一句口号。

目标函数不是新的评分模型，也不改变榜单；它只回答“本轮候选池在哪些维度
有证据、哪些维度没有”。所有输入来自已发布的正式榜行、真实中债指数与 ETF 代理基准、
forward/DSR 证据，不引入模拟值。
"""
from __future__ import annotations

from typing import Any

VERSION = "objective-v1"
DEFINITION = (
    "长期优质 = 3/5 年风险调整收益位于同类前列 + 相对可投资基准的正超额 "
    "+ 回撤/波动约束 + 可投性（规模、成本、申购状态）+ 团队覆盖业绩窗口。"
)


def _ratio(n: int, total: int) -> float | None:
    return None if not total else round(n / total, 4)


def _status(ok: bool, partial: bool = False) -> str:
    if ok:
        return "pass"
    return "partial" if partial else "fail"


def evaluate(rows: list[dict[str, Any]], benchmark: dict[str, Any] | None,
             forward: dict[str, Any] | None, dsr: float | None,
             dsr_threshold: float = 0.95) -> dict[str, Any]:
    total = len(rows)
    long3 = sum(1 for r in rows if r.get("is_full_3y"))
    cov5 = sum(1 for r in rows if (r.get("coverage_ratio_5y") or 0) >= 0.60
               and r.get("annual_5y_pct") is not None)
    risk_ok = sum(1 for r in rows if r.get("risk_status") == "ok")
    risk_block = sum(1 for r in rows if r.get("risk_status") == "block")
    risk_watch = sum(1 for r in rows
                     if r.get("risk_status") in ("watch", "unknown"))
    investable = sum(1 for r in rows if r.get("buyable_state") == "confirmed_yes"
                     and (r.get("share_scale_billion") or 0) >= 2.0
                     and (r.get("cost_1y_pct") is None or r.get("cost_1y_pct") <= 1.30))
    team = sum(1 for r in rows if (r.get("manager_tenure_coverage_3y") or 0) >= 0.80)

    brows = [b for b in ((benchmark or {}).get("rows") or [])]
    avail = [b for b in brows if b.get("excess_1y_pct") is not None
             and b.get("excess_3y_pct") is not None]
    positive = [b for b in avail if b["excess_1y_pct"] > 0 and b["excess_3y_pct"] > 0]
    matured = int((forward or {}).get("matured_formal_observations") or 0)

    criteria = [
        {"id": "long_horizon", "label": "3/5 年风险调整收益",
         "status": _status(total > 0 and long3 == total and (cov5 / total if total else 0) >= 0.60,
                           total > 0 and long3 == total),
         "evidence": "3 年完整 %s/%s；5 年覆盖≥60%% 且 5 年年化非空 %s/%s"
                     % (long3, total, cov5, total),
         "target": "正式榜全部 3 年完整，且至少 60% 有 5 年覆盖"},
        {"id": "benchmark_excess", "label": "相对基准超额",
         "status": _status(bool(avail) and len(positive) / len(avail) >= 0.60,
                           bool(positive)),
         "evidence": "有 1/3 年可比超额样本 %s 个风格；1/3 年同时为正 %s 个；基准类型 %s"
                     % (len(avail), len(positive), (benchmark or {}).get("type") or "peer_median"),
         "target": "至少一个可投资基准，且≥60% 风格 1/3 年超额同时为正"},
        {"id": "drawdown", "label": "回撤/波动约束",
         "status": _status(total > 0 and risk_block == 0 and risk_watch == 0,
                           total > 0 and risk_block == 0),
         "evidence": "风险门 block %s；watch/unknown %s；ok %s；合计 %s"
                     % (risk_block, risk_watch, risk_ok, total),
         "target": "正式榜无 block；watch/unknown 逐只披露，不当作通过"},
        {"id": "investability", "label": "可投性（规模/成本/申购）",
         "status": _status(total > 0 and investable == total,
                           total > 0 and investable / total >= 0.90),
         "evidence": "confirmed_yes 且份额≥2 亿、1 年成本≤1.30%% 的 %s/%s"
                     % (investable, total),
         "target": "全部可申购、规模≥2 亿、成本≤1.30%"},
        {"id": "team_coverage", "label": "团队覆盖业绩窗口",
         "status": _status(total > 0 and team == total, total > 0 and team / total >= 0.80),
         "evidence": "经理覆盖 3 年窗口≥80%% 的 %s/%s" % (team, total),
         "target": "全部达到 80% 覆盖"},
        {"id": "validation", "label": "前瞻/多重检验验证",
         "status": _status(matured > 0 and dsr is not None and dsr >= dsr_threshold,
                           matured > 0 or (dsr is not None and dsr >= 0.90)),
         "evidence": "成熟正式观测 %s 条；DSR %s（阈值 %s）"
                     % (matured, "—" if dsr is None else dsr, dsr_threshold),
         "target": "有成熟前瞻观测且 DSR≥0.95"},
    ]
    all_pass = all(c["status"] == "pass" for c in criteria)
    validation = next(c for c in criteria if c["id"] == "validation")["status"]
    if all_pass:
        verdict = "validated"
        headline = "目标函数全部维度通过；仍不构成未来收益保证。"
    elif validation == "fail":
        verdict = "candidate_pool"
        headline = ("候选池，未经验证：历史规则条件通过，但前瞻成熟观测与 DSR 不足，"
                    "不能宣称“已发现优质基金”。")
    else:
        verdict = "partial"
        headline = "部分维度通过；仍需补齐缺失证据后才能升级结论。"

    return {
        "version": VERSION, "definition": DEFINITION, "total": total,
        "criteria": criteria, "verdict": verdict, "headline": headline,
        "ratios": {"long_3y": _ratio(long3, total), "coverage_5y": _ratio(cov5, total),
                   "risk_ok": _ratio(risk_ok, total),
                   "risk_block": _ratio(risk_block, total),
                   "risk_watch": _ratio(risk_watch, total),
                   "investable": _ratio(investable, total),
                   "team_coverage": _ratio(team, total)},
    }


def table_rows(result: dict[str, Any]) -> list[list[Any]]:
    label = {"pass": "通过", "partial": "部分", "fail": "未通过"}
    return [[c["label"], label.get(c["status"], c["status"]), c["evidence"], c["target"]]
            for c in result.get("criteria") or []]
