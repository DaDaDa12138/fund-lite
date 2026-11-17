"""把筛选证据翻译成面向用户的“为什么选它 / 它不适合谁”（P1）。

输入是 publish.builder.collect_rows 的行字典，输出只引用该行里已有的数值，
不生成泛泛的营销文案；报告、API 与 CSV 共用同一套解释。
"""
from __future__ import annotations

from typing import Any


def _num(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}{suffix}"


def build(row: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    why: list[dict[str, Any]] = []
    caveat: list[dict[str, Any]] = []

    def add_why(code: str, text: str) -> None:
        why.append({"code": code, "text": text})

    def add_caveat(code: str, text: str) -> None:
        caveat.append({"code": code, "text": text})

    board = row.get("board")
    enhanced = str(row.get("style_code") or "") in {
        "bond_convertible", "bond_secondary_enhanced", "bond_primary_enhanced"}
    if row.get("formal_score") is not None:
        if board == "bond_enhanced" or enhanced:
            add_why(
                "enhanced_admission",
                "通过增强债相对准入：同风格分位+地板、严格口径 7 项至少 5 项、正式分≥55；"
                "增强债不执行 L6C 的 70%/2%/0.90% 固定线，不能表述为“通过固定线”。",
            )
        elif board == "bond_stable":
            add_why(
                "fixed_lines",
                "通过稳健榜 L6C 固定线：综合分 %s（门槛 62）、1 年非负日占比 %s（门槛 70%%）、"
                "1 年持有成本 %s（L6C 上限 0.90%%）。"
                % (_num(row.get("composite")), _num(row.get("non_negative_1y_pct"), 2, "%"),
                   _num(row.get("cost_1y_pct"), 2, "%")),
            )
        elif board == "bond_long":
            add_why(
                "long_board_admission",
                "进入长期核心观察榜：3 年完整、5 年覆盖≥60% 且 5 年年化非空、"
                "经理在本基金≥730 天且覆盖 3 年窗口≥80%、份额≥2 亿元；该榜是影子观察榜，不占正式名额。",
            )
        else:
            add_why(
                "screen_admission",
                "通过筛选准入（综合分 %s；该榜为观察/研究视图，不等于正式榜固定线）。"
                % _num(row.get("composite")),
            )
    if row.get("is_full_3y"):
        add_why(
            "long_evidence",
            "3 年窗口有效 %s 年（覆盖 %s），满足正式榜的 3 年完整业绩要求。"
            % (_num(row.get("effective_years_3y"), 2),
               _num((row.get("coverage_ratio_3y") or 0) * 100, 1, "%")),
        )
    if (row.get("coverage_ratio_5y") or 0) >= 0.60 and row.get("annual_5y_pct") is not None:
        add_why(
            "five_year_evidence",
            "5 年窗口有效 %s 年（覆盖 %s），5 年年化 %s，长期指标不是只靠 1 年外推。"
            % (_num(row.get("effective_years_5y"), 2),
               _num((row.get("coverage_ratio_5y") or 0) * 100, 1, "%"),
               _num(row.get("annual_5y_pct"), 2, "%")),
        )
    elif (row.get("coverage_ratio_5y") or 0) >= 0.60:
        add_caveat(
            "five_year_annual_missing",
            "5 年窗口跨了 %s 年，但 5 年年化为空（点数不足或起点缺口）；"
            "不能把窗口长度当作 5 年收益证据。"
            % _num(row.get("effective_years_5y"), 2),
        )
    if (row.get("manager_tenure_min_days") or 0) >= 730:
        add_why(
            "manager_tenure",
            "现任经理在本基金最短任职 %s 天，超过 2 年，业绩与当前团队有较长重叠。"
            % _num(row.get("manager_tenure_min_days"), 0),
        )
    if (row.get("share_scale_billion") or 0) >= 2.0:
        add_why(
            "share_scale",
            "目标份额规模 %s 亿元，高于核心榜 2 亿元门槛。"
            % _num(row.get("share_scale_billion"), 2),
        )
    if board == "bond_long":
        add_why(
            "long_board",
            "长期核心观察榜按 0.6×长期正式分 + 0.4×长期核心分排序，长期分 %s；"
            "该榜是影子观察榜，不替换稳健榜。" % _num(row.get("long_score")),
        )
    if row.get("risk_status") == "ok":
        add_why("risk_gate", "持仓集中度、股票/转债暴露与历史压力测试未触发风险门阻断。")

    if row.get("non_negative_1y_pct") is not None and float(row["non_negative_1y_pct"]) < 72.0:
        nn = float(row["non_negative_1y_pct"])
        if nn < 70.0:
            add_caveat(
                "non_negative_below_floor",
                "1 年非负日占比 %s，低于 70%% 固定线 %s 个百分点；增强债不执行该固定线，"
                "此项只作收益分布参考，不能写成“距门槛只有”。"
                % (_num(nn, 2, "%"), _num(70.0 - nn, 2)),
            )
        else:
            add_caveat(
                "non_negative_near_floor",
                "1 年非负日占比 %s，距 70%% 门槛只有 %s 个百分点；平坦日较多，"
                "收益分布对估值精度敏感。"
                % (_num(nn, 2, "%"), _num(nn - 70.0, 2)),
            )
    if row.get("cost_1y_pct") is not None and float(row["cost_1y_pct"]) >= 1.0:
        cap = 1.30 if enhanced else 0.90
        cap_label = "L5.5 硬天花板" if enhanced else "L6C 固定线"
        add_caveat(
            "fee_near_cap",
            "1 年持有成本 %s，接近 %s%%（%s）；长期持有需比较 A/C 份额与赎回费。"
            % (_num(row.get("cost_1y_pct"), 2, "%"), _num(cap, 2), cap_label),
        )
    if (row.get("coverage_ratio_5y") or 0) < 0.90:
        add_caveat(
            "five_year_incomplete",
            "5 年窗口覆盖 %s，5 年指标含缺失或较短样本；不能把 5 年分位当作完整周期证据。"
            % _num((row.get("coverage_ratio_5y") or 0) * 100, 1, "%"),
        )
    if (row.get("manager_tenure_min_days") or 0) < 1095:
        days = float(row.get("manager_tenure_min_days") or 0)
        cover = min(1.0, days / 1095.0) * 100
        if board == "bond_long" and cover < 80:
            add_caveat(
                "manager_coverage_low",
                "现任经理最短任职 %s 天，覆盖 3 年窗口约 %s%%，不足 3 年；"
                "3 年业绩不完全由当前团队创造，低于长期榜 80%% 门槛。"
                % (_num(days, 0), _num(cover, 1)),
            )
        elif board == "bond_long":
            add_caveat(
                "manager_partial_coverage",
                "现任经理最短任职 %s 天，覆盖 3 年窗口约 %s%%，未满 3 年；"
                "3 年业绩不完全由当前团队创造，但已满足长期榜 80%% 门槛。"
                % (_num(days, 0), _num(cover, 1)),
            )
        else:
            add_caveat(
                "manager_short",
                "现任经理最短任职 %s 天，覆盖 3 年窗口约 %s%%，不足 3 年；"
                "3 年业绩不完全由当前团队创造。"
                % (_num(days, 0), _num(cover, 1)),
            )
    if row.get("share_scale_billion") is not None and float(row["share_scale_billion"]) < 3.0:
        add_caveat(
            "scale_small",
            "目标份额规模 %s 亿元，低于 3 亿元；大额申赎与清盘风险需单独评估。"
            % _num(row.get("share_scale_billion"), 2),
        )
    if row.get("risk_status") == "watch":
        add_caveat("risk_watch", "风险门为 watch（未阻断）：%s；需看最新季报是否延续。"
                   % (row.get("risk_flags") or "单项指标接近阈值"))
    elif row.get("risk_status") == "unknown":
        add_caveat("risk_unknown", "风险门为 unknown：%s；持仓或压力测试证据不完整。"
                   % (row.get("risk_flags") or "缺少持仓/压力测试"))
    if row.get("ongoing_drawdown") not in (None, 0, 0.0):
        # ongoing_drawdown 是 0/1 标志，不是幅度；不要把 1 印成 1.00%。
        add_caveat(
            "ongoing_drawdown",
            "当前仍处于未修复回撤中（尾部指标只记录是否发生，不记录幅度）；"
            "窗口内最大回撤可能低估真实回撤。",
        )
    if row.get("quality_status") == "watch":
        add_caveat("quality_watch", "长期质量观察为 watch（%s 项），需核对 MRAR/alpha/尾部指标。"
                   % _num(row.get("quality_watch_n"), 0))
    if (row.get("annual_1y_pct") is not None and row.get("annual_3y_pct") is not None
            and float(row["annual_1y_pct"]) > float(row["annual_3y_pct"]) + 0.5):
        add_caveat(
            "short_window_strong",
            "1 年年化 %s 高于 3 年年化 %s，近期收益更强；排序分为 0.85×综合分"
            "（含 1/3/5 年权重）+0.15×6 个月收益分位，不要把近一年表现外推为长期能力。"
            % (_num(row.get("annual_1y_pct"), 2, "%"), _num(row.get("annual_3y_pct"), 2, "%")),
        )
    if (row.get("mdd_3y_pct") is not None and row.get("mdd_1y_pct") is not None
            and abs(float(row["mdd_3y_pct"])) > abs(float(row["mdd_1y_pct"])) + 0.2):
        add_caveat(
            "mdd_window",
            "3 年最大回撤 %s 大于 1 年最大回撤 %s；1 年窗口没有覆盖完整压力期。"
            % (_num(row.get("mdd_3y_pct"), 2, "%"), _num(row.get("mdd_1y_pct"), 2, "%")),
        )
    if not caveat:
        add_caveat("execution", "规则层面未发现硬伤；仍需核对赎回费、持有期与个人现金流，历史净值不代表未来。")
    return {"why": why, "caveat": caveat}


def as_text(items: list[dict[str, Any]], limit: int = 6) -> str:
    parts = [str(x.get("text")).rstrip("。；") for x in items[:limit] if x.get("text")]
    return "；".join(parts)
