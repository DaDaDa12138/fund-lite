"""报告 JSON 的结构契约（P2-12）：写入前校验，避免前端/门禁读到缺字段的报告。"""
from __future__ import annotations

from typing import Any

REQUIRED_TOP = ("schema_version", "run_id", "generated_at", "run", "counts",
                "facts", "sections", "markdown")
REQUIRED_FACTS = ("l9_pass", "published", "folded", "formal_by_board",
                  "board_counts", "cohorts", "matured_formal_observations",
                  "data_asof")
REQUIRED_SECTIONS = ("conclusion", "scope", "funnel", "long", "formal",
                     "benchmark", "shadow", "portfolio", "core10", "capacity",
                     "fees", "risk", "evidence", "method")


def validate(report: dict[str, Any]) -> list[str]:
    """返回错误列表；空列表表示结构完整。只校验结构，不校验数字真伪。"""
    errors = []
    for key in REQUIRED_TOP:
        if key not in report:
            errors.append("缺少顶层字段 %s" % key)
    facts = report.get("facts") or {}
    for key in REQUIRED_FACTS:
        if key not in facts:
            errors.append("缺少 facts.%s" % key)
    sections = report.get("sections") or []
    ids = [s.get("id") for s in sections]
    for sid in REQUIRED_SECTIONS:
        if sid not in ids:
            errors.append("缺少章节 %s" % sid)
    if len(ids) != len(set(ids)):
        errors.append("章节 id 重复")
    for s in sections:
        if not s.get("title") or not isinstance(s.get("markdown"), str):
            errors.append("章节 %s 缺少 title/markdown" % s.get("id"))
    if not isinstance(report.get("markdown"), str) or not report["markdown"].strip():
        errors.append("markdown 为空")
    return errors
