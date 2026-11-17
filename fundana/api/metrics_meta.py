"""指标字典与漏斗层说明：前端列头 tooltip / 感叹号的唯一来源。

设计要点：
* 一个指标只在这里定义一次（名称、单位、方向、口径说明），前端不再各页硬编码，
  否则同一个"夏普比率"在榜单页与详情页会出现两种解释。
* direction 决定排序默认方向与提示语：higher=越大越好、lower=越小越好、none=仅参考。
* 漏斗层的门槛数值从 screen.thresholds 读，不写死在文案里——改门槛时说明自动跟着变。
"""
from __future__ import annotations

from ..screen.thresholds import (DYNAMIC_MIN_DIMENSIONS_PASSED, DYNAMIC_MIN_STYLE_N,
                                 DYNAMIC_QUANTILES, ENHANCED_RELATIVE, FIXED,
                                 FORMAL_TARGET_MAX, MANAGER_CAREER_MIN_DAYS,
                                 MATURITY_SCALE_MIN, MAX_LOCK_DAYS,
                                 MAX_SAMPLING_GAP_DAYS, METRIC_STALE_TOLERANCE_DAYS,
                                 SHARE_SCALE_MIN, SHORT_SCALE_MIN)
from ..screen.thresholds import COST_CAP_PCT_1Y, COST_HORIZON_DAYS, ROOKIE_QUOTA

# 单位：pct=百分点、dec=小数比率、num=无量纲、billion=亿元、count=计数、days=天
METRICS = [
    {"key": "ret_1w", "label": "近 1 周", "unit": "pct", "direction": "higher",
     "desc": "最近 7 个自然日窗口内的复投总收益（按日增长率链式相乘），已扣分红再投。"},
    {"key": "ret_1m", "label": "近 1 月", "unit": "pct", "direction": "higher",
     "desc": "最近 30 天窗口的复投总收益。窗口不足 30 天时为 —。"},
    {"key": "ret_3m", "label": "近 3 月", "unit": "pct", "direction": "higher",
     "desc": "最近 90 天窗口的复投总收益。"},
    {"key": "ret_6m", "label": "近 6 月", "unit": "pct", "direction": "higher",
     "desc": "最近 180 天窗口的复投总收益。"},
    {"key": "ret_1y", "label": "近 1 年", "unit": "pct", "direction": "higher",
     "desc": "最近 365 天窗口的复投总收益。与单位净值涨跌不同：分红会按当日净值再投回去。"},
    {"key": "annual_1w", "label": "1 周年化", "unit": "pct", "direction": "higher",
     "desc": "把 7 天收益按 365.25 天外推成年化。样本极短，波动被放大数倍，只作观察。"},
    {"key": "annual_1m", "label": "1 月年化", "unit": "pct", "direction": "higher",
     "desc": "把 30 天收益外推成年化，短窗口外推值参考价值有限。"},
    {"key": "annual_3m", "label": "3 月年化", "unit": "pct", "direction": "higher",
     "desc": "把 90 天收益外推成年化。"},
    {"key": "annual_6m", "label": "6 月年化", "unit": "pct", "direction": "higher",
     "desc": "把 180 天收益外推成年化。"},
    {"key": "annual_1y", "label": "1 年年化", "unit": "pct", "direction": "higher",
     "desc": "365 天窗口的复投总收益按 365.25 天折算成年化（与区间收益同窗口，不是两条曲线）。"},
    {"key": "sharpe", "label": "夏普比率", "unit": "num", "direction": "higher",
     "desc": "（年化收益 − 无风险利率 1.64%）÷ 年化波动率。衡量每承担 1 单位波动换来多少超额收益；"
             "债基通常 0.5–3，封顶显示 ±5 并标 *。"},
    {"key": "sortino", "label": "索提诺比率", "unit": "num", "direction": "higher",
     "desc": "（年化收益 − 目标收益 0）÷ 下行波动率。只惩罚下跌波动，比夏普更贴近债基体感；"
             "封顶 ±10，无下跌时为空（不伪造）。"},
    {"key": "calmar", "label": "卡玛比率", "unit": "num", "direction": "higher",
     "desc": "年化收益 ÷ 同期最大回撤。衡量「为拿到这份收益，最坏要忍多深的回撤」；"
             "回撤为 0 时为空。本项目用 1 年窗口，与 36 个月口径不同，故称策略卡玛。"},
    {"key": "mdd_1y", "label": "1 年最大回撤", "unit": "pct", "direction": "lower",
     "desc": "最近 365 天内从任意高点到随后最低点的最大跌幅（正数表示跌幅）。"},
    {"key": "mdd_3y", "label": "3 年最大回撤", "unit": "pct", "direction": "lower",
     "desc": "最近 1095 天内的最大回撤，能抓到 1 年窗口看不到的旧重挫。"},
    {"key": "vol", "label": "波动率", "unit": "pct", "direction": "lower",
     "desc": "日收益标准差 × √(年观测数)，年化后的波动幅度。纯债通常 0.3%–1.5%。"},
    {"key": "positive", "label": "日胜率", "unit": "pct", "direction": "higher",
     "desc": "窗口内日收益为正的交易日占比。仅展示，不再作为债基门槛：低波动债基大量交易日"
             "四舍五入为 0.00%，会被系统性压低。"},
    {"key": "non_negative", "label": "非负日占比", "unit": "pct", "direction": "higher",
     "desc": "窗口内日收益 ≥0 的交易日占比（上涨日 + 持平 0.00% 日）。稳定风格的门槛与评分用这项，"
             "避免把“没跌”误判为“没赚”。"},
    {"key": "flat", "label": "持平日占比", "unit": "pct", "direction": "none",
     "desc": "窗口内日收益恰为 0.00% 的交易日占比。越高说明净值披露精度/策略久期越低，"
             "用于解释日胜率与非负日占比的差异。"},
    {"key": "n_obs", "label": "观测数", "unit": "count", "direction": "none",
     "desc": "该窗口实际取到的净值点数 / 该窗口要求的最少点数。门槛按序列自身节奏折算"
             "（日频 365 天=202 点，周频=42 点）。"},
    {"key": "nav_date", "label": "净值日期", "unit": "date", "direction": "none",
     "desc": "该基金最新一条净值的业务日（不是抓取时间）。日更在收盘后追加，"
             "因此盘中看到的仍是上一个已披露业务日。"},
    {"key": "day_pct", "label": "日涨跌", "unit": "pct", "direction": "higher",
     "desc": "最新业务日的净值涨跌幅，取自上交所/深交所披露口径（B1 快照的官方日增长率），"
             "不是用两个净值手算的。定开基金在非开放日没有日涨跌，显示为 —。"},
    {"key": "lockup_days", "label": "最短持有期", "unit": "days", "direction": "lower",
     "desc": "买入后必须持有的天数（份额锁定期/最短持有期）。0 或空=当日可赎回。"
             "注意「0-3 年期」「1-3 年」这类名称说的是标的指数久期，不是持有期，"
             "判定只看名称里的「持有/定开」与结构化字段。"},
    {"key": "worst90", "label": "最差滚动 90 日", "unit": "pct", "direction": "higher",
     "desc": "3 年窗口内任意连续 90 天的最低收益。比最大回撤更贴近「买了就跌」的真实体验。"},
    {"key": "cvar5", "label": "CVaR 5%", "unit": "pct", "direction": "higher",
     "desc": "最差 5% 交易日的平均收益（条件风险价值）。样本不足 100 天时为空。"},
    {"key": "worst180", "label": "最差滚动 180 日", "unit": "pct", "direction": "higher",
     "desc": "3 年窗口内任意连续 180 个自然日的累计收益里最差的一段（百分点）。"
             "与最差 90 日一起看：90 日看急跌，180 日看慢性阴跌。窗口按自然日对齐，"
             "遇到净值缺口整段跳过，不会把缺口当成 0 收益。"},
    {"key": "worst_day", "label": "最差单日", "unit": "pct", "direction": "higher",
     "desc": "3 年窗口内单日跌幅最大的一天（百分点，负值）。债基单日跌超 1% 通常意味着"
             "持仓有权益或信用事件，是回撤来源的直接证据。"},
    {"key": "worst_week", "label": "最差单周", "unit": "pct", "direction": "higher",
     "desc": "3 年窗口内最差的一个 5 个交易日区间收益（百分点）。用 5 个观测近似一周，"
             "不是自然周，跨节假日时区间会拉长。"},
    {"key": "month_positive", "label": "自然月正收益比例", "unit": "pct", "direction": "higher",
     "desc": "3 年窗口内收益为正的自然月占比（百分点）。只统计观测数 ≥10 的完整月份，"
             "残缺月不计入分母；与日胜率相比，它更贴近持有人的真实体验。"},
    {"key": "underwater", "label": "最长水下观测数", "unit": "count", "direction": "lower",
     "desc": "从净值高点回落到重新创新高之间最长的观测个数（不是自然日）。"
             "数值越大代表套牢时间越久；单位是观测数，换算成天要看该基金的披露频率。"},
    {"key": "tail_completeness", "label": "尾部完整度", "unit": "num", "direction": "higher",
     "desc": "3 年尾部指标 8 个组件里算出了几个（0–1）。低于 1 说明历史不足，尾部数据不完整。"},
    {"key": "formal_score", "label": "正式分", "unit": "num", "direction": "higher",
     "desc": "短期核心分 45% + 长期核心分 55%，组内百分位化后合成。只用于同风格比较。"},
    {"key": "rank_score", "label": "排序分", "unit": "num", "direction": "higher",
     "desc": "同一榜单内的展示顺序分：综合分 85% + 近 6 个月收益分位 15%（ADR-062）。"
             "只影响排名先后，不参与任何准入门槛。"},
    {"key": "composite", "label": "综合分", "unit": "num", "direction": "higher",
     "desc": "同风格百分位（90%）+ 绝对基准（10%）的合成分，决定是否通过 L6C 固定门槛。"},
    {"key": "confidence", "label": "数据质量分", "unit": "num", "direction": "higher",
     "desc": "净值样本数/披露间隔/新鲜度/指标与资料完整度五项加权，0–100（≥80 高、≥60 中）。只用于提示数据可信度，不参与排序。"},
    {"key": "scale", "label": "家族规模", "unit": "billion", "direction": "higher",
     "desc": "同一基金各份额净资产之和（基金级），不是单个份额的规模。单位亿元。"},
    {"key": "fee", "label": "持续计提费", "unit": "pct", "direction": "lower",
     "desc": "管理费 + 托管费 + 销售服务费，按年化百分点计。不含申购/赎回的一次性费用。"},
    {"key": "manager_career", "label": "经理职业年限", "unit": "days", "direction": "higher",
     "desc": "现任经理中最短的一位从业天数（不是本基金任职天数）。不足 365 天会被 L8 拦下。"},
    {"key": "lockup", "label": "最短持有期", "unit": "days", "direction": "lower",
     "desc": "买入后必须持有的天数（结构化条款与名称推断取大者）。超过 90 天进受限榜。"},
    {"key": "annual_3y", "label": "3 年年化", "unit": "pct", "direction": "higher",
     "desc": "最近 1095 天窗口的复投总收益按 365.25 天折算；覆盖不足 90% 时只能作参考，不能当完整 3 年业绩。"},
    {"key": "annual_5y", "label": "5 年年化", "unit": "pct", "direction": "higher",
     "desc": "最近 1825 天窗口的复投总收益按 365.25 天折算；5 年覆盖不足 60% 时不进入长期榜。"},
    {"key": "sharpe_3y", "label": "3 年夏普", "unit": "num", "direction": "higher",
     "desc": "3 年窗口年化收益减无风险利率后除以 3 年窗口年化波动率；与 1 年夏普同口径但窗口更长。"},
    {"key": "sharpe_5y", "label": "5 年夏普", "unit": "num", "direction": "higher",
     "desc": "5 年窗口年化收益减无风险利率后除以 5 年窗口年化波动率；仅在 5 年覆盖足够时展示。"},
    {"key": "vol_3y", "label": "3 年波动率", "unit": "pct", "direction": "lower",
     "desc": "3 年窗口日收益标准差年化；可检查 1 年窗口之外的波动稳定性。"},
    {"key": "vol_5y", "label": "5 年波动率", "unit": "pct", "direction": "lower",
     "desc": "5 年窗口日收益标准差年化；覆盖不足时为空，不用短窗口冒充。"},
    {"key": "mdd_3y_metric", "label": "3 年最大回撤（净值）", "unit": "pct", "direction": "lower",
     "desc": "按 fund_metric 的 1095 天净值窗口计算的最大回撤；与尾部指标表中的 mdd_3y 来源不同，页面会分别标注。"},
    {"key": "mdd_5y_metric", "label": "5 年最大回撤", "unit": "pct", "direction": "lower",
     "desc": "按 1825 天净值窗口计算的最大回撤；5 年覆盖不足时为空。"},
    {"key": "redemption_1y", "label": "1 年赎回费", "unit": "pct", "direction": "lower",
     "desc": "持有满 1 年时的赎回费率；用于成本边界核对，不进入 L6C 的持有 1 年成本门槛（该项已按满 1 年免赎回费处理）。"},
    {"key": "long_score", "label": "长期分", "unit": "num", "direction": "higher",
     "desc": "长期核心观察榜排序分 = 60% 长期正式分 + 40% 长期核心分；只用于长期观察榜，不参与正式榜准入。"},
    {"key": "share_scale", "label": "份额规模", "unit": "billion", "direction": "higher",
     "desc": "目标份额自身的净资产（亿元），不是同家族各份额合计；核心榜要求≥2 亿，L5 仅要求≥0.5 亿。"},
]

METRIC_INDEX = {m["key"]: m for m in METRICS}

DIRECTION_LABELS = {"higher": "越大越好", "lower": "越小越好", "none": "仅参考"}


# 前端列头与排序白名单用的短键 -> 字典规范键。
# 允许别名是为了不让"改个列名"变成全站改 URL 的破坏性变更；
# payload() 会把别名作为重复条目一起下发，前端 map 两种键都能查到说明。
ALIASES = {
    "mdd": "mdd_1y",
    "tail": "tail_completeness",
}


def get_metric(key: str):
    """按规范键或别名取指标定义，找不到返回 None。"""
    canonical = ALIASES.get(key, key)
    for m in METRICS:
        if m["key"] == canonical:
            return m
    return None


def _pct(v: float) -> str:
    return ("%g" % v) + "%"


def layer_details(nav_age_limit_days: int = 10, establish_days: int = 365) -> list[dict]:
    """漏斗每一层的门槛说明。数值全部来自 thresholds 常量与配置，改门槛即自动更新。"""
    scale = " / ".join("%g" % MATURITY_SCALE_MIN[k] for k in sorted(MATURITY_SCALE_MIN)) + " 亿"
    fixed = [
        "综合分 ≥%g" % FIXED["composite_min"],
        "1 年年化 ≥%s" % _pct(FIXED["annual_1y_min_pct"]),
        "夏普 ≥%g" % FIXED["sharpe_1y_min"],
        "卡玛 ≥%g" % FIXED["calmar_1y_min"],
        "非负日占比 ≥%s" % _pct(FIXED["non_negative_1y_min_pct"]),
        "1 年回撤 ≤%s" % _pct(FIXED["mdd_1y_max_dec"] * 100),
        "1 年波动 ≤%s" % _pct(FIXED["vol_1y_max_dec"] * 100),
        "持有 %d 年总成本 ≤%s" % (COST_HORIZON_DAYS // 365, _pct(FIXED["fee_max_pct"])),
        "3 年完整业绩（窗口有效年数 ≥90%）",
    ]
    enh = ENHANCED_RELATIVE
    q = enh["quantiles"]
    enh_txt = ("同风格分位：年化 q%g、卡玛 q%g、夏普 q%g、日胜率 q%g、回撤 q%g、波动 q%g、费率 q%g；"
               "再加绝对地板、正式分 ≥%g 与 3 年完整业绩；每风格配额 %d、总量 %d"
               % (q["annual_1y"] * 100, q["calmar"] * 100, q["sharpe"] * 100,
                  q["positive"] * 100, q["mdd"] * 100, q["vol"] * 100, q["fee"] * 100,
                  enh["formal_score_min"], enh["quota_per_style"], enh["quota_total"]))
    dyn = DYNAMIC_QUANTILES
    dyn_txt = ("综合分 ≥ max(固定线 %g, 同风格 q%g)，且 6 项指标至少 %d 项过线（年化 q%g、夏普 q%g、索提诺 q%g、"
               "卡玛 q%g、非负日占比 q%g、费率 q%g）；母体为可投域（L1–L5.5 通过者），"
               "同风格样本 < %d 时回退全局分位；已上榜者按 exit 档保留（滞回）"
               % (FIXED["composite_min"], dyn["composite"] * 100, DYNAMIC_MIN_DIMENSIONS_PASSED,
                  dyn["annual_1y"] * 100, dyn["sharpe_1y"] * 100, dyn["sortino_1y"] * 100,
                  dyn["calmar_1y"] * 100, dyn["non_negative_1y"] * 100, dyn["fee"] * 100,
                  DYNAMIC_MIN_STYLE_N))
    return [
        {"code": "L1", "detail": "6 位代码在名录内、成立日可查、有有效净值、净值距今 ≤ %d 天、"
                                 "披露间隔 p75 ≤ %g 天、1 年期至少 202 个观测；"
                                 "指标日可早于有效日 ≤%d 天（记 metric_stale_days），"
                                 "更早或晚于有效日即阻断"
                                 % (nav_age_limit_days, MAX_SAMPLING_GAP_DAYS,
                                    METRIC_STALE_TOLERANCE_DAYS)},
        {"code": "L2", "detail": "只看 1 个条件：当日申赎快照状态为开放申购或限大额（限大额不设数值门槛）才算可申购；暂停申购、封闭期、认购期直接淘汰，状态缺失记 unknown 不通过。场内 ETF/LOF 的申赎状态写作“场内交易”，可在证券账户买卖但不在场外申购口径内，单列 exchange_traded 原因码"},
        {"code": "L3", "detail": "最短持有期 ≤ %d 天；定期开放/封闭式不进正式榜（协会口径下不参与同类排名），"
                                  "与超过 %d 天者一起单列持有期受限榜" % (MAX_LOCK_DAYS, MAX_LOCK_DAYS)},
        {"code": "L4", "detail": "3 项资料必须可验：family 规模、目标份额规模、持有 1 年总成本；规模/份额取报告期 <= 有效日-45 天的最近一期，回退 profile 当前值只能进观察/受限；缺任意 1 项即不通过（不猜、不用行业均值代替）"},
        {"code": "L5", "detail": "成立满 %d 天；family 规模 ≥ %s（按成立 1/2/3/4/5 年），中短债 ≥ %g 亿；目标份额 ≥ %g 亿"
                                 % (establish_days, scale, SHORT_SCALE_MIN, SHARE_SCALE_MIN)},
        {"code": "L5.5", "detail": "持有 %d 年总成本（持续计提费 + 申购费摊到每年）≤ %s；"
                                   "A 类一次性申购费按 1 折摊薄、C 类销售服务费按年计入，两类同尺"
                                   % (COST_HORIZON_DAYS // 365, _pct(COST_CAP_PCT_1Y))},
        {"code": "L6C", "detail": "、".join(fixed)},
        {"code": "L6E", "detail": enh_txt},
        {"code": "L7", "detail": dyn_txt},
        {"code": "L8", "detail": "现任经理职业年限最短者 ≥ %d 天，且在本基金任职 ≥365 天"
                                 "（核心榜 ≥730 天）；缺值放行，不伪造低分"
                                 % MANAGER_CAREER_MIN_DAYS},
        {"code": "L8R", "detail": "独立风险门：最新持仓集中度（前十大/单一债券/单一信用发行人/转债/股票）"
                                  "与 2022Q4、2020H1、2016Q4、近 5 年最差 60 日压力测试；"
                                  "block 才拦截，watch/unknown 只展示；持仓报告期超过 270 天按陈旧处理"},
        {"code": "L9", "detail": "排序：排序分（0.85×综合分 + 0.15×6 个月收益分位）→ 正式分 → 综合 → 年化 → 夏普 → 代码；"
                                 "同一只基金只留正式分最高的份额；稳健榜上限 %d 只，增强榜每风格 %d / 总量 %d"
                                 % (FORMAL_TARGET_MAX, enh["quota_per_style"], enh["quota_total"])},
        {"code": "L10", "detail": "输出 8 张榜：纯债稳健榜（≤320 只）、增强债榜（每风格配额 40、总量 110）、"
                                 "核心精选榜（从正式候选跨风格按排序分取 50 只，不含增强债/可转债，不占正式名额）、"
                                 "收益精选榜（同排序分再集中到 10 只，按风格上限与相关性控制集中度）、"
                                 "长期核心观察榜（3 年完整、5 年覆盖≥60%%、经理在本基金覆盖 3 年窗口≥80%%、份额≥2 亿，不占正式名额）、"
                                 "观察榜（质量达标但当期买不到）、持有期受限榜（最短持有期 >90 天或定开/封闭，只展示不参与正式排名）、"
                                 "新锐榜（历史 1–3 年、只差综合分这一条者，按 1 年窗口新锐分取前 %d 只，不占正式名额）"
                                 % ROOKIE_QUOTA},
    ]


def _with_aliases() -> list[dict]:
    out = []
    for m in METRICS:
        out.append(dict(m, direction_label=DIRECTION_LABELS.get(m["direction"], "仅参考")))
    by_key = {m["key"]: m for m in out}
    for alias, canonical in ALIASES.items():
        src = by_key.get(canonical)
        if src is not None:
            item = dict(src)
            item["key"] = alias
            item["alias_of"] = canonical
            out.append(item)
    return out


def payload(nav_age_limit_days: int = 10) -> dict:
    return {
        "metrics": _with_aliases(),
        "directions": [{"code": k, "label": v} for k, v in DIRECTION_LABELS.items()],
        "layers": layer_details(nav_age_limit_days),
    }
