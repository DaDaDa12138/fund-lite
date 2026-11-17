"""费用模型：从 jjfl 阶梯事实推导情景净收益。

边界（架构书 §14）：
- 只有"同份额 + 确定 NAV + 比例费 + 无其他费用"才可能 verified；本项目一律 scenario_assumed。
- 固定费（每笔 1000 元）单独走份额扣减，不得当成 0%，也不得混入比例公式。
- 不从 NAV 再扣管理费/托管费/销售服务费：这些已在每日净值中计提。
"""
from __future__ import annotations

import datetime as _dt
import sqlite3

from dataclasses import dataclass, field
from typing import Any, Sequence

RETURN_TYPE = "fee_net_scenario_assumed"
FEE_MODEL_STATE = "scenario_assumed"
ASSUMPTIONS = [
    "按 jjfl 页面当日公布的阶梯费率计算，未考虑平台额外折扣与活动",
    "持有期按自然日、赎回费按先进先出单笔计算",
    "未计入认/申购份额确认日与赎回款到账日的资金占用",
    "管理费/托管费/销售服务费已在每日净值中计提，不重复扣除",
]


@dataclass
class Tier:
    tier_no: int
    amount_lo_cny: float | None = None
    amount_hi_cny: float | None = None
    days_lo: int | None = None
    days_hi: int | None = None
    rate_pct: float | None = None
    discount_pct: float | None = None
    fixed_cny: float | None = None


@dataclass
class Quote:
    fcode: str
    amount_cny: float
    holding_days: int
    nav_return_pct: float | None = None
    nav_return_window_days: int | None = None
    purchase: Tier | None = None
    redemption: Tier | None = None
    net_return_pct: float | None = None
    net_return_discount_pct: float | None = None
    return_type: str = "unknown"
    fee_model_state: str = "unavailable"
    user_fit: str = "compatible_with_cost_unknown"
    assumptions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _in_amount(t: Tier, amount: float) -> bool:
    lo = t.amount_lo_cny if t.amount_lo_cny is not None else 0.0
    hi = t.amount_hi_cny
    return amount >= lo and (hi is None or amount < hi)


def _in_days(t: Tier, days: int) -> bool:
    lo = t.days_lo if t.days_lo is not None else 0
    hi = t.days_hi
    return days >= lo and (hi is None or days <= hi)


def pick_purchase(tiers: Sequence[Tier], amount_cny: float) -> Tier | None:
    for t in tiers:
        if _in_amount(t, amount_cny):
            return t
    return None


def pick_redemption(tiers: Sequence[Tier], holding_days: int) -> Tier | None:
    for t in tiers:
        if _in_days(t, holding_days):
            return t
    return None


def net_return_decimal(nav_return_pct: float, f_in_pct: float, f_out_pct: float, *,
                       fixed_cny: float = 0.0, amount_cny: float = 0.0) -> float:
    """外扣模型：比例费 net=(1+R)*(1-f_out)/(1+f_in)-1；固定费按份额扣减。"""
    r = nav_return_pct / 100.0
    f_out = f_out_pct / 100.0
    if fixed_cny and fixed_cny > 0:
        if amount_cny <= fixed_cny:
            raise ValueError("fixed_fee_exceeds_amount")
        # 固定申购费：先从本金扣费，剩余资金按 NAV 买入
        invested_ratio = (amount_cny - fixed_cny) / amount_cny
        return invested_ratio * (1.0 + r) * (1.0 - f_out) - 1.0
    f_in = f_in_pct / 100.0
    return (1.0 + r) * (1.0 - f_out) / (1.0 + f_in) - 1.0


def _trailing_return_pct(conn, fcode: str, days: int) -> tuple[float | None, int]:
    """按持有期窗口复算链式复投总回报 (窗口=(end-days, end])；数据不足返回 (None, n)。

    与指标引擎同一口径：只用日增长率链式复利，不用单位净值/累计净值（见 ADR-005）。
    """
    try:
        rows = conn.execute(
            "SELECT nav_date, growth_pct FROM fund_nav_daily"
            " WHERE fcode=? AND growth_pct IS NOT NULL ORDER BY nav_date", (fcode,)).fetchall()
    except sqlite3.OperationalError:
        return None, 0
    if len(rows) < 2:
        return None, len(rows)
    try:
        end = _dt.date.fromisoformat(str(rows[-1]["nav_date"])[:10])
        cutoff = (end - _dt.timedelta(days=int(days))).isoformat()
    except ValueError:
        return None, len(rows)
    wealth = 1.0
    n = 0
    for r in rows:
        d = str(r["nav_date"])[:10]
        if d > cutoff and d <= end.isoformat():
            wealth *= 1.0 + float(r["growth_pct"]) / 100.0
            n += 1
    if n < 2:
        return None, n
    return (wealth - 1.0) * 100.0, n


def quote(conn, fcode: str, amount_cny: float, holding_days: int,
          *, user_max_drawdown: float | None = None,
          formula_contract: str | None = None) -> Quote:
    q = Quote(fcode=fcode, amount_cny=float(amount_cny), holding_days=int(holding_days))
    q.assumptions = list(ASSUMPTIONS)
    sql = ("SELECT total_return_pct, mdd_dec FROM fund_metric"
           " WHERE fcode=? AND period_days=365")
    params: list = [fcode]
    if formula_contract:
        sql += " AND formula_contract=?"
        params.append(formula_contract)
    metric = conn.execute(sql + " ORDER BY computed_at DESC LIMIT 1", params).fetchone()
    period_return, period_obs = _trailing_return_pct(conn, fcode, holding_days)
    if period_return is not None:
        q.nav_return_pct = period_return
        q.nav_return_window_days = int(holding_days)
    elif metric and metric["total_return_pct"] is not None:
        q.nav_return_pct = float(metric["total_return_pct"])
        q.nav_return_window_days = 365
        q.reasons.append("nav_return_window_proxy:365d")
    else:
        q.reasons.append("nav_return_window_unavailable")
    lock_days = None
    try:
        policy = conn.execute("SELECT effective_days FROM fund_holding_policy WHERE fcode=?",
                              (fcode,)).fetchone()
        if policy and policy["effective_days"]:
            lock_days = int(policy["effective_days"])
    except sqlite3.OperationalError:
        lock_days = None
    if lock_days and int(holding_days) < lock_days:
        q.reasons.append("lockup_conflict:%dd" % lock_days)

    meta = conn.execute("SELECT * FROM fund_fee_meta WHERE fcode=?", (fcode,)).fetchone()
    if not meta:
        q.reasons.append("fee_schedule_unavailable")
        if lock_days and int(holding_days) < lock_days:
            q.user_fit = "conflict_lockup"
        return q
    purchase = [Tier(**{k: r[k] for k in r.keys() if k in Tier.__dataclass_fields__})
                for r in conn.execute(
                    "SELECT * FROM fund_fee_tier WHERE fcode=? AND kind='purchase' ORDER BY tier_no",
                    (fcode,))]
    redemption = [Tier(**{k: r[k] for k in r.keys() if k in Tier.__dataclass_fields__})
                  for r in conn.execute(
                      "SELECT * FROM fund_fee_tier WHERE fcode=? AND kind='redemption' ORDER BY tier_no",
                      (fcode,))]
    q.purchase = pick_purchase(purchase, amount_cny)
    q.redemption = pick_redemption(redemption, holding_days)
    if q.purchase is None:
        q.reasons.append("purchase_tier_not_found")
    if q.redemption is None:
        q.reasons.append("redemption_tier_not_found")

    if q.nav_return_pct is not None and q.purchase and q.redemption:
        try:
            if q.purchase.fixed_cny:
                base = net_return_decimal(q.nav_return_pct, 0.0, q.redemption.rate_pct or 0.0,
                                          fixed_cny=q.purchase.fixed_cny, amount_cny=amount_cny)
                q.net_return_pct = base * 100.0
            elif q.purchase.rate_pct is not None:
                q.net_return_pct = net_return_decimal(
                    q.nav_return_pct, q.purchase.rate_pct, q.redemption.rate_pct or 0.0) * 100.0
            if q.purchase.discount_pct is not None and q.redemption.rate_pct is not None:
                q.net_return_discount_pct = net_return_decimal(
                    q.nav_return_pct, q.purchase.discount_pct, q.redemption.rate_pct) * 100.0
            q.return_type = RETURN_TYPE
            q.fee_model_state = FEE_MODEL_STATE
        except ValueError as exc:
            q.reasons.append(str(exc))

    buyable = conn.execute(
        "SELECT buyable_state FROM fund_buyable_daily WHERE fcode=?"
        " AND business_date=(SELECT MAX(business_date) FROM fund_buyable_daily)",
        (fcode,)).fetchone()
    state = buyable["buyable_state"] if buyable else "unknown"
    mdd = float(metric["mdd_dec"]) if metric and metric["mdd_dec"] is not None else None

    if lock_days and int(holding_days) < lock_days:
        q.user_fit = "conflict_lockup"
    elif q.purchase is None or q.redemption is None or q.net_return_pct is None:
        q.user_fit = "compatible_with_cost_unknown"
    elif user_max_drawdown is not None and mdd is not None and mdd > user_max_drawdown:
        q.user_fit = "risk_budget_conflict"
    elif state != "confirmed_yes":
        q.user_fit = "unknown_or_unavailable"
    elif q.fee_model_state == FEE_MODEL_STATE:
        q.user_fit = "compatible_under_modeled_assumptions"
    return q


def as_dict(q: Quote) -> dict[str, Any]:
    def tier(t: Tier | None) -> dict[str, Any] | None:
        if t is None:
            return None
        return {"tier_no": t.tier_no, "amount_lo_cny": t.amount_lo_cny,
                "amount_hi_cny": t.amount_hi_cny, "days_lo": t.days_lo, "days_hi": t.days_hi,
                "rate_pct": t.rate_pct, "discount_pct": t.discount_pct, "fixed_cny": t.fixed_cny}

    return {
        "fcode": q.fcode, "amount_cny": q.amount_cny, "holding_days": q.holding_days,
        "nav_return_pct": q.nav_return_pct, "nav_return_window_days": q.nav_return_window_days,
        "purchase": tier(q.purchase),
        "redemption": tier(q.redemption), "net_return_pct": q.net_return_pct,
        "net_return_discount_pct": q.net_return_discount_pct,
        "return_type": q.return_type, "fee_model_state": q.fee_model_state,
        "user_fit": q.user_fit, "assumptions": q.assumptions, "reasons": q.reasons,
    }