"""筛选阈值契约：所有魔法数集中于此，便于审计与版本化。

契约 ID：screen-bond-v11-2026.12.7（与下方 SCREEN_CONTRACT 一致；日期段是迭代标识，不是生效日期）
v11（2026-11-04，第三轮审计 P0）：日胜率拆分（non_negative 入固定线）、显式 3 年门槛、
动态阈值无条件 q70、固定综合分 62、k=5/6；详见 ADR-102 ~ ADR-104。
v2 相对 v1 的语义变更（见 docs/decisions.md ADR-050 ~ ADR-055）：
* 动态分位母体不再被固定线预筛（v1 的母体 = 固定线通过者，实测把 q70 从 55.7 抬到 71.0，
  等于第三条绝对线）；
* 动态维度 8 → 6（去掉 mdd/vol：与 calmar/vol 相关 0.62–0.89，且固定线已覆盖），k-of-n 6/8 → 4/6；
* 删除 FIXED.scale_min（与 L5 规模线重复，且与短债 1.5 亿口径冲突）；
* 删除未实现的 NORMALIZER 与从未使用的 DIAGNOSTIC；
* 新增阈值冻结（screen_batch/screen_threshold）与双侧带滞回（DYNAMIC_HYSTERESIS）；
* 信心分改为 data_quality（0–100 数据质量分），不再参与排序。
注意：所有 *_pct 为百分点，*_dec 为小数比率，*_billion 为亿元。
"""
from __future__ import annotations

SCREEN_PROFILE = "bond_v1"
SCREEN_CONTRACT = "screen-bond-v11-2026.12.7"  # v11：P1 长期核心观察榜 bond_long（3y 完整/5y 覆盖>=60%/经理>=730天/份额>=2亿/long_score）；P0（non_negative/3y显式/无条件q70/固定62/k=5/指标日容忍7天/规模45天滞后）；v10：周期覆盖/3y5y风险/分位母体/规模份额/经理 tenure/cap 治理；v9：增强债严格门 5/7（ADR-080）；v8：滚动持有期（ADR-073）；v7：中文数字持有期；v6：收益精选 10（ADR-072）；v5：核心精选 50（ADR-071）；v4：排序分（ADR-062）
# v3：L5.5/L6C/动态费率改持有期总成本（ADR-059）；v4 只改排序键，准入不变。

# ── 风格与分组 ──────────────────────────────────────────────
STYLE_ORDER = [
    "bond_convertible", "bond_secondary_enhanced", "bond_primary_enhanced", "bond_short",
    "bond_index", "bond_rate", "bond_credit", "bond_cash_enhanced", "bond_pure",
]
STYLE_LABELS = {
    "bond_convertible": "转债增强", "bond_secondary_enhanced": "二级债基",
    "bond_primary_enhanced": "一级债基", "bond_short": "中短债", "bond_index": "固收指数",
    "bond_rate": "利率债", "bond_credit": "信用债", "bond_cash_enhanced": "现金增强",
    "bond_pure": "综合纯债",
}
ENHANCED_STYLES = ("bond_convertible", "bond_secondary_enhanced", "bond_primary_enhanced")
STABLE_STYLES = ("bond_pure", "bond_short", "bond_rate", "bond_credit", "bond_index",
                 "bond_cash_enhanced")

# ── L3 持有期 ───────────────────────────────────────────────
MAX_LOCK_DAYS = 90

# ── L5 规模（亿元）：按成立年限分层；中短债用短期口径 ─────────
MATURITY_SCALE_MIN = {1: 2.0, 2: 2.0, 3: 2.5, 4: 3.0, 5: 3.0}
SHORT_SCALE_MIN = 1.5
# P0-5：规模/份额取 report_period <= as_of-45 天的最近一期（季报披露滞后），
# 与 fundana/fuse/scale.py 的 walkforward 口径一致；profile 当前值只作展示，
# 不得进入正式榜（见 engine.scale_pit_check）。
SCALE_DISCLOSURE_LAG_DAYS = 45
# P0-7：family 规模之外再看目标份额规模；基础门只挡极小份额，核心榜另设更严门槛。
# field_src_json.share_class_scale_billion 是当前唯一可用的份额级规模（fund_profile.share_scale_billion 全 NULL）。
SHARE_SCALE_MIN = 0.5          # 亿元，基础门 L5
CORE_SHARE_SCALE_MIN = 2.0     # 亿元，核心 50 / 收益精选 10（006432 C 1.58 亿在此被降级）

# ── L5.5 持有期总成本上限（P1-2，ADR-059）────────────────────
# 旧口径只比较"持续计提费"（管理+托管+销售服务），对 A 类系统性有利：
# A 类 0.8% 申购费不计入，C 类 0.4%/年销售服务费计入。实测 1,496 只
# 基金"recurring≤0.60 但持有 1 年总成本>0.60"（A 类 584、无字母份额 788）。
# v3 改为持有期总成本 cost(h) = 经常性费率 + 申购费有效值/(h/365)：
#   申购费有效值 = max_sub_fee_pct × (0.1 if 第三方 1 折 else 1.0)
#   赎回费不进入门槛（产品决策）：cost_1y 按"持有满 1 年免收"；阶梯表 2026-11-02 已落库，只做展示
# 主口径取 1 年（与本 profile 的年化/夏普窗口一致），3 年只做展示。
COST_HORIZON_DAYS = 365
COST_DISPLAY_HORIZONS = (365, 1095)
COST_DISCOUNT_FACTOR = 0.1
# 粗筛上限（硬天花板，不是筛选主力）：全市场 cost_1y p90=1.20 / p95=1.20 / p99=1.55，
# 1.30 ≈ p98，砍掉 160 只（2.1%）。取 1.30 是为了"任何风格 p75 都在线内"（转债 1.15、
# 二级 1.05、一级 0.98），只清掉"无折扣高申购费 + 高持续费"的异常尾巴。
# 费率的主要约束在下游：L6C 固定线 0.90、L7 动态分位 q85（实测 0.65）。
COST_CAP_PCT_1Y = 1.30

# ── L6C 固定质量硬比较（9 项真硬比较）──────────────────────
FIXED = {
    # P0-3 配额校准：62 ≈ 全门槛池 q84（2026-11-02 实测 63.59 向下取整），目标正式榜 120–160。
    # 这是选择性参数，不是收益预测线；不得把“过 62”解释成未来收益保证。
    "composite_min": 62.0,        # 综合分（0–100）
    "annual_1y_min_pct": 2.0,     # 1 年年化（百分点）
    "sharpe_1y_min": 0.8,
    "calmar_1y_min": 2.8,
    # P0-1：日胜率把 0.00% 的平坦日记为“非正”，会系统性压低低波动债基；
    # 固定线改用“非负日占比”（>0 或 =0），positive 仅展示。70 ≈ 旧 55 的选择性。
    "non_negative_1y_min_pct": 70.0,
    "mdd_1y_max_dec": 0.020,
    "vol_1y_max_dec": 0.060,
    # 1 年持有期总成本（pct）；漏斗内 p85=0.95，取 0.90 ≈ p82。
    # 实测现有 222 只正式入选者只有 8 只 >0.90 → 换口径不推翻榜单。
    "fee_max_pct": 0.90,
}
# v2：DIAGNOSTIC 已删除（定义后全仓无引用）。诊断能力由 validate walkforward 承担。

# ── L8 经理后过滤 ───────────────────────────────────────────
# 职业年限取"现任经理中最短者"（弱环节口径）；缺值默认不阻断（缺失≠差），
# 但会写进决策的 manager 证据块，并在 diagnostics 里统计缺失数（ADR-019）。
# 采样节奏：观测间隔 p75 > 7 天的序列（周披露/定开）指标不可与日频基金横比，
# 只进研究视图，不进正式双榜（ADR-026 / v10 P0-9）。
MAX_SAMPLING_GAP_DAYS = 7          # P0-9：周披露/定开从 3 天放宽到 7 天，避免误杀低频但合法的债基
# P0-4：指标截止日允许比有效净值日早 0–7 天（周更/定开常见），记 metric_stale_days；
# 超过 7 天或指标日 > as_of 仍阻断。容忍不是“用旧指标排新榜”，报告必须披露 stale 天数。
METRIC_STALE_TOLERANCE_DAYS = 7
MANAGER_CAREER_MIN_DAYS = 365
MANAGER_CAREER_REQUIRE_KNOWN = False
MANAGER_TENURE_MIN_DAYS = 365      # P0-8：现任经理在本基金的最短任职
MANAGER_TENURE_REQUIRE_KNOWN = False
CORE_MANAGER_TENURE_MIN_DAYS = 730 # 核心榜要求现任经理在本基金 >=2 年

# ── L7 动态分位（母体=基础门禁 L1–L5.5 通过者）────────────────
DYNAMIC_MIN_STYLE_N = 15
DYNAMIC_QUANTILES = {
    "composite": 0.70, "annual_1y": 0.55, "sharpe_1y": 0.50, "sortino_1y": 0.50,
    "calmar_1y": 0.50, "non_negative_1y": 0.45, "fee": 0.85,
}
WINSOR_LOW = 0.05
WINSOR_HIGH = 0.95

# 动态门槛的"应用规则"。架构书字面要求全部维度同时通过；实测该口径把 2,050 只
# 基础通过者压到 40 只（联合概率约 0.2%），低于"每组最低配额 80"的设计意图。
# 因此默认改为：
#   综合分必须过绝对底线（P0-3 校准为 60）
#   + 其余 6 项至少 5 项过各自分位（k-of-n 相对比较，避免联合概率塌缩）
# 设为 "all" 可恢复架构书字面口径（结果写进 docs/screening-rules.md 的敏感性表）。
DYNAMIC_GATE_RULE = "composite_and_k_of_n"
# P0-3：k 从 4/6 提到 5/6。实测 2026-11-02 批次 stable 151 只中，4 维 24 只、
# 5 维 45 只、6 维 82 只；k=5 会真实挡掉 24 只，而不是像 k=4 那样只作伴随项。
# 6 维之间的共线（mdd-vol 0.89、calmar-mdd −0.62）已通过删维处理，k=5 不重复计票。
DYNAMIC_MIN_DIMENSIONS_PASSED = 5
DYNAMIC_DIMENSIONS = ("annual_1y", "sharpe", "sortino", "calmar", "non_negative", "fee")

# ── 双侧带滞回（ADR-050）────────────────────────────────────
# 只对"已在上批榜单里"的基金放宽到 exit 线，新进入者仍用 enter 线。
# 理由：实测 486 只动态母体里 48 只（9.9%）落在阈值 ±1 分内、8 只以 ≤0.26 分过线，
# 单侧线必然产生"名次没变、榜单变了"的抖动。
DYNAMIC_HYSTERESIS = {
    "composite": 5.0,      # 分
    "annual_1y": 0.50,     # 百分点
    "sharpe": 0.10,
    "sortino": 0.10,
    "calmar": 0.30,
    "non_negative_1y": 2.0,  # 百分点
    "fee": 0.08,           # 百分点（上限型：exit = enter + delta）；cost_1y 尺度
}

# ── 分位母体口径（ADR-052）──────────────────────────────────
# data_qualified：L1 通过（有 1 年指标、采样达标）；v9 及以前作为默认。
# investable：L1–L5.5 通过者（v10 默认）；排除 L2 不可申购、L3 长锁定/定开、
#   L4 缺资料、L5 不成熟/太小者。
# all：v1 行为（全量 7,459）。
# v10：分位母体与动态阈值母体统一为 investable（L1–L5.5 通过者），
# 排除 L1 无指标、L2 不可投、L3 长锁定/定开、L4 缺费率规模、L5 不成熟/太小者；
# 这样 87/88 个月摊余成本定开债不会污染 q70/q85，也不会把无法买入的基金算进分布。
PERCENTILE_POOL = "investable"
# 动态分位母体口径：investable = L1–L5.5 通过者（v2 正确口径）；
# fixed_passed = v1 行为（先留固定线通过者再算分位，与文档自相矛盾）。
DYNAMIC_MOTHER = "investable"

# 风格绝对底线（收益取 max、风险费用取 min）
# fee = 1 年持有期总成本上限（pct，ADR-059）；数值按 cost_1y 尺度重标：
# 全市场 cost_1y p50=0.65 / p75=0.96 / p90=1.20，漏斗内 p50=0.50 / p85=0.95。
STYLE_FLOORS = {
    # P0-1：稳定风格用 non_negative（非负日占比）；增强三风格仍用 positive
    # （上涨日参与率），因为其收益分布厚尾、0 日占比低，且增强准入口径未改。
    "DEFAULT":            {"annual": 2.0, "calmar": 2.5, "mdd": 0.020, "vol": 0.050,
                           "non_negative": 70.0, "fee": 1.00, "sharpe": 0.8, "sortino": 0.8},
    "bond_rate":          {"annual": 2.0, "calmar": 2.5, "mdd": 0.020, "vol": 0.050,
                           "non_negative": 65.0, "fee": 1.00, "sharpe": 0.5, "sortino": 0.5},
    "bond_credit":        {"annual": 2.0, "calmar": 2.5, "mdd": 0.020, "vol": 0.050,
                           "non_negative": 72.0, "fee": 1.00, "sharpe": 0.8, "sortino": 0.8},
    "bond_pure":          {"annual": 2.0, "calmar": 2.8, "mdd": 0.018, "vol": 0.045,
                           "non_negative": 74.0, "fee": 0.95, "sharpe": 0.8, "sortino": 0.8},
    "bond_short":         {"annual": 2.0, "calmar": 4.0, "mdd": 0.008, "vol": 0.025,
                           "non_negative": 86.0, "fee": 0.95, "sharpe": 1.0, "sortino": 1.0},
    "bond_cash_enhanced": {"annual": 2.0, "calmar": 4.0, "mdd": 0.015, "vol": 0.040,
                           "non_negative": 80.0, "fee": 0.95, "sharpe": 1.0, "sortino": 1.0},
    "bond_index":         {"annual": 2.0, "calmar": 2.5, "mdd": 0.020, "vol": 0.050,
                           "non_negative": 72.0, "fee": 0.85, "sharpe": 0.8, "sortino": 0.8},
    "bond_primary_enhanced":   {"annual": 4.0, "calmar": 2.4, "mdd": 0.080, "vol": 0.120,
                                "positive": 55.0, "fee": 1.20, "sharpe": 1.0, "sortino": 1.0},
    "bond_secondary_enhanced": {"annual": 5.0, "calmar": 2.4, "mdd": 0.080, "vol": 0.120,
                                "positive": 55.0, "fee": 1.25, "sharpe": 1.0, "sortino": 1.0},
    "bond_convertible":        {"annual": 3.5, "calmar": 3.0, "mdd": 0.030, "vol": 0.070,
                                "positive": 54.0, "fee": 1.30, "sharpe": 1.0, "sortino": 1.0},
}

# ── L10 增强债独立准入（至少七项硬比较）────────────────────
ENHANCED_ADMISSION = {
    "bond_primary_enhanced":   {"annual": 4.5, "sharpe": 1.1, "calmar": 2.6, "mdd": 0.080,
                                "positive": 55.0, "fee": 1.20, "scale": 2.0},
    "bond_secondary_enhanced": {"annual": 5.5, "sharpe": 1.1, "calmar": 2.6, "mdd": 0.080,
                                "positive": 55.0, "fee": 1.25, "scale": 2.0},
    "bond_convertible":        {"annual": 4.0, "sharpe": 1.1, "calmar": 3.2, "mdd": 0.030,
                                "positive": 54.0, "fee": 1.30, "scale": 2.0},
}


# ── L6E 增强债"相对口径"（ADR-031）────────────────────────────
# 背景：绝对准入（一级 annual≥4.5% / 二级 5.5% / 转债 4.0%，Sharpe≥1.1、Calmar≥2.6）
# 在 2026 年债市（1 年收益中枢 2–4%）位于各风格 p95 以上，实测 688 只进入 L6E、
# 通过 0 只——门槛是绝对的，与同风格的真实分布脱节。
# 改为：同风格分位（n≥30 才用，否则回退全局）+ 绝对地板（防止负收益/高回撤混入）
# + 正式分下限 + 每风格配额。原严格口径保留为诊断（thresholds_json.enhanced_strict）。
# 增强债年化改用混合口径（ADR-035）：1 年年化与 3 年年化各占一半。
# 只用一个 1 年窗口会把"某一年恰好赶上行情的基金"当成能力，也会把"这一年正好逆风"的
# 长期优秀基金一刀切掉；混两年窗口能显著降低单一年份的偶然性。
# 3 年窗口需要连续历史，历史不足时退回 1 年并在 checks 里标 window=1y_only（不静默当 3 年用）。
ENHANCED_ANNUAL_BLEND = {"w1y": 0.5, "w3y": 0.5}

ENHANCED_RELATIVE = {
    "min_peers": 30,
    "quantiles": {"annual_1y": 0.60, "calmar": 0.60, "sharpe": 0.50,
                  "positive": 0.45, "mdd": 0.75, "vol": 0.75, "fee": 0.85},
    "floors": {
        "bond_primary_enhanced":   {"annual": 1.0, "calmar": 0.8, "sharpe": 0.2,
                                    "positive": 52.0, "mdd": 0.080, "vol": 0.120,
                                    "fee": 1.20, "scale": 1.0},
        "bond_secondary_enhanced": {"annual": 1.0, "calmar": 0.8, "sharpe": 0.2,
                                    "positive": 51.0, "mdd": 0.080, "vol": 0.120,
                                    "fee": 1.20, "scale": 1.0},
        "bond_convertible":        {"annual": 1.0, "calmar": 0.8, "sharpe": 0.2,
                                    "positive": 50.0, "mdd": 0.050, "vol": 0.150,
                                    "fee": 1.30, "scale": 1.0},
    },
    "formal_score_min": 55.0,
    "quota_per_style": 40,
    "quota_total": 110,
}

# ── 数据质量分（ADR-054，替代 v1 的"信心分"）──────────────────
# v1 的 12 项边际分实测 205 只入选者 median 10.416 / 满分 10.454，99.8% 落在 high，
# 且是 _rank 的第一排序键（恒等）→ 等于没有排序。v2 改为只描述"数据够不够干净"，
# 0–100 分，只做展示徽章，不参与排序，也不因缓释而打折。
# 每项：(达标值, 分值, 方向)；方向 ratio = v/target（higher）或 target/v（lower）。
DATA_QUALITY_ITEMS = {
    "nav_points": (700, 30.0, "higher"),      # 近 3 年日频约 730 点
    "gap_max_days": (3, 20.0, "lower"),       # 最大采样间隔
    "freshness_days": (5, 20.0, "lower"),     # 净值新鲜度
    "metrics_filled": (10, 15.0, "higher"),   # 8 项核心指标非空计数
    "profile_filled": (3, 15.0, "higher"),    # 规模/经常性费率/经理 三项非空
}
DATA_QUALITY_HIGH = 80.0
DATA_QUALITY_MID = 60.0
MANAGER_MGOLD_MIN = 7.2

# ── 评分权重 ───────────────────────────────────────────────
SHORT_CORE_WEIGHTS = {"return_1m": 0.20, "return_3m": 0.20, "sharpe": 0.30,
                      "sortino": 0.15, "vol": 0.15}
# P0-2：3 年标签必须显示有效覆盖；>=95% 视为完整 3 年（2.795/3=93.2% 不能
# 冒充 3 年），>=60% 才允许进入长期评分。
FULL_COVERAGE_RATIO = 0.95
MIN_LONG_COVERAGE = 0.60

# P0-5：long_core 的 3y/5y 风险权重合计 >= 50%，1y 风险只作辅助。
LONG_CORE_WEIGHTS = {"annual_3y": 0.22, "consistency": 0.13, "mdd_3y": 0.18,
                     "vol_3y": 0.10, "calmar_3y": 0.10, "mdd_5y": 0.12,
                     "sortino_3y": 0.05, "calmar": 0.05, "mdd": 0.05}
CORE_COMBO = {"short": 0.40, "long": 0.60}
SHORT_FORMAL_WEIGHTS = {"return_6m": 0.16, "return_1y": 0.14, "annual_1y": 0.14,
                        "sharpe": 0.14, "sortino": 0.12, "mdd": 0.10, "vol": 0.10,
                        "non_negative": 0.10}
LONG_FORMAL_WEIGHTS = {"annual_1y": 0.12, "annual_3y": 0.10, "return_1y": 0.08,
                       "return_3y": 0.05, "sharpe": 0.08, "sortino": 0.06,
                       "calmar": 0.08, "mdd": 0.04, "mdd_3y": 0.12,
                       "vol_3y": 0.08, "calmar_3y": 0.10, "non_negative": 0.04,
                       "fee": 0.03, "scale": 0.02}
FORMAL_COMBO = {"short": 0.45, "long": 0.55}

# ── 排序分（ADR-062）────────────────────────────────────────
# 综合分是"质量"（波动/回撤/夏普，长窗口），6 个月收益分位是"动量"（中窗口）。
# 实测（2021–2025 五个 6-30 队列、稳健池取前 118 只、前瞻 365 天）：
#   纯综合分 4.24/3.14/4.09/3.02/2.16（年化%，2021→2025），
#   0.85/0.15 混权 4.42/3.17/4.29/3.18/2.21，前瞻夏普 −0.00/−0.04/−0.05/−0.09/+0.03，
#   命中率（跑赢池中位数）46–64% → 51–72%。权重 0.10 收益增益更小、0.25 夏普开始掉。
# 只影响排序，不参与准入（固定线/动态线仍看综合分与各项绝对指标）。
RANK_WEIGHTS = {"composite": 0.85, "return_6m": 0.15}

# ── L9 名额 ────────────────────────────────────────────────
# v2：两个值仍留作兜底常量，但引擎改读 config/app.yaml 的 screen.formal_target_max /
# screen.group_min_ratio（v1 里这两个配置键是死键，改了不生效）。
FORMAL_TARGET_MAX = 320
GROUP_MIN_RATIO = 0.25

# ── 榜单结构（ADR-051 / ADR-053）────────────────────────────
BOARD_STABLE = "bond_stable"
BOARD_ENHANCED = "bond_enhanced"
BOARD_WATCH = "bond_watch"      # v2 新增：合格但当期买不到（L2）
BOARD_LOCKED = "bond_locked"    # 只放"质量合格但持有期受限"者
BOARD_ROOKIE = "bond_rookie"    # v3 新增：历史 1–3 年、缺 3 年窗口的新锐榜
BOARD_CORE = "bond_core"        # v5 新增：从正式候选跨风格精选 50 只（ADR-071）
BOARD_CORE10 = "bond_core10"    # v6 新增：收益精选 10 只（ADR-072），风格限额后按排序分取前 10
BOARD_LONG = "bond_long"        # P1 新增：长期核心观察榜（3y 完整 + 5y 覆盖 >=60%）

# ── 新锐榜（P1-1）：1–3 年历史、没有 3 年年化的基金单开一榜 ──────────────
# 背景：v2 里 long_core 的 annual_3y(.30)+consistency(.25) 缺失按 0 计，等效要求 ~3 年历史；
# 2,910 只基础通过者中 685 只无 3 年年化、665 只（97%）composite<58、最高仅 65.46。
# 等于把"成立 1–3 年"整段市场切掉。新锐榜用纯 1 年窗口打分，单独设配额与数据质量门槛。
ROOKIE_MIN_HISTORY_DAYS = 365
ROOKIE_MAX_HISTORY_DAYS = 1095
ROOKIE_WEIGHTS = {"annual_1y": 0.35, "sharpe": 0.25, "sortino": 0.15,
                  "calmar": 0.15, "non_negative": 0.10}
ROOKIE_MIN_SCORE = 60.0          # 新锐分（0–100，同风格分位加权）
ROOKIE_DATA_QUALITY_MIN = 70.0   # 历史短，数据必须干净
ROOKIE_QUOTA = 30

# ── 核心精选榜（v5 / ADR-071）：不新增 alpha 信号，只把正式候选按排序分集中 ──
# 十队列回测（含家族/公司折叠）：核心 50 只前瞻年化 3.60% / 夏普 2.75，
# 完整正式榜 3.43% / 2.54，胜 8/10 个建仓时点；30 只收益略高但集中度风险大。
# 上限：增强风格与可转债各 0（见下），避免精选榜被高 beta 风格占满。
CORE_QUOTA = 50
# P0-6：核心榜只收纯债/短债/信用/利率/指数；增强债留在增强榜。
CORE_ENHANCED_CAP = 0
CORE_CONVERTIBLE_CAP = 0
# P0-6b：跨风格混排时，短债/货币增强会靠低波动占满核心榜。核心榜只收
# 纯债/短债/信用/利率/指数，并按风险预算给非纯债风格设上限；纯债用剩余名额。
CORE_STYLES = ("bond_pure", "bond_short", "bond_credit", "bond_rate", "bond_index")
CORE_STYLE_CAPS = {"bond_short": 10, "bond_credit": 6, "bond_rate": 4, "bond_index": 6}

# ── 收益精选 10 榜（v6 / ADR-072）：同一套排序分，风格限额后集中到 10 只 ───
# 十队列回测（含家族/公司折叠、并剔除当前 >90 天持有期/定开封闭）：10 只前瞻
# 年化 3.99% / 夏普 2.95 / 回撤 0.84%，核心 50 为 3.57% / 2.72 / 0.94%；
# 逐队列差均值 +0.421pp（sd 0.396，t=3.36，10/10 胜）。
# 上限：增强与可转债各 0（见下）；风格上限 + 相关性惩罚控制集中度。
CORE10_QUOTA = 10
# P0-6：收益精选 10 只同样只收稳定风格，避免 30% 二级债基。
CORE10_ENHANCED_CAP = 0
CORE10_CONVERTIBLE_CAP = 0
# 10 只组合的风险预算：短债 2、信用 2、利率 1、指数 1，其余 4 只给纯债。
CORE10_STYLE_CAPS = {"bond_short": 2, "bond_credit": 2, "bond_rate": 1, "bond_index": 1}
# P2-2：core10 不再只按排序分截前 10。选样时对“与已选基金的最大相关”罚分，
# 并硬性拒绝最大相关 >0.85 的候选；目标有效 bet>=1.8（债券基金共同利率因子
# 很强，1.8 是方向性目标，达不到时报告实际值而不是假装达标）。
CORE10_CORR_PENALTY = 50.0
CORE10_MAX_PAIR_CORR = 0.85
CORE10_TARGET_EFFECTIVE_BETS = 1.8
CORE10_MIN_COMMON_OBS = 40
# P2-2b：贪婪选样后的有界局部搜索——单次换入最多让出 2 分排序分，
# 只有 effective bets 提升 >=0.005 才接受；最多 3 轮。仍达不到 1.8 就保留
# downgraded 标记，不为了“看起来分散”牺牲选样质量。
CORE10_SWAP_MAX_SCORE_LOSS = 2.0
CORE10_SWAP_MIN_GAIN = 0.005
CORE10_SWAP_MAX_ROUNDS = 3

# ── 长期核心观察榜（P1）：不替换稳健榜，只把“3 年完整 + 5 年覆盖 >=60%”的
# 核心风格单独列出，验证“长期证据更充分”是否真的带来更好的持有体验。
# 评分 0.6×long_formal + 0.4×long_core：long_formal 含 3y/5y 收益与风险，
# long_core 含 1m/3m/6m/1y/3y/5y 的收益、夏普、回撤；不让 1 年短窗口主导。
LONG_QUOTA = 20
LONG_MIN_5Y_COVERAGE = 0.60
LONG_MANAGER_TENURE_MIN_DAYS = 730
# P1-8：长期榜不只看“任职>=730 天”，还要求现任经理覆盖 3 年业绩窗口 >=80%；
# 730 天只覆盖 66.7%，不足以证明 3 年业绩由当前团队创造。
LONG_MANAGER_COVERAGE_MIN = 0.80
LONG_SHARE_SCALE_MIN = 2.0
LONG_SCORE_WEIGHTS = {"long_formal": 0.6, "long_core": 0.4}
FORMAL_BOARDS = (BOARD_STABLE, BOARD_ENHANCED)
ALL_BOARDS = (BOARD_STABLE, BOARD_ENHANCED, BOARD_CORE, BOARD_CORE10, BOARD_LONG,
              BOARD_WATCH, BOARD_LOCKED, BOARD_ROOKIE)
# 正式榜每 family 只留 1 只（代表 = 总成本最低者；平手取 formal 最高）
FAMILY_CAP = 1
COMPANY_CAP = 2   # P0-6：单公司最多 2 只，避免 3 只长城同榜
# 未纳入宇宙的口径缺口（对外必须显式声明，不许沉默）
UNCOVERED_SCOPE = {"index_bond": 0, "as_of": "2026-11-02",
                   "note": "指数型-固收（债券指数/ETF/联接）679 只已纳入 universe（ADR-061），"
                           "按 bond_index 独立分位组比较；未覆盖：QDII 债基 166 只、"
                           "偏债混合 1,475 只（2026-11-02 目录；非债券型口径，"
                           "试点见 docs/hybrid-pilot-2026-11.md）"}
GRADE_BANDS = [("A", 75.0), ("B+", 68.0), ("B", 60.0), ("C", 52.0)]


def style_floor(style_code: str) -> dict:
    return STYLE_FLOORS.get(style_code) or STYLE_FLOORS["DEFAULT"]