"""采集编排：把解析结果写入 L1 源事实层，并把 NAV 直接落 L2（行级带来源）。"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any, Iterable

from . import cn_calendar, db, fundtypes
from .fees import store as fee_store
from .fetch import parsers
from .fetch.http import Fetcher, FetchResult
from .fundtypes import (BOND_INDEX_TYPE, EXTRA_PREFIXES,  # noqa: F401  (兼容旧名字)
                        TYPE_PREFIXES as TYPE_GROUP,
                        type_group, type_prefixes)

# 风格因子归因用的指数（ADR-078）：国债=久期，企债-国债=信用，中证转债=转债 beta。
INDEX_SERIES = {"000012": "国债指数", "000013": "企债指数", "000832": "中证转债"}
# 真实债券总回报基准：中国债券信息网公开 JSON 接口（中债-新综合财富（总值）指数）。
# 与上面的价格指数分开存，避免把财富指数混进三因子归因。
CHINABOND_INDEX = {"code": "CBC_CFZS_00", "name": "中债-新综合财富(总值)指数"}
CHINABOND_FORM = {
    "indexid": "8a8b2ca0332abed20134ea76d8885831",
    "qxlxt": "00",
    "ltcslx": "",
    "zslxt": "CFZS",
    "lx": "1",
    "locale": "",
}

# 类型前缀表与归类逻辑已迁到 fundana/fundtypes.py（类型插件注册表的唯一入口，ADR-077）。
# 这里保留 TYPE_GROUP / type_group / type_prefixes 旧名字做兼容：fuse/identity.py、
# cli.py、tests/test_fuse.py 仍按 collect 的路径引用。


def _now() -> str:
    return db.now_iso()


# ── A1 名录 ─────────────────────────────────────────────────
def collect_directory(fetcher: Fetcher, business_date: str) -> dict[str, Any]:
    res = fetcher.get("em_directory", key="-")
    if not res.business_ok:
        return {"ok": False, "status": res.status, "rows": 0}
    try:
        rows = parsers.parse_directory(res.text)
    except parsers.ParseError as exc:
        fetcher.mark_business_failure(res, "parse_failed", str(exc))
        return {"ok": False, "status": "parse_failed", "rows": 0}
    observed = _now()
    payload = [(business_date, observed, r["code"], r["name"], r["pinyin_abbr"], r["type_text"],
                r["pinyin_full"], res.raw_sha256) for r in rows]
    fetcher.conn.executemany(
        "INSERT INTO src_directory(business_date, observed_at, code, name, pinyin_abbr, type_text,"
        " pinyin_full, raw_sha256) VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(business_date, code) DO UPDATE SET name=excluded.name, type_text=excluded.type_text,"
        " pinyin_abbr=excluded.pinyin_abbr, pinyin_full=excluded.pinyin_full, observed_at=excluded.observed_at,"
        " raw_sha256=excluded.raw_sha256", payload)
    fetcher.conn.commit()
    groups: dict[str, int] = {}
    for r in rows:
        g = type_group(r["type_text"])
        groups[g] = groups.get(g, 0) + 1
    return {"ok": True, "status": "ok", "rows": len(rows), "groups": groups,
            "raw_sha256": res.raw_sha256, "from_cache": res.from_cache}


# ── E3 全市场申赎状态 ───────────────────────────────────────
def collect_status(fetcher: Fetcher, business_date: str) -> dict[str, Any]:
    res = fetcher.get("em_status_snapshot", key="-")
    if not res.business_ok:
        return {"ok": False, "status": res.status, "rows": 0}
    try:
        parsed = parsers.parse_status(res.text)
    except parsers.ParseError as exc:
        fetcher.mark_business_failure(res, "parse_failed", str(exc))
        return {"ok": False, "status": "parse_failed", "rows": 0}
    observed = _now()
    payload = []
    for r in parsed["rows"]:
        payload.append((business_date, observed, r["code"], r["name"], r["type_text"],
                        _s(r["unit_nav"]), _s(r["nav_date_md"]), r["purchase_status"],
                        r["redemption_status"], _s(r["next_open_date"]), _s(r["min_purchase"]),
                        _s(r["fee_pct"]), _s(r["flag_code"]), res.raw_sha256))
    fetcher.conn.executemany(
        "INSERT INTO src_snapshot_status(business_date, observed_at, code, name, type_text, unit_nav,"
        " nav_date_md, purchase_status, redemption_status, next_open_date, min_purchase, fee_pct,"
        " flag_code, raw_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(business_date, code) DO UPDATE SET unit_nav=excluded.unit_nav,"
        " purchase_status=excluded.purchase_status, redemption_status=excluded.redemption_status,"
        " next_open_date=excluded.next_open_date, raw_sha256=excluded.raw_sha256", payload)
    fetcher.conn.commit()
    return {"ok": True, "status": "ok", "rows": len(payload),
            "declared_total": parsed["declared_total"], "showday": parsed["showday"],
            "raw_sha256": res.raw_sha256, "from_cache": res.from_cache}


# ── 指数日线（风格因子归因）──────────────────────────────────
def collect_index(fetcher: Fetcher) -> dict[str, Any]:
    """拉三只指数日线并 upsert。数据量很小（约 2,900 行/只），每天整段覆盖即可。"""
    series: dict[str, Any] = {}
    total = 0
    ok = True
    for code, label in INDEX_SERIES.items():
        res = fetcher.get("em_index_kline", key=code)
        if not res.business_ok:
            series[code] = {"label": label, "ok": False, "status": res.status}
            ok = False
            continue
        try:
            parsed = parsers.parse_index_kline(res.text)
        except parsers.ParseError as exc:
            fetcher.mark_business_failure(res, "parse_failed", str(exc))
            series[code] = {"label": label, "ok": False, "status": "parse_failed"}
            ok = False
            continue
        observed = _now()
        payload = [(code, r["trade_date"], r["close"], r["pct_chg"], "em_index_kline",
                    res.raw_sha256, observed) for r in parsed["rows"]]
        fetcher.conn.executemany(
            "INSERT INTO market_index_daily(index_code, trade_date, close, pct_chg, source_id,"
            " raw_sha256, observed_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(index_code, trade_date) DO UPDATE SET close=excluded.close,"
            " pct_chg=excluded.pct_chg, raw_sha256=excluded.raw_sha256,"
            " observed_at=excluded.observed_at", payload)
        total += len(payload)
        series[code] = {"label": label, "ok": True, "rows": len(payload),
                        "first": payload[0][1] if payload else None,
                        "last": payload[-1][1] if payload else None,
                        "from_cache": res.from_cache}
    fetcher.conn.commit()
    try:
        cb = collect_chinabond_index(fetcher)
    except Exception as exc:  # 基准失败不能抹掉已经落库的三只因子指数
        cb = {"ok": False, "status": "exception", "rows": 0, "error": str(exc)}
    return {"ok": ok and bool(cb.get("ok")), "factor_ok": ok, "rows": total,
            "series": series, "chinabond": cb}


def collect_chinabond_index(fetcher: Fetcher) -> dict[str, Any]:
    """拉取中债-新综合财富（总值）指数并 upsert 到 market_index_daily。

    该接口只接受 POST form；返回全历史约 6,200 个交易日。指数是财富/总值口径，
    可直接作为债券基金的真实总回报基准，不再用 ETF 复权净值冒充。
    """
    code = CHINABOND_INDEX["code"]
    res = fetcher.post_form("cb_new_composite", key=code, data=CHINABOND_FORM)
    if not res.business_ok:
        return {"ok": False, "status": res.status, "rows": 0, "code": code}
    try:
        parsed = parsers.parse_chinabond_index(res.text)
    except parsers.ParseError as exc:
        fetcher.mark_business_failure(res, "parse_failed", str(exc))
        return {"ok": False, "status": "parse_failed", "rows": 0, "code": code}
    observed = _now()
    payload = [(code, r["trade_date"], r["close"], r["pct_chg"], "cb_new_composite",
                res.raw_sha256, observed) for r in parsed["rows"]]
    fetcher.conn.executemany(
        "INSERT INTO market_index_daily(index_code, trade_date, close, pct_chg, source_id,"
        " raw_sha256, observed_at) VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(index_code, trade_date) DO UPDATE SET close=excluded.close,"
        " pct_chg=excluded.pct_chg, raw_sha256=excluded.raw_sha256,"
        " observed_at=excluded.observed_at", payload)
    fetcher.conn.commit()
    return {"ok": True, "status": "ok", "rows": len(payload), "code": code,
            "name": parsed["name"], "first": payload[0][1] if payload else None,
            "last": payload[-1][1] if payload else None, "raw_sha256": res.raw_sha256,
            "from_cache": res.from_cache}


def _s(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ── D1 排行（横截面，分页）──────────────────────────────────
def collect_rank(fetcher: Fetcher, business_date: str, ft: str = "zq", page_size: int = 500,
                 max_pages: int = 40) -> dict[str, Any]:
    # 排行请求的起止日必须跟本次业务日一致，不能用宿主机本地日期：
    # 否则在 UTC 机器上跨过北京时间零点时会取错一天。
    today = date.fromisoformat(str(business_date)[:10])
    params_base = {
        "op": "ph", "dt": "kf", "ft": ft, "rs": "", "gs": "0",
        "sc": "1nzf", "st": "desc",
        "sd": (today - timedelta(days=365 * 3)).isoformat(),
        "ed": today.isoformat(),
        "qdii": "", "tabSubtype": ",,,,", "dx": "1",
    }
    total_rows = 0
    declared = None
    seen: set[str] = set()
    pages = 0
    for page in range(1, max_pages + 1):
        params = dict(params_base, pi=str(page), pn=str(page_size))
        res = fetcher.get("em_rank", key=f"page{page}", params=params, allow_cache=False)
        if not res.business_ok:
            return {"ok": total_rows > 0, "status": res.status, "rows": total_rows, "pages": pages}
        try:
            parsed = parsers.parse_rank(res.text)
        except parsers.ParseError as exc:
            fetcher.mark_business_failure(res, "parse_failed", str(exc))
            return {"ok": total_rows > 0, "status": "parse_failed", "rows": total_rows, "pages": pages}
        declared = parsed.get("declared_total") or declared
        rows = parsed["rows"]
        if not rows:
            break
        pages += 1
        observed = _now()
        payload = []
        for r in rows:
            if r["code"] in seen:
                continue
            seen.add(r["code"])
            payload.append((business_date, observed, r["code"], r["name"], r["pinyin"],
                            _s(r["nav_date"]), _s(r["unit_nav"]), _s(r["acc_nav"]), _s(r["day_pct"]),
                            _s(r["w1_pct"]), _s(r["m1_pct"]), _s(r["m3_pct"]), _s(r["m6_pct"]),
                            _s(r["y1_pct"]), _s(r["y2_pct"]), _s(r["y3_pct"]), _s(r["ytd_pct"]),
                            _s(r["since_pct"]), _s(r["estab_date"]), _s(r["scale_yi"]),
                            _s(r["fee_orig_pct"]), _s(r["fee_now_pct"]), page, r["row_ord"],
                            res.raw_sha256, _s(r["custom_pct"])))
        fetcher.conn.executemany(
            "INSERT INTO src_rank_row(business_date, observed_at, code, name, pinyin, nav_date,"
            " unit_nav, acc_nav, day_pct, w1_pct, m1_pct, m3_pct, m6_pct, y1_pct, y2_pct, y3_pct,"
            " ytd_pct, since_pct, estab_date, scale_yi, fee_orig_pct, fee_now_pct, page, row_ord,"
            " raw_sha256, custom_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(business_date, code) DO UPDATE SET scale_yi=excluded.scale_yi,"
            " custom_pct=excluded.custom_pct, fee_now_pct=excluded.fee_now_pct,"
            " y1_pct=excluded.y1_pct, raw_sha256=excluded.raw_sha256",
            payload)
        fetcher.conn.commit()
        total_rows += len(payload)
        if len(rows) < page_size:
            break
    return {"ok": True, "status": "ok", "rows": total_rows, "pages": pages, "declared_total": declared}


# ── A2 单基金全档案 + NAV ───────────────────────────────────
def collect_pzd_one(fetcher: Fetcher, code: str, business_date: str, *,
                    force: bool = False) -> dict[str, Any]:
    # force=True 绕过当日缓存：历史回补时同一业务日可能已经缓存过失败/截断响应，
    # 走缓存会把“上次失败”当成“上游没有更早数据”。
    res = fetcher.get("em_pzd", key=code, force=force)
    if not res.business_ok:
        return {"ok": False, "status": res.status, "code": code, "nav_rows": 0}
    try:
        parsed = parsers.parse_pzd(res.text)
    except parsers.ParseError as exc:
        # 页面存在但数据集为空 = 上游业务空，不是解析器缺陷
        status = "provider_null" if ("为空" in str(exc) or "缺少" in str(exc)) else "parse_failed"
        fetcher.mark_business_failure(res, status, str(exc))
        return {"ok": False, "status": status, "code": code, "nav_rows": 0}
    nav_rows = parsers.pzd_nav_rows(parsed["nav"], parsed["acc"])
    if not nav_rows:
        fetcher.mark_business_failure(res, "provider_null", "净值数组无有效日期")
        return {"ok": False, "status": "provider_null", "code": code, "nav_rows": 0}
    conn = fetcher.conn
    observed = _now()
    conn.execute(
        "INSERT INTO src_pzd_profile(code, business_date, observed_at, nav_points, acc_points, syl_1n_pct,"
        " rate_pct, source_rate_pct, minsg, manager_json, scale_json, asset_alloc_json, holder_json,"
        " performance_json, raw_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(code, business_date) DO UPDATE SET nav_points=excluded.nav_points,"
        " acc_points=excluded.acc_points, syl_1n_pct=excluded.syl_1n_pct, manager_json=excluded.manager_json,"
        " scale_json=excluded.scale_json, raw_sha256=excluded.raw_sha256",
        (code, business_date, observed, len(nav_rows), len(parsed["acc"]), parsed["syl_1n_pct"],
         parsed["rate_pct"], parsed["source_rate_pct"], parsed["minsg"], parsed["manager_json"],
         parsed["scale_json"], parsed["asset_alloc_json"], parsed["holder_json"],
         parsed["performance_json"], res.raw_sha256))

    existing = {r["nav_date"]: (r["unit_nav"], r["growth_pct"], r["revision_no"])
                for r in conn.execute(
                    "SELECT nav_date, unit_nav, growth_pct, revision_no FROM fund_nav_daily WHERE fcode=?",
                    (code,))}
    revisions = []
    payload = []
    for row in nav_rows:
        prev = existing.get(row["nav_date"])
        revision_no = 0
        if prev is not None:
            revision_no = prev[2]
            old_unit, old_growth = prev[0], prev[1]
            if _differs(old_unit, row["unit_nav"]) or _differs(old_growth, row["growth_pct"]):
                revisions.append((code, row["nav_date"], observed, old_unit, row["unit_nav"],
                                  old_growth, row["growth_pct"], "em_pzd"))
                revision_no += 1
        payload.append((code, row["nav_date"], row["unit_nav"], row["acc_nav"], row["growth_pct"],
                        "em_pzd", res.raw_sha256, revision_no))
    conn.executemany(
        "INSERT INTO fund_nav_daily(fcode, nav_date, unit_nav, acc_nav, growth_pct, nav_source,"
        " raw_sha256, revision_no) VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode, nav_date) DO UPDATE SET unit_nav=excluded.unit_nav, acc_nav=excluded.acc_nav,"
        " growth_pct=excluded.growth_pct, nav_source=excluded.nav_source, raw_sha256=excluded.raw_sha256,"
        " revision_no=excluded.revision_no", payload)
    if revisions:
        conn.executemany(
            "INSERT OR IGNORE INTO fund_nav_revision(fcode, nav_date, detected_at, old_unit, new_unit,"
            " old_growth, new_growth, source) VALUES(?,?,?,?,?,?,?,?)", revisions)
    conn.commit()
    return {"ok": True, "status": "ok", "code": code, "nav_rows": len(payload),
            "revisions": len(revisions), "from_cache": res.from_cache,
            "first": nav_rows[0]["nav_date"], "last": nav_rows[-1]["nav_date"]}


def _differs(a: float | None, b: float | None, tol: float = 1e-9) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return abs(a - b) > tol


# ── A3 静态档案 ─────────────────────────────────────────────
def collect_jbgk_one(fetcher: Fetcher, code: str, business_date: str) -> dict[str, Any]:
    res = fetcher.get("em_jbgk", key=code)
    if not res.business_ok:
        return {"ok": False, "status": res.status, "code": code}
    try:
        parsed = parsers.parse_jbgk(res.text)
    except parsers.ParseError as exc:
        fetcher.mark_business_failure(res, "parse_failed", str(exc))
        return {"ok": False, "status": "parse_failed", "code": code}
    fetcher.conn.execute(
        "INSERT INTO src_jbgk_profile(code, business_date, observed_at, full_name, estab_date, company_name,"
        " custodian, asset_scale, share_scale, mgr_fee_pct, trust_fee_pct, sales_fee_pct, benchmark_text,"
        " tracking_text, max_sub_fee_pct, max_red_fee_pct, raw_sha256)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(code, business_date) DO UPDATE SET full_name=excluded.full_name,"
        " estab_date=excluded.estab_date, company_name=excluded.company_name, custodian=excluded.custodian,"
        " asset_scale=excluded.asset_scale, share_scale=excluded.share_scale,"
        " mgr_fee_pct=excluded.mgr_fee_pct, trust_fee_pct=excluded.trust_fee_pct,"
        " sales_fee_pct=excluded.sales_fee_pct, benchmark_text=excluded.benchmark_text,"
        " tracking_text=excluded.tracking_text, max_sub_fee_pct=excluded.max_sub_fee_pct,"
        " max_red_fee_pct=excluded.max_red_fee_pct, raw_sha256=excluded.raw_sha256",
        (code, business_date, _now(), parsed["full_name"], parsed["estab_date"], parsed["company_name"],
         parsed["custodian"], parsed["asset_scale_text"], parsed["share_scale_text"],
         parsed["mgr_fee_pct"], parsed["trust_fee_pct"], parsed["sales_fee_pct"],
         parsed["benchmark_text"], parsed["tracking_text"], parsed["max_sub_fee_pct"],
         parsed["max_red_fee_pct"], res.raw_sha256))
    fetcher.conn.commit()
    return {"ok": True, "status": "ok", "code": code, "from_cache": res.from_cache,
            "estab_date": parsed["estab_date"], "scale_text": parsed["asset_scale_text"]}


# ── C2 债券持仓 ─────────────────────────────────────────────
def collect_zqcc_one(fetcher: Fetcher, code: str) -> dict[str, Any]:
    res = fetcher.get("em_zqcc", key=code)
    if not res.business_ok:
        return {"ok": False, "status": res.status, "code": code, "rows": 0}
    try:
        parsed = parsers.parse_zqcc(res.text)
    except parsers.ParseError as exc:
        fetcher.mark_business_failure(res, "parse_failed", str(exc))
        return {"ok": False, "status": "parse_failed", "code": code, "rows": 0}
    period = parsed["period"] or "unknown"
    observed = _now()
    payload = [(code, period, observed, r["bond_code"], r["bond_name"], r["pct_nav"],
                r["market_value_wan"], res.raw_sha256) for r in parsed["rows"]]
    if payload:
        fetcher.conn.executemany(
            "INSERT INTO src_zqcc_holding(code, report_period, observed_at, bond_code, bond_name,"
            " pct_nav, market_value_wan, raw_sha256) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(code, report_period, bond_code) DO UPDATE SET pct_nav=excluded.pct_nav,"
            " market_value_wan=excluded.market_value_wan, raw_sha256=excluded.raw_sha256", payload)
        fetcher.conn.commit()
    return {"ok": True, "status": "ok", "code": code, "rows": len(payload), "period": period}


def collect_zqcc_year(fetcher: Fetcher, code: str, year: int, *,
                       force: bool = False) -> dict[str, Any]:
    """采集某一年的四个季度债券持仓。

    东财 type=zqcc&year=YYYY 一次返回该年 Q1–Q4；缓存键带年份，
    避免不同年份共用同一个 source_state。
    """
    res = fetcher.get("em_zqcc_year", key=f"{code}:{year}", force=force,
                      variables={"code": code, "year": year})
    if not res.business_ok:
        return {"ok": False, "status": res.status, "code": code, "periods": 0, "rows": 0}
    try:
        parsed = parsers.parse_zqcc_year(res.text)
    except parsers.ParseError as exc:
        fetcher.mark_business_failure(res, "parse_failed", str(exc))
        return {"ok": False, "status": "parse_failed", "code": code, "periods": 0, "rows": 0}
    observed = _now()
    payload = []
    for block in parsed["blocks"]:
        for r in block["rows"]:
            payload.append((code, block["period"], observed, r["bond_code"], r["bond_name"],
                            r["pct_nav"], r["market_value_wan"], res.raw_sha256))
    if payload:
        fetcher.conn.executemany(
            "INSERT INTO src_zqcc_holding(code, report_period, observed_at, bond_code, bond_name,"
            " pct_nav, market_value_wan, raw_sha256) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(code, report_period, bond_code) DO UPDATE SET pct_nav=excluded.pct_nav,"
            " market_value_wan=excluded.market_value_wan, raw_sha256=excluded.raw_sha256", payload)
        fetcher.conn.commit()
    return {"ok": True, "status": "ok", "code": code, "periods": len(parsed["blocks"]),
            "rows": len(payload), "year": year}


# ── A4 费率阶梯 ─────────────────────────────────────────────
def collect_fee_one(fetcher: Fetcher, code: str) -> dict[str, Any]:
    res = fetcher.get("em_jjfl", key=code)
    if not res.business_ok:
        return {"ok": False, "status": res.status, "code": code, "tiers": 0}
    try:
        parsed = parsers.parse_jjfl(res.text)
    except parsers.ParseError as exc:
        fetcher.mark_business_failure(res, "parse_failed", str(exc))
        return {"ok": False, "status": "parse_failed", "code": code, "tiers": 0}
    observed = _now()
    conn = fetcher.conn
    written = fee_store.write_fee_tiers(conn, code, parsed, observed, res.raw_sha256)
    meta = fee_store.write_fee_meta(conn, code, parsed, observed, res.raw_sha256)
    conn.commit()
    return {"ok": True, "status": "ok", "code": code, "tiers": written["tiers"],
            "schedule": written["schedule"], "purchase": len(parsed["purchase"]),
            "redemption": len(parsed["redemption"]), **meta,
            "from_cache": res.from_cache}


# ── A3 规模变动全历史（F10 gmbd）────────────────────────────
def collect_scale_one(fetcher: Fetcher, code: str, *, force: bool = False) -> dict[str, Any]:
    """把 F10 规模变动表落到 fund_scale_history。

    pzd 的 Data_fluctuationScale 只有最近 5 个季度；gmbd 是自成立日起的完整季度序列，
    用于历史 as-of 回测（避免拿最新规模去筛 2020 年的基金）。
    """
    res = fetcher.get("em_gmbd", key=code, force=force)
    if not res.business_ok:
        return {"ok": False, "status": res.status, "code": code, "periods": 0}
    try:
        parsed = parsers.parse_gmbd(res.text)
    except parsers.ParseError as exc:
        fetcher.mark_business_failure(res, "parse_failed", str(exc))
        return {"ok": False, "status": "parse_failed", "code": code, "periods": 0}
    conn = fetcher.conn
    payload = [(code, r["report_period"], r["asset_scale_billion"],
                r["share_scale_billion"], "em_gmbd", res.raw_sha256) for r in parsed["rows"]]
    conn.executemany(
        "INSERT INTO fund_scale_history(fcode, report_period, asset_scale_billion,"
        " share_scale_billion, src, raw_sha256) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(fcode, report_period) DO UPDATE SET"
        " asset_scale_billion=excluded.asset_scale_billion,"
        " share_scale_billion=excluded.share_scale_billion, src=excluded.src,"
        " raw_sha256=excluded.raw_sha256", payload)
    conn.commit()
    return {"ok": True, "status": "ok", "code": code, "periods": len(payload),
            "first": parsed["rows"][0]["report_period"],
            "last": parsed["rows"][-1]["report_period"], "from_cache": res.from_cache}


# ── A4 基金经理任职全历史（F10 jjjl）────────────────────────
def collect_manager_one(fetcher: Fetcher, code: str, business_date: str, *,
                        force: bool = False) -> dict[str, Any]:
    """把基金经理变动一览落到 src_manager_history + fund_manager_tenure。

    「至今」的 end_date 存 NULL，is_current=1；days 按 business_date 重算，
    不直接信页面的“2年又46天”（页面按自然年/月口径，跨闰年会有几天误差）。
    """
    res = fetcher.get("em_jjjl", key=code, force=force)
    if not res.business_ok:
        return {"ok": False, "status": res.status, "code": code, "tenures": 0}
    try:
        parsed = parsers.parse_jjjl(res.text)
    except parsers.ParseError as exc:
        fetcher.mark_business_failure(res, "parse_failed", str(exc))
        return {"ok": False, "status": "parse_failed", "code": code, "tenures": 0}
    observed = _now()
    bd = date.fromisoformat(business_date)
    conn = fetcher.conn
    src_payload = []
    tenure_payload = []
    seen = set()
    for r in parsed["rows"]:
        key = (r["mgr_id"], r["start_date"])
        if key in seen:
            continue
        seen.add(key)
        try:
            start = date.fromisoformat(r["start_date"])
        except ValueError:
            continue
        end = r["end_date"]
        try:
            days = (date.fromisoformat(end) - start).days if end else (bd - start).days
        except ValueError:
            days = r.get("days")
        src_payload.append((code, r["mgr_id"], r["start_date"], observed, r["mgr_name"],
                            end, days, res.raw_sha256))
        tenure_payload.append((code, r["mgr_id"], r["start_date"], r["mgr_name"], end,
                               days, 0 if end else 1, "em_jjjl"))
    conn.executemany(
        "INSERT INTO src_manager_history(code, mgr_id, start_date, observed_at, mgr_name,"
        " end_date, days, raw_sha256) VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(code, mgr_id, start_date) DO UPDATE SET"
        " observed_at=excluded.observed_at, mgr_name=excluded.mgr_name,"
        " end_date=excluded.end_date, days=excluded.days, raw_sha256=excluded.raw_sha256",
        src_payload)
    conn.executemany(
        "INSERT INTO fund_manager_tenure(fcode, mgr_id, start_date, mgr_name, end_date,"
        " days, is_current, src) VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode, mgr_id, start_date) DO UPDATE SET"
        " mgr_name=excluded.mgr_name, end_date=excluded.end_date, days=excluded.days,"
        " is_current=excluded.is_current, src=excluded.src", tenure_payload)
    conn.commit()
    return {"ok": True, "status": "ok", "code": code, "tenures": len(tenure_payload),
            "current": sum(1 for t in tenure_payload if t[6]), "from_cache": res.from_cache}


# ── 代码选取 ────────────────────────────────────────────────
def select_codes(conn, types: str = "bond", limit: int | None = None) -> list[str]:
    latest = conn.execute("SELECT MAX(business_date) AS d FROM src_directory").fetchone()["d"]
    if not latest:
        return []
    if types == "all":
        rows = conn.execute("SELECT code FROM src_directory WHERE business_date=? ORDER BY code",
                            (latest,)).fetchall()
    else:
        prefixes = type_prefixes(types)
        if not prefixes:
            return []
        clause = " OR ".join(["type_text LIKE ?"] * len(prefixes))
        rows = conn.execute(
            f"SELECT code FROM src_directory WHERE business_date=? AND ({clause}) ORDER BY code",
            (latest, *[p + "%" for p in prefixes])).fetchall()
    codes = [r["code"] for r in rows]
    return codes[:limit] if limit else codes
# ── B1 全市场当日净值快照（日更主源）────────────────────────
def _snapshot_universe(conn, types: str = "bond") -> set[str]:
    """当日名录里属于本类型的代码集合。"""
    return {r["code"] for r in conn.execute(
        "SELECT code FROM src_directory WHERE business_date=("
        "SELECT MAX(business_date) FROM src_directory) AND (" +
        " OR ".join(["type_text LIKE ?"] * len(type_prefixes(types))) +
        ")", tuple(p + "%" for p in type_prefixes(types)))}


def collect_nav_snapshot(fetcher: Fetcher, business_date: str,
                         types: str = "bond", include_previous: bool = True) -> dict[str, Any]:
    """一次请求拿全市场当日净值：只保留本类型基金，写 fund_nav_daily + 当日可购状态。

    与逐只 pzd 的分工：pzd 负责**全历史**（首次/回补），B1 负责**当日增量**。
    两者共用 (fcode, nav_date) 主键，后写覆盖前写，且各自带 raw_sha256 可回溯。
    """
    res = fetcher.get("em_nav_snapshot", key="all")
    if not res.business_ok:
        return {"ok": False, "status": res.status, "error": res.error_msg}
    try:
        parsed = parsers.parse_nav_snapshot(res.text)
    except parsers.ParseError as exc:
        fetcher.mark_business_failure(res, "parse_failed", str(exc))
        return {"ok": False, "status": "parse_failed", "error": str(exc)}
    showday = parsed["showday"]
    nav_date = showday[0] if showday else None
    if not nav_date:
        fetcher.mark_business_failure(res, "provider_null", "showday 为空，无法确定业务日")
        return {"ok": False, "status": "provider_null", "error": "showday_empty"}

    conn = fetcher.conn
    universe = _snapshot_universe(conn, types)
    rows = [r for r in parsed["rows"] if r["code"] in universe
            and (r["unit_nav"] is not None or r.get("prev_unit_nav") is not None)]
    existing = {r["fcode"]: (r["unit_nav"], r["growth_pct"], r["revision_no"])
                for r in conn.execute(
                    "SELECT fcode, unit_nav, growth_pct, revision_no FROM fund_nav_daily"
                    " WHERE nav_date=?", (nav_date,))}
    payload, revisions = [], []
    for r in rows:
        if r["unit_nav"] is None:
            continue
        prev = existing.get(r["code"])
        revision_no = 0
        if prev is not None:
            revision_no = prev[2] or 0
            # 只有"双方都有值且差异超过精度"才算修订：pzd 是 4 位小数、B1 是 6 位，
            # 同一净值会差 1e-5 量级；把精度升级记成"净值修订"会淹没真正的修订。
            unit_changed = (prev[0] is not None and r["unit_nav"] is not None
                            and abs(prev[0] - r["unit_nav"]) > 1e-4)
            growth_changed = (prev[1] is not None and r["growth_pct"] is not None
                              and abs(prev[1] - r["growth_pct"]) > 0.02)
            if unit_changed or growth_changed:
                revisions.append((r["code"], nav_date, _now(), prev[0], r["unit_nav"],
                                  prev[1], r["growth_pct"], "em_b1"))
                revision_no += 1
        payload.append((r["code"], nav_date, r["unit_nav"], r["acc_nav"], r["growth_pct"],
                        "em_b1", res.raw_sha256, revision_no))
    conn.executemany(
        "INSERT INTO fund_nav_daily(fcode, nav_date, unit_nav, acc_nav, growth_pct, nav_source,"
        " raw_sha256, revision_no) VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode, nav_date) DO UPDATE SET unit_nav=excluded.unit_nav,"
        # B1 对定开/封闭基金不给日涨幅（None），不能把 pzd 已经算好的涨幅抹成 NULL；
        # 同理累计净值缺失时保留旧值。缺失 ≠ 0，也 ≠ 覆盖。
        " acc_nav=COALESCE(excluded.acc_nav, fund_nav_daily.acc_nav),"
        " growth_pct=COALESCE(excluded.growth_pct, fund_nav_daily.growth_pct),"
        " nav_source=excluded.nav_source, raw_sha256=excluded.raw_sha256,"
        " revision_no=excluded.revision_no", payload)
    if revisions:
        conn.executemany(
            "INSERT OR IGNORE INTO fund_nav_revision(fcode, nav_date, detected_at, old_unit,"
            " new_unit, old_growth, new_growth, source) VALUES(?,?,?,?,?,?,?,?)", revisions)

    # B1 的 showday[1] 是前一交易日净值。只写 showday[0] 会让「最新日」停在
    # 上一交易日：2026-11-03 只披露 185 只 11-02 净值时，全市场 11-02 永远缺失。
    # 前一日行只用于回补；已有 pzd 值时保留 pzd（更精确），不覆盖。
    prev_date = showday[1] if len(showday) > 1 else None
    prev_payload = []
    if include_previous and prev_date:
        for r in rows:
            if r.get("prev_unit_nav") is None:
                continue
            prev_payload.append((r["code"], prev_date, r["prev_unit_nav"],
                                 r.get("prev_acc_nav"), "em_b1_prev", res.raw_sha256))
        conn.executemany(
            "INSERT INTO fund_nav_daily(fcode, nav_date, unit_nav, acc_nav, growth_pct,"
            " nav_source, raw_sha256, revision_no) VALUES(?,?,?,?,NULL,?,?,0)"
            " ON CONFLICT(fcode, nav_date) DO UPDATE SET"
            " unit_nav=COALESCE(fund_nav_daily.unit_nav, excluded.unit_nav),"
            " acc_nav=COALESCE(fund_nav_daily.acc_nav, excluded.acc_nav),"
            " nav_source=CASE WHEN fund_nav_daily.unit_nav IS NOT NULL"
            "   THEN fund_nav_daily.nav_source ELSE excluded.nav_source END",
            prev_payload)

    from .fuse.profile import map_buyable
    buy_rows = [(r["code"], business_date, r["purchase_status"], r["redemption_status"],
                 map_buyable(r["purchase_status"]), r["next_open_date"], r["min_purchase"],
                 r["fee_now_pct"], res.raw_sha256)
                for r in parsed["rows"] if r["code"] in universe]
    conn.executemany(
        "INSERT INTO fund_buyable_daily(fcode, business_date, purchase_status, redemption_status,"
        " buyable_state, next_open_date, min_purchase, fee_pct, raw_sha256)"
        " VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(fcode, business_date) DO UPDATE SET"
        " purchase_status=excluded.purchase_status, redemption_status=excluded.redemption_status,"
        " buyable_state=excluded.buyable_state, next_open_date=excluded.next_open_date,"
        " min_purchase=excluded.min_purchase, fee_pct=excluded.fee_pct,"
        " raw_sha256=excluded.raw_sha256", buy_rows)
    conn.commit()
    return {"ok": True, "status": "ok", "nav_date": nav_date, "showday": showday,
            "previous_date": prev_date, "previous_written": len(prev_payload),
            "snapshot_rows": len(parsed["rows"]), "universe": len(universe),
            "written": len(payload), "revisions": len(revisions),
            "buyable_written": len(buy_rows), "raw_sha256": res.raw_sha256,
            "declared_total": parsed["declared_total"]}


def missing_nav_dates(conn, start_date: str, end_date: str, *, types: str = "bond",
                      min_ratio: float = 0.5) -> list[dict[str, Any]]:
    """返回 (start_date, end_date) 之间覆盖不足的交易日。

    B1 快照只带 showday[0]/[1]：跨周末或短停机可以由 showday[1] 补上前一
    交易日，但连停三天以上时中间那天没有任何来源。这里只做**诊断**，不抓
    数据；backfill_nav_gaps 再决定是否为候选池逐只回补。
    """
    try:
        start = date.fromisoformat(str(start_date)[:10])
        end = date.fromisoformat(str(end_date)[:10])
    except (TypeError, ValueError):
        return []
    if end <= start:
        return []
    universe = _snapshot_universe(conn, types)
    if not universe:
        return []
    clause, params = fundtypes.where(types, column="f.type_group")
    out: list[dict[str, Any]] = []
    day = start + timedelta(days=1)
    while day < end:
        if cn_calendar.is_trading_day(day):
            covered = conn.execute(
                "SELECT COUNT(DISTINCT n.fcode) FROM fund_nav_daily n"
                " JOIN fund f ON f.fcode=n.fcode"
                " WHERE n.nav_date=? AND n.unit_nav IS NOT NULL AND " + clause,
                (day.isoformat(), *params)).fetchone()[0]
            ratio = covered / len(universe)
            if ratio < min_ratio:
                out.append({"date": day.isoformat(), "covered": covered,
                            "universe": len(universe), "ratio": round(ratio, 4)})
        day += timedelta(days=1)
    return out


def _backfill_candidates(conn, limit: int) -> list[str]:
    """停机回补的候选池：最近一次成功筛选的 base_pass，按规模降序。

    全市场 8k 只逐只拉 pzd 代价过高；只回补最可能进入正式榜的候选，缺口
    会由 data_quality/门禁继续暴露，而不是静默当作完整历史。
    """
    if limit <= 0:
        return []
    try:
        run = conn.execute(
            "SELECT run_id FROM screen_run WHERE status='success'"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
        if run:
            rows = conn.execute(
                "SELECT fcode FROM screen_run_input WHERE run_id=? AND passed=1"
                " ORDER BY COALESCE(asset_scale_billion, 0) DESC, fcode LIMIT ?",
                (run["run_id"], limit)).fetchall()
            if rows:
                return [r["fcode"] for r in rows]
        rows = conn.execute(
            "SELECT DISTINCT fcode FROM screen_pick ORDER BY fcode LIMIT ?",
            (limit,)).fetchall()
        return [r["fcode"] for r in rows]
    except Exception:                                       # noqa: BLE001
        return []


def backfill_nav_gaps(fetcher: Fetcher, business_date: str, start_date: str,
                      end_date: str, *, types: str = "bond", max_funds: int = 200,
                      min_ratio: float = 0.5) -> dict[str, Any]:
    """跨多日停机后，用 pzd 对候选池回补中间缺失的交易日。

    只补中间日（start_date, end_date），不重复抓 end_date：B1 已写当日和
    showday[1]。pzd 返回全历史，写入仍是幂等 upsert；已有更精确的 pzd 值
    不会被 B1 的 6 位小数覆盖。
    """
    gaps = missing_nav_dates(fetcher.conn, start_date, end_date, types=types,
                             min_ratio=min_ratio)
    if not gaps:
        return {"ok": True, "status": "not_needed", "missing": [], "attempted": 0,
                "succeeded": 0, "failed": 0, "nav_rows": 0, "candidates": 0}
    codes = _backfill_candidates(fetcher.conn, max_funds)
    if not codes:
        return {"ok": False, "status": "no_candidates", "missing": gaps, "attempted": 0,
                "succeeded": 0, "failed": 0, "nav_rows": 0, "candidates": 0}
    succeeded = failed = nav_rows = 0
    failures: list[str] = []
    for code in codes:
        try:
            res = collect_pzd_one(fetcher, code, business_date, force=True)
        except Exception as exc:                            # noqa: BLE001
            failed += 1
            if len(failures) < 5:
                failures.append(f"{code}:{str(exc)[:60]}")
            continue
        if res.get("ok"):
            succeeded += 1
            nav_rows += int(res.get("nav_rows") or 0)
        else:
            failed += 1
            if len(failures) < 5:
                failures.append(f"{code}:{res.get('status')}")
    return {"ok": failed == 0, "status": "ok" if failed == 0 else "partial",
            "missing": gaps, "attempted": len(codes), "succeeded": succeeded,
            "failed": failed, "nav_rows": nav_rows, "candidates": len(codes),
            "failures": failures}


