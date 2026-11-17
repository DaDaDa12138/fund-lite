"""纯解析函数：输入响应文本/HTML，输出结构化事实。不做网络、不写库，便于单测。

统一原则：
- HTTP 200 不等于业务成功，业务判定由适配器层完成；
- 任何字段解析失败保留 None，绝不填 0；
- 每个函数在无法识别时抛 ParseError（由调用方记 parse_failed）。
"""
from __future__ import annotations

import html as html_mod
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any


class ParseError(ValueError):
    pass


# ── 通用工具 ─────────────────────────────────────────────────
def extract_var(text: str, name: str) -> str | None:
    """截取 var name = ...; 的字面量，按括号深度与字符串状态定位结尾分号。"""
    m = re.search(r"(?:var\s+)?" + re.escape(name) + r"\s*=\s*", text)
    if not m:
        return None
    i = m.end()
    depth = 0
    quote: str | None = None
    j = i
    while j < len(text):
        c = text[j]
        if quote:
            if c == "\\":
                j += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "[{(":
            depth += 1
        elif c in "]})":
            depth -= 1
        elif c == ";" and depth <= 0:
            break
        j += 1
    lit = text[i:j].strip()
    return lit or None


def lenient_json(literal: str) -> Any:
    """解析 JS 字面量：先严格 JSON，再给裸键加引号、去尾逗号、undefined→null。"""
    s = literal.strip()
    if not s:
        raise ParseError("空字面量")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    t = re.sub(r"([{,])\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*:", r'\1"\2":', s)
    t = re.sub(r",\s*([}\]])", r"\1", t)
    t = re.sub(r"\bundefined\b", "null", t)
    t = re.sub(r"\bNaN\b", "null", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError as exc:
        raise ParseError(f"字面量无法解析: {exc}") from exc


def strip_tags(fragment: str) -> str:
    txt = re.sub(r"<[^>]+>", "", fragment)
    txt = html_mod.unescape(txt)
    return txt.replace("\xa0", " ").strip()


def to_float(value: Any) -> float | None:
    """严格数值解析：空串/占位符/非数值一律 None（禁止 Number(空串)→0）。"""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if s in {"", "-", "--", "---", "null", "None", "undefined"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ── A1 名录 ──────────────────────────────────────────────────
def parse_directory(text: str) -> list[dict[str, Any]]:
    lit = extract_var(text, "r")
    if lit is None:
        raise ParseError("未找到 var r")
    rows = lenient_json(lit)
    if not isinstance(rows, list) or not rows:
        raise ParseError("名录为空")
    out = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 4:
            continue
        code = str(row[0]).strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        out.append({
            "code": code,
            "name": row[2],
            "pinyin_abbr": row[1],
            "type_text": row[3],
            "pinyin_full": row[4] if len(row) > 4 else None,
        })
    if not out:
        raise ParseError("名录无有效行")
    return out


# ── E3 全市场申赎状态快照 ────────────────────────────────────
STATUS_COLUMNS = ["code", "name", "type_text", "unit_nav", "nav_date_md", "purchase_status",
                  "redemption_status", "next_open_date", "min_purchase", "col9", "col10",
                  "flag_code", "fee_pct"]


def parse_status(text: str) -> dict[str, Any]:
    lit = extract_var(text, "reData")
    if lit is None:
        raise ParseError("未找到 var reData")
    obj = lenient_json(lit)
    if not isinstance(obj, dict) or "datas" not in obj:
        raise ParseError("reData 结构异常")
    datas = obj.get("datas") or []
    if not datas:
        raise ParseError("reData.datas 为空（业务失败）")
    rows = []
    for row in datas:
        if not isinstance(row, list) or len(row) < 7:
            continue
        rec = {col: (row[i] if i < len(row) else None) for i, col in enumerate(STATUS_COLUMNS)}
        code = str(rec["code"]).strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        rec["code"] = code
        rows.append(rec)
    if not rows:
        raise ParseError("reData 无有效行")
    return {
        "rows": rows,
        "declared_total": int(obj["record"]) if str(obj.get("record", "")).isdigit() else None,
        "pages": obj.get("pages"),
        "showday": obj.get("showday") or [],
    }


# ── D1 排行（横截面）────────────────────────────────────────
RANK_COLUMNS = ["code", "name", "pinyin", "nav_date", "unit_nav", "acc_nav", "day_pct",
                "w1_pct", "m1_pct", "m3_pct", "m6_pct", "y1_pct", "y2_pct", "y3_pct",
                "ytd_pct", "since_pct", "estab_date"]


def parse_rank(text: str) -> dict[str, Any]:
    lit = extract_var(text, "rankData")
    if lit is None:
        raise ParseError("未找到 var rankData")
    obj = lenient_json(lit)
    datas = obj.get("datas") if isinstance(obj, dict) else None
    if not datas:
        raise ParseError("rankData.datas 为空（业务失败）")
    rows = []
    for ord_, item in enumerate(datas):
        parts = str(item).split(",")
        if len(parts) < 17:
            continue
        rec = {col: parts[i] for i, col in enumerate(RANK_COLUMNS)}
        code = rec["code"].strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        rec["code"] = code
        # 第 18 段实测是「自定义区间涨幅」而非规模：与 y3_pct 的相关系数 0.9989
        # （000003 该段 16.1290 而 y3=15.27；110027 10.7606 而 y3=10.52），
        # 且与 jbgk 净资产规模无相关性（16.13 vs 0.80 亿）。故 scale_yi 一律留空、不再作为规模来源。
        rec["custom_pct"] = parts[18] if len(parts) > 18 else None
        rec["scale_yi"] = None
        rec["fee_orig_pct"] = parts[19] if len(parts) > 19 else None
        rec["fee_now_pct"] = parts[20] if len(parts) > 20 else None
        rec["row_ord"] = ord_
        rows.append(rec)
    if not rows:
        raise ParseError("rankData 无有效行")
    return {"rows": rows, "declared_total": obj.get("allRecords"), "page_index": obj.get("pageIndex")}


# ── A2 单基金全档案 ─────────────────────────────────────────
def parse_pzd(text: str) -> dict[str, Any]:
    nav_lit = extract_var(text, "Data_netWorthTrend")
    if nav_lit is None:
        raise ParseError("缺少 Data_netWorthTrend")
    nav = lenient_json(nav_lit)
    if not isinstance(nav, list) or not nav:
        raise ParseError("Data_netWorthTrend 为空（业务失败）")
    acc_lit = extract_var(text, "Data_ACWorthTrend")
    acc = lenient_json(acc_lit) if acc_lit else []
    out: dict[str, Any] = {
        "name": (extract_var(text, "fS_name") or "").strip("\"'") or None,
        "nav": nav,
        "acc": acc if isinstance(acc, list) else [],
        "syl_1n_pct": to_float((extract_var(text, "syl_1n") or "").strip("\"'")),
        "rate_pct": to_float((extract_var(text, "fund_Rate") or "").strip("\"'")),
        "source_rate_pct": to_float((extract_var(text, "fund_sourceRate") or "").strip("\"'")),
        "minsg": (extract_var(text, "fund_minsg") or "").strip("\"'") or None,
    }
    for key, name in (("manager_json", "Data_currentFundManager"),
                      ("scale_json", "Data_fluctuationScale"),
                      ("asset_alloc_json", "Data_assetAllocation"),
                      ("holder_json", "Data_holderStructure"),
                      ("performance_json", "Data_performanceEvaluation")):
        out[key] = extract_var(text, name)
    return out


def pzd_nav_rows(nav: list, acc: list) -> list[dict[str, Any]]:
    """把 pingzhongdata 的净值数组归一为按业务日排序的行（单位净值/累计净值/日增长率）。"""
    acc_map: dict[str, float] = {}
    for pair in acc or []:
        if isinstance(pair, list) and len(pair) >= 2 and pair[1] is not None:
            day = _ms_to_date(pair[0])
            if day:
                acc_map[day] = to_float(pair[1])
    rows = []
    for point in nav:
        if not isinstance(point, dict):
            continue
        day = _ms_to_date(point.get("x"))
        if not day:
            continue
        rows.append({
            "nav_date": day,
            "unit_nav": to_float(point.get("y")),
            "acc_nav": acc_map.get(day),
            "growth_pct": to_float(point.get("equityReturn")),
        })
    rows.sort(key=lambda r: r["nav_date"])
    return rows


# 东财 pingzhongdata 的 x 是"业务日零点（北京时间）"的毫秒时间戳，
# 例如 1793289600000 = 2026-10-30T00:00+08:00。
# 用 UTC 解释会把整条净值序列整体左移一天（曾导致 901 万行 nav_date 全错位），
# 因此这里固定用 +08:00 解释，不依赖本机时区或 tzdata。
CN_TZ = timezone(timedelta(hours=8))


def _ms_to_date(value: Any) -> str | None:
    num = to_float(value)
    if num is None:
        return None
    try:
        return datetime.fromtimestamp(num / 1000, tz=CN_TZ).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


# ── B1 全市场当日净值快照（一次请求拿全市场） ─────────────
# 响应形如 var reData={chars:"...",datas:[[...]],count:24316,record:"24316",
#   pages:"1",curpage:"1",indexsy:[-1.44,...],showday:["2026-10-30","2026-10-29"]};
# 三个坑：①裸键（count/record/showday 没有引号）②indexsy 末尾有尾逗号
# ③chars 是名称压缩字母表，但 datas 里的名称已经是明文，不需要解码。
# 列序（实测 2026-10-31，21 列）：0 代码 1 名称 2 拼音 3 单位净值 4 累计净值
# 5 前一日单位净值 6 前一日累计净值 7 日增长值 8 日增长率(%) 9 申购状态
# 10 赎回状态 11 下一开放日 12 起购金额 13/14/15/16 状态码 17 原费率 18 现费率
# 19 未知 20 最高费率。
NAV_SNAPSHOT_COLUMNS = (
    "code", "name", "pinyin", "unit_nav", "acc_nav", "prev_unit_nav", "prev_acc_nav",
    "change_value", "growth_pct", "purchase_status", "redemption_status", "next_open_date",
    "min_purchase", "flag_a", "flag_b", "flag_c", "flag_d", "fee_orig_pct", "fee_now_pct",
    "flag_e", "fee_max_pct",
)


def parse_nav_snapshot(text: str) -> dict[str, Any]:
    """解析全市场当日净值快照。返回 rows / declared_total / showday。"""
    raw = extract_var(text, "reData")
    if not raw:
        raise ParseError("缺少 reData 变量（可能为错误页）")
    obj = lenient_json(raw)
    datas = obj.get("datas") if isinstance(obj, dict) else None
    if not isinstance(datas, list) or not datas:
        raise ParseError("快照 datas 为空")
    rows = []
    for parts in datas:
        if not isinstance(parts, list) or len(parts) < 9:
            continue
        code = str(parts[0]).strip()
        if len(code) != 6 or not code.isdigit():
            continue
        rec = {"code": code}
        for idx, name in enumerate(NAV_SNAPSHOT_COLUMNS):
            if idx == 0 or idx >= len(parts):
                continue
            val = parts[idx]
            if name in ("name", "pinyin", "purchase_status", "redemption_status",
                        "next_open_date"):
                rec[name] = (str(val).strip() or None) if val is not None else None
            else:
                rec[name] = to_float(val)
        rows.append(rec)
    if not rows:
        raise ParseError("快照无可识别行")
    showday = obj.get("showday") or []
    return {
        "rows": rows,
        "declared_total": int(to_float(obj.get("record")) or len(rows)),
        "showday": [str(d) for d in showday if d],
        "chars_len": len(str(obj.get("chars") or "")),
    }


# ── A3 基本概况（静态档案）──────────────────────────────────
_CELL = re.compile(r"<(th|td)([^>]*)>(.*?)(?=<t[hd][\s>]|</tr>|<tr[\s>]|</table>)", re.S | re.I)


def parse_jbgk(text: str) -> dict[str, Any]:
    if "基金全称" not in text:
        raise ParseError("页面无档案表（可能为错误页）")
    cells: list[tuple[str, str]] = []
    for kind, _attrs, body in _CELL.findall(text):
        cells.append((kind.lower(), strip_tags(body)))
    pairs: dict[str, str] = {}
    pending: str | None = None
    for kind, value in cells:
        if kind == "th":
            pending = value
        elif pending is not None:
            pairs.setdefault(pending, value)
            pending = None
    if "基金全称" not in pairs:
        raise ParseError("档案表解析失败")

    def pick(*labels: str) -> str | None:
        for label in labels:
            val = pairs.get(label)
            if val:
                return val
        return None

    estab_raw = pick("成立日期/规模", "成立日期")
    estab = None
    if estab_raw:
        m = re.search(r"(\d{4})年(\d{2})月(\d{2})日", estab_raw)
        if m:
            estab = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    scale_raw = pick("净资产规模")
    scale = None
    if scale_raw:
        m = re.search(r"([\d.]+)\s*亿元", scale_raw)
        if m:
            scale = to_float(m.group(1))

    def rate(label: str) -> float | None:
        raw = pairs.get(label)
        if not raw:
            return None
        m = re.search(r"([\d.]+)\s*%", raw)
        return to_float(m.group(1)) if m else None

    return {
        "full_name": pick("基金全称"),
        "estab_date": estab,
        "company_name": pick("基金管理人"),
        "custodian": pick("基金托管人"),
        "asset_scale": scale,
        "asset_scale_text": scale_raw,
        "share_scale_text": pick("份额规模"),
        "mgr_fee_pct": rate("管理费率"),
        "trust_fee_pct": rate("托管费率"),
        "sales_fee_pct": rate("销售服务费率"),
        "max_sub_fee_pct": rate("最高申购费率"),
        "max_red_fee_pct": rate("最高赎回费率"),
        "benchmark_text": pick("业绩比较基准"),
        "tracking_text": pick("跟踪标的"),
        "type_text": pick("基金类型"),
        "raw_pairs": pairs,
    }


# ── C2 债券持仓 ─────────────────────────────────────────────
def _zqcc_period(block: str) -> str | None:
    pm = re.search(r"(\d{4})年(\d)季度债券投资明细", block)
    if pm:
        return f"{pm.group(1)}Q{pm.group(2)}"
    dm = re.search(r"截止至：\s*<font[^>]*>(\d{4}-\d{2}-\d{2})</font>", block)
    return dm.group(1) if dm else None


def _zqcc_rows(block: str) -> list[dict[str, Any]]:
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S):
        cells = [strip_tags(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 4 or not re.fullmatch(r"\d+", cells[0] or ""):
            continue
        bond_code = cells[1].strip()
        if not bond_code:
            continue
        rows.append({
            "bond_code": bond_code,
            "bond_name": cells[2] if len(cells) > 2 else None,
            "pct_nav": to_float(cells[3]) if len(cells) > 3 else None,
            "market_value_wan": to_float(cells[4]) if len(cells) > 4 else None,
        })
    return rows


def parse_zqcc(text: str) -> dict[str, Any]:
    m = re.search(r'content:"(.*?)",\s*arryear:', text, re.S)
    if not m:
        raise ParseError("未找到 apidata.content")
    content = m.group(1).replace("\\'", "'").replace('\\"', '"')
    arryear_lit = re.search(r"arryear:\s*(\[[^\]]*\])", text)
    years = lenient_json(arryear_lit.group(1)) if arryear_lit else []
    return {"rows": _zqcc_rows(content), "period": _zqcc_period(content),
            "years": years, "content_len": len(content)}


def parse_zqcc_year(text: str) -> dict[str, Any]:
    """解析 type=zqcc&year=YYYY：一次返回该年四个季度的持仓块。

    东财的年度页把每个季度放在独立的 <div class='boxitem'> 里；旧
    parse_zqcc 只取第一个 period，会把四个季度的行混成一个季度。
    """
    m = re.search(r'content:"(.*?)",\s*arryear:', text, re.S)
    if not m:
        raise ParseError("未找到 apidata.content")
    content = m.group(1).replace("\\'", "'").replace('\\"', '"')
    starts = [mm.start() for mm in re.finditer(r"<div class=['\"]boxitem", content)]
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(content)
        block = content[start:end]
        period = _zqcc_period(block)
        rows = _zqcc_rows(block)
        if period and rows:
            blocks.append({"period": period, "rows": rows})
    return {"blocks": blocks, "content_len": len(content)}

# ── A4 费率页（jjfl）────────────────────────────────────────
def _amount_range(text: str) -> tuple[float | None, float | None]:
    """把"小于100万元/大于等于100万元，小于200万元"转成 (lo, hi) 元。"""
    t = text.replace(" ", "").replace(",", "")
    nums = [float(x) for x in re.findall(r"([0-9]+(?:\.[0-9]+)?)万元", t)]
    if not nums:
        return None, None
    if "大于等于" in t or "不低于" in t:
        lo = nums[0] * 10000.0
        hi = nums[1] * 10000.0 if len(nums) > 1 else None
        return lo, hi
    if "小于" in t or "低于" in t:
        return 0.0, nums[0] * 10000.0
    return None, None


def _days_range(text: str) -> tuple[int | None, int | None]:
    """把赎回档位的自然语言期限转成 [days_lo, days_hi]（两端都含）。

    2026-11 实测的坑：jjfl 页面大量用“开放运作期时持有不小于7天”“大于等于7天，小于30天”
    “T<7日”“7日≤T<1个封闭期”等写法；旧实现只认“大于等于/不低于/小于”，
    会把“不小于7天”解析成全周期 1.5%，并让“小于7天”的 0% 档与 1.5% 档冲突。
    """
    t = text.replace(" ", "").replace("　", "")
    t = t.replace("＜", "<").replace("＞", ">").replace("≤", "<=").replace("≥", ">=")

    def _to_days(m):
        val = float(m.group(1))
        unit = m.group(2)
        days = val * 365 if unit == "年" else val * 30 if unit == "个月" else val
        return "%d天" % int(round(days))

    t = re.sub(r"([0-9]+(?:\.[0-9]+)?)(年|个月)", _to_days, t)
    if not re.search(r"[0-9]", t):
        return None, None

    lo = hi = None
    m = re.search(r"(?:大于等于|不小于|不少于|不低于|>=|≥|大于|超过)\s*([0-9]+)\s*天?", t)
    if m and "封闭期" not in t[m.end():m.end() + 5]:
        n = int(m.group(1))
        lo = n + 1 if ("大于" in m.group(0) and "大于等于" not in m.group(0)
                        and "不小于" not in m.group(0)) else n
    m = re.search(r"(?:小于等于|不大于|不超过|不高于|<=|≤|(?<!不)小于|不足|(?<!不)低于)"
                  r"\s*([0-9]+)\s*天?", t)
    if m:
        n = int(m.group(1))
        hi = n - 1 if ("小于" in m.group(0) and "小于等于" not in m.group(0)
                        and "不大于" not in m.group(0)) else n
    # “7日≤T<1个封闭期”“T<7日”这类符号写法
    if lo is None:
        m = re.search(r"([0-9]+)\s*天?(?:日)?\s*(?:<=|≤)\s*T", t, re.I)
        if m:
            lo = int(m.group(1))
    if hi is None:
        m = re.search(r"T\s*(?:<|<=)\s*([0-9]+)(?:\s*(?:天|日))?", t, re.I)
        if m and "封闭期" not in t[m.end():m.end() + 5]:
            n = int(m.group(1))
            hi = n - 1 if "<" in m.group(0) and "<=" not in m.group(0) else n
    if lo is None and hi is None:
        m = re.search(r"([0-9]+)\s*[-~至]\s*([0-9]+)\s*天", t)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
    if hi is None:
        m = re.search(r"([0-9]+)\s*天\s*(?:以内|以下)", t)
        if m:
            hi = int(m.group(1))
    if lo is None:
        m = re.search(r"([0-9]+)\s*天\s*(?:以上)", t)
        if m:
            lo = int(m.group(1))
    return lo, hi


def _rate_and_fixed(text: str) -> tuple[float | None, float | None]:
    """返回 (rate_pct, fixed_cny)。"每笔1000元" 是固定费，不是 0%。"""
    if "每笔" in text:
        m = re.search(r"每笔\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
        return None, (float(m.group(1)) if m else None)
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)%", text)
    return (float(m.group(1)) if m else None), None


def _jjfl_tables(text: str) -> dict[str, str]:
    """按 <h4> 标题切出费率表；标题里的 <a name=...> 会被 strip_tags 去掉。"""
    out: dict[str, str] = {}
    for m in re.finditer(r"<h4[^>]*>(.*?)</h4>(.*?)(?=<h4|$)", text, re.S):
        heading = strip_tags(m.group(1))
        table = re.search(r"<table[^>]*>(.*?)</table>", m.group(2), re.S)
        if table:
            out[heading] = table.group(1)
    return out


def _rows_of(table_html: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S):
        cells = [strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if cells:
            rows.append(cells)
    return rows


def parse_gmbd(text: str) -> dict[str, Any]:
    """解析 gmbd_{code}.html 的规模变动表（期末份额/期末净资产，按报告期）。

    F10 把表格塞在 `var gmbd_apidata={ content:"<table...>"}` 里；pzd 的
    Data_fluctuationScale 只有最近 5 个季度，做历史 as-of 回测必须用这张全历史表。
    """
    literal = extract_var(text, "gmbd_apidata")
    if not literal:
        raise ParseError("gmbd 缺少 gmbd_apidata")
    m = re.search(r'content\s*:\s*"(.*?)"\s*,', literal, re.S)
    if not m:
        raise ParseError("gmbd content 为空")
    content = m.group(1).replace('\\"', '"').replace("\\/", "/")
    rows: list[dict[str, Any]] = []
    for cells in _rows_of(content):
        if len(cells) < 5:
            continue
        period = cells[0].strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", period):
            continue
        share = to_float(cells[3].replace(",", ""))
        asset = to_float(cells[4].replace(",", ""))
        if share is None and asset is None:
            continue
        rows.append({"report_period": period, "share_scale_billion": share,
                     "asset_scale_billion": asset})
    if not rows:
        raise ParseError("gmbd 无有效规模行")
    rows.sort(key=lambda r: r["report_period"])
    return {"rows": rows, "count": len(rows)}


def _tenure_days(text: str) -> int | None:
    t = strip_tags(text)
    m = re.match(r"(\d+)年(?:又(\d+)天)?", t)
    if m:
        return int(m.group(1)) * 365 + int(m.group(2) or 0)
    m = re.match(r"(\d+)天", t)
    if m:
        return int(m.group(1))
    return None


def parse_jjjl(text: str) -> dict[str, Any]:
    """解析 jjjl_{code}.html 的「基金经理变动一览」。

    一行可能挂多位经理（如「周其源 江小震」），必须拆成多条 tenure；mgr_id 从
    /manager/{id}.html 提取，和 pzd 的 Data_currentFundManager 是同一套 id。
    """
    table = re.search(r'<table[^>]*class=["\'][^"\']*jloff[^"\']*["\'][^>]*>(.*?)</table>',
                      text, re.S)
    if not table:
        raise ParseError("jjjl 未找到基金经理变动表")
    rows: list[dict[str, Any]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(cells) < 5:
            continue
        start = strip_tags(cells[0])[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
            continue
        end_raw = strip_tags(cells[1])
        end = None if "至今" in end_raw else end_raw[:10]
        if end is not None and not re.match(r"^\d{4}-\d{2}-\d{2}$", end):
            end = None
        managers = re.findall(
            r"<a[^>]*href=['\"][^'\"]*?/manager/(\d+)\.html['\"][^>]*>(.*?)</a>",
            cells[2], re.S)
        if not managers:
            managers = [("", n) for n in strip_tags(cells[2]).split()]
        ret = to_float(strip_tags(cells[4]).replace("%", ""))
        for mid, name in managers:
            rows.append({"mgr_id": mid or name, "mgr_name": strip_tags(name),
                         "start_date": start, "end_date": end,
                         "days": _tenure_days(cells[3]), "return_pct": ret})
    if not rows:
        raise ParseError("jjjl 无有效任职行")
    return {"rows": rows, "count": len(rows)}


def parse_jjfl(text: str) -> dict[str, Any]:
    """解析 jjfl_{code}.html 的四张表：运作费用/认购/申购/赎回。

    返回的 rate 一律是百分点（0.80 表示 0.80%）；固定费单独放 fixed_cny。
    """
    tables = _jjfl_tables(text)
    if not tables:
        raise ParseError("jjfl 未找到费率表")
    operating = {"mgr_fee_pct": None, "trust_fee_pct": None, "sales_fee_pct": None}
    op_html = tables.get("运作费用")
    if op_html:
        cells = [c for row in _rows_of(op_html) for c in row]
        for i in range(0, len(cells) - 1, 2):
            label, value = cells[i], cells[i + 1]
            pct, _ = _rate_and_fixed(value)
            if "管理费" in label:
                operating["mgr_fee_pct"] = pct
            elif "托管费" in label:
                operating["trust_fee_pct"] = pct
            elif "销售服务费" in label:
                operating["sales_fee_pct"] = pct

    def _amount_tiers(name: str) -> list[dict[str, Any]]:
        html = tables.get(name)
        if not html:
            return []
        out: list[dict[str, Any]] = []
        for cells in _rows_of(html):
            if len(cells) < 2 or "适用金额" in cells[0]:
                continue
            lo, hi = _amount_range(cells[0])
            rate, fixed = _rate_and_fixed(cells[1])
            discount = None
            if "|" in cells[1]:
                right = cells[1].split("|")[-1]
                discount, _ = _rate_and_fixed(right)
            out.append({"amount_lo_cny": lo, "amount_hi_cny": hi, "rate_pct": rate,
                        "discount_pct": discount, "fixed_cny": fixed, "label": cells[0]})
        return out

    def _redemption_tiers() -> list[dict[str, Any]]:
        html = tables.get("赎回费率")
        if not html:
            return []
        out: list[dict[str, Any]] = []
        for cells in _rows_of(html):
            if len(cells) < 2 or "适用期限" in cells[0]:
                continue
            lo, hi = _days_range(cells[0])
            rate, fixed = _rate_and_fixed(cells[1])
            out.append({"days_lo": lo, "days_hi": hi, "rate_pct": rate, "fixed_cny": fixed,
                        "label": cells[0]})
        return out

    return {
        "operating": operating,
        "purchase": _amount_tiers("申购费率"),
        "subscription": _amount_tiers("认购费率"),
        "redemption": _redemption_tiers(),
        "tables": sorted(tables.keys()),
    }


def parse_chinabond_index(text: str) -> dict[str, Any]:
    """解析中国债券信息网单指数 JSON（POST form 返回）。

    返回结构形如 ``{"CFZS_00": {"<ms_epoch>": 258.2533, ...}, "name": ...}``；
    键是北京时间零点的毫秒时间戳，值是财富/总值指数点位。只接受正数点位，
    按日期升序返回，首行 pct_chg 记 None（不填 0）。
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ParseError(f"chinabond_index 不是 JSON：{exc}") from exc
    series = payload.get("CFZS_00") if isinstance(payload, dict) else None
    if not isinstance(series, dict) or not series:
        raise ParseError("chinabond_index 无 CFZS_00 序列")
    items: list[tuple[int, float]] = []
    for key, value in series.items():
        try:
            ms = int(str(key))
            close = float(value)
        except (TypeError, ValueError):
            continue
        if close <= 0 or ms <= 0:
            continue
        items.append((ms, close))
    items.sort(key=lambda item: item[0])
    rows: list[dict[str, Any]] = []
    prev: float | None = None
    for ms, close in items:
        # 源站时间戳是北京时间零点对应的 UTC 毫秒；加 8 小时后取日期，
        # 避免在 UTC 机器上把 2002-01-04 记成 2002-01-03。
        day = (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
               + timedelta(hours=8)).date().isoformat()
        pct = None if prev is None else round((close / prev - 1.0) * 100.0, 6)
        rows.append({"trade_date": day, "close": close, "pct_chg": pct})
        prev = close
    if not rows:
        raise ParseError("chinabond_index 无有效行")
    return {"name": str(payload.get("name") or "中债-新综合财富(总值)指数"),
            "rows": rows, "count": len(rows)}


def parse_index_kline(text: str) -> dict[str, Any]:
    """解析东财指数日线（kline）响应：返回 close 序列，pct_chg 由相邻收盘价还原。

    只依赖 data.klines 的 "日期,收盘" 两列；fields2 固定 f51,f53。
    第一条没有前收盘，pct_chg 记 None（不填 0）。
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ParseError(f"index_kline 不是 JSON：{exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    klines = (data or {}).get("klines") or []
    if not klines:
        raise ParseError("index_kline 无 klines")
    rows: list[dict[str, Any]] = []
    prev: float | None = None
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 2:
            continue
        day = parts[0].strip()
        try:
            close = float(parts[1])
        except ValueError:
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or close <= 0:
            continue
        pct = None if prev is None else round((close / prev - 1.0) * 100.0, 6)
        rows.append({"trade_date": day, "close": close, "pct_chg": pct})
        prev = close
    if not rows:
        raise ParseError("index_kline 无有效行")
    return {"name": (data or {}).get("name"), "rows": rows, "count": len(rows)}



def parse_csrc_lifecycle(text: str) -> dict[str, Any]:
    """解析证监会 EID 基金公告检索 JSON（advanced_search_report.do）。

    只保留 6 位数字基金代码、有合法 reportSendDate 的公告；不猜测字段。
    ``success=false`` 视为业务失败（通常是日期窗口超过 30 天），抛 ParseError。
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ParseError(f"csrc_lifecycle 不是 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ParseError("csrc_lifecycle 顶层不是对象")
    if payload.get("success") is False:
        raise ParseError(str(payload.get("message") or "csrc_lifecycle success=false"))
    data = payload.get("aaData")
    if not isinstance(data, list):
        raise ParseError("csrc_lifecycle 无 aaData 数组")
    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = str(item.get("fundCode") or "").strip()
        send = str(item.get("reportSendDate") or "").strip()[:10]
        if not re.fullmatch(r"\d{6}", code) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", send):
            continue
        rows.append({
            "fcode": code,
            "fund_short_name": str(item.get("fundShortName") or "").strip(),
            "report_code": str(item.get("reportCode") or "").strip(),
            "report_name": str(item.get("reportName") or "").strip(),
            "report_desp": str(item.get("reportDesp") or "").strip(),
            "report_send_date": send,
            "report_year": str(item.get("reportYear") or "").strip(),
            "organ_name": str(item.get("organName") or "").strip(),
            "upload_info_id": str(item.get("uploadInfoId") or "").strip(),
        })
    return {"total": int(payload.get("iTotalRecords") or len(rows)),
            "rows": rows, "count": len(rows)}
