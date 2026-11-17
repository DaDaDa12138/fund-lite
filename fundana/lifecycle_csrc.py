"""证监会 EID 基金生命周期采集：清算/合同终止/合并公告 → fund_lifecycle。

数据边界：
* 只使用证监会基金电子披露网站（eid.csrc.gov.cn）的公开公告检索 JSON 与公告 PDF；
* 不把“目录消失 / NAV 停更”推断为清盘；
* effective_date 优先取清算报告 PDF 的“自 YYYY 年 M 月 D 日进入清算期”，
  缺失时退化为“最后运作日”，再缺失才用公告日并显式标记 effective_date_source；
* 原始 JSON / PDF 全部写 raw_object，公告字段原样写 raw_json，不编造日期。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from . import db
from .fetch import parsers

# 只枚举会改变“是否可投资”状态的公告类型。
REPORT_TYPES = {
    "FB060010": "liquidated",   # 清算报告
    "FC030010": "liquidated",   # 基金合同终止公告
    "FC030030": "liquidated",   # 基金清算公告
    "FC050050": "merged",       # 基金合并运作公告
    "FC050060": "merged",       # 基金合并运作方式的其他公告
}
TERMINAL = {"liquidated", "merged"}


def _date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def windows(start: str, end: str, days: int = 30) -> Iterable[tuple[str, str]]:
    """把 [start,end] 切成不超过 days 天的闭区间（EID 无 fundCode 时限制 30 天）。"""
    cur, last = _date(start), _date(end)
    while cur <= last:
        stop = min(cur + timedelta(days=days - 1), last)
        yield cur.isoformat(), stop.isoformat()
        cur = stop + timedelta(days=1)


def _ao_data(report_type: str, start: str, end: str, offset: int, length: int = 100) -> str:
    """EID 要求 DataTables 的 aoData 数组；扁平 dict 或二次编码都会 500。"""
    ao = [
        {"name": "sEcho", "value": 1},
        {"name": "iDisplayStart", "value": offset},
        {"name": "iDisplayLength", "value": length},
        {"name": "fundType", "value": ""},
        {"name": "reportType", "value": report_type},
        {"name": "reportYear", "value": ""},
        {"name": "fundCompanyShortName", "value": ""},
        {"name": "fundCode", "value": ""},
        {"name": "fundShortName", "value": ""},
        {"name": "startUploadDate", "value": start},
        {"name": "endUploadDate", "value": end},
    ]
    return json.dumps(ao, ensure_ascii=False, separators=(",", ":"))


def parse_pdf_dates(text: str) -> dict[str, str]:
    """从清算报告文本提取最后运作日 / 进入清算期日；只返回实际出现的日期。"""
    compact = re.sub(r"\s+", "", text or "")
    out: dict[str, str] = {}
    pats = (
        ("last_operating_date", r"最后运作日(?:为|是|：|:)?(\d{4})年(\d{1,2})月(\d{1,2})日"),
        ("liquidation_start", r"(?:自|于)(\d{4})年(\d{1,2})月(\d{1,2})日(?:起)?进入清算"),
    )
    for key, pat in pats:
        m = re.search(pat, compact)
        if m:
            y, mo, d = (int(x) for x in m.groups())
            try:
                out[key] = date(y, mo, d).isoformat()
            except ValueError:
                pass
    return out


def _fetch_pdf(fetcher, instance_id: str, *, force: bool = False) -> tuple[bytes | None, str | None, str]:
    """按 instanceid 取公告 PDF；落 raw_object/fetch_log/source_state，返回 (bytes, sha, status)。"""
    spec = fetcher.cfg.sources["csrc_lifecycle_pdf"]
    url = fetcher.build_url("csrc_lifecycle_pdf", instance_id)
    if not force:
        cached = fetcher.is_fresh("csrc_lifecycle_pdf", instance_id)
        if cached:
            return db.get_raw(fetcher.conn, cached), cached, "ok"
    started_at = db.now_iso()
    t0 = time.monotonic()
    try:
        fetcher.limiter.wait(urlparse(url).netloc, spec.rate_qps)
        resp = fetcher.session.get(url, headers=spec.headers, timeout=60)
        duration = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
            err = f"HTTP {resp.status_code} / 非 PDF"
            db.log_fetch(fetcher.conn, fetcher.run_id, "csrc_lifecycle_pdf", instance_id, url,
                         started_at, duration, resp.status_code, False, "bad_pdf", err, None)
            db.update_source_state(fetcher.conn, "csrc_lifecycle_pdf", instance_id, "bad_pdf", None, err, False)
            fetcher.conn.commit()
            return None, None, "bad_pdf"
        sha = db.put_raw(fetcher.conn, fetcher.cfg, resp.content, url, resp.status_code,
                         resp.headers.get("Content-Type"), "utf-8")
        db.log_fetch(fetcher.conn, fetcher.run_id, "csrc_lifecycle_pdf", instance_id, url,
                     started_at, duration, resp.status_code, True, "ok", None, sha)
        db.update_source_state(fetcher.conn, "csrc_lifecycle_pdf", instance_id, "ok", sha, None, True)
        fetcher.conn.commit()
        return resp.content, sha, "ok"
    except Exception as exc:  # noqa: BLE001 - 单份 PDF 失败不应中断整批
        duration = int((time.monotonic() - t0) * 1000)
        err = f"{type(exc).__name__}: {exc}"
        db.log_fetch(fetcher.conn, fetcher.run_id, "csrc_lifecycle_pdf", instance_id, url,
                     started_at, duration, None, False, "timeout", err, None)
        db.update_source_state(fetcher.conn, "csrc_lifecycle_pdf", instance_id, "timeout", None, err, False)
        fetcher.conn.commit()
        return None, None, "timeout"


def _pdf_text(body: bytes) -> str:
    try:
        proc = subprocess.run(["pdftotext", "-layout", "-", "-"], input=body,
                              capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "ignore")


def _pick_candidate(items: list[dict[str, Any]]) -> dict[str, Any]:
    """同一基金多条公告时选一条：优先清算报告，其次合同终止/清算公告，最后合并。"""
    order = {"FB060010": 0, "FC030030": 1, "FC030010": 2, "FC050050": 3, "FC050060": 4}
    return sorted(items, key=lambda x: (order.get(x["report_code"], 9), x["report_send_date"], x["upload_info_id"]))[0]


def collect(fetcher, *, start: str = "2015-01-01", end: str | None = None,
            types: list[str] | None = None, parse_pdfs: bool = True,
            force: bool = False, limit: int | None = None) -> dict[str, Any]:
    """枚举公告并写 fund_lifecycle；返回统计与失败明细。"""
    end = end or date.today().isoformat()
    types = types or list(REPORT_TYPES)
    announcements: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    calls = 0
    for rt in types:
        for win_start, win_end in windows(start, end):
            offset = 0
            while True:
                key = f"{rt}:{win_start}:{win_end}:{offset}"
                res = fetcher.get("csrc_lifecycle", key=key, force=force,
                                  params={"aoData": _ao_data(rt, win_start, win_end, offset)})
                calls += 1
                if not res.business_ok:
                    failures.append({"type": rt, "window": f"{win_start}~{win_end}",
                                     "offset": offset, "status": res.status, "error": res.error_msg})
                    break
                try:
                    parsed = parsers.parse_csrc_lifecycle(res.text)
                except parsers.ParseError as exc:
                    fetcher.mark_business_failure(res, "parse_failed", str(exc))
                    failures.append({"type": rt, "window": f"{win_start}~{win_end}",
                                     "offset": offset, "status": "parse_failed", "error": str(exc)})
                    break
                for row in parsed["rows"]:
                    announcements[row["upload_info_id"]] = row
                offset += 100
                if offset >= parsed["total"] or not parsed["rows"] or offset > 5000:
                    break
    by_code: dict[str, list[dict[str, Any]]] = {}
    for row in announcements.values():
        by_code.setdefault(row["fcode"], []).append(row)

    rows_out: list[tuple] = []
    pdf_ok = pdf_fail = 0
    for fcode, items in sorted(by_code.items()):
        if limit and len(rows_out) >= limit:
            break
        statuses = {REPORT_TYPES.get(x["report_code"]) for x in items}
        statuses.discard(None)
        if not statuses:
            continue
        # 同一基金若同时有清算/合并公告，优先清算（清盘是更终局的状态）。
        status = "liquidated" if "liquidated" in statuses else "merged"
        chosen = _pick_candidate([x for x in items if REPORT_TYPES.get(x["report_code"]) == status])
        pdf_sha = None
        parsed_dates: dict[str, str] = {}
        if parse_pdfs and chosen["report_code"] == "FB060010" and chosen.get("upload_info_id"):
            body, pdf_sha, st = _fetch_pdf(fetcher, chosen["upload_info_id"], force=force)
            if body:
                parsed_dates = parse_pdf_dates(_pdf_text(body))
                pdf_ok += 1
            else:
                pdf_fail += 1
        eff = parsed_dates.get("liquidation_start") or parsed_dates.get("last_operating_date")
        if eff:
            eff_source = "pdf_liquidation_start" if parsed_dates.get("liquidation_start") else "pdf_last_operating_date"
        else:
            # 退化：同一基金所有终局公告中取最早公告日，避免用较晚的清算报告日把基金留在宇宙里。
            eff = min(x["report_send_date"] for x in items if REPORT_TYPES.get(x["report_code"]) in TERMINAL)
            eff_source = "announcement_date"
        last_nav = parsed_dates.get("last_operating_date")
        raw = {
            "announcements": items,
            "chosen": chosen,
            "pdf_sha256": pdf_sha,
            "effective_date_source": eff_source,
            "parsed_dates": parsed_dates,
        }
        rows_out.append((
            fcode, status, eff, last_nav, chosen["report_name"],
            f"csrc_eid:{chosen['report_code']}:{chosen['upload_info_id']}",
            json.dumps(raw, ensure_ascii=False), db.now_iso(),
        ))

    # 只替换 EID 来源的同一 fcode/status 记录，保留人工 CSV 导入。
    for fcode, status, eff, last_nav, reason, source, raw_json, created in rows_out:
        fetcher.conn.execute(
            "DELETE FROM fund_lifecycle WHERE fcode=? AND status=? AND source LIKE 'csrc_eid:%'",
            (fcode, status))
        fetcher.conn.execute(
            "INSERT INTO fund_lifecycle(fcode,status,effective_date,last_nav_date,reason,source,raw_json,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(fcode,status,effective_date) DO UPDATE SET"
            " last_nav_date=excluded.last_nav_date, reason=excluded.reason,"
            " source=excluded.source, raw_json=excluded.raw_json",
            (fcode, status, eff, last_nav, reason, source, raw_json, created))
    fetcher.conn.commit()
    counts: dict[str, int] = {}
    for r in fetcher.conn.execute("SELECT status, COUNT(*) AS n FROM fund_lifecycle GROUP BY status"):
        counts[r["status"]] = r["n"]
    return {"ok": not failures, "calls": calls, "announcements": len(announcements),
            "funds": len(by_code), "rows": len(rows_out), "pdf_ok": pdf_ok,
            "pdf_fail": pdf_fail, "counts": counts, "failures": failures[:20]}
