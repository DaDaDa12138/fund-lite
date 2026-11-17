"""P0-3 独立风险门（持仓集中度 + 净值压力测试）。

这个模块回答的是“这只基金的风险是不是被收益/夏普掩盖了”，而不是再算一遍
收益排名。它只读 fund_holding_bond / fund_nav_daily / src_pzd_profile，
不修改基金指标；筛选引擎在 L8 经理门之后调用 assess()，把 block 写进
screen_decision.risk_json，并在 L8R 层留下原因码。

关键口径：
* 持仓按**债券名称**重新分类，不信任 bond_class（银行二级资本债常被标成
  other/credit 混合；可转债也可能被标错）。
* 压力测试一律用 acc_nav（累计净值），不用 unit_nav：分红/拆分会让 unit_nav
  出现假暴跌。2022-12-12 的 006431 就是典型例子。
* 缺失不静默通过：纯债/短债/信用债没有持仓时直接 block；利率债/指数允许
  unknown，因为政策性金融债指数天然只有少数发行人。
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import date, timedelta
from typing import Any

from .screen import thresholds as T

# 阈值集中在这里，方便审计时逐条对照；不要散落在 engine 里。
PURE_TOP10_BLOCK = 90.0
PURE_TOP10_WATCH = 75.0
PURE_TOP1_BLOCK = 25.0
PURE_TOP1_WATCH = 15.0
PURE_CREDIT_ISSUER_BLOCK = 25.0
PURE_CREDIT_ISSUER_WATCH = 15.0
PURE_CREDIT_TOP3_BLOCK = 45.0
PURE_CREDIT_TOP3_WATCH = 30.0
PURE_CONVERTIBLE_BLOCK = 5.0
PURE_CONVERTIBLE_WATCH = 3.0
PURE_EQUITY_BLOCK = 5.0
PURE_EQUITY_WATCH = 2.0
PURE_STRESS_2022_BLOCK = -3.0
PURE_STRESS_2022_WATCH = -2.0
PURE_STRESS_5Y_BLOCK = -4.0
PURE_STRESS_5Y_WATCH = -3.0

RATE_TOP10_BLOCK = 120.0
RATE_TOP10_WATCH = 100.0
RATE_TOP1_BLOCK = 60.0
RATE_TOP1_WATCH = 35.0

ENH_TOP10_BLOCK = 95.0
ENH_TOP10_WATCH = 85.0
ENH_TOP1_BLOCK = 35.0
ENH_TOP1_WATCH = 25.0
ENH_CREDIT_ISSUER_BLOCK = 35.0
ENH_CREDIT_ISSUER_WATCH = 25.0
ENH_CONVERTIBLE_BLOCK = 40.0
ENH_CONVERTIBLE_WATCH = 30.0
ENH_EQUITY_BLOCK = 20.0
ENH_EQUITY_WATCH = 10.0
ENH_STRESS_2022_BLOCK = -5.0
ENH_STRESS_2022_WATCH = -3.5
ENH_STRESS_5Y_BLOCK = -8.0
ENH_STRESS_5Y_WATCH = -6.0

RATE_LIKE_STYLES = {"bond_rate", "bond_index"}
# 持仓是季度披露；超过 270 天（约 3 个季度）不能再当作“最新一期”证明当前集中度。
MAX_HOLDING_AGE_DAYS = 270
CAPITAL_WORDS = ("二级资本债", "二级资本", "二级", "永续债", "资本债")
RATE_WORDS = ("国债", "地方政府债", "地方债", "政金债", "国开", "农发",
              "进出口", "口行", "政策性金融债")

_ALIASES = {
    "中行": "中国银行", "中国银行": "中国银行",
    "工行": "中国工商银行", "工商银行": "中国工商银行",
    "建行": "中国建设银行", "建设银行": "中国建设银行",
    "农行": "中国农业银行", "农业银行": "中国农业银行",
    "交行": "交通银行", "交通银行": "交通银行",
    "邮储": "中国邮政储蓄银行", "邮储银行": "中国邮政储蓄银行",
    "浦发": "上海浦东发展银行", "浦发银行": "上海浦东发展银行",
    "中信": "中信银行", "中信银行": "中信银行",
    "兴业": "兴业银行", "兴业银行": "兴业银行",
    "招商": "招商银行", "招商银行": "招商银行",
    "民生": "中国民生银行", "民生银行": "中国民生银行",
    "光大": "中国光大银行", "光大银行": "中国光大银行",
    "华夏": "华夏银行", "华夏银行": "华夏银行",
    "平安": "平安银行", "平安银行": "平安银行",
    "国开": "国家开发银行", "国开行": "国家开发银行",
    "农发": "中国农业发展银行", "农发行": "中国农业发展银行",
    "进出口": "中国进出口银行", "进出口行": "中国进出口银行",
    "进出口银行": "中国进出口银行",
    "邮政": "中国邮政", "国债": "中国政府", "地方债": "地方政府",
}


def period_end(period: str) -> date | None:
    """把 2026Q3 / 2026-09-30 统一成报告期末日。"""
    s = str(period or "").strip()
    m = re.match(r"^(\d{4})Q([1-4])$", s)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        month = q * 3
        return date(y, month, calendar.monthrange(y, month)[1])
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def issuer_key(name: str | None) -> str:
    """从债券简称提取发行人，做保守归一；未知名称返回清洗后的字符串。"""
    raw = str(name or "").strip()
    s = re.sub(r"^\d{2,3}", "", raw)
    s = re.sub(r"[（(].*?[)）]", "", s)
    # 先去掉资本债/永续债/发行批次等后缀；二级资本债必须在“国开/农发”之前判断，
    # 否则政策银行二级资本债会被误当成利率债。
    s = re.sub(r"(二级资本债|二级资本|二级|永续债|资本债|清发|SCP|CP|MTN|PPN).*$",
               "", s, flags=re.IGNORECASE)
    s = re.sub(r"(转债|转\d+).*$", "", s)
    s = re.sub(r"\d{1,3}[A-Za-z]*$", "", s)
    s = s.strip(" -_/")
    if not s:
        return raw
    return _ALIASES.get(s, s)


def bond_kind(name: str | None, stored: str | None = None) -> str:
    """按名称重分类：convertible / rate / credit。"""
    n = str(name or "")
    if "转债" in n or re.search(r"转\d", n):
        return "convertible"
    # 银行/政策银行二级资本债、永续债都是信用债，即使发行人是国开/农发。
    if any(w in n for w in CAPITAL_WORDS):
        return "credit"
    if any(w in n for w in RATE_WORDS):
        return "rate"
    if stored == "convertible":
        return "convertible"
    if stored == "rate":
        return "rate"
    return "credit"


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _latest_holdings(conn, fcode: str, as_of: str) -> tuple[str | None, list[dict[str, Any]]]:
    if not _has_table(conn, "fund_holding_bond"):
        return None, []
    rows = conn.execute(
        "SELECT report_period, COUNT(*) AS n FROM fund_holding_bond"
        " WHERE fcode=? GROUP BY report_period", (fcode,)).fetchall()
    candidates = []
    for r in rows:
        end = period_end(r["report_period"])
        if end is not None and end <= date.fromisoformat(as_of[:10]):
            candidates.append((end, r["report_period"]))
    if not candidates:
        return None, []
    end, period = max(candidates)
    if (date.fromisoformat(as_of[:10]) - end).days > MAX_HOLDING_AGE_DAYS:
        return period, []
    detail = conn.execute(
        "SELECT bond_code, bond_name, pct_nav, bond_class FROM fund_holding_bond"
        " WHERE fcode=? AND report_period=? ORDER BY pct_nav DESC",
        (fcode, period)).fetchall()
    return period, [dict(r) for r in detail]


def holdings_risk(conn, fcode: str, as_of: str) -> dict[str, Any]:
    period, rows = _latest_holdings(conn, fcode, as_of)
    if not rows:
        return {"available": False, "report_period": period, "n_holdings": 0,
                "top10_pct": None, "top1_pct": None, "credit_issuer_top_pct": None,
                "credit_issuer_top3_pct": None, "convertible_pct": None,
                "rate_pct": None, "credit_pct": None, "bond_alloc_pct": None,
                "reason": "stale_holdings" if period else "no_holdings"}
    items = []
    for r in rows:
        pct = r.get("pct_nav")
        if pct is None:
            continue
        pct = float(pct)
        name = r.get("bond_name") or ""
        items.append({"name": name, "pct": pct, "kind": bond_kind(name, r.get("bond_class")),
                      "issuer": issuer_key(name)})
    if not items:
        return {"available": False, "report_period": period, "n_holdings": 0,
                "top10_pct": None, "top1_pct": None, "credit_issuer_top_pct": None,
                "credit_issuer_top3_pct": None, "convertible_pct": None,
                "rate_pct": None, "credit_pct": None, "bond_alloc_pct": None,
                "reason": "no_pct_nav"}
    pcts = sorted((x["pct"] for x in items), reverse=True)
    credit = [x for x in items if x["kind"] == "credit"]
    by_issuer: dict[str, float] = {}
    for x in credit:
        by_issuer[x["issuer"]] = by_issuer.get(x["issuer"], 0.0) + x["pct"]
    issuer_sorted = sorted(by_issuer.values(), reverse=True)
    total = sum(pcts)
    return {
        "available": True,
        "report_period": period,
        "n_holdings": len(items),
        "top10_pct": round(sum(pcts[:10]), 2),
        "top1_pct": round(pcts[0], 2),
        "credit_issuer_top_pct": round(issuer_sorted[0], 2) if issuer_sorted else 0.0,
        "credit_issuer_top3_pct": round(sum(issuer_sorted[:3]), 2) if issuer_sorted else 0.0,
        "convertible_pct": round(sum(x["pct"] for x in items if x["kind"] == "convertible"), 2),
        "rate_pct": round(sum(x["pct"] for x in items if x["kind"] == "rate"), 2),
        "credit_pct": round(sum(x["pct"] for x in items if x["kind"] == "credit"), 2),
        "bond_alloc_pct": round(total, 2),
    }


def _nav_return(conn, fcode: str, start: str, end: str) -> dict[str, Any] | None:
    if not _has_table(conn, "fund_nav_daily"):
        return None
    a = conn.execute(
        "SELECT nav_date, acc_nav FROM fund_nav_daily WHERE fcode=? AND nav_date<=?"
        " AND acc_nav IS NOT NULL AND acc_nav>0 ORDER BY nav_date DESC LIMIT 1",
        (fcode, start)).fetchone()
    b = conn.execute(
        "SELECT nav_date, acc_nav FROM fund_nav_daily WHERE fcode=? AND nav_date<=?"
        " AND acc_nav IS NOT NULL AND acc_nav>0 ORDER BY nav_date DESC LIMIT 1",
        (fcode, end)).fetchone()
    if not a or not b or a["nav_date"] == b["nav_date"]:
        return None
    if (date.fromisoformat(b["nav_date"]) - date.fromisoformat(a["nav_date"])).days > 120:
        return None
    return {"pct": round((float(b["acc_nav"]) / float(a["acc_nav"]) - 1) * 100, 2),
            "start": a["nav_date"], "end": b["nav_date"]}


def _worst_rolling(conn, fcode: str, start: str, end: str,
                   window: int = 60) -> dict[str, Any] | None:
    if not _has_table(conn, "fund_nav_daily"):
        return None
    rows = conn.execute(
        "SELECT nav_date, acc_nav FROM fund_nav_daily WHERE fcode=? AND nav_date>=?"
        " AND nav_date<=? AND acc_nav IS NOT NULL AND acc_nav>0 ORDER BY nav_date",
        (fcode, start, end)).fetchall()
    if len(rows) < window + 1:
        return None
    worst = None
    for i in range(window, len(rows)):
        base = float(rows[i - window]["acc_nav"])
        if base <= 0:
            continue
        pct = (float(rows[i]["acc_nav"]) / base - 1) * 100
        if worst is None or pct < worst["pct"]:
            worst = {"pct": round(pct, 2), "start": rows[i - window]["nav_date"],
                     "end": rows[i]["nav_date"]}
    return worst


def stress_risk(conn, fcode: str, as_of: str) -> dict[str, Any]:
    as_of_d = date.fromisoformat(as_of[:10])
    windows = {
        "2022_q4": ("2022-11-01", "2022-12-31"),
        "2020_h1": ("2020-05-01", "2020-06-30"),
        "2016_q4": ("2016-12-01", "2016-12-31"),
    }
    out: dict[str, Any] = {"available": False, "n_nav": None, "nav_first": None,
                           "nav_last": None}
    if not _has_table(conn, "fund_nav_daily"):
        return out
    r = conn.execute(
        "SELECT COUNT(*) AS n, MIN(nav_date) AS first, MAX(nav_date) AS last"
        " FROM fund_nav_daily WHERE fcode=? AND acc_nav IS NOT NULL AND acc_nav>0"
        " AND nav_date<=?", (fcode, as_of)).fetchone()
    if not r or not r["n"]:
        return out
    out.update({"available": True, "n_nav": r["n"], "nav_first": r["first"],
                "nav_last": r["last"]})
    for key, (start, end) in windows.items():
        out[key] = _nav_return(conn, fcode, start, end)
    out["worst60_5y"] = _worst_rolling(
        conn, fcode, (as_of_d - timedelta(days=365 * 5)).isoformat(), as_of)
    out["worst60_all"] = _worst_rolling(conn, fcode, "2000-01-01", as_of)
    return out


def _last_series_value(raw: str | None, key: str) -> float | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    for item in (data.get("series") or []):
        if item.get("name") != key:
            continue
        vals = item.get("data") or []
        if not vals:
            return None
        try:
            return float(vals[-1])
        except (TypeError, ValueError):
            return None
    return None


def _alloc(conn, fcode: str, as_of: str) -> dict[str, float | None]:
    if not _has_table(conn, "src_pzd_profile"):
        return {"stock_pct": None, "bond_pct": None, "cash_pct": None}
    row = conn.execute(
        "SELECT asset_alloc_json FROM src_pzd_profile WHERE code=? AND business_date<=?"
        " ORDER BY business_date DESC LIMIT 1", (fcode, as_of)).fetchone()
    raw = row["asset_alloc_json"] if row else None
    if not raw:
        # business_date 是抓取日，不是报告期。2026-10-31 抓到的 2026Q3 资产配置，
        # 报告期是 2026-09-30；若 as_of=2026-10-30，抓取日虽晚一天，报告期已经可用。
        # 只在 categories 最后一期 <= as_of 时才回退到最近一条未来抓取记录，避免前视。
        row = conn.execute(
            "SELECT asset_alloc_json FROM src_pzd_profile WHERE code=? AND business_date>?"
            " ORDER BY business_date ASC LIMIT 1", (fcode, as_of)).fetchone()
        raw = row["asset_alloc_json"] if row else None
        if raw:
            try:
                cats = json.loads(raw).get("categories") or []
            except (TypeError, ValueError):
                cats = []
            last = str(cats[-1])[:10] if cats else ""
            if not last or last > as_of[:10]:
                raw = None
    return {"stock_pct": _last_series_value(raw, "股票占净比"),
            "bond_pct": _last_series_value(raw, "债券占净比"),
            "cash_pct": _last_series_value(raw, "现金占净比")}


def _flag(code: str, label: str, status: str, value: Any, threshold: str,
          detail: str) -> dict[str, Any]:
    return {"code": code, "label": label, "status": status, "value": value,
            "threshold": threshold, "detail": detail}


def _add(flags: list[dict[str, Any]], code: str, label: str, value: float | None,
         block: float, watch: float, threshold: str, higher_is_worse: bool = True,
         detail: str = "") -> None:
    if value is None:
        flags.append(_flag(code, label, "unknown", None, threshold, detail or "数据缺失"))
        return
    if higher_is_worse:
        status = "block" if value > block else ("watch" if value > watch else "ok")
    else:
        status = "block" if value < block else ("watch" if value < watch else "ok")
    flags.append(_flag(code, label, status, value, threshold, detail))


def _style_asof(conn, fcode: str, as_of: str) -> str | None:
    if not _has_table(conn, "fund_style_history"):
        return None
    row = conn.execute(
        "SELECT style_code FROM fund_style_history WHERE fcode=? AND as_of_date<=?"
        " ORDER BY as_of_date DESC LIMIT 1", (fcode, as_of)).fetchone()
    return row["style_code"] if row else None


def assess(conn, fcode: str, style: str | None = None, as_of: str | None = None,
           name: str | None = None) -> dict[str, Any]:
    """对单只基金做独立风险体检；返回 flags + summary，不修改数据库。"""
    as_of = (as_of or date.today().isoformat())[:10]
    as_of_d = date.fromisoformat(as_of)
    row = conn.execute("SELECT name, estab_date FROM fund WHERE fcode=?", (fcode,)).fetchone()
    if row:
        name = name or row["name"]
        estab = row["estab_date"]
    else:
        estab = None
    if style is None:
        style = _style_asof(conn, fcode, as_of)
        if not style:
            srow = conn.execute("SELECT style_code FROM fund_style WHERE fcode=?", (fcode,)).fetchone()
            style = srow["style_code"] if srow else ""
    style = style or ""
    enhanced = style in T.ENHANCED_STYLES
    holdings = holdings_risk(conn, fcode, as_of)
    stress = stress_risk(conn, fcode, as_of)
    alloc = _alloc(conn, fcode, as_of)
    flags: list[dict[str, Any]] = []

    rate_like = style in RATE_LIKE_STYLES
    if holdings.get("available"):
        rate_like = rate_like or (float(holdings.get("rate_pct") or 0) >= 85
                                  and float(holdings.get("credit_pct") or 0) <= 10)
    else:
        reason = holdings.get("reason")
        stale_note = ("持仓报告期 %s 已超过 %d 天" % (holdings.get("report_period"),
                                                   MAX_HOLDING_AGE_DAYS)
                      if reason == "stale_holdings" else "无可用持仓")
        flags.append(_flag("holdings", "持仓披露", "unknown", None,
                           "最近一期持仓（≤%d 天）" % MAX_HOLDING_AGE_DAYS,
                           stale_note + "；利率/指数基金允许未知，信用债/增强债不放行"))
        if not rate_like:
            flags.append(_flag("holdings_required", "持仓披露（信用债/增强债）", "block", None,
                               "必须有最近一期持仓",
                               stale_note + "，不能证明当前集中度与含权暴露"))

    if holdings.get("available"):
        if rate_like:
            _add(flags, "top10", "前十大债券集中度", holdings["top10_pct"],
                 RATE_TOP10_BLOCK, RATE_TOP10_WATCH, "前十大≤120%（利率债/指数）")
            _add(flags, "top1", "单一债券集中度", holdings["top1_pct"],
                 RATE_TOP1_BLOCK, RATE_TOP1_WATCH, "单一债券≤60%（利率债/指数）")
        elif enhanced:
            _add(flags, "top10", "前十大债券集中度", holdings["top10_pct"],
                 ENH_TOP10_BLOCK, ENH_TOP10_WATCH, "前十大≤95%（增强债）")
            _add(flags, "top1", "单一债券集中度", holdings["top1_pct"],
                 ENH_TOP1_BLOCK, ENH_TOP1_WATCH, "单一债券≤35%（增强债）")
        else:
            _add(flags, "top10", "前十大债券集中度", holdings["top10_pct"],
                 PURE_TOP10_BLOCK, PURE_TOP10_WATCH, "前十大≤90%")
            _add(flags, "top1", "单一债券集中度", holdings["top1_pct"],
                 PURE_TOP1_BLOCK, PURE_TOP1_WATCH, "单一债券≤25%")
        if not rate_like:
            _add(flags, "credit_issuer", "信用债单一发行人", holdings["credit_issuer_top_pct"],
                 ENH_CREDIT_ISSUER_BLOCK if enhanced else PURE_CREDIT_ISSUER_BLOCK,
                 ENH_CREDIT_ISSUER_WATCH if enhanced else PURE_CREDIT_ISSUER_WATCH,
                 "单一信用发行人≤35%（增强债）/≤25%（纯债）")
            if not enhanced:
                _add(flags, "credit_issuer_top3", "信用债前三大发行人",
                     holdings["credit_issuer_top3_pct"], PURE_CREDIT_TOP3_BLOCK,
                     PURE_CREDIT_TOP3_WATCH, "前三大信用发行人≤45%")
        _add(flags, "convertible", "可转债暴露", holdings["convertible_pct"],
             ENH_CONVERTIBLE_BLOCK if enhanced else PURE_CONVERTIBLE_BLOCK,
             ENH_CONVERTIBLE_WATCH if enhanced else PURE_CONVERTIBLE_WATCH,
             "可转债≤40%（增强债）/≤5%（纯债）")

    equity = alloc.get("stock_pct")
    if equity is None and not rate_like:
        flags.append(_flag("equity_required", "股票暴露（必填）", "block", None,
                           "股票≤20%（增强债）/≤5%（纯债）",
                           "无资产配置数据；纯债/信用债/增强债不能带着未知股票暴露通过"))
    else:
        _add(flags, "equity", "股票暴露", equity,
             ENH_EQUITY_BLOCK if enhanced else PURE_EQUITY_BLOCK,
             ENH_EQUITY_WATCH if enhanced else PURE_EQUITY_WATCH,
             "股票≤20%（增强债）/≤5%（纯债）",
             detail=("无资产配置数据" if equity is None else ""))

    window_starts = {"2022_q4": "2022-11-01", "2020_h1": "2020-05-01",
                     "2016_q4": "2016-12-01",
                     "worst60_5y": (as_of_d - timedelta(days=365 * 5)).isoformat()}
    # 完全没有累计净值：老基金是数据缺口，新基金只是“尚未经历压力窗口”。
    # 不能对每只新基金都记一条 unknown，否则风险门会被“数据缺失”淹没。
    if not stress.get("available"):
        if estab and estab <= "2022-11-01":
            flags.append(_flag("stress", "净值压力", "unknown", None,
                               "应覆盖 2022Q4 等压力窗口", "无可用累计净值"))
    for key, label, block, watch, threshold in (
        ("2022_q4", "2022Q4 债市压力", ENH_STRESS_2022_BLOCK if enhanced else PURE_STRESS_2022_BLOCK,
         ENH_STRESS_2022_WATCH if enhanced else PURE_STRESS_2022_WATCH,
         "2022-11/12 累计收益≥-5%（增强债）/≥-3%（纯债）"),
        ("2020_h1", "2020H1 债市压力", -6.0 if enhanced else -4.0,
         -4.0 if enhanced else -2.5, "2020-05/06 累计收益"),
        ("2016_q4", "2016Q4 债市压力", -6.0 if enhanced else -5.0,
         -4.0 if enhanced else -3.0, "2016-12 累计收益"),
        ("worst60_5y", "近5年最差60日", ENH_STRESS_5Y_BLOCK if enhanced else PURE_STRESS_5Y_BLOCK,
         ENH_STRESS_5Y_WATCH if enhanced else PURE_STRESS_5Y_WATCH,
         "近5年最差60日≥-8%（增强债）/≥-4%（纯债）"),
    ):
        if not stress.get("available"):
            continue
        item = stress.get(key)
        if item is None:
            start = window_starts.get(key)
            # 基金成立晚于窗口：这是“不适用”，不是“未知”，不能因此把状态拖成 unknown。
            if start and estab and estab > start:
                continue
            # 有净值但第一笔晚于窗口，同样是不适用；成立早于窗口却缺窗口收益才是数据缺口。
            if (start and stress.get("available") and stress.get("nav_first")
                    and stress["nav_first"] > start):
                continue
            flags.append(_flag(key, label, "unknown", None, threshold,
                               "应覆盖窗口但缺少累计净值"))
            continue
        _add(flags, key, label, item["pct"], block, watch, threshold,
             higher_is_worse=False, detail="窗口收益")

    allworst = stress.get("worst60_all")
    if allworst and allworst["pct"] < (-10.0 if enhanced else -8.0):
        flags.append(_flag("worst60_all", "历史最差60日", "watch", allworst["pct"],
                           "仅作证据，不作硬门", "历史极端窗口；不直接判定当前风险"))

    blocked = [f["code"] for f in flags if f["status"] == "block"]
    watch = [f["code"] for f in flags if f["status"] == "watch"]
    unknown = [f["code"] for f in flags if f["status"] == "unknown"]
    status = "block" if blocked else ("watch" if watch else ("unknown" if unknown else "ok"))
    return {
        "as_of": as_of,
        "style_code": style,
        "holdings": holdings,
        "stress": stress,
        "alloc": alloc,
        "flags": flags,
        "summary": {"status": status, "blocked": bool(blocked), "watch": bool(watch),
                    "unknown": bool(unknown), "blocked_flags": blocked,
                    "watch_flags": watch, "unknown_flags": unknown,
                    "n_flags": len(flags)},
        "note": "独立风险门只做集中度/压力体检；block 会写入 L8R，watch/unknown 只展示，"
                "不改变收益分。",
    }
