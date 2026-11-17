"""单一原因边界缓释（§11.9 / ADR-047）。

规则极其克制，原因是缓释本质上是"给差一点点的基金开后门"：
- **恰好一项**硬比较失败才可试，两项及以上一律不缓释（避免把弱基金拼成强基金）；
- 只对三条边界开放：规模、1 年年化、日胜率，且必须同时满足综合分门槛；
- 缓释不是"通过"：confidence 乘 0.9、reason_codes 记 relief:<type>，前端与决策卡可见；
- 门槛值全部来自常量，随 run 冻结进 thresholds_json，改规则必须换 strategy 版本。
"""
from __future__ import annotations

from typing import Any

SCALE_RELIEF_MIN_BILLION = 1.8
SCALE_COMPOSITE_MIN = 80.0
ANNUAL_RELIEF_GAP_PP = 0.15
ANNUAL_COMPOSITE_MIN = 80.0
NON_NEGATIVE_RELIEF_GAP_PP = 0.05
NON_NEGATIVE_COMPOSITE_MIN = 72.0
CONFIDENCE_FACTOR = 0.9

# P0-1 后固定线已从 positive 改为 non_negative；缓释必须跟同一口径，
# 否则 non_negative 近失永远进不了 try_relief（旧 positive 分支成了死路）。
ELIGIBLE = ("scale", "annual_1y", "non_negative")


def try_relief(checks: dict[str, dict[str, Any]], fails: list[str],
               score: dict[str, Any]) -> tuple[bool, str | None, dict[str, Any]]:
    """返回 (allowed, relief_type, detail)。detail 一律回传，供审计。"""
    comp = score.get("composite")
    detail: dict[str, Any] = {"failed": list(fails), "composite": comp}
    if len(fails) != 1:
        detail["blocked"] = "not_single_failure"
        return False, None, detail
    key = fails[0]
    if key not in ELIGIBLE:
        detail["blocked"] = "type_not_eligible"
        return False, None, detail
    c = checks.get(key) or {}
    value = c.get("value")
    threshold = c.get("min") if "min" in c else c.get("max")
    detail.update({"type": key, "value": value, "threshold": threshold})
    if value is None or comp is None:
        detail["blocked"] = "missing_value"
        return False, None, detail
    if key == "scale":
        if value >= SCALE_RELIEF_MIN_BILLION and comp >= SCALE_COMPOSITE_MIN:
            detail["rule"] = "scale>=%.1f 且 综合>=%.0f" % (SCALE_RELIEF_MIN_BILLION, SCALE_COMPOSITE_MIN)
            return True, key, detail
        detail["blocked"] = "scale_or_composite_below_relief"
        return False, None, detail
    gap = (threshold - value) if threshold is not None else None
    detail["gap"] = None if gap is None else round(gap, 6)
    if key == "annual_1y":
        if gap is not None and gap <= ANNUAL_RELIEF_GAP_PP and comp >= ANNUAL_COMPOSITE_MIN:
            detail["rule"] = "年化差<=%.2fpp 且 综合>=%.0f" % (ANNUAL_RELIEF_GAP_PP, ANNUAL_COMPOSITE_MIN)
            return True, key, detail
        detail["blocked"] = "annual_gap_or_composite_below_relief"
        return False, None, detail
    # non_negative
    if gap is not None and gap <= NON_NEGATIVE_RELIEF_GAP_PP and comp >= NON_NEGATIVE_COMPOSITE_MIN:
        detail["rule"] = "非负日占比差<=%.2fpp 且 综合>=%.0f" % (NON_NEGATIVE_RELIEF_GAP_PP, NON_NEGATIVE_COMPOSITE_MIN)
        return True, key, detail
    detail["blocked"] = "non_negative_gap_or_composite_below_relief"
    return False, None, detail
