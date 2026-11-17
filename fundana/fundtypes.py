"""基金类型注册表：一种类型 = 一套「怎么认 + 能筛什么 + 怎么分风格 + 出哪些榜」。

为什么需要这一层
----------------
采集层（collect）从 ADR-061 起就按 type_text 前缀工作，融合层的 identity /
purge_non_universe 也早已接受 types 集合；但筛选、指标、API、跨源核对、经理证据
等 10 处仍把 type_group='bond' 写死在 SQL 里。结果是一种类型要加进来，必须改遍
全仓、漏一处就静默产出一份空榜或混入别类型的数据——这正是「插件化」要消除的东西。

约定
----
1. 登记 ≠ 支持。未实现的类型在 resolve() 里直接报错，并列出缺口，
   不允许「配置里写了但跑出来是空榜」。
2. 新增一种类型，必须同时补齐四件事：采集域（净值口径/费率/持仓解析器）、
   风格体系、阈值标定（固定线与分位母体）、榜单语义。缺一项名单就不可比。
3. 默认类型来自 config/app.yaml 的 universe.types，缺省 bond（ADR-077）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# ── type_text → type_group ──────────────────────────────────
# 顺序有意义：先匹配到的前缀生效（「指数型-固收」必须排在「指数型」前面，见下）。
TYPE_PREFIXES: list[tuple[str, str]] = [
    ("债券型", "bond"),
    ("货币型", "money"),
    ("股票型", "equity"),
    ("混合型", "hybrid"),
    ("指数型", "index"),
    ("QDII", "qdii"),
    ("FOF", "fof"),
]

# 债券指数基金在东财被归到「指数型-固收」（实测 1,356 只，含场内 ETF 与场外联接），
# 按协会口径它们属于债券基金。若不特判，type_text LIKE '债券型%' 的 universe 会把
# 整个被动债基市场漏掉（见 ADR-061）。
BOND_INDEX_TYPE = "指数型-固收"

# 某个 type_group 额外的 type_text 前缀（bond 额外含「指数型-固收」）
EXTRA_PREFIXES: dict[str, tuple[str, ...]] = {"bond": (BOND_INDEX_TYPE,)}


def type_prefixes(types: str) -> list[str]:
    """某 type_group 的全部 type_text 前缀。"""
    return [p for p, g in TYPE_PREFIXES if g == types] + list(EXTRA_PREFIXES.get(types, ()))


def type_group(type_text: str | None) -> str:
    """把东财的 type_text 归到 type_group；认不出的归 other（identity 会隔离）。"""
    if not type_text:
        return "other"
    if type_text.startswith(BOND_INDEX_TYPE):
        return "bond"
    for prefix, group in TYPE_PREFIXES:
        if type_text.startswith(prefix):
            return group
    return "other"


# ── 类型插件 ────────────────────────────────────────────────
# 注意：styles / boards 与 screen/thresholds.py 的常量必须一致，由
# tests/test_fundtypes.py 的 test_registry_matches_thresholds 守住。
BOND_STYLES: tuple[str, ...] = (
    "bond_convertible", "bond_secondary_enhanced", "bond_primary_enhanced", "bond_short",
    "bond_index", "bond_rate", "bond_credit", "bond_cash_enhanced", "bond_pure",
)
BOND_BOARDS: tuple[str, ...] = (
    "bond_stable", "bond_enhanced", "bond_core", "bond_core10", "bond_long",
    "bond_watch", "bond_locked", "bond_rookie",
)


@dataclass(frozen=True)
class FundType:
    """一种基金类型的插件描述。implemented=False 表示只登记、不能跑。"""

    code: str
    label: str
    screen_profile: str | None = None
    styles: tuple[str, ...] = ()
    boards: tuple[str, ...] = ()
    implemented: bool = False
    gaps: tuple[str, ...] = ()

    def prefixes(self) -> list[str]:
        return type_prefixes(self.code)


REGISTRY: dict[str, FundType] = {
    "bond": FundType(
        code="bond", label="债券型", screen_profile="bond_v1",
        styles=BOND_STYLES, boards=BOND_BOARDS, implemented=True),
    "money": FundType(
        code="money", label="货币型",
        gaps=("净值口径是万份收益 / 七日年化，不是单位净值（fund_nav_daily 语义不兼容）",
              "没有久期/信用/转债暴露，现有七榜语义不适用",
              "固定线与分位母体未标定")),
    "hybrid": FundType(
        code="hybrid", label="混合型",
        gaps=("净值可直接复用 B1 快照与 pzd 历史，但费率/持仓解析按债基口径写",
              "风格体系未建（现只有 bond_* 九类）",
              "阈值与七榜语义未标定",
              "2026-11 偏债混合 200 只试点：1 年 MDD 中位 4.96%、Vol 4.73%，"
              "债基 8 条固定线仅 2/196 全过；不并入 bond universe（ADR-093）")),
    "equity": FundType(
        code="equity", label="股票型",
        gaps=("净值可复用；持仓 zqcc 是债券持仓，股票持仓未接",
              "风格体系未建（需成长/价值/行业等维度）",
              "阈值与榜单语义未标定")),
    "index": FundType(
        code="index", label="指数型",
        gaps=("「指数型-固收」已被 bond 吸收（ADR-061），其余指数型未拆分子类",
              "跟踪误差/费率是主要维度，现有评分体系不覆盖")),
    "qdii": FundType(
        code="qdii", label="QDII",
        gaps=("净值 T+1/T+2 滞后，nav_age_limit_days 与新鲜度阈值需另设",
              "汇率与境外市场休市日，交易日历与 A 股不同")),
    "fof": FundType(
        code="fof", label="FOF",
        gaps=("底层是基金持仓，需新增持仓穿透采集",
              "双重收费使 cost_1y 口径失真")),
}

DEFAULT_TYPES: tuple[str, ...] = ("bond",)


def get(code: str) -> FundType:
    ft = REGISTRY.get(code)
    if ft is None:
        raise ValueError(f"未知基金类型 {code!r}；已注册：{', '.join(sorted(REGISTRY))}")
    return ft


def implemented_codes() -> tuple[str, ...]:
    return tuple(c for c, ft in REGISTRY.items() if ft.implemented)


def _config_types(cfg: Any) -> Any:
    """从配置对象取 universe.types；测试桩 / dict / None 都安全降级为 None。"""
    if cfg is None:
        return None
    if isinstance(cfg, dict):
        uni = cfg.get("universe")
        return (uni or {}).get("types") if isinstance(uni, dict) else None
    getter = getattr(cfg, "get", None)
    if not callable(getter):
        return None
    try:
        return getter("universe", "types", default=None)
    except TypeError:          # 只接受 (key, default) 的简化桩
        return None


def resolve(types: str | Iterable[str] | None = None,
            cfg: Any = None) -> tuple[str, ...]:
    """把「用户给的 / 配置里的 / 缺省」三种来源收敛成一个有序类型元组。

    优先级：显式 types > config universe.types > DEFAULT_TYPES。
    未实现或未登记的类型直接报错（含缺口说明），不静默降级成空榜。
    """
    raw: Any = types
    if raw is None:
        raw = _config_types(cfg)
    if raw is None:
        raw = DEFAULT_TYPES
    if isinstance(raw, str):
        items = [t.strip() for t in raw.split(",")]
    else:
        items = [str(t).strip() for t in raw]
    items = [t for t in items if t]
    if not items:
        items = list(DEFAULT_TYPES)
    if items == ["all"]:
        return implemented_codes()
    out: list[str] = []
    for code in items:
        ft = get(code)
        if not ft.implemented:
            raise ValueError(
                f"基金类型 {code!r}（{ft.label}）尚未实现；缺口："
                + "；".join(ft.gaps))
        if code not in out:
            out.append(code)
    return tuple(out)


def where(types: str | Iterable[str] | None = None, column: str = "type_group",
          cfg: Any = None) -> tuple[str, list[str]]:
    """生成 column IN (?,?) 片段与参数，供各层 SQL 直接拼接。"""
    codes = list(resolve(types, cfg))
    marks = ",".join("?" * len(codes))
    return f"{column} IN ({marks})", codes


def describe() -> list[dict[str, Any]]:
    """给 CLI / API 用的注册表快照。"""
    return [{
        "code": ft.code, "label": ft.label, "implemented": ft.implemented,
        "screen_profile": ft.screen_profile,
        "prefixes": ft.prefixes(),
        "styles": len(ft.styles), "boards": len(ft.boards),
        "gaps": list(ft.gaps),
    } for ft in REGISTRY.values()]


def uncovered(conn) -> list[dict[str, Any]]:
    """已登记但未实现的类型及最新公开目录里的数量；只用于披露范围，不进入债券榜。

    优先用 src_directory 最新快照按 type_text 归类：fund 表只装已纳入 universe 的
    债券基金，拿它数混合/QDII 会全是 0。没有目录快照时才退回 fund 表。
    """
    latest = conn.execute("SELECT MAX(business_date) AS d FROM src_directory").fetchone()
    counts: dict[str, int] = {}
    if latest and latest["d"]:
        for row in conn.execute("SELECT type_text FROM src_directory WHERE business_date=?",
                                (latest["d"],)):
            group = type_group(row["type_text"])
            counts[group] = counts.get(group, 0) + 1
    else:
        counts = {r["type_group"]: int(r["n"]) for r in conn.execute(
            "SELECT type_group, COUNT(*) AS n FROM fund GROUP BY 1")}
    out = []
    for ft in REGISTRY.values():
        if ft.implemented:
            continue
        out.append({"code": ft.code, "label": ft.label,
                    "funds": counts.get(ft.code, 0), "gaps": list(ft.gaps)})
    return out

