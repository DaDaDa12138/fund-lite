"""L2 融合层编排：身份 → 份额类别 → 画像/费率/持有期/可购 → 持仓与风格 → 净值覆盖。"""
from __future__ import annotations

from typing import Any

from .. import fundtypes
from . import coverage, identity, manager, nav, profile, style


def run_fuse(conn, cfg, run_id: str, business_date: str,
             types: set[str] | None = None) -> dict[str, Any]:
    types = set(fundtypes.resolve(types, cfg))
    quality = cfg.get("quality", default={}) or {}
    max_lock = int(quality.get("max_short_lock_days", 90))
    nav_age = int(quality.get("nav_age_limit_days", 10))
    max_gap = int(cfg.get("risk", default={}).get("max_gap_days", 14))
    tol = (cfg.field_policy.get("tolerances") or {})
    scale_family_rel = float(tol.get("scale_family_rel", 0.15))

    result: dict[str, Any] = {}
    result["identity"] = identity.run_identity(conn, run_id, business_date, types)
    result["purge"] = identity.purge_non_universe(conn, types)
    result["share_classes"] = identity.run_share_classes(conn, business_date)
    result["profile"] = profile.run_profiles(conn, business_date, max_lock, scale_family_rel)
    result["manager"] = manager.run_managers(conn, business_date, types)
    style_cov = float(quality.get("style_holdings_min_coverage", 0.80))
    result["style"] = style.run_style(conn, business_date, style_cov)
    result["coverage"] = nav.run_coverage(conn, run_id, business_date, nav_age, max_gap)
    report = coverage.build_report(conn, run_id)
    report["stages"] = result
    return report
