"""榜单查询的 SQL 片段（单独成模块，便于单测直接验证语义）。

同一只基金的 A/C/D 份额是独立的基金份额（fcode 不同、净值不同、费率不同），
筛选时**不能合并**；但展示时常常只想看"每个家族一只代表"，于是提供 dedupe=family：
只保留同一 family_id 下排名最靠前的那只，其余仍在 decisions/ 与候选 CSV 里可查。
"""
from __future__ import annotations

from ..fees import holding as holding_mod

# 持有期总成本（ADR-059）：页面与排序都用同一口径，避免"显示的费率"与"排序依据"打架。
# P1：榜单必须读发布时快照 screen_run_input，而不是当前的 fund_profile /
# fund_fee_meta / fund_holding_policy / fund_share_class。发布后规模、费率、
# 锁定期与家族关系都会漂移；再 JOIN 当前表会把历史榜单改写成今天的口径。
COST_1Y_SQL = holding_mod.cost_sql("p.fcode", alias="i", discount_alias="i")
COST_3Y_SQL = holding_mod.cost_sql("p.fcode", alias="i", discount_alias="i",
                                   horizon_days=1095)
COST_5Y_SQL = holding_mod.cost_sql("p.fcode", alias="i", discount_alias="i",
                                   horizon_days=1825)

FAMILY_DEDUPE_CLAUSE = (
    "(i.family_id IS NULL OR p.rank = (SELECT MIN(p2.rank)"
    " FROM screen_pick p2 JOIN screen_run_input i2"
    "   ON i2.run_id=p2.run_id AND i2.fcode=p2.fcode"
    " WHERE i2.family_id=i.family_id AND p2.run_id=p.run_id"
    "   AND p2.board=p.board))"
)

DEDUPE_MODES = {"": None, "family": FAMILY_DEDUPE_CLAUSE}


def dedupe_clause(mode: str | None) -> str | None:
    """返回要追加到 WHERE 的片段；未知模式返回 None（不静默改成 family）。"""
    if not mode:
        return None
    return DEDUPE_MODES.get(mode.strip())


def family_member_count(conn, run_id: str, board: str) -> dict[str, int]:
    """每个家族本轮入榜的份额数，用于页面上标"同家族另有 N 只"。"""
    rows = conn.execute(
        "SELECT i.family_id, COUNT(*) AS n FROM screen_pick p"
        " JOIN screen_run_input i ON i.run_id=p.run_id AND i.fcode=p.fcode"
        " WHERE p.run_id=? AND p.board=? AND i.family_id IS NOT NULL"
        " GROUP BY i.family_id", (run_id, board)).fetchall()
    return {r["family_id"]: r["n"] for r in rows}


# ── 榜单行 SQL ──────────────────────────────────────────────
# 七个周期各 JOIN 一次 fund_metric（7/30/90/180/365/1095/1825 天）：区间收益与年化都取出来，
# 因为 7 天的年化会被放大成三位数（一周 +0.5% ≈ 年化 26%），单看任何一个都会误读。
# 3/5 年字段是「证据」而不是排序键；缺失为 NULL，绝不用 1 年值冒充。
# 占位符顺序固定：净值 as-of → (风险契约 + 指标 as-of) ×7 → 尾部窗口天数 + 尾部契约 + 指标 as-of
# → WHERE 条件 → limit/offset。所有 JOIN 都必须钉在 run 的 nav_as_of/metric_as_of 上，
# 否则 API 会在发布后读到下一交易日的净值/指标，出现“报告 11-02、页面 11-03”的前视。
# 契约必须锁全：fund_metric 与 fund_tail_metric 都按 formula_contract 多版本共存，
# 漏锁任何一处，同一只基金就会 JOIN 出两行，榜单序号变成 1、1、2、2（线上事故根因）。
ROW_SQL = (
    "SELECT p.rank, p.fcode, f.name, p.formal_score, p.rank_score, p.confidence, d.style_code,"
    " json_extract(d.scores_json, '$.long_score') AS long_score,"
    " json_extract(d.scores_json, '$.coverage.coverage_ratio_5y') AS coverage_ratio_5y,"
    " i.family_id, d.layer_reached, d.risk_json, i.asset_scale_billion, i.recurring_fee_pct,"
    " " + COST_1Y_SQL + " AS cost_1y_pct, " + COST_3Y_SQL + " AS cost_3y_pct,"
    " " + COST_5Y_SQL + " AS cost_5y_pct,"
    " COALESCE(i.redemption_1y_pct, fm.redemption_1y_pct) AS redemption_1y_pct,"
    " i.effective_days AS lockup_days, i.lockup_type,"
    " n.nav_date, n.growth_pct AS day_pct, m.end_date AS metric_end_date,"
    " w1.total_return_pct AS ret_1w_pct, w1.annual_dec AS annual_1w_dec,"
    " w2.total_return_pct AS ret_1m_pct, w2.annual_dec AS annual_1m_dec,"
    " w3.total_return_pct AS ret_3m_pct, w3.annual_dec AS annual_3m_dec,"
    " w4.total_return_pct AS ret_6m_pct, w4.annual_dec AS annual_6m_dec,"
    " m.total_return_pct AS ret_1y_pct, m.annual_dec AS annual_1y,"
    " m.sharpe AS sharpe_1y, m.sortino, m.calmar_dec AS calmar,"
    " m.positive_ratio_pct AS positive, m.flat_ratio_pct AS flat,"
    " m.non_negative_ratio_pct AS non_negative, m.mdd_dec AS mdd_1y,"
    " m3.annual_dec AS annual_3y, m3.mdd_dec AS fund_mdd_3y,"
    " m3.vol_dec AS vol_3y, m3.sharpe AS sharpe_3y, m3.calmar_dec AS calmar_3y,"
    " m3.years AS effective_years_3y, m3.n_obs AS n_obs_3y,"
    " m5.annual_dec AS annual_5y, m5.mdd_dec AS fund_mdd_5y,"
    " m5.vol_dec AS vol_5y, m5.sharpe AS sharpe_5y, m5.calmar_dec AS calmar_5y,"
    " m5.years AS effective_years_5y, m5.n_obs AS n_obs_5y,"
    " i.share_scale_billion,"
    " (SELECT MIN(j.days) FROM fund_manager_tenure j"
    "   WHERE j.fcode=p.fcode AND j.is_current=1) AS manager_tenure_min_days,"
    " (SELECT mc.flags_json FROM fund_metric_comparability mc"
    "   WHERE mc.fcode=p.fcode AND mc.period_days=365"
    "   ORDER BY mc.as_of DESC LIMIT 1) AS metric_flags_json,"
    " (SELECT mc.is_comparable FROM fund_metric_comparability mc"
    "   WHERE mc.fcode=p.fcode AND mc.period_days=365"
    "   ORDER BY mc.as_of DESC LIMIT 1) AS metric_comparable,"
    " t.mdd_dec AS mdd_3y, t.worst_rolling90d_pct AS worst_rolling90d,"
    " t.completeness AS tail_completeness"
    " FROM screen_pick p JOIN fund f ON f.fcode=p.fcode"
    " LEFT JOIN screen_run_input i ON i.run_id=p.run_id AND i.fcode=p.fcode"
    " LEFT JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
    " LEFT JOIN fund_fee_meta fm ON fm.fcode=p.fcode"
    " LEFT JOIN fund_nav_daily n ON n.fcode=p.fcode AND n.nav_date="
    "   (SELECT MAX(nav_date) FROM fund_nav_daily x WHERE x.fcode=p.fcode"
    "     AND x.nav_date<=?)"
    " LEFT JOIN fund_metric w1 ON w1.fcode=p.fcode AND w1.period_days=7"
    "   AND w1.formula_contract=? AND w1.end_date<=?"
    " LEFT JOIN fund_metric w2 ON w2.fcode=p.fcode AND w2.period_days=30"
    "   AND w2.formula_contract=? AND w2.end_date<=?"
    " LEFT JOIN fund_metric w3 ON w3.fcode=p.fcode AND w3.period_days=90"
    "   AND w3.formula_contract=? AND w3.end_date<=?"
    " LEFT JOIN fund_metric w4 ON w4.fcode=p.fcode AND w4.period_days=180"
    "   AND w4.formula_contract=? AND w4.end_date<=?"
    " LEFT JOIN fund_metric m ON m.fcode=p.fcode AND m.period_days=365"
    "   AND m.formula_contract=? AND m.end_date<=?"
    " LEFT JOIN fund_metric m3 ON m3.fcode=p.fcode AND m3.period_days=1095"
    "   AND m3.formula_contract=? AND m3.end_date<=?"
    " LEFT JOIN fund_metric m5 ON m5.fcode=p.fcode AND m5.period_days=1825"
    "   AND m5.formula_contract=? AND m5.end_date<=?"
    " LEFT JOIN fund_tail_metric t ON t.fcode=p.fcode AND t.window_days=?"
    "   AND t.formula_contract=? AND t.end_date<=?"
)
