"""自选（收藏）：本项目唯一可写的业务表。

设计取舍：
- 只存 fcode / added_at / source / note，不存净值或评分快照——快照会过期，
  而收藏的用途是"长期观察"，展示时应实时 JOIN 当前库里的最新数据。
- 不做用户体系：本项目是单机/内网只读服务，收藏属于"这台机器的使用者"，
  多用户隔离留到真正需要鉴权时再做（见 docs/decisions.md ADR-032）。
"""
from __future__ import annotations

from typing import Any

from .. import db
from ..fees import holding as holding_mod

_COST_1Y = holding_mod.cost_sql("w.fcode", alias="p")

_SELECT = (
    "SELECT w.fcode, w.added_at, w.source, w.note, f.name, f.type_text, f.estab_date,"
    " s.style_code, p.asset_scale_billion, p.recurring_fee_pct,"
    " " + _COST_1Y + " AS cost_1y_pct,"
    " b.buyable_state, b.business_date,"
    " m.annual_dec AS annual_1y_dec, m.sharpe AS sharpe_1y, m.mdd_dec AS mdd_1y_dec,"
    " m.positive_ratio_pct AS positive_1y_pct,"
    " n.nav_date, n.unit_nav"
    " FROM fund_watchlist w"
    " JOIN fund f ON f.fcode=w.fcode"
    " LEFT JOIN fund_style s ON s.fcode=w.fcode"
    " LEFT JOIN fund_profile p ON p.fcode=w.fcode"
    " LEFT JOIN fund_fee_meta fm ON fm.fcode=w.fcode"
    " LEFT JOIN fund_buyable_daily b ON b.fcode=w.fcode"
    "   AND b.business_date=(SELECT MAX(business_date) FROM fund_buyable_daily)"
    " LEFT JOIN fund_metric m ON m.fcode=w.fcode AND m.period_days=365"
    "   AND m.formula_contract=?"
    " LEFT JOIN (SELECT fcode, MAX(nav_date) AS nav_date FROM fund_nav_daily GROUP BY fcode) d"
    "   ON d.fcode=w.fcode"
    " LEFT JOIN fund_nav_daily n ON n.fcode=w.fcode AND n.nav_date=d.nav_date"
)


class WatchlistError(ValueError):
    """收藏操作失败（代码为空、库里没有这只基金、重复添加）。"""


def list_items(conn, formula_contract: str) -> list[dict[str, Any]]:
    """按加入时间倒序列出收藏，附带当前口径下的关键指标。"""
    # 同一秒内连续添加时 added_at 相同，用 rowid 兜底保证"后加的在前面"
    rows = conn.execute(_SELECT + " ORDER BY w.added_at DESC, w.rowid DESC",
                        (formula_contract,)).fetchall()
    return [dict(r) for r in rows]


def get(conn, fcode: str, formula_contract: str) -> dict[str, Any] | None:
    row = conn.execute(_SELECT + " WHERE w.fcode=?", (formula_contract, fcode)).fetchone()
    return dict(row) if row else None


def add(conn, fcode: str, formula_contract: str, source: str = "manual",
        note: str | None = None) -> dict[str, Any]:
    code = (fcode or "").strip()
    if not code:
        raise WatchlistError("基金代码不能为空")
    if not conn.execute("SELECT 1 FROM fund WHERE fcode=?", (code,)).fetchone():
        raise WatchlistError("库里没有这只基金：%s" % code)
    if conn.execute("SELECT 1 FROM fund_watchlist WHERE fcode=?", (code,)).fetchone():
        raise WatchlistError("已经在收藏里：%s" % code)
    conn.execute("INSERT INTO fund_watchlist(fcode, added_at, source, note) VALUES(?,?,?,?)",
                 (code, db.now_iso(), source or "manual", (note or "").strip() or None))
    conn.commit()
    return get(conn, code, formula_contract) or {}


def remove(conn, fcode: str) -> bool:
    cur = conn.execute("DELETE FROM fund_watchlist WHERE fcode=?", ((fcode or "").strip(),))
    conn.commit()
    return cur.rowcount > 0
