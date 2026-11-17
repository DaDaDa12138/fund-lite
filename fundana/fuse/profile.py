"""画像层：规模/费率/业绩基准/可购状态/持有期，按 field_policy 的优先级裁决并记录冲突。"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from .. import db
from ..fetch.parsers import to_float

CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
          "九": 9, "十": 10, "十八": 18, "二十四": 24, "三十六": 36}


def _cn_int(token: str) -> int | None:
    """把“六”“十二”“二十四”这类中文数字转成整数（0–99 够用）。"""
    if token in CN_NUM:
        return CN_NUM[token]
    if "十" in token:
        tens_txt, _, ones_txt = token.partition("十")
        tens = CN_NUM.get(tens_txt, 0) if tens_txt else 1
        ones = CN_NUM.get(ones_txt, 0) if ones_txt else 0
        return tens * 10 + ones
    return None


def _parse_scale_text(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*亿元", text)
    if m:
        return to_float(m.group(1))
    m = re.search(r"([\d.]+)\s*万元", text)
    if m:
        val = to_float(m.group(1))
        return val / 10000 if val is not None else None
    return None


def infer_holding_days(name: str | None, full_name: str | None = None) -> tuple[int | None, str | None]:
    """从名称推断最短持有期（天）。返回 (days, lockup_type)。"""
    text = (name or "") + " " + (full_name or "")
    if not text.strip():
        return None, None
    # 名称前缀式：180天持有 / 6个月持有期 / 一年持有（东财绝大多数持有期基金是这种写法）
    # 中文数字月份必须先匹配：2026-11 实测 75 只“六个月/九个月/三个月持有”被漏判成
    # lockup_type=none、effective_days=NULL，其中 41 只六个月/九个月持有因此混进正式榜。
    m = re.search(r"([一二两三四五六七八九十]+)\s*个月\s*(?:滚动)?\s*持有", text)
    if m:
        num = _cn_int(m.group(1))
        if num:
            return num * 30, "holding"
    m = re.search(r"(\d+)\s*(天|日|个月|月|年)\s*(?:滚动)?\s*持有", text)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        if unit in ("天", "日"):
            return num, "holding"
        if unit in ("个月", "月"):
            return num * 30, "holding"
        return num * 365, "holding"
    m = re.search(r"(一年|两年|三年|五年|半年|十八个月)\s*(?:滚动)?\s*持有", text)
    if m:
        token = m.group(1)
        if token == "半年":
            return 180, "holding"
        if token == "十八个月":
            return 540, "holding"
        num = CN_NUM.get(token[0])
        if num:
            return num * 365, "holding"
    # 持有期 N 天/月/年（"持有期6个月" 这种后缀写法）
    m = re.search(r"持有期?\s*(\d+)\s*(天|日|个月|月|年)", text)
    if m:
        num, unit = int(m.group(1)), m.group(2)
        if unit in ("天", "日"):
            return num, "holding"
        if unit in ("个月", "月"):
            return num * 30, "holding"
        return num * 365, "holding"
    # X 个月定开 / 定期开放
    m = re.search(r"(\d+)\s*个月(?:定开|定期开放)", text)
    if m:
        return int(m.group(1)) * 30, "periodic_open"
    m = re.search(r"(一年|两年|三年|五年|半年|十八个月)(?:定开|定期开放)", text)
    if m:
        token = m.group(1)
        if token == "半年":
            return 180, "periodic_open"
        if token == "十八个月":
            return 540, "periodic_open"
        num = CN_NUM.get(token[0])
        if num:
            return num * 365, "periodic_open"
    if "定开" in text or "定期开放" in text:
        return None, "periodic_open"
    if "封闭" in text:
        return None, "closed"
    return None, None


def run_profiles(conn, business_date: str, max_short_lock_days: int = 90,
                 scale_family_rel: float = 0.15) -> dict[str, Any]:
    # 逐基金取“各自最近一次”画像，而不是全表 MAX(business_date)：
    # 增量补采（--skip-collected）会写入一个只有新基金的更新业务日，按全表 MAX 取会让
    # 其余基金瞬间丢掉规模/费率（2026-11-01 实测 4,662 只 scale_missing，formal 330→4）。
    jbgk = {r["code"]: r for r in conn.execute(
        "SELECT p.* FROM src_jbgk_profile p JOIN (SELECT code, MAX(business_date) AS d"
        " FROM src_jbgk_profile GROUP BY code) t ON t.code=p.code AND t.d=p.business_date")}
    rank = {r["code"]: r for r in conn.execute(
        "SELECT p.* FROM src_rank_row p JOIN (SELECT code, MAX(business_date) AS d"
        " FROM src_rank_row GROUP BY code) t ON t.code=p.code AND t.d=p.business_date")}
    status = {r["code"]: r for r in conn.execute(
        "SELECT * FROM src_snapshot_status WHERE business_date=?", (business_date,))}
    pzd_scale = {}
    for r in conn.execute("SELECT code, scale_json FROM src_pzd_profile WHERE business_date=("
                          "SELECT MAX(business_date) FROM src_pzd_profile)"):
        pzd_scale[r["code"]] = _last_scale(r["scale_json"])

    # 家族成员表：用于把"份额级净资产"汇总成"基金级净资产"
    fam_of: dict[str, str] = {}
    members: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT fcode, family_id FROM fund_share_class WHERE family_id IS NOT NULL"):
        fam_of[r["fcode"]] = r["family_id"]
        members[r["family_id"]].append(r["fcode"])

    funds = conn.execute("SELECT fcode, name, type_group FROM fund WHERE last_seen_date=?",
                         (business_date,)).fetchall()
    now = db.now_iso()
    prof_rows, policy_rows, buyable_rows = [], [], []
    conflicts = 0
    for f in funds:
        code = f["fcode"]
        jb, rk, st = jbgk.get(code), rank.get(code), status.get(code)
        field_src: dict[str, Any] = {}
        conflict: dict[str, Any] = {}

        # 规模两级语义（2026-10-31 实测校正）：
        #   em_jbgk.asset_scale = 份额级净资产（单一份额，如 000003 A 类 0.37 亿、C 类 0.43 亿）
        #   基金级 = 同家族各份额 jbgk 净资产之和（000003 家族 0.80 亿）
        # em_rank 不含规模（第 18 段实测是自定义区间涨幅，与 y3 相关 0.9989），已剔除出规模来源。
        # 筛选门槛必须用基金级；只有基金级完全缺失时才回退份额级，并在 field_src 标明层级。
        share_class_scale = _parse_scale_text(jb["asset_scale"]) if jb is not None else None
        family_sum = None
        fam = fam_of.get(code)
        if fam:
            vals = [v for v in (_parse_scale_text(jbgk[m]["asset_scale"]) if m in jbgk else None
                                for m in members[fam]) if v is not None]
            if vals:
                family_sum = round(sum(vals), 4)
        elif share_class_scale is not None:
            family_sum = share_class_scale  # 单成员家族：基金级=份额级
        # 顺序与 field_policy.fields.asset_scale_billion.priority 一致：家族汇总为唯一基金级来源
        family_level = []
        if family_sum is not None:
            family_level.append(("em_jbgk_family_sum", family_sum))
        scale, scale_src = _pick_with_conflict(family_level, scale_family_rel, "asset_scale_billion",
                                               field_src, conflict)
        if conflict:
            conflicts += 1
        if scale is not None:
            field_src["asset_scale_level"] = "family"
        else:
            class_scale = pzd_scale.get(code)
            if class_scale is not None:
                scale, scale_src = class_scale, "em_pzd"
                field_src["asset_scale_billion"] = "em_pzd"
                field_src["asset_scale_level"] = "share_class"
        if share_class_scale is not None:
            field_src["share_class_scale_billion"] = share_class_scale

        mgr = jb["mgr_fee_pct"] if jb else None
        trust = jb["trust_fee_pct"] if jb else None
        sales = jb["sales_fee_pct"] if jb else None
        recurring = None
        if mgr is not None or trust is not None or sales is not None:
            recurring = round((mgr or 0.0) + (trust or 0.0) + (sales or 0.0), 4)
            field_src["recurring_fee_pct"] = "em_jbgk"
        prof_rows.append((
            code, jb["full_name"] if jb else None, scale, scale_src,
            jb["observed_at"] if jb else None,
            _parse_scale_text(jb["share_scale"]) if jb else None,
            "em_jbgk" if jb else None,
            mgr, trust, sales, recurring,
            jb["max_sub_fee_pct"] if jb else None, jb["max_red_fee_pct"] if jb else None,
            jb["benchmark_text"] if jb else None, jb["tracking_text"] if jb else None,
            json.dumps(field_src, ensure_ascii=False) if field_src else None,
            json.dumps(conflict, ensure_ascii=False) if conflict else None, now))

        # 持有期
        structured_days = None
        name_days, lockup = infer_holding_days(f["name"], jb["full_name"] if jb else None)
        effective = max([d for d in (structured_days, name_days) if d is not None], default=None)
        evidence = {"name_days": name_days, "structured_days": structured_days, "lockup_type": lockup,
                    "max_short_lock_days": max_short_lock_days}
        policy_rows.append((code, structured_days, name_days, effective, lockup or "none",
                            json.dumps(evidence, ensure_ascii=False), now))

        # 可购状态
        purchase = st["purchase_status"] if st else None
        redemption = st["redemption_status"] if st else None
        buyable = map_buyable(purchase)
        buyable_rows.append((code, business_date, purchase, redemption, buyable,
                             st["next_open_date"] if st else None,
                             st["min_purchase"] if st else None, st["fee_pct"] if st else None,
                             st["raw_sha256"] if st else None))

    conn.executemany(
        "INSERT INTO fund_profile(fcode, full_name, asset_scale_billion, asset_scale_src, asset_scale_obs,"
        " share_scale_billion, share_scale_src, mgr_fee_pct, trust_fee_pct, sales_fee_pct, recurring_fee_pct,"
        " max_sub_fee_pct, max_red_fee_pct, benchmark_text, tracking_text, field_src_json, conflict_json,"
        " updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode) DO UPDATE SET full_name=excluded.full_name,"
        " asset_scale_billion=excluded.asset_scale_billion, asset_scale_src=excluded.asset_scale_src,"
        " asset_scale_obs=excluded.asset_scale_obs, share_scale_billion=excluded.share_scale_billion,"
        " mgr_fee_pct=excluded.mgr_fee_pct, trust_fee_pct=excluded.trust_fee_pct,"
        " sales_fee_pct=excluded.sales_fee_pct, recurring_fee_pct=excluded.recurring_fee_pct,"
        " max_sub_fee_pct=excluded.max_sub_fee_pct, max_red_fee_pct=excluded.max_red_fee_pct,"
        " benchmark_text=excluded.benchmark_text, tracking_text=excluded.tracking_text,"
        " field_src_json=excluded.field_src_json, conflict_json=excluded.conflict_json,"
        " updated_at=excluded.updated_at", prof_rows)
    conn.executemany(
        "INSERT INTO fund_holding_policy(fcode, structured_days, name_inferred_days, effective_days,"
        " lockup_type, evidence_json, updated_at) VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode) DO UPDATE SET structured_days=excluded.structured_days,"
        " name_inferred_days=excluded.name_inferred_days, effective_days=excluded.effective_days,"
        " lockup_type=excluded.lockup_type, evidence_json=excluded.evidence_json,"
        " updated_at=excluded.updated_at", policy_rows)
    conn.executemany(
        "INSERT INTO fund_buyable_daily(fcode, business_date, purchase_status, redemption_status,"
        " buyable_state, next_open_date, min_purchase, fee_pct, raw_sha256) VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(fcode, business_date) DO UPDATE SET purchase_status=excluded.purchase_status,"
        " redemption_status=excluded.redemption_status, buyable_state=excluded.buyable_state,"
        " next_open_date=excluded.next_open_date, min_purchase=excluded.min_purchase,"
        " fee_pct=excluded.fee_pct, raw_sha256=excluded.raw_sha256", buyable_rows)
    conn.commit()
    locked = sum(1 for p in policy_rows if p[3] is not None and p[3] > max_short_lock_days)
    return {"profiles": len(prof_rows), "conflicts": conflicts, "locked_over_90d": locked,
            "buyable_yes": sum(1 for b in buyable_rows if b[4] == "confirmed_yes"),
            "buyable_no": sum(1 for b in buyable_rows if b[4] == "confirmed_no"),
            "buyable_unknown": sum(1 for b in buyable_rows if b[4] == "unknown")}


def map_buyable(purchase_status: str | None) -> str:
    """申购状态 → 四态。限大额仍可小额申购，判 confirmed_yes；空值一律 unknown。"""
    if not purchase_status:
        return "unknown"
    text = purchase_status.strip()
    if text in {"开放申购", "限大额", "开放"}:
        return "confirmed_yes"
    if text in {"暂停申购", "封闭期", "认购期", "暂停"}:
        return "confirmed_no"
    return "unknown"


def _pick_with_conflict(candidates: list[tuple[str, float]], rel_tol: float, field: str,
                        field_src: dict[str, Any], conflict: dict[str, Any]) -> tuple[float | None, str | None]:
    """按优先级取第一个；若与后续候选超出相对容差，保留双值、canonical 取首选并记冲突。"""
    if not candidates:
        return None, None
    src, val = candidates[0]
    field_src[field] = src
    for other_src, other_val in candidates[1:]:
        base = abs(val) if val else 1e-9
        if abs(other_val - val) / base > rel_tol:
            conflict[field] = {"canonical": {"src": src, "value": val},
                               "alternates": [{"src": other_src, "value": other_val}],
                               "tolerance_rel": rel_tol}
            break
    return val, src


def _last_scale(scale_json: str | None) -> float | None:
    if not scale_json:
        return None
    try:
        data = json.loads(scale_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict):
        series = data.get("series") or []
    else:
        series = data if isinstance(data, list) else []
    if not series:
        return None
    last = series[-1]
    if isinstance(last, dict):
        return to_float(last.get("y"))
    if isinstance(last, list) and len(last) >= 2:
        return to_float(last[1])
    return None
