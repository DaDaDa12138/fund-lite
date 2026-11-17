"""内部代码 → 中文展示标签。

前端只显示中文；原始代码只作为 title 悬停提示，避免 "bond_secondary_enhanced"
这类内部枚举直接暴露给使用者。所有映射都在服务端，改文案不需要动前端。
"""
from __future__ import annotations

SOURCE_LABELS = {
    "em_directory": "东财 · 基金名录",
    "em_status_snapshot": "东财 · 申赎状态快照",
    "em_jbgk": "东财 · 基本概况（jbgk）",
    "em_pzd": "东财 · 基金档案（pingzhongdata）",
    "em_nav_snapshot": "东财 · 当日净值快照",
    "em_rank": "东财 · 开放式排行",
    "em_zqcc": "东财 · 债券持仓（季报）",
    "em_jjfl": "东财 · 费率阶梯（jjfl）",
    "em_gmbd": "东财 · 规模变动（gmbd）",
    "em_jjjl": "东财 · 基金经理（jjjl）",
    "dj_nav": "蛋卷 · 历史净值",
    "em_lsjz": "东财 · 历史净值（lsjz）",
}

PROFILE_LABELS = {
    "bond_v1": "债券型 V1（正式口径）",
    "bond_v2": "债券型 V2（影子口径）",
}

RUN_MODE_LABELS = {
    "initial": "初次全量",
    "incremental": "增量",
    "daily": "日常增量",
    "probe": "数据源探针",
    "collect": "采集",
    "fuse": "融合",
    "metrics": "指标重算",
    "tail": "尾部指标",
    "screen": "筛选",
    "shadow": "影子榜",
    "publish": "发布",
    "verify": "发布校验",
    "replay": "离线重放",
}

RUN_STATUS_LABELS = {"success": "成功", "ok": "成功", "partial": "部分成功",
                    "running": "运行中", "failed": "失败", "aborted": "中断"}

FETCH_STATUS_LABELS = {
    "ok": "成功",
    "provider_null": "上游无数据",
    "parse_failed": "解析失败",
    "http_error": "HTTP 错误",
    "timeout": "超时",
    "soft_fail": "业务失败（200 但无效）",
    "skipped_fresh": "缓存未过期",
}

STYLE_LABELS = {
    "bond_pure": "纯债（综合）",
    "bond_short": "中短债",
    "bond_rate": "利率债",
    "bond_credit": "信用债",
    "bond_convertible": "可转债",
    "bond_primary_enhanced": "一级债基（增强）",
    "bond_secondary_enhanced": "二级债基（增强）",
    "bond_cash_enhanced": "现金增强",
    "bond_index": "指数债",
    "unknown": "未分类",
}

CONFIDENCE_LABELS = {
    "structured_confirmed": "结构化证据确认",
    "multi_field_consistent": "多字段一致",
    "text_heuristic": "名称推断",
    "conflict": "证据冲突",
    "unknown": "无法判定",
}

SCALE_SOURCE_LABELS = {
    "em_jbgk_family_sum": "同家族份额合计（基金级）",
    "em_jbgk": "单一份额净资产",
    "em_pzd": "规模变动末点（份额级）",
    "missing": "未获取",
}

BOARD_LABELS = {
    "bond_stable": "纯债稳健榜",
    "bond_enhanced": "增强债榜",
    "bond_core": "核心精选榜（50 只）",
    "bond_core10": "收益精选榜（10 只）",
    "bond_long": "长期核心观察榜（20 只）",
    "bond_locked": "持有期受限榜",
    "bond_watch": "观察榜（当期买不到）",
    "bond_rookie": "新锐榜（历史 1–3 年）",
    "bond_enhanced_watch": "增强债观察池（研究）",
}

LAYER_LABELS = {
    "L1": "身份与数据资格",
    "L2": "当日可申购",
    "L3": "持有期限制",
    "L4": "资料完整性",
    "L5": "成熟度与规模",
    "L5.5": "持续计提费",
    "L6A": "窗口质量",
    "L6B": "相对风险",
    "L6C": "绝对门槛",
    "L6E": "增强债独立准入",
    "L7": "动态分位门槛",
    "L8": "经理稳定性",
    "L8R": "独立风险门",
    "L9": "排序与配额",
    "L10": "双榜",
}

BUYABLE_LABELS = {
    "confirmed_yes": "可申购",
    "confirmed_no": "不可申购",
    "unknown": "状态未知",
    "conflicting": "状态冲突",
}

RETURN_TYPE_LABELS = {
    "fee_net_scenario_assumed": "按公布费率测算（情景）",
    "provider_period_return": "上游区间收益",
    "validated_nav_total_return": "净值复权总回报",
    "nav_price_return_proxy": "单位净值价格回报（代理）",
    "fee_net_estimate_verified": "费率估算（已核验）",
    "unknown": "未知",
}

USER_FIT_LABELS = {
    "conflict_lockup": "持有期未满，不能赎回",
    "unknown_or_unavailable": "当日不可申购或信息不足",
    "risk_budget_conflict": "超出你的回撤预算",
    "compatible_with_cost_unknown": "可投，但费用未知",
    "compatible_under_modeled_assumptions": "可投（按假设测算）",
}

BOND_CLASS_LABELS = {
    "convertible": "可转债",
    "rate": "利率债",
    "credit": "信用债",
    "other": "其他债券",
    "unknown": "未分类",
}

# 原因码里的字段名与比较方向
CHECK_LABELS = {
    "composite": "综合分",
    "formal_score": "正式分",
    "scale": "家族规模",
    "annual_1y": "1 年年化",
    "sharpe": "夏普比率",
    "sortino": "索提诺比率",
    "calmar": "卡玛比率",
    "positive": "日胜率",
    "non_negative": "非负日占比",
    "flat": "持平日占比",
    "history_3y": "3 年完整业绩",
    "scale_pit": "规模 as-of 可得",
    "mdd": "最大回撤",
    "vol": "波动率",
    "fee": "持续计提费",
    "min_dimensions": "通过维度数",
    "strict_overlay": "严格口径通过项数",
}

# 单位决定前端怎么格式化：score=0-100 分，pct=百分点，dec=小数比率（乘 100 显示），
# billion=亿元，num=纯数值（比率类，保留两位）
CHECK_UNITS = {
    "composite": "score", "formal_score": "score", "scale": "billion",
    "annual_1y": "pct", "positive": "pct", "non_negative": "pct", "flat": "pct", "fee": "pct",
    "history_3y": "bool", "scale_pit": "bool",
    "mdd": "dec", "vol": "dec",
    "sharpe": "num", "sortino": "num", "calmar": "num",
    "min_dimensions": "count", "strict_overlay": "count",
}

CHECK_GROUPS = {
    "fixed_or_admission": "基础门槛（固定线）",
    "dynamic": "动态分位门槛",
    "enhanced_strict": "增强债严格准入（留档口径）",
}

LOCKUP_LABELS = {
    "periodic_open": "定期开放",
    "holding": "持有期",
    "none": "无持有期限制",
    "unknown": "未判定",
}

LOCK_BUCKETS = [
    ("91-180", "91–180 天", 91, 180),
    ("181-365", "181–365 天", 181, 365),
    ("366-730", "366–730 天", 366, 730),
    ("731+", "731 天以上", 731, 100000),
]

FIELD_LABELS = {
    "composite": "综合分",
    "annual_1y": "1 年年化",
    "sharpe": "夏普比率",
    "sortino": "索提诺比率",
    "calmar": "卡玛比率",
    "positive": "日胜率",
    "non_negative": "非负日占比",
    "flat": "持平日占比",
    "history_3y": "3 年完整业绩",
    "mdd": "1 年最大回撤",
    "vol": "1 年波动率",
    "fee": "持续计提费",
    "scale": "规模",
    "formal_score": "正式分",
    "min_dimensions": "通过维度数",
}
KIND_LABELS = {
    "below_min": "低于门槛",
    "above_max": "高于上限",
    "below_dynamic": "低于动态门槛",
    "above_dynamic": "高于动态上限",
    "missing": "缺失",
}

REASON_LABELS = {
    "buyable_confirmed_no": "当日不可申购",
    "buyable_unknown": "申购状态未知",
    "exchange_traded": "场内交易（ETF/LOF，走证券账户，不参与场外申购榜）",
    "no_1y_metrics": "缺少 1 年期指标",
    "sampling_cadence_gt_3d": "披露频率低于日频（仅研究）",
    "nav_stale": "净值不新鲜",
    "no_nav": "没有净值数据",
    "estab_date_missing": "缺少成立日",
    "identity_quarantined": "身份待核实",
    "maturity_lt_1y": "成立不足 1 年",
    "insufficient_points": "观测数不足",
    "coverage_gap": "净值覆盖有缺口",
    "gap_exceeded": "净值间隔过大",
    "empty_window": "窗口内没有净值",
    "zero_downside": "没有下行波动，索提诺不定义",
    "zero_drawdown": "没有回撤，卡玛不定义",
    "zero_volatility": "波动为零，夏普不定义",
    "fee_schedule_unavailable": "未采到费率档位",
    "nav_return_window_unavailable": "持有窗口内没有净值",
    "purchase_tier_not_found": "投入金额未落到任何申购档",
    "redemption_tier_not_found": "持有天数未落到任何赎回档",
    "scale_missing": "缺少规模数据",
    "manager_missing": "缺少经理信息",
}

KIND_WORDS = {"week": "周", "month": "月", "year": "年"}


def _fallback(kind: str) -> str:
    return {"style": "其他风格", "confidence": "未评估", "scale": "未获取",
            "board": "未入榜", "layer": "未进入筛选", "buyable": "状态未知",
            "return_type": "未知", "user_fit": "未知", "bond_class": "未分类",
            "reason": "其他原因", "source": "未知来源", "fetch_status": "未知状态"}.get(kind, "—")


def label_style(code) -> str:
    return STYLE_LABELS.get(str(code or ""), _fallback("style"))


def label_confidence(code) -> str:
    return CONFIDENCE_LABELS.get(str(code or ""), _fallback("confidence"))


def label_scale_source(code) -> str:
    return SCALE_SOURCE_LABELS.get(str(code or ""), _fallback("scale"))


def label_board(code) -> str:
    return BOARD_LABELS.get(str(code or ""), _fallback("board"))


def label_layer(code) -> str:
    return LAYER_LABELS.get(str(code or ""), _fallback("layer"))


def label_buyable(code) -> str:
    return BUYABLE_LABELS.get(str(code or ""), _fallback("buyable"))


def label_return_type(code) -> str:
    return RETURN_TYPE_LABELS.get(str(code or ""), _fallback("return_type"))


def label_user_fit(code) -> str:
    return USER_FIT_LABELS.get(str(code or ""), _fallback("user_fit"))


def label_bond_class(code) -> str:
    return BOND_CLASS_LABELS.get(str(code or ""), _fallback("bond_class"))


def label_source(code) -> str:
    return SOURCE_LABELS.get(str(code or ""), _fallback("source"))


def label_fetch_status(code) -> str:
    return FETCH_STATUS_LABELS.get(str(code or ""), _fallback("fetch_status"))


def label_profile(code) -> str:
    return PROFILE_LABELS.get(str(code or ""), str(code or "—"))


def label_run_mode(code) -> str:
    code = str(code or "")
    if code in RUN_MODE_LABELS:
        return RUN_MODE_LABELS[code]
    if code.startswith("probe"):
        return "数据源探针"
    return code or "—"


def label_run_status(code) -> str:
    return RUN_STATUS_LABELS.get(str(code or ""), str(code or "—"))


def label_check(code) -> str:
    return CHECK_LABELS.get(code, "其他检查项")


def check_unit(code) -> str:
    return CHECK_UNITS.get(code, "num")


def label_lockup(code) -> str:
    return LOCKUP_LABELS.get(code, "未判定")


def label_check_group(code) -> str:
    return CHECK_GROUPS.get(code, "门槛")


def label_reason(code) -> str:
    """把 composite:below_min / scale_below_3.00 / manager_career_below_365d:81d 等
    结构化原因码翻译成一句话。未知码统一显示“其他原因”，原始码放 title。"""
    text = str(code or "").strip()
    if not text:
        return _fallback("reason")
    if text in REASON_LABELS:
        return REASON_LABELS[text]
    if text.startswith("scale_below_"):
        return "规模低于该成熟度要求（%s 亿）" % text[len("scale_below_"):]
    if text.startswith("holding_cost_above_"):
        return "持有 1 年总成本高于 %s%%" % text[len("holding_cost_above_"):]
    if text.startswith("recurring_fee_above_"):
        # v1/v2 历史决策里的原因码，保留可读性（新批次不再产生）
        return "持续计提费高于 %s%%" % text[len("recurring_fee_above_"):]
    if text.startswith("sampling_cadence_gt_") and text.endswith("d"):
        days = text[len("sampling_cadence_gt_"):-1]
        return "披露频率低于 %s 天（仅研究）" % days
    if text.startswith("manager_career_below_365d:"):
        days = text.split(":", 1)[1].rstrip("d")
        return "经理职业年限不足 365 天（%s 天）" % days
    if text.startswith("lockup_conflict:"):
        days = text.split(":", 1)[1].rstrip("d")
        return "持有期未满 %s 天" % days
    if text.startswith("dyn_min_dimensions:"):
        passed = text.split(":", 1)[1].split("<")[0]
        return "动态门槛通过维度不足（%s 项）" % passed
    if text.startswith("nav_return_window_proxy:"):
        return "持有窗口不足，改用 %s 天收益" % text.split(":", 1)[1].rstrip("d")
    head, _, kind = text.partition(":")
    field = FIELD_LABELS.get(head)
    if field and kind in KIND_LABELS:
        return field + KIND_LABELS[kind]
    if head.startswith("locked_") and head.endswith("d"):
        return "最短持有期 %s 天（超过 90 天，进持有期受限榜）" % head[len("locked_"):-1]
    if head.startswith("manager_career_below_"):
        return "经理职业年限不足门槛（%s）" % head.split(":", 1)[-1]
    return _fallback("reason")


def labels_payload() -> dict:
    """前端下拉框/图例用：只给中文标签与代码，不给内部含义。"""
    def pairs(mapping):
        return [{"code": k, "label": v} for k, v in mapping.items()]

    return {
        "styles": pairs(STYLE_LABELS),
        "confidences": pairs(CONFIDENCE_LABELS),
        "boards": pairs(BOARD_LABELS),
        "layers": pairs(LAYER_LABELS),
        "scale_sources": pairs(SCALE_SOURCE_LABELS),
        "return_types": pairs(RETURN_TYPE_LABELS),
        "user_fits": pairs(USER_FIT_LABELS),
        "bond_classes": pairs(BOND_CLASS_LABELS),
        "buyable": pairs(BUYABLE_LABELS),
        "sources": pairs(SOURCE_LABELS),
        "fetch_statuses": pairs(FETCH_STATUS_LABELS),
        "profiles": pairs(PROFILE_LABELS),
        "run_modes": pairs(RUN_MODE_LABELS),
        "run_statuses": pairs(RUN_STATUS_LABELS),
        "checks": [{"code": k, "label": v, "unit": CHECK_UNITS.get(k, "num")}
                   for k, v in CHECK_LABELS.items()],
        "check_groups": pairs(CHECK_GROUPS),
        "lockups": pairs(LOCKUP_LABELS),
        "lock_buckets": [{"code": b[0], "label": b[1]} for b in LOCK_BUCKETS],
    }
