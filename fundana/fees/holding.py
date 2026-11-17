"""持有期总成本（holding-period total cost，ADR-059）。

审查结论（F8）：旧口径只用"经常性费率"（管理+托管+销售服务费），A 类与 C 类不在同一把尺子上——
A 类申购费一次性支付、持有越久摊得越薄；C 类不收申购费但每年多收销售服务费。用经常性费率比较，
A 类系统性占优（002073 A=0.40% vs 002074 C=0.80%），而用户实际付出的成本取决于**持有多久**。

口径：
    cost(h) = 经常性费率 + 申购费有效值 / (h / 365)

* 申购费有效值 = max_sub_fee_pct × 折扣系数；`has_discount` 为真时按 1 折（第三方平台通行），
  否则按挂牌价全额。缺 `has_discount` 数据按"无折扣"处理（保守，不美化）。
* 赎回费按"持有满 1 年免收"处理（A/C 类通行条款）。阶梯表 `fund_fee_schedule` 已于 2026-11-02
  落库（37,781 行），但本函数仍不下探 7 天惩罚档；`fund_fee_meta.redemption_1y_pct` **不进入筛选门槛**，
  只做展示/审计与 /cost 情景。门槛衡量持续持有成本，不把“是否持有满 1 年”这个行为变量混进质量分；
  页面必须写明赎回费另见费用页签。
* 管理费/托管费/销售服务费已在每日净值中计提，本函数只做横向比较，不再从收益里二次扣除。
"""
from __future__ import annotations

from typing import Any

DISCOUNT_FACTOR = 0.1          # 第三方平台常见 1 折
DEFAULT_HORIZONS = (365, 1095, 1825)  # 1 年 / 3 年 / 5 年


def holding_cost(recurring_pct: float | None, max_sub_fee_pct: float | None,
                 has_discount: bool | None, horizon_days: int,
                 discount_factor: float = DISCOUNT_FACTOR) -> dict[str, Any] | None:
    """返回 {horizon_days, recurring_pct, purchase_effective_pct, amortized_purchase_pct, total_pct, discount_applied}。"""
    if recurring_pct is None or horizon_days <= 0:
        return None
    sub = max_sub_fee_pct or 0.0
    discounted = bool(has_discount)
    purchase = sub * discount_factor if discounted else sub
    amortized = purchase / (horizon_days / 365.0)
    return {
        "horizon_days": int(horizon_days),
        "recurring_pct": round(float(recurring_pct), 4),
        "purchase_effective_pct": round(purchase, 4),
        "amortized_purchase_pct": round(amortized, 4),
        "total_pct": round(float(recurring_pct) + amortized, 4),
        "discount_applied": discounted,
    }


def cost_sql(fcode_expr: str, alias: str = "fp", horizon_days: int = 365,
             discount_alias: str = "fm") -> str:
    """同口径的 SQL 表达式（列表/排序/导出用）。

    与 holding_cost() 逐项等价：经常性 + 申购费有效值 × 365/h。
    `alias` 提供 recurring_fee_pct / max_sub_fee_pct；`discount_alias` 提供
    has_discount。默认沿用 fund_profile fp + fund_fee_meta fm；榜单 API 传
    `alias="i", discount_alias="i"` 以读取 screen_run_input 的发布时快照。
    """
    factor = 365.0 / float(horizon_days)
    return ("ROUND(%s.recurring_fee_pct + COALESCE(%s.max_sub_fee_pct, 0)"
            " * (CASE WHEN %s.has_discount THEN %.2f ELSE 1.0 END) * %.6f, 4)"
            % (alias, alias, discount_alias, DISCOUNT_FACTOR, factor))


def cost_profile(recurring_pct: float | None, max_sub_fee_pct: float | None,
                 has_discount: bool | None,
                 horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> dict[int, dict[str, Any]]:
    """多档持有期成本，键为天数。"""
    out: dict[int, dict[str, Any]] = {}
    for h in horizons:
        row = holding_cost(recurring_pct, max_sub_fee_pct, has_discount, h)
        if row is not None:
            out[int(h)] = row
    return out
