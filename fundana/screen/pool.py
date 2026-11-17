"""研究优先池（§11.9）：24–36 只"值得人工再看一眼"的基金。

它不是榜单：不进 screen_pick、不计 formal_count、不参与跟踪榜，
只写 screen_research_pool 供研究页展示。三条硬约束：
- 同家族只留最优份额（A/C 是同一只基金，研究时不该占两个名额）；
- 同一家公司最多 2 只（避免整池子被一家包场）；
- 数量落在 [pool_min, pool_max]，宁少不凑数。
"""
from __future__ import annotations

from typing import Any


def _score(scores: dict[str, dict[str, Any]], code: str) -> float:
    sc = scores.get(code) or {}
    v = sc.get("formal_score")
    return float(v) if v is not None else -1.0


def build_pool(conn, run_id: str, mother: list[dict], scores: dict[str, dict[str, Any]],
               decisions: dict[str, dict[str, Any]], pool_min: int = 24,
               pool_max: int = 36, company_cap: int = 2) -> list[dict[str, Any]]:
    ordered = sorted(mother, key=lambda c: -_score(scores, c["fcode"]))
    chosen: list[dict[str, Any]] = []
    seen_family: set[str] = set()
    per_company: dict[str, int] = {}

    def take(c: dict, reason: str) -> None:
        code = c["fcode"]
        fam = c.get("family_id")
        comp = c.get("company_name") or ""
        chosen.append({
            "run_id": run_id, "fcode": code,
            "bucket": (decisions.get(code) or {}).get("style_code") or c.get("style_code"),
            "score": _score(scores, code), "rank": len(chosen) + 1, "reason": reason})
        if fam:
            seen_family.add(fam)
        if comp:
            per_company[comp] = per_company.get(comp, 0) + 1

    for c in ordered:
        if len(chosen) >= pool_max:
            break
        fam = c.get("family_id")
        comp = c.get("company_name") or ""
        if fam and fam in seen_family:
            continue
        if comp and per_company.get(comp, 0) >= company_cap:
            continue
        take(c, "family_and_company_capped")
    # 公司上限导致不足下限时，放宽公司限制补到下限（家族去重仍保留）
    if len(chosen) < pool_min:
        for c in ordered:
            if len(chosen) >= pool_min:
                break
            if any(x["fcode"] == c["fcode"] for x in chosen):
                continue
            fam = c.get("family_id")
            if fam and fam in seen_family:
                continue
            take(c, "company_cap_relaxed_to_fill_min")

    conn.execute("DELETE FROM screen_research_pool WHERE run_id=?", (run_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO screen_research_pool(run_id, fcode, bucket, score, rank, reason)"
        " VALUES(:run_id, :fcode, :bucket, :score, :rank, :reason)", chosen)
    conn.commit()
    return chosen
