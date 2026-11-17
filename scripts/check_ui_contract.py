#!/usr/bin/env python3
"""前端契约自检：列头说明、排序白名单、SQL 列名三者是否对得上。

为什么需要这个脚本：新增一个指标列要同时改三个地方——
  1) fundana/api/metrics_meta.METRICS（列头 ⓘ 的文案）
  2) fundana/api/server.py 的 sort_map / fundana/api/locked.py 的 SORT_MAP（能不能点）
  3) 真正 SELECT 出来的列（点得动但点下去 500，比不能点更糟）
少改任何一处，页面表现都是"看起来正常、其实坏了"。这里用静态检查把它们对齐。

退出码 0 = 全部一致；1 = 有缺口（打印明细）。
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fundana.api import metrics_meta  # noqa: E402

# 列头不是"指标"但同样可排序/需要有标题的键
NON_METRIC_KEYS = {"code", "name", "style", "rank", "board", "lockup"}

# SQL 别名 -> 表名（新增 JOIN 时必须登记，否则检查不到）
SERVER_ALIASES = {
    "p": "screen_pick", "f": "fund", "s": "fund_share_class", "d": "screen_decision",
    "fp": "fund_profile", "m": "fund_metric", "w1": "fund_metric", "w2": "fund_metric",
    "w3": "fund_metric", "w4": "fund_metric", "m3": "fund_metric", "m5": "fund_metric",
    "fm": "fund_fee_meta", "t": "fund_tail_metric",
    "n": "fund_nav_daily", "hp": "fund_holding_policy", "i": "screen_run_input",
}
LOCKED_ALIASES = dict(SERVER_ALIASES)
LOCKED_ALIASES.update({"h": "fund_holding_policy"})


def _columns(db_path: Path) -> dict[str, set[str]]:
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        out[name] = {r[1] for r in conn.execute('PRAGMA table_info("%s")' % name)}
    conn.close()
    return out


def _sort_maps() -> tuple[dict[str, str], dict[str, str]]:
    server = (ROOT / "fundana/api/server.py").read_text(encoding="utf-8")
    block = server[server.index("sort_map = {"):server.index("}", server.index("sort_map = {"))]
    smap = dict(re.findall(r'"([a-z0-9_]+)":\s*"([A-Za-z0-9_.]+)"', block))

    locked = (ROOT / "fundana/api/locked.py").read_text(encoding="utf-8")
    lblock = locked[locked.index("SORT_MAP = {"):locked.index("}", locked.index("SORT_MAP = {"))]
    lmap = dict(re.findall(r'"([a-z0-9_]+)":\s*"([A-Za-z0-9_.]+)"', lblock))
    return smap, lmap


def _ui_sort_keys() -> set[str]:
    app = (ROOT / "web/app.js").read_text(encoding="utf-8")
    return set(re.findall(r'sortTh\("([a-z0-9_]+)"', app))



# 指标键 -> 真实落库位置。新增指标列时必须在这里登记，
# 否则第 5 项检查会报"数据库里有列但字典里没有说明"。
DB_FIELD_MAP = {
    "ret_1w": ("fund_metric", "total_return_pct"),
    "ret_1m": ("fund_metric", "total_return_pct"),
    "ret_3m": ("fund_metric", "total_return_pct"),
    "ret_6m": ("fund_metric", "total_return_pct"),
    "ret_1y": ("fund_metric", "total_return_pct"),
    "annual_1w": ("fund_metric", "annual_dec"),
    "annual_1m": ("fund_metric", "annual_dec"),
    "annual_3m": ("fund_metric", "annual_dec"),
    "annual_6m": ("fund_metric", "annual_dec"),
    "annual_1y": ("fund_metric", "annual_dec"),
    "sharpe": ("fund_metric", "sharpe"),
    "sortino": ("fund_metric", "sortino"),
    "calmar": ("fund_metric", "calmar_dec"),
    "mdd_1y": ("fund_metric", "mdd_dec"),
    "vol": ("fund_metric", "vol_dec"),
    "positive": ("fund_metric", "positive_ratio_pct"),
    "flat": ("fund_metric", "flat_ratio_pct"),
    "non_negative": ("fund_metric", "non_negative_ratio_pct"),
    "n_obs": ("fund_metric", "n_obs"),
    "mdd_3y": ("fund_tail_metric", "mdd_dec"),
    "worst90": ("fund_tail_metric", "worst_rolling90d_pct"),
    "cvar5": ("fund_tail_metric", "cvar5_dec"),
    "tail_completeness": ("fund_tail_metric", "completeness"),
    "worst180": ("fund_tail_metric", "worst_rolling180d_pct"),
    "worst_day": ("fund_tail_metric", "worst_day_pct"),
    "worst_week": ("fund_tail_metric", "worst_week_pct"),
    "month_positive": ("fund_tail_metric", "positive_month_ratio_pct"),
    "underwater": ("fund_tail_metric", "max_underwater_obs"),
    "scale": ("fund_profile", "asset_scale_billion"),
    "fee": ("fund_profile", "recurring_fee_pct"),
    "manager_career": ("fund_profile", "manager_career_min_days"),
    "lockup": ("fund_holding_policy", "effective_days"),
}

# 由 JSON 派生、不对应单列的指标
DERIVED_KEYS = {
    "formal_score": "screen_decision.scores_json",
    "composite": "screen_decision.scores_json",
    "confidence": "screen_pick.confidence",
}

# 内部列：计算留痕 / 口径元数据 / 由其他列派生，不需要在页面上单列展示
INTERNAL_COLUMNS = {
    "fund_metric": {
        "fcode": "主键", "period_days": "窗口", "formula_contract": "口径版本",
        "nav_sig": "净值快照指纹", "start_date": "窗口起", "end_date": "窗口止",
        "years": "窗口年数（派生）", "dd_duration_obs": "水下观测数（未上页面）",
        "sharpe_t": "夏普 t 值（显著性检验用）", "var95_dec": "VaR 未上页面",
        "raw_sharpe": "封顶前原值", "raw_sortino": "封顶前原值", "raw_calmar": "封顶前原值",
        "sharpe_cap_hit": "是否触顶", "sortino_cap_hit": "是否触顶", "calmar_cap_hit": "是否触顶",
        "undefined_reasons_json": "缺失原因", "computed_at": "计算时间",
        "median_gap_days": "采样间隔中位数", "min_points_used": "最少点数门槛",
        "cvar5_dec": "周期级 CVaR；页面展示的是 3 年尾部 CVaR（cvar5）",
        "p75_gap_days": "采样间隔 p75",
    },
    "fund_tail_metric": {
        "fcode": "主键", "window_days": "窗口", "formula_contract": "口径版本",
        "nav_sig": "净值快照指纹", "start_date": "窗口起", "end_date": "窗口止",
        "worst_rolling90d_start": "区间起", "worst_rolling90d_end": "区间止",
        "worst_rolling180d_start": "区间起", "worst_rolling180d_end": "区间止",
        "worst_day_date": "日期", "worst_week_end_date": "日期",
        "months_total": "自然月总数", "months_positive": "正收益月数（派生）",
        "n_obs": "观测数（同 fund_metric.n_obs 语义）",
        "max_underwater_obs": "最长水下观测数（未上页面）",
        "ongoing_drawdown": "是否仍在回撤中", "undefined_reasons_json": "缺失原因",
        "computed_at": "计算时间",
    },
}


def _check_db_fields(cols: dict[str, set[str]]) -> int:
    """第 5 项：数据库里的每个数值列，要么有指标说明，要么明确声明是内部列。"""
    covered = {}
    for key, (table, column) in DB_FIELD_MAP.items():
        covered.setdefault(table, set()).add(column)
    failures = 0
    for table in ("fund_metric", "fund_tail_metric"):
        internal = set(INTERNAL_COLUMNS.get(table, {}))
        used = covered.get(table, set())
        unmapped = []
        for column in sorted(cols.get(table, set())):
            if column in used or column in internal:
                continue
            unmapped.append(column)
        print("[5] %s 字段字典覆盖：%d 列，%d 列有指标说明，%d 列声明为内部"
              % (table, len(cols.get(table, set())), len(used & cols.get(table, set())),
                 len(internal & cols.get(table, set()))))
        if unmapped:
            failures += 1
            print("    FAIL 以下列既没有指标说明也没登记为内部：", ", ".join(unmapped))
        else:
            print("    OK")
    # 反向：登记了但数据库里不存在的列（改名/删列后忘了改这里）
    for table, columns in covered.items():
        missing = sorted(c for c in columns if c not in cols.get(table, set()))
        if missing:
            failures += 1
            print("    FAIL %s 登记的列不存在：%s" % (table, ", ".join(missing)))
    derived = sorted(k for k in DERIVED_KEYS if k not in {m["key"] for m in metrics_meta.payload()["metrics"]})
    if derived:
        failures += 1
        print("    FAIL 派生指标不在字典里：", ", ".join(derived))
    return failures




def _frontend_api_paths() -> set[str]:
    """从 web/app.js 提取 api() 的第一个字符串字面量路径。

    前端有字符串拼接（"/funds/" + code），只取第一个字面量做前缀匹配；
    模板字符串里的 ${...} 同样截断。这不是运行时测试，而是“前端调用的路径
    至少在后端有对应路由”的静态契约，能挡住改路径只改一边的常见回归。
    """
    app = (ROOT / "web/app.js").read_text(encoding="utf-8")
    out: set[str] = set()
    for m in re.finditer(r"""api\(\s*(['"])(.*?)\1""", app, re.S):
        raw = m.group(2)
        if not raw.startswith("/"):
            continue
        raw = raw.split("${", 1)[0].split("?", 1)[0]
        out.add(raw or "/")
    return out


def _server_routes() -> tuple[set[str], set[str]]:
    """返回 (精确路由, 前缀路由)。只解析 server.py 的 if/elif 路由块。"""
    src = (ROOT / "fundana/api/server.py").read_text(encoding="utf-8")
    exact = set(re.findall(r'path\s*==\s*["\']([^"\']+)["\']', src))
    prefix = set(re.findall(r'path\.startswith\(\s*["\']([^"\']+)["\']', src))
    return exact, prefix


def _check_routes() -> int:
    """第 6 项：前端 api() 路径必须能在后端找到精确/前缀路由。"""
    front = _frontend_api_paths()
    exact, prefix = _server_routes()
    missing = []
    for path in sorted(front):
        full = path if path.startswith("/api/v1/") else "/api/v1" + path
        if full in exact:
            continue
        if any(full.startswith(p) for p in prefix):
            continue
        missing.append(path)
    print("[6] 前端 API 路径：%d 个，后端精确 %d / 前缀 %d" % (len(front), len(exact), len(prefix)))
    if missing:
        print("    FAIL 后端没有对应路由：", ", ".join(missing))
        return 1
    print("    OK")
    return 0


def check(db_path: Path) -> int:
    failures = 0
    cols = _columns(db_path)
    metric_keys = {m["key"] for m in metrics_meta.payload()["metrics"]}  # 含别名

    # 1) 前端用了 sortTh("x") 的键，必须在指标字典里（或属于非指标列）
    ui_keys = _ui_sort_keys()
    missing_meta = sorted(k for k in ui_keys if k not in metric_keys and k not in NON_METRIC_KEYS)
    print("[1] 列头说明：前端使用 %d 个指标键，字典 %d 条" % (len(ui_keys), len(metric_keys)))
    if missing_meta:
        failures += 1
        print("    FAIL 以下键没有说明文案：", ", ".join(missing_meta))
    else:
        print("    OK")

    # 2) 服务端排序白名单里的 alias.column 必须真实存在
    for label, smap, aliases in (
        ("榜单", _sort_maps()[0], SERVER_ALIASES),
        ("受限榜", _sort_maps()[1], LOCKED_ALIASES),
    ):
        print("[2] %s排序白名单：%d 个键" % (label, len(smap)))
        bad = []
        for key, expr in smap.items():
            if "." not in expr:
                continue
            alias, col = expr.split(".", 1)
            table = aliases.get(alias)
            if table is None:
                bad.append("%s -> 未知别名 %s" % (key, alias))
            elif col not in cols.get(table, set()):
                bad.append("%s -> %s.%s 不存在" % (key, expr, ""))
        if bad:
            failures += 1
            print("    FAIL " + "；".join(bad))
        else:
            print("    OK")

    # 3) 白名单键必须在指标字典里（保证点得动的列都有说明）
    smap = _sort_maps()[0]
    missing = sorted(k for k in smap if k not in metric_keys and k not in NON_METRIC_KEYS)
    print("[3] 排序键与字典对齐")
    if missing:
        failures += 1
        print("    FAIL 排序键缺说明：", ", ".join(missing))
    else:
        print("    OK")

    # 4) 每个 layer 说明都带数字（门槛必须写出来，不能只说"看规则"）
    layers = metrics_meta.layer_details()
    no_digit = [d["code"] for d in layers if not re.search(r"\d", d["detail"])]
    print("[4] 漏斗层说明：%d 层，含数值门槛" % len(layers))
    if no_digit:
        failures += 1
        print("    FAIL 以下层没有数值门槛：", ", ".join(no_digit))
    else:
        print("    OK")

    failures += _check_db_fields(cols)
    failures += _check_routes()

    print("\n结论：" + ("全部一致" if not failures else "%d 项不一致" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    default_db = ROOT / "var/db/fundana.db"
    raise SystemExit(check(Path(sys.argv[1]) if len(sys.argv) > 1 else default_db))
