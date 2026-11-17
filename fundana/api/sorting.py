"""多列排序：把 ?sort=a,b&order=desc,asc 解析成安全的 ORDER BY。

设计要点：
* 排序键必须在调用方给的**白名单**里，键只用于查表，永远不拼进 SQL；
* 未知键直接丢弃（不是整条拒绝），全部未知时回退 rank 升序，页面不会白屏；
* NULL 永远排最后：债基里大量指标是 NULL（历史不足），NULLS LAST 才不会把
  "没有数据"的基金顶到"最好"的位置；
* 最后一定补 rank 升序做 tiebreak，否则同分基金在多列排序下顺序不稳定，
  翻页会看到重复或漏项。
"""
from __future__ import annotations

MAX_SORT_KEYS = 3
DEFAULT_ORDER = "asc"


def parse(sort_raw: str | None, order_raw: str | None,
          allowed: dict, default_key: str = "rank") -> list[tuple[str, str]]:
    keys = [k.strip() for k in (sort_raw or "").split(",") if k.strip()]
    orders = [o.strip().lower() for o in (order_raw or "").split(",") if o.strip()]
    spec: list[tuple[str, str]] = []
    for i, key in enumerate(keys[:MAX_SORT_KEYS]):
        if key not in allowed:
            continue
        direction = orders[i] if i < len(orders) else (orders[-1] if orders else DEFAULT_ORDER)
        spec.append((key, "desc" if direction == "desc" else "asc"))
    if not spec:
        spec = [(default_key, "asc")]
    return spec


def order_sql(spec: list[tuple[str, str]], allowed: dict, tiebreak: str = "p.rank ASC") -> str:
    parts = []
    for key, direction in spec:
        if key == "rank":
            parts.append("p.rank ASC")
            continue
        parts.append("%s %s NULLS LAST" % (allowed[key], "DESC" if direction == "desc" else "ASC"))
    sql = ", ".join(parts)
    if spec[0][0] != "rank" and "p.rank ASC" not in sql:
        sql = sql + ", " + tiebreak
    return sql


def as_query(spec: list[tuple[str, str]]) -> dict:
    return {"sort": ",".join(k for k, _ in spec), "order": ",".join(d for _, d in spec)}
