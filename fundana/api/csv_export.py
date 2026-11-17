"""把当前榜单视图原样导出成 CSV。

为什么要导出"当前视图"而不是固定列：用户在页面上筛选了风格、搜了关键词、
按三列排了序，导出的文件必须和眼前看到的一致；否则"导出"就成了另一个榜单。

约定：
* UTF-8 带 BOM —— Excel 直接双击不乱码；
* 代码列按文本写出（000003 不会被 Excel 变成 3）；
* 缺失写空串，不写 0、不写 "--"，避免和真实 0 混淆；
* 浮点用 %.10g，去掉 0.04919182865403782 这种二进制尾巴。
"""
from __future__ import annotations

import csv
import io

from .. import db

BOM = "\ufeff"
MAX_ROWS = 5000


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return "%.10g" % value
    return str(value)


def _to_pct(value):
    """小数比率 -> 百分点（0.0492 -> 4.92），缺失仍是空。"""
    return None if value is None else value * 100


def build(columns, rows: list[dict]) -> bytes:
    """columns = [(字段名, 表头文字[, 转换函数])]，按给定顺序输出。

    转换函数只做单位换算（小数比率 -> 百分点），不做四舍五入，
    以免导出的数字和页面看到的对不上。
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([c[1] for c in columns])
    for row in rows:
        cells = []
        for col in columns:
            value = row.get(col[0])
            if len(col) > 2 and col[2] is not None:
                value = col[2](value)
            cells.append(_cell(value))
        writer.writerow(cells)
    return (BOM + buf.getvalue()).encode("utf-8")


def filename(prefix: str, suffix: str = "") -> str:
    stamp = db.today_cn().replace("-", "")
    tail = ("_" + suffix) if suffix else ""
    return "%s%s_%s.csv" % (prefix, tail, stamp)


RANKING_COLUMNS = [
    ("rank", "排名"),
    ("fcode", "基金代码"),
    ("name", "基金名称"),
    ("style_label", "风格"),
    ("formal_score", "正式分"),
    ("rank_score", "排序分"),
    ("confidence", "数据质量分"),
    ("nav_date", "净值日期"),
    ("metric_end_date", "指标截止日"),
    ("metric_stale", "指标落后有效净值日"),
    ("day_pct", "最新日涨跌(%)"),   # 已是百分点，不能再乘 100
    ("annual_1y", "1年年化(%)", _to_pct),
    ("sharpe_1y", "夏普"),
    ("sortino", "索提诺"),
    ("calmar", "卡玛"),
    ("mdd_1y", "1年最大回撤(%)", _to_pct),
    ("positive", "日胜率(%)"),
    ("flat", "持平日占比(%)"),
    ("non_negative", "非负日占比(%)"),
    ("mdd_3y", "3年最大回撤(%)", _to_pct),
    ("annual_3y", "3年年化(%)", _to_pct),
    ("fund_mdd_3y", "3年最大回撤(基金指标,%)", _to_pct),
    ("vol_3y", "3年年化波动(%)", _to_pct),
    ("sharpe_3y", "3年夏普"),
    ("coverage_ratio_3y", "3年窗口覆盖率"),
    ("is_full_3y", "3年窗口完整"),
    ("annual_5y", "5年年化(%)", _to_pct),
    ("fund_mdd_5y", "5年最大回撤(基金指标,%)", _to_pct),
    ("vol_5y", "5年年化波动(%)", _to_pct),
    ("sharpe_5y", "5年夏普"),
    ("coverage_ratio_5y", "5年窗口覆盖率"),
    ("is_full_5y", "5年窗口完整"),
    ("worst_rolling90d", "最差滚动90日(%)"),
    ("tail_completeness", "尾部完整度"),
    ("asset_scale_billion", "家族规模(亿)"),
    ("share_scale_billion", "当前份额规模(亿)"),
    ("manager_tenure_min_days", "现任经理最短任职(天)"),
    ("metric_comparable", "指标可比"),
    ("metric_flags", "指标不可比旗标",
     lambda v: ",".join(v) if isinstance(v, list) else v),
    ("risk_status", "风险门状态"),
    ("risk_blocked", "风险门拦截"),
    ("risk_flags", "风险旗标",
     lambda v: ",".join(v) if isinstance(v, list) else v),
    ("recurring_fee_pct", "持续计提费(%)"),
    ("cost_1y_pct", "持有1年总成本(%)"),
    ("cost_3y_pct", "持有3年总成本(%)"),
    ("cost_5y_pct", "持有5年总成本(%)"),
    ("redemption_1y_pct", "持有满1年赎回费(%)"),
    ("lockup_days", "最短持有期(天)"),
    ("lockup_label", "持有期类型"),
    ("family_members", "家族份额数"),
]

LOCKED_COLUMNS = [
    ("rank", "排名"),
    ("fcode", "基金代码"),
    ("name", "基金名称"),
    ("style_label", "风格"),
    ("lockup_label", "持有期类型"),
    ("effective_days", "最短持有期(天)"),
    ("next_open_date", "下次开放日"),
    ("hint", "开放状态"),
    ("annual_1y_pct", "1年年化"),
    ("sharpe_1y", "夏普"),
    ("mdd_1y_pct", "1年最大回撤"),
    ("positive_1y_pct", "日胜率"),
    ("ret_1w_pct", "近1周"),
    ("ret_1m_pct", "近1月"),
    ("ret_3m_pct", "近3月"),
    ("ret_6m_pct", "近6月"),
    ("scale_billion", "家族规模(亿)"),
    ("recurring_fee_pct", "持续计提费(%)"),
    ("cost_1y_pct", "持有1年总成本(%)"),
    ("cost_3y_pct", "持有3年总成本(%)"),
    ("cost_5y_pct", "持有5年总成本(%)"),
    ("redemption_1y_pct", "持有满1年赎回费(%)"),
]
