"""本地只读 API + 静态前端（stdlib http.server）。

统一响应：{ok, data, meta, error}。
路由：
  GET  /                            静态前端（web/index.html）
  GET  /api/v1/healthz              存活与表数
  GET  /api/v1/publications/current 当前发布 manifest
  GET  /api/v1/publications/summary 当前发布 summary.md 原文
  GET  /api/v1/diagnostics          当前发布的诊断 CSV 列表
  GET  /api/v1/diagnostics/<name>   单个诊断 CSV（最多 500 行）
  GET  /api/v1/research/enhanced_watch   增强债观察池（research_only）
  GET  /api/v1/research/enhanced_shadow  增强债影子榜（换窗口复算，research_only）
  GET  /api/v1/overview             首页聚合（漏斗/风格/榜单规模/可申购）
  GET  /api/v1/universe/coverage    覆盖统计
  GET  /api/v1/rankings             榜单（board/limit/offset/q/style）
  GET  /api/v1/meta/labels          中文标签字典（前端下拉框/图例）
  GET  /api/v1/funds/<code>         单基金档案 + 九周期指标 + 风格因子暴露
  GET  /api/v1/funds/<code>/nav/rows 逐日净值明细分页（可按日期区间）
  GET  /api/v1/funds/<code>/periods 周/月/年净值涨跌聚合
  GET  /api/v1/funds/<code>/peer    同风格分位（好于同类多少）
  GET  /api/v1/funds/<code>/drawdown 回撤曲线（水下深度）
  GET  /api/v1/search               代码/名称搜索（顶栏联想）
  GET  /api/v1/market/summary       全市场横截面（收益分布/风格中位）
  GET  /api/v1/compare?codes=a,b,c  并排对比（档案+指标+归一化净值）
  GET  /api/v1/stats/storage        数据库/raw/freeze/reports 体量与表级占用
  GET  /api/v1/schema               表结构（查询控制台补全）
  GET  /api/v1/runs                 运行历史
  GET  /api/v1/operations/status    运行 + 抓取状态分布
  GET  /api/v1/api-health          API 健康页（注册表 + fetch_log 延迟/成功率/新鲜度）
  GET  /api/v1/alerts              产品告警（净值/经理/规模/回撤/风格漂移）
  POST /api/v1/query                只读 SQL 查询（闸门见 query.py）
"""
from __future__ import annotations

import csv
import hmac
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import db, factors as factors_mod, freshness, fundtypes, quality as quality_mod
from ..audit import exposure as exposure_mod
from ..publish.verify import verify as verify_publication
from ..publish import PublicationError, current_revision
from ..screen import thresholds as screen_thresholds
from . import api_health as api_health_mod
from . import decision as decision_mod
from . import diagnostics as diagnostics_mod
from . import insights as insights_mod
from . import locked as locked_mod
from . import rankings as rankings_mod
from . import labels as labels_mod
from . import metrics_meta
from . import sorting
from . import periods as periods_mod
from . import query as query_mod
from . import series as series_mod
from . import storage as storage_mod
from . import csv_export
from . import watchlist as watchlist_mod
from ..fees import model as fee_model
from ..metrics.tail import TAIL_CONTRACT, TAIL_WINDOW_DAYS

JSON_TYPE = "application/json; charset=utf-8"


def _response(ok: bool, data=None, error=None, meta=None) -> bytes:
    body = {"ok": ok, "data": data, "meta": meta or {}, "error": error}
    return json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")


# ── 只读响应缓存 ───────────────────────────────────────────
# 首页/质量页/组合体检每次请求都会做全表扫描或重算相关矩阵，实测 1.7–8.1s。
# 这些数据在两次发布之间是静态的：以 publication revision 为 key，TTL 只用于
# 兜底数据库直写（例如手工补数）；发布切换后 key 变化，旧缓存自然失效。
import copy as _copy
import threading as _threading
import time as _time

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = _threading.Lock()
_CACHE_MAX_ENTRIES = 256


def cache_get(key: str, ttl: float = 60.0):
    now = _time.monotonic()
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item is None:
            return None
        created, value = item
        if now - created > ttl:
            _CACHE.pop(key, None)
            return None
        return _copy.deepcopy(value)


def cache_put(key: str, value) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            # 简单淘汰：先清过期项，仍超限则丢掉最早写入的一半。
            now = _time.monotonic()
            expired = [k for k, (t, _) in _CACHE.items() if now - t > 600]
            for k in expired:
                _CACHE.pop(k, None)
            if len(_CACHE) >= _CACHE_MAX_ENTRIES:
                oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])
                for k, _ in oldest[: max(1, len(oldest) // 2)]:
                    _CACHE.pop(k, None)
        _CACHE[key] = (_time.monotonic(), _copy.deepcopy(value))


def cache_clear() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _as_float(value, default=float("-inf")) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── 可选鉴权（ADR-092）──────────────────────────────────────
# 默认关闭（auth_token 为空），保持内网只读用法；配置后所有 /api/v1 接口（healthz 除外）
# 都要求 Authorization: Bearer <token>。静态前端本身不含数据，仍可公开加载。
def extract_token(headers) -> str:
    auth = (headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (headers.get("X-API-Key") or "").strip()


def authorized(cfg, headers) -> bool:
    expected = str(cfg.get("api", "auth_token", default="") or "")
    if not expected:
        return True
    provided = extract_token(headers)
    return hmac.compare_digest(provided, expected)


class Handler(BaseHTTPRequestHandler):
    cfg = None
    server_version = "fundana/0.2"

    def log_message(self, fmt, *args):        # 静音默认访问日志
        return

    # ── 输出 ───────────────────────────────────────────────
    def _send(self, code: int, payload: bytes, ctype: str = JSON_TYPE,
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, code: int, ok: bool, data=None, error=None, meta=None) -> None:
        self._send(code, _response(ok, data, error, meta))

    # ── 路由 ───────────────────────────────────────────────
    def do_GET(self):                          # noqa: N802 - http.server 约定
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)
        if (path.startswith("/api/v1/") and path.rstrip("/") != "/api/v1/healthz"
                and not authorized(self.cfg, self.headers)):
            self._json(401, False, error={"code": "unauthorized",
                                          "message": "需要 Authorization: Bearer <token>"})
            return
        try:
            if path.startswith("/api/v1/"):
                self._api(path.rstrip("/"), qs)
            else:
                self._static(path)
        except Exception as exc:              # noqa: BLE001 - 统一错误封装
            self._json(500, False, error={"code": "internal_error", "message": str(exc)})

    def do_POST(self):                         # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path not in ("/api/v1/query", "/api/v1/watchlist"):
            self._json(404, False, error={"code": "not_found", "message": parsed.path})
            return
        if not authorized(self.cfg, self.headers):
            self._json(401, False, error={"code": "unauthorized",
                                          "message": "需要 Authorization: Bearer <token>"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, False, error={"code": "bad_request", "message": str(exc)})
            return
        if not isinstance(body, dict):
            self._json(400, False, error={"code": "bad_request", "message": "请求体必须是 JSON 对象"})
            return
        if path == "/api/v1/watchlist":
            self._watchlist_add(body)
            return
        try:
            result = query_mod.run(self.cfg.db_path, body.get("sql", ""),
                                   body.get("limit", query_mod.DEFAULT_LIMIT))
            self._json(200, True, result, meta={"mode": "read_only"})
        except query_mod.QueryRejected as exc:
            self._json(400, False, error={"code": "query_rejected", "message": str(exc)})

    def do_DELETE(self):                       # noqa: N802
        parsed = urlparse(self.path)
        prefix = "/api/v1/watchlist/"
        if not parsed.path.startswith(prefix):
            self._json(404, False, error={"code": "not_found", "message": parsed.path})
            return
        if not authorized(self.cfg, self.headers):
            self._json(401, False, error={"code": "unauthorized",
                                          "message": "需要 Authorization: Bearer <token>"})
            return
        try:
            self._watchlist_remove(parsed.path[len(prefix):].strip("/"))
        except Exception as exc:              # noqa: BLE001
            self._json(500, False, error={"code": "internal_error", "message": str(exc)})

    # ── API 分派 ───────────────────────────────────────────
    def _api(self, path: str, qs) -> None:
        if path == "/api/v1/healthz":
            self._healthz()
        elif path == "/api/v1/publications/current":
            self._publication()
        elif path == "/api/v1/publications/summary":
            self._summary()
        elif path == "/api/v1/report/current":
            self._report_current()
        elif path == "/api/v1/methodology":
            self._methodology()
        elif path == "/api/v1/portfolio/diagnostics":
            self._portfolio_diagnostics(qs)
        elif path.startswith("/api/v1/publications/files/"):
            self._publication_file(path.split("/api/v1/publications/files/", 1)[1])
        elif path == "/api/v1/tracker":
            self._tracker(qs)
        elif path.startswith("/api/v1/tracker/"):
            self._tracker_fund(path.split("/api/v1/tracker/", 1)[1])
        elif path == "/api/v1/diagnostics":
            self._diagnostics_list()
        elif path.startswith("/api/v1/diagnostics/"):
            self._diagnostic(path.split("/api/v1/diagnostics/", 1)[1], qs)
        elif path == "/api/v1/research/enhanced_watch":
            self._enhanced_watch(qs)
        elif path == "/api/v1/research/enhanced_shadow":
            self._enhanced_shadow(qs)
        elif path == "/api/v1/validation/summary":
            self._validation()
        elif path == "/api/v1/forward/summary":
            self._forward(qs)
        elif path == "/api/v1/shadow-forward/summary":
            self._shadow_forward(qs)
        elif path == "/api/v1/survivorship":
            self._survivorship(qs)
        elif path == "/api/v1/capacity":
            self._capacity(qs)
        elif path == "/api/v1/research/oos":
            self._oos(qs)
        elif path == "/api/v1/quality":
            self._quality(qs)
        elif path == "/api/v1/api-health":
            self._api_health(qs)
        elif path == "/api/v1/alerts":
            self._alerts(qs)
        elif path == "/api/v1/overview":
            self._overview()
        elif path == "/api/v1/universe/coverage":
            self._coverage()
        elif path == "/api/v1/rankings":
            self._rankings(qs)
        elif path == "/api/v1/locked":
            self._locked(qs)
        elif path.startswith("/api/v1/funds/") and path.endswith("/rank-history"):
            self._rank_history(path[len("/api/v1/funds/"):-len("/rank-history")])
        elif path.startswith("/api/v1/funds/") and path.endswith("/decision"):
            self._decision(path[len("/api/v1/funds/"):-len("/decision")], qs)
        elif path.startswith("/api/v1/funds/") and path.endswith("/cost"):
            self._cost(path[len("/api/v1/funds/"):-len("/cost")], qs)
        elif path.startswith("/api/v1/funds/") and path.endswith("/peer"):
            self._peer(path[len("/api/v1/funds/"):-len("/peer")])
        elif path.startswith("/api/v1/funds/") and path.endswith("/drawdown"):
            self._drawdown(path[len("/api/v1/funds/"):-len("/drawdown")], qs)
        elif path == "/api/v1/search":
            self._search(qs)
        elif path == "/api/v1/market/summary":
            self._market()
        elif path == "/api/v1/compare":
            self._compare(qs)
        elif path == "/api/v1/meta/labels":
            self._json(200, True, labels_mod.labels_payload())
        elif path == "/api/v1/meta/metrics":
            nav_age = int(self.cfg.get("quality", "nav_age_limit_days", default=10))
            self._json(200, True, metrics_meta.payload(nav_age_limit_days=nav_age))
        elif path == "/api/v1/watchlist":
            self._watchlist()
        elif path.startswith("/api/v1/funds/") and path.endswith("/nav/rows"):
            self._nav_rows(path[len("/api/v1/funds/"):-len("/nav/rows")], qs)
        elif path.startswith("/api/v1/funds/") and path.endswith("/periods"):
            self._periods(path[len("/api/v1/funds/"):-len("/periods")], qs)
        elif path.startswith("/api/v1/funds/") and path.endswith("/nav"):
            self._nav_series(path[len("/api/v1/funds/"):-len("/nav")], qs)
        elif path.startswith("/api/v1/funds/"):
            self._fund(path.rsplit("/", 1)[-1])
        elif path == "/api/v1/stats/storage":
            self._json(200, True, storage_mod.storage(self.cfg))
        elif path == "/api/v1/schema":
            self._json(200, True, storage_mod.schema(self.cfg))
        elif path == "/api/v1/runs":
            self._runs(qs)
        elif path == "/api/v1/operations/status":
            self._operations()
        else:
            self._json(404, False, error={"code": "not_found", "message": path})

    # ── 静态文件 ───────────────────────────────────────────
    def _web_root(self) -> Path:
        root = Path(self.cfg.get("api", "web_dir", default="web"))
        return root if root.is_absolute() else Path(self.cfg.root) / root

    def _static(self, path: str) -> None:
        root = self._web_root().resolve()
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (root / rel).resolve()
        if not str(target).startswith(str(root)) or not target.is_file():
            self._json(404, False, error={"code": "not_found", "message": path})
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # ── handlers ───────────────────────────────────────────
    def _healthz(self) -> None:
        conn = db.connect(self.cfg)
        tables = conn.execute(
            "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()["c"]
        self._json(200, True, {"status": "ok", "tables": tables, "db": str(self.cfg.db_path),
                               "code_version": self.cfg.code_version})

    def _publication(self) -> None:
        try:
            verify_publication(self.cfg, full=False)
            rev = current_revision(self.cfg)
            manifest = json.loads((rev / "validated.json").read_text(encoding="utf-8"))
            state_path = Path(self.cfg.report_root) / "publication_state.json"
            if state_path.is_file():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                manifest["generation"] = state.get("generation")
                manifest["committed_at"] = state.get("committed_at")
            self._json(200, True, manifest, meta={"revision": str(rev)})
        except PublicationError as exc:
            self._json(503, False, error={"code": "publication_unavailable", "message": str(exc)})

    def _summary(self) -> None:
        try:
            rev = current_revision(self.cfg)
            text = (rev / "summary.md").read_text(encoding="utf-8")
            self._json(200, True, {"summary_md": text, "revision": rev.name})
        except (PublicationError, OSError) as exc:
            self._json(503, False, error={"code": "publication_unavailable", "message": str(exc)})

    def _report_current(self) -> None:
        try:
            rev = current_revision(self.cfg)
            report_path = rev / "report.json"
            if not report_path.is_file():
                self._json(404, False, error={"code": "report_not_found",
                                              "message": "当前发布没有 report.json"})
                return
            report = json.loads(report_path.read_text(encoding="utf-8"))
            md_path = rev / "report.md"
            data = {"report": report,
                    "markdown": md_path.read_text(encoding="utf-8") if md_path.is_file() else None}
            self._json(200, True, data, meta={"revision": rev.name})
        except (PublicationError, OSError, ValueError) as exc:
            self._json(503, False, error={"code": "publication_unavailable", "message": str(exc)})

    def _methodology(self) -> None:
        try:
            rev = current_revision(self.cfg)
            report_path = rev / "report.json"
            if not report_path.is_file():
                self._json(404, False, error={"code": "report_not_found",
                                              "message": "当前发布没有 report.json"})
                return
            report = json.loads(report_path.read_text(encoding="utf-8"))
            method = next((s for s in report.get("sections", []) if s.get("id") == "method"), {})
            run = report.get("run") or {}
            self._json(200, True, {
                "screen_contract": run.get("screen_contract"),
                "formula_contract": run.get("formula_contract"),
                "method_markdown": method.get("markdown"),
                "sections": [{"id": s.get("id"), "title": s.get("title")}
                             for s in report.get("sections", [])],
            }, meta={"revision": rev.name})
        except (PublicationError, OSError, ValueError) as exc:
            self._json(503, False, error={"code": "publication_unavailable", "message": str(exc)})

    def _portfolio_diagnostics(self, qs) -> None:
        board = (qs.get("board") or ["bond_core10"])[0]
        cached = cache_get(self._cache_key("portfolio", board), ttl=600.0)
        if cached is not None:
            self._json(200, True, cached)
            return
        try:
            rev = current_revision(self.cfg)
            manifest = json.loads((rev / "validated.json").read_text(encoding="utf-8"))
            run_id = manifest.get("run_id")
            # 发布时 report_v2 已经算过 core10 的相关矩阵/久期桶；直接读发布快照，
            # 避免每次打开“组合体检”都全量 collect_rows + 重算 8 秒。
            if board == "bond_core10":
                report_path = rev / "report.json"
                if report_path.is_file():
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    section = next((s for s in report.get("sections", [])
                                    if s.get("id") == "portfolio"), None)
                    data = (section or {}).get("data") or {}
                    if data.get("portfolio"):
                        out = dict(data)
                        out.update({"board": board, "run_id": run_id,
                                    "n_rows": int((out.get("portfolio") or {}).get("requested") or 0)})
                        cache_put(self._cache_key("portfolio", board), out)
                        self._json(200, True, out, meta={"revision": rev.name,
                                                         "source": "report.json"})
                        return
            conn = db.connect(self.cfg)
            try:
                run = conn.execute("SELECT * FROM screen_run WHERE run_id=?", (run_id,)).fetchone()
                if run is None:
                    self._json(404, False, error={"code": "run_not_found", "message": str(run_id)})
                    return
                from ..publish import builder as builder_mod
                from ..publish import report_v2 as report_v2_mod
                boards = builder_mod.collect_rows(conn, run_id, self.cfg)
                rows = boards.get(board) or []
                if not rows:
                    self._json(404, False, error={"code": "board_empty", "message": board})
                    return
                data = report_v2_mod._portfolio(conn, run, rows) or {}
                data.update({"board": board, "run_id": run_id, "n_rows": len(rows)})
                cache_put(self._cache_key("portfolio", board), data)
                self._json(200, True, data, meta={"revision": rev.name,
                                                  "source": "computed"})
            finally:
                conn.close()
        except (PublicationError, OSError, ValueError) as exc:
            self._json(503, False, error={"code": "publication_unavailable", "message": str(exc)})

    def _publication_file(self, rel: str) -> None:
        """只允许下载当前 manifest 里登记过的文件，杜绝目录穿越。"""
        try:
            rev = current_revision(self.cfg)
        except PublicationError as exc:
            self._json(503, False, error={"code": "publication_unavailable", "message": str(exc)})
            return
        manifest = json.loads((rev / "validated.json").read_text(encoding="utf-8"))
        allowed = {f.get("path") for f in manifest.get("files", [])}
        rel = unquote(rel)
        if rel not in allowed:
            self._json(404, False, error={"code": "file_not_in_manifest", "message": rel})
            return
        target = (rev / rel).resolve()
        root = rev.resolve()
        if not str(target).startswith(str(root)) or not target.is_file():
            self._json(404, False, error={"code": "not_found", "message": rel})
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype,
                   extra={"Content-Disposition": f'attachment; filename="{target.name}"'})

    def _diagnostics_list(self) -> None:
        try:
            files = diagnostics_mod.list_files(self.cfg)
        except diagnostics_mod.DiagnosticError as exc:
            self._json(503, False, error={"code": "publication_unavailable", "message": str(exc)})
            return
        self._json(200, True, files)

    def _diagnostic(self, name: str, qs) -> None:
        raw_limit = (qs.get("limit") or [""])[0]
        limit = int(raw_limit) if raw_limit.isdigit() else diagnostics_mod.DEFAULT_ROWS
        try:
            data = diagnostics_mod.load(self.cfg, unquote(name), limit=limit)
        except diagnostics_mod.DiagnosticError as exc:
            msg = str(exc)
            code = 400 if "invalid" in msg else 404 if "not found" in msg else 503
            self._json(code, False, error={"code": "diagnostic_unavailable", "message": str(exc)})
            return
        self._json(200, True, data)

    def _enhanced_watch(self, qs) -> None:
        """增强债观察池：只读 diagnostics/enhanced_near_miss.csv，research_only，不进正式榜。"""
        limit = max(1, min(int((qs.get("limit") or ["36"])[0]), 200))
        try:
            data = diagnostics_mod.load(self.cfg, "enhanced_near_miss.csv",
                                        limit=diagnostics_mod.MAX_ROWS)
        except diagnostics_mod.DiagnosticError as exc:
            self._json(503, False, error={"code": "diagnostic_unavailable", "message": str(exc)})
            return
        rows = []
        for r in data["rows"]:
            try:
                passed = int(r.get("checks_passed") or 0)
            except ValueError:
                continue
            if passed < 6:
                continue
            rows.append(r)
        rows.sort(key=lambda r: (-(int(r.get("checks_passed") or 0)),
                                 -_as_float(r.get("annual_1y_pct"))))
        self._json(200, True, rows[:limit], meta={
            "research_only": True,
            "note": "增强债正式榜为 0 只；本池为距门槛最近的观察名单，不构成正式入选",
            "total_near_miss": len(rows),
            "source": "diagnostics/enhanced_near_miss.csv",
        })

    def _enhanced_shadow(self, qs) -> None:
        """增强债影子榜：读 reports/analysis/enhanced_shadow_<window>d.{csv,json}，research_only。"""
        try:
            window = int((qs.get("window") or ["1095"])[0])
        except ValueError:
            window = 1095
        if window not in (365, 730, 1095, 1825):
            self._json(400, False, error={"code": "invalid_window", "message": "window 只能是 365/730/1095/1825"})
            return
        try:
            limit = max(1, min(int((qs.get("limit") or ["60"])[0]), 500))
        except ValueError:
            limit = 60
        base_dir = (Path(self.cfg.report_root) / "analysis").resolve()
        csv_path = base_dir / ("enhanced_shadow_%dd.csv" % window)
        meta_path = base_dir / ("enhanced_shadow_%dd.json" % window)
        if base_dir not in csv_path.parents or not csv_path.exists():
            self._json(404, False, error={"code": "shadow_unavailable",
                                          "message": "尚未生成 %dd 影子榜，请先运行 fundana shadow --window %d" % (window, window)})
            return
        meta = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        with csv_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        meta.update({"research_only": True, "window_days": window, "total": len(rows),
                     "note": "同一套增强准入阈值换窗口复算；不写库、不进 screen_pick、不影响正式发布"})
        self._json(200, True, rows[:limit], meta=meta)

    def _contract(self) -> str:
        """当前指标契约。fund_metric 按 (fcode, period_days, formula_contract) 存多版本，
        任何 JOIN 都必须锁定契约，否则同一只基金会出两行（gen12 实测的重复行 bug）。"""
        return str(self.cfg.get("risk", "formula_contract", default="") or "")

    def _tail_contract(self) -> str:
        """当前尾部指标契约。fund_tail_metric 与 fund_metric 一样按 formula_contract
        多版本共存（换契约后旧行不会自动消失），JOIN/COUNT 都必须锁定契约。"""
        return TAIL_CONTRACT

    def _cache_key(self, name: str, extra: str = "") -> str:
        """发布切换即失效：revision 是缓存的第一段 key。"""
        try:
            rev = current_revision(self.cfg).name
        except PublicationError:
            rev = "no-publication"
        return "%s:%s:%s" % (name, rev, extra)

    def _overview(self) -> None:
        cached = cache_get(self._cache_key("overview"), ttl=600.0)
        if cached is not None:
            self._json(200, True, cached)
            return
        conn = db.connect(self.cfg)
        run = conn.execute("SELECT run_id, created_at, base_pass_count FROM screen_run"
                           " ORDER BY created_at DESC LIMIT 1").fetchone()
        data: dict = {"screen_run": dict(run) if run else None}
        if run:
            details = {d["code"]: d["detail"] for d in metrics_meta.layer_details()}
            data["layers"] = [{"code": r["layer_reached"],
                               "label": labels_mod.label_layer(r["layer_reached"]),
                               "detail": details.get(r["layer_reached"], ""),
                               "count": r["c"]} for r in conn.execute(
                "SELECT layer_reached, COUNT(*) AS c FROM screen_decision WHERE run_id=?"
                " GROUP BY 1 ORDER BY c DESC", (run["run_id"],))]
            data["boards"] = {r["board"]: r["c"] for r in conn.execute(
                "SELECT board, COUNT(*) AS c FROM screen_pick WHERE run_id=? GROUP BY 1",
                (run["run_id"],))}
            data["styles"] = [{"code": r["style_code"],
                               "label": labels_mod.label_style(r["style_code"]),
                               "count": r["c"]} for r in conn.execute(
                "SELECT style_code, COUNT(*) AS c FROM fund_style GROUP BY 1 ORDER BY c DESC")]
            data["reasons"] = [{"code": r["reason"],
                                "label": labels_mod.label_reason(r["reason"]),
                                "count": r["c"]} for r in conn.execute(
                "SELECT je.value AS reason, COUNT(*) AS c FROM screen_decision,"
                " json_each(screen_decision.reason_codes_json) AS je"
                " WHERE run_id=? GROUP BY 1 ORDER BY c DESC LIMIT 12", (run["run_id"],))]
        clause, tparams = fundtypes.where(column="type_group", cfg=self.cfg)
        data["funds"] = conn.execute(
            f"SELECT COUNT(*) AS c FROM fund WHERE {clause}", tparams).fetchone()["c"]
        types = fundtypes.resolve(cfg=self.cfg)
        nav_cov = freshness.nav_coverage(conn, types=types)
        effective = nav_cov.get("effective_date")
        metric_cov = (freshness.metric_coverage(
            conn, effective, formula_contract=self.cfg.get("risk", "formula_contract", default=None),
            types=types) if effective else {})
        data["nav"] = dict(conn.execute(
            "SELECT COUNT(*) AS rows, COUNT(DISTINCT fcode) AS funds, MAX(nav_date) AS max_date"
            " FROM fund_nav_daily").fetchone())
        data["nav"].update({
            "last_date": effective or data["nav"].get("max_date"),
            "effective_date": effective,
            "max_date": data["nav"].get("max_date"),
            "coverage_pct": nav_cov.get("effective_coverage_pct"),
            "max_coverage_pct": nav_cov.get("max_coverage_pct"),
            "partial_latest": bool(data["nav"].get("max_date") and effective
                                   and str(data["nav"]["max_date"]) > str(effective)),
        })
        data["freshness"] = {"nav": nav_cov, "metric": metric_cov,
                             "as_of": effective, "metric_as_of": metric_cov.get("metric_as_of")}
        data["buyable"] = {r["buyable_state"]: r["c"] for r in conn.execute(
            "SELECT buyable_state, COUNT(*) AS c FROM fund_buyable_daily"
            " WHERE business_date=(SELECT MAX(business_date) FROM fund_buyable_daily) GROUP BY 1")}
        data["holdings"] = dict(conn.execute(
            "SELECT COUNT(DISTINCT code) AS funds, COUNT(*) AS rows FROM src_zqcc_holding").fetchone())
        data["tail"] = dict(conn.execute(
            "SELECT COUNT(*) AS funds,"
            " SUM(CASE WHEN completeness>=1.0 THEN 1 ELSE 0 END) AS full,"
            " ROUND(AVG(completeness),4) AS avg_completeness"
            " FROM fund_tail_metric WHERE window_days=? AND formula_contract=?",
            (TAIL_WINDOW_DAYS, self._tail_contract())).fetchone())
        data["fees"] = dict(conn.execute(
            "SELECT (SELECT COUNT(*) FROM fund_fee_meta) AS funds,"
            " (SELECT COUNT(DISTINCT fcode) FROM fund_fee_tier) AS with_tiers,"
            " (SELECT COUNT(*) FROM fund_fee_tier) AS tiers").fetchone())
        cache_put(self._cache_key("overview"), data)
        self._json(200, True, data)

    def _coverage(self) -> None:
        conn = db.connect(self.cfg)
        clause, tparams = fundtypes.where(column="type_group", cfg=self.cfg)
        row = conn.execute(f"SELECT COUNT(*) AS funds FROM fund WHERE {clause}",
                           tparams).fetchone()
        nav = conn.execute("SELECT COUNT(*) AS rows, MAX(nav_date) AS max_date"
                           " FROM fund_nav_daily").fetchone()
        types = fundtypes.resolve(cfg=self.cfg)
        nav_cov = freshness.nav_coverage(conn, types=types)
        effective = nav_cov.get("effective_date")
        styles = [{"code": r["style_code"], "label": labels_mod.label_style(r["style_code"]),
                   "count": r["c"]} for r in conn.execute(
            "SELECT style_code, COUNT(*) AS c FROM fund_style GROUP BY 1 ORDER BY c DESC")]
        self._json(200, True, {"bond_funds": row["funds"],
                               "uncovered_types": fundtypes.uncovered(conn),
                               "nav_rows": nav["rows"],
                               "nav_last_date": effective or nav["max_date"],
                               "nav_effective_date": effective,
                               "nav_max_date": nav["max_date"],
                               "nav_coverage_pct": nav_cov.get("effective_coverage_pct"),
                               "nav_partial_latest": bool(nav["max_date"] and effective
                                                          and str(nav["max_date"]) > str(effective)),
                               "styles": styles})

    def _locked(self, qs) -> None:
        conn = db.connect(self.cfg)
        fmt = (qs.get("fmt") or [""])[0].strip().lower()
        page_max = int(self.cfg.get("api", "page_size_max", default=200))
        if fmt == "csv":
            limit, offset = csv_export.MAX_ROWS, 0
        else:
            limit = max(1, min(int((qs.get("limit") or ["200"])[0]), page_max))
            offset = max(0, int((qs.get("offset") or ["0"])[0]))
        lockup = (qs.get("lockup") or [""])[0].strip() or None
        bucket = (qs.get("bucket") or [""])[0].strip() or None
        sort_key = (qs.get("sort") or ["rank"])[0]
        order = (qs.get("order") or ["asc"])[0].lower()
        out = locked_mod.rows(conn, self._contract(), lockup=lockup, bucket=bucket,
                              limit=limit, offset=offset, sort=sort_key, order=order)
        if fmt == "csv":
            body = csv_export.build(csv_export.LOCKED_COLUMNS, out["rows"])
            self._send(200, body, "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="%s"'
                        % csv_export.filename("bond_locked", lockup or "all")})
            return
        self._json(200, True, out, meta={"board": "bond_locked", "total": out["total"]})

    def _decision(self, fcode: str, qs) -> None:
        conn = db.connect(self.cfg)
        row = conn.execute("SELECT 1 FROM fund WHERE fcode=?", (fcode,)).fetchone()
        if not row:
            self._json(404, False, error={"code": "fund_not_found", "message": fcode})
            return
        run_id = (qs.get("run_id") or [None])[0]
        # prev= 指定要对比的历史轮次；不传则默认对比上一轮
        prev_run = (qs.get("prev") or [""])[0].strip() or None
        key = self._cache_key("decision", "%s|%s|%s" % (fcode, run_id or "latest",
                                                         prev_run or "auto"))
        cached = cache_get(key, ttl=600.0)
        if cached is not None:
            self._json(200, True, cached)
            return
        view = decision_mod.view(conn, fcode, run_id, prev_run=prev_run)
        if view is None:
            self._json(404, False, error={"code": "no_decision",
                                          "message": "该基金还没有决策记录"})
            return
        cache_put(key, view)
        self._json(200, True, view)

    def _rank_history(self, fcode: str) -> None:
        conn = db.connect(self.cfg)
        row = conn.execute("SELECT 1 FROM fund WHERE fcode=?", (fcode,)).fetchone()
        if not row:
            self._json(404, False, error={"code": "fund_not_found", "message": fcode})
            return
        self._json(200, True, decision_mod.rank_history(conn, fcode))

    def _rankings(self, qs) -> None:
        conn = db.connect(self.cfg)
        board = (qs.get("board") or ["bond_stable"])[0]
        if board not in screen_thresholds.ALL_BOARDS:
            self._json(400, False, error={
                "code": "unknown_board", "board": board,
                "allowed": list(screen_thresholds.ALL_BOARDS)})
            return
        fmt = (qs.get("fmt") or [""])[0].strip().lower()
        page_max = int(self.cfg.get("api", "page_size_max", default=200))
        if fmt == "csv":
            limit, offset = csv_export.MAX_ROWS, 0
        else:
            limit = max(1, min(int((qs.get("limit") or ["50"])[0]), page_max))
            offset = max(0, int((qs.get("offset") or ["0"])[0]))
        keyword = (qs.get("q") or [""])[0].strip()
        style = (qs.get("style") or [""])[0].strip()
        # 排序白名单：只允许已知列，避免拼接注入；NULL 永远排最后
        # 排序白名单覆盖页面上每一个可点列：任何新增列都必须在这里登记，
        # 否则前端点了没反应（或退化成按 rank 排），用户会以为排序坏了。
        sort_map = {
            "rank": "p.rank", "formal_score": "p.formal_score", "rank_score": "p.rank_score",
            "long_score": "json_extract(d.scores_json, '$.long_score')",
            "confidence": "p.confidence",
            "annual_1y": "m.annual_dec", "sharpe": "m.sharpe", "mdd": "m.mdd_dec",
            "sortino": "m.sortino", "calmar": "m.calmar_dec", "positive": "m.positive_ratio_pct",
            "flat": "m.flat_ratio_pct", "non_negative": "m.non_negative_ratio_pct",
            "mdd_3y": "t.mdd_dec", "worst90": "t.worst_rolling90d_pct",
            "tail": "t.completeness", "worst180": "t.worst_rolling180d_pct",
            "worst_day": "t.worst_day_pct", "worst_week": "t.worst_week_pct",
            "month_positive": "t.positive_month_ratio_pct",
            "underwater": "t.max_underwater_obs",
            "scale": "i.asset_scale_billion",
            # 排序按 1 年持有期总成本（ADR-059），与列表展示的 cost_1y_pct 列一致
            "fee": rankings_mod.COST_1Y_SQL,
            "cost_1y": rankings_mod.COST_1Y_SQL, "cost_3y": rankings_mod.COST_3Y_SQL,
            "cost_5y": rankings_mod.COST_5Y_SQL,
            "redemption_1y": "COALESCE(i.redemption_1y_pct, fm.redemption_1y_pct)",
            "ret_1w": "w1.total_return_pct", "ret_1m": "w2.total_return_pct",
            "ret_3m": "w3.total_return_pct", "ret_6m": "w4.total_return_pct",
            "ret_1y": "m.total_return_pct",
            "annual_1w": "w1.annual_dec", "annual_1m": "w2.annual_dec",
            "annual_3m": "w3.annual_dec", "annual_6m": "w4.annual_dec",
            "annual_3y": "m3.annual_dec", "mdd_3y_metric": "m3.mdd_dec",
            "vol_3y": "m3.vol_dec", "sharpe_3y": "m3.sharpe",
            "annual_5y": "m5.annual_dec", "mdd_5y_metric": "m5.mdd_dec",
            "vol_5y": "m5.vol_dec", "sharpe_5y": "m5.sharpe",
            "share_scale": "i.share_scale_billion",
            "day_pct": "n.growth_pct", "nav_date": "n.nav_date",
            "lockup_days": "i.effective_days",
            "code": "p.fcode", "name": "f.name", "style": "d.style_code",
        }
        # 多列排序：?sort=annual_1y,sharpe&order=desc,desc；键只在白名单里查表，不拼进 SQL
        spec = sorting.parse((qs.get("sort") or [""])[0], (qs.get("order") or [""])[0],
                             sort_map)
        sort_key = spec[0][0]
        order = spec[0][1]
        order_sql = sorting.order_sql(spec, sort_map)
        # 只服务已提交的 publication，而不是“最新一次 screen_run”。
        # 否则 gate 阻断或盘中重跑时，页面会提前展示尚未发布的榜单；
        # 失败保旧（commit.py 的原子切换）必须在 API 层也成立。
        run = conn.execute(
            "SELECT r.run_id, r.business_date,"
            " COALESCE(p.nav_as_of, r.nav_as_of) AS nav_as_of,"
            " COALESCE(p.metric_as_of, r.metric_as_of) AS metric_as_of,"
            " r.freshness_json, p.generation"
            " FROM publication p JOIN screen_run r ON r.run_id=p.run_id"
            " WHERE p.status='committed' ORDER BY p.generation DESC LIMIT 1").fetchone()
        if not run:
            self._json(404, False, error={"code": "no_publication"})
            return
        types = fundtypes.resolve(cfg=self.cfg)
        fresh = freshness.assess(conn, business_date=run["business_date"], types=types,
                                 formula_contract=self._contract())
        nav_as_of = run["nav_as_of"] or fresh.get("as_of")
        metric_as_of = run["metric_as_of"] or (fresh.get("metric") or {}).get("metric_as_of")
        where = ["p.run_id=?", "p.board=?"]
        args: list = [run["run_id"], board]
        dedupe = (qs.get("dedupe") or [""])[0].strip()
        clause = rankings_mod.dedupe_clause(dedupe)
        if clause:
            where.append(clause)
        if keyword:
            where.append("(p.fcode LIKE ? OR f.name LIKE ?)")
            args += [f"%{keyword}%", f"%{keyword}%"]
        if style:
            where.append("d.style_code=?")
            args.append(style)
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM screen_pick p JOIN fund f ON f.fcode=p.fcode"
            " LEFT JOIN screen_run_input i ON i.run_id=p.run_id AND i.fcode=p.fcode"
            " LEFT JOIN screen_decision d ON d.run_id=p.run_id AND d.fcode=p.fcode"
            " WHERE " + " AND ".join(where), args).fetchone()["c"]
        # 行 SQL 在 rankings.ROW_SQL（五个周期的 JOIN 与契约锁都在那里）：
        # 7 天的年化会被放大成三位数（一周 +0.5% ≈ 年化 26%），收益与年化两列都给。
        contract = self._contract()
        # 尾部指标有独立契约版本，不能用风险契约去锁（否则 t.* 全为 NULL）。
        tail_contract = self._tail_contract()
        # 发布日与指标日必须钉住：run 之后的净值/指标不能被榜单读到。
        nav_cut = nav_as_of or "9999-12-31"
        metric_cut = metric_as_of or "9999-12-31"
        row_params: list = [nav_cut]
        for _ in range(7):
            row_params += [contract, metric_cut]
        row_params += [TAIL_WINDOW_DAYS, tail_contract, metric_cut]
        rows = conn.execute(
            rankings_mod.ROW_SQL + " WHERE " + " AND ".join(where)
            + " ORDER BY " + order_sql + " LIMIT ? OFFSET ?",
            row_params + args + [limit, offset]).fetchall()
        fam_counts = rankings_mod.family_member_count(conn, run["run_id"], board)
        out = []
        for r in rows:
            item = dict(r)
            item["style_label"] = labels_mod.label_style(item.get("style_code"))
            item["lockup_label"] = labels_mod.label_lockup(item.get("lockup_type"))
            item["family_members"] = fam_counts.get(item.get("family_id"), 1)
            # P0-2：不可比净值曲线旗标。缺失记录视为可比（旧库兼容），但不会伪装成
            # “无风险”；前端/CSV 用 flags 明确展示为什么这条曲线不能按同一把尺子排序。
            raw_flags = item.pop("metric_flags_json", None)
            try:
                flags = json.loads(raw_flags) if raw_flags else []
            except (TypeError, ValueError, json.JSONDecodeError):
                flags = []
            item["metric_flags"] = [str(x) for x in flags] if isinstance(flags, list) else []
            item["metric_comparable"] = (True if item.get("metric_comparable") is None
                                          else bool(item.get("metric_comparable")))
            metric_end = item.get("metric_end_date")
            item["metric_stale"] = bool(metric_end and metric_as_of
                                        and str(metric_end) < str(metric_as_of))
            item["nav_stale"] = bool(item.get("nav_date") and nav_as_of
                                     and str(item["nav_date"]) < str(nav_as_of))
            # P0-3：独立风险门结果。risk 是完整明细，risk_status/risk_flags 是列表页
            # 与 CSV 用的摘要；旧库没有该列时保持 None，不伪造“无风险”。
            raw_risk = item.pop("risk_json", None)
            try:
                risk = json.loads(raw_risk) if raw_risk else None
            except (TypeError, ValueError, json.JSONDecodeError):
                risk = None
            item["risk"] = risk if isinstance(risk, dict) else None
            risk_summary = (item["risk"] or {}).get("summary") or {}
            item["risk_status"] = risk_summary.get("status")
            item["risk_blocked"] = bool(risk_summary.get("blocked"))
            item["risk_flags"] = [str(f.get("code")) for f in
                                  ((item["risk"] or {}).get("flags") or [])
                                  if f.get("status") in ("block", "watch")]
            # 3/5 年证据：有效年数缺失时用 n_obs/252 兜底；覆盖率只表示窗口覆盖，
            # 不表示数据质量；<90% 不冒充完整窗口，前端/客户看到 is_full=False。
            for prefix, target in (("3y", 3.0), ("5y", 5.0)):
                years = item.get("effective_years_" + prefix)
                n_obs = item.get("n_obs_" + prefix)
                if years is None and n_obs:
                    years = float(n_obs) / 252.0
                ratio = None if years is None else max(0.0, min(1.0, float(years) / target))
                item["coverage_ratio_" + prefix] = None if ratio is None else round(ratio, 4)
                item["is_full_" + prefix] = bool(ratio is not None and ratio >= 0.90)
            out.append(item)
        if fmt == "csv":
            body = csv_export.build(csv_export.RANKING_COLUMNS, out)
            self._send(200, body, "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="%s"'
                        % csv_export.filename("bond_rankings", board)})
            return
        self._json(200, True, out,
                   meta={"run_id": run["run_id"], "board": board,
                         "board_label": labels_mod.label_board(board), "total": total,
                         "limit": limit, "offset": offset, "dedupe": dedupe or None,
                         "nav_as_of": nav_as_of, "metric_as_of": metric_as_of,
                         "freshness_status": fresh.get("status"),
                         "freshness_warnings": fresh.get("warnings"),
                         "sort": sort_key, "order": order,
                         "sort_spec": [{"key": k, "order": o} for k, o in spec]})


    # ── 长期跟踪榜 ─────────────────────────────────────────
    def _tracker(self, qs) -> None:
        from .. import tracker as tracker_mod
        conn = db.connect(self.cfg)
        board = (qs.get("board") or [""])[0].strip() or None
        limit = max(1, min(int((qs.get("limit") or ["50"])[0]),
                           int(self.cfg.get("api", "page_size_max", default=200))))
        offset = max(0, int((qs.get("offset") or ["0"])[0]))
        active_only = (qs.get("include_exited") or [""])[0] not in ("1", "true")
        data = tracker_mod.current(conn, board=board, limit=limit, offset=offset,
                                   active_only=active_only,
                                   formula_contract=self._contract() or None)
        for r in data["rows"]:
            r["style_label"] = labels_mod.label_style(r.get("style_code"))
            r["board_label"] = labels_mod.label_board(r.get("last_board"))
            r["status_label"] = "在榜" if r.get("status") == "active" else "已退出"
        data["summary"] = tracker_mod.summary(conn)
        data["limit"], data["offset"] = limit, offset
        self._json(200, True, data)

    def _tracker_fund(self, fcode: str) -> None:
        from .. import tracker as tracker_mod
        conn = db.connect(self.cfg)
        data = tracker_mod.history(conn, fcode)
        if not data["tracker"] and not data["events"]:
            self._json(404, False, error={"code": "not_found", "message": fcode})
            return
        for e in data["events"]:
            e["board_label"] = labels_mod.label_board(e.get("board"))
        self._json(200, True, data)

    # ── 收藏（唯一可写业务表） ─────────────────────────────
    def _watch_item(self, item: dict) -> dict:
        item = dict(item or {})
        item["style_label"] = labels_mod.label_style(item.get("style_code"))
        item["buyable_label"] = labels_mod.label_buyable(item.get("buyable_state"))
        return item

    def _watchlist(self) -> None:
        conn = db.connect(self.cfg)
        items = [self._watch_item(r) for r in watchlist_mod.list_items(conn, self._contract())]
        self._json(200, True, {"items": items, "count": len(items)})

    def _watchlist_add(self, body) -> None:
        conn = db.connect(self.cfg)
        try:
            item = watchlist_mod.add(conn, body.get("fcode") or "", self._contract(),
                                     body.get("source") or "manual", body.get("note"))
        except watchlist_mod.WatchlistError as exc:
            self._json(400, False, error={"code": "watchlist_rejected", "message": str(exc)})
            return
        self._json(201, True, self._watch_item(item))

    def _watchlist_remove(self, fcode: str) -> None:
        conn = db.connect(self.cfg)
        if not watchlist_mod.remove(conn, fcode):
            self._json(404, False, error={"code": "not_found", "message": fcode})
            return
        self._json(200, True, {"removed": fcode})

    def _nav_rows(self, fcode: str, qs) -> None:
        """逐日净值明细：默认全历史，可用 start/end 收窄，分页返回。"""
        conn = db.connect(self.cfg)
        if not conn.execute("SELECT 1 FROM fund WHERE fcode=?", (fcode,)).fetchone():
            self._json(404, False, error={"code": "not_found", "message": fcode})
            return
        def _int(name, default):
            raw = (qs.get(name) or [""])[0]
            return int(raw) if raw.isdigit() else default
        try:
            data = series_mod.nav_rows(
                conn, fcode,
                start=(qs.get("start") or [""])[0] or None,
                end=(qs.get("end") or [""])[0] or None,
                page=_int("page", 1), page_size=_int("page_size", series_mod.DEFAULT_PAGE_SIZE))
        except series_mod.RangeError as exc:
            self._json(400, False, error={"code": "bad_request", "message": str(exc)})
            return
        self._json(200, True, data)

    def _periods(self, fcode: str, qs) -> None:
        """周/月/年净值涨跌聚合（复投口径 + 单位净值对照 + 期内最大回撤）。"""
        conn = db.connect(self.cfg)
        if not conn.execute("SELECT 1 FROM fund WHERE fcode=?", (fcode,)).fetchone():
            self._json(404, False, error={"code": "not_found", "message": fcode})
            return
        granularity = (qs.get("granularity") or ["month"])[0]
        raw_limit = (qs.get("limit") or ["120"])[0]
        limit = int(raw_limit) if raw_limit.isdigit() else 120
        try:
            data = periods_mod.period_rows(conn, fcode, granularity, limit)
        except periods_mod.PeriodError as exc:
            self._json(400, False, error={"code": "bad_request", "message": str(exc)})
            return
        self._json(200, True, data)

    def _nav_series(self, fcode: str, qs) -> None:
        conn = db.connect(self.cfg)
        if not conn.execute("SELECT 1 FROM fund WHERE fcode=?", (fcode,)).fetchone():
            self._json(404, False, error={"code": "not_found", "message": fcode})
            return
        try:
            days = int((qs.get("days") or ["1095"])[0])
        except ValueError:
            self._json(400, False, error={"code": "bad_request", "message": "days 必须是整数"})
            return
        if days < 30 or days > 3650:
            self._json(400, False, error={"code": "bad_request", "message": "days 需在 [30,3650]"})
            return
        data = series_mod.nav_series(conn, fcode, days)
        self._json(200, True, data, meta={"days": days, "downsampled": data["downsampled"]})

    def _cost(self, fcode: str, qs) -> None:
        conn = db.connect(self.cfg)
        try:
            amount = float((qs.get("amount") or ["10000"])[0])
            days = int((qs.get("holding_days") or ["30"])[0])
            max_dd_raw = (qs.get("max_drawdown") or [""])[0]
            max_dd = float(max_dd_raw) if max_dd_raw else None
        except ValueError:
            self._json(400, False, error={"code": "bad_request",
                                          "message": "amount/holding_days/max_drawdown 必须是数字"})
            return
        if not (0 < amount <= 1e9) or days < 0 or days > 36500:
            self._json(400, False, error={"code": "bad_request",
                                          "message": "amount 需在 (0,1e9]，holding_days 需在 [0,36500]"})
            return
        if not conn.execute("SELECT 1 FROM fund WHERE fcode=?", (fcode,)).fetchone():
            self._json(404, False, error={"code": "not_found", "message": fcode})
            return
        q = fee_model.quote(conn, fcode, amount, days, user_max_drawdown=max_dd,
                            formula_contract=self._contract())
        schedule = {
            "meta": dict(conn.execute("SELECT * FROM fund_fee_meta WHERE fcode=?", (fcode,)).fetchone())
            if conn.execute("SELECT 1 FROM fund_fee_meta WHERE fcode=?", (fcode,)).fetchone() else None,
            "purchase": [dict(x) for x in conn.execute(
                "SELECT tier_no, amount_lo_cny, amount_hi_cny, rate_pct, discount_pct, fixed_cny"
                " FROM fund_fee_tier WHERE fcode=? AND kind='purchase' ORDER BY tier_no", (fcode,))],
            "redemption": [dict(x) for x in conn.execute(
                "SELECT tier_no, days_lo, days_hi, rate_pct FROM fund_fee_tier"
                " WHERE fcode=? AND kind='redemption' ORDER BY tier_no", (fcode,))],
        }
        payload = fee_model.as_dict(q)
        payload["reason_labels"] = [{"code": r, "label": labels_mod.label_reason(r)}
                                    for r in payload.get("reasons") or []]
        self._json(200, True, {**payload, "schedule": schedule},
                   meta={"mode": "scenario", "standard_metric_claim": False})

    # ── 洞察：搜索 / 市场 / 同类分位 / 回撤 / 对比 ─────────
    def _search(self, qs) -> None:
        q = (qs.get("q") or [""])[0].strip()
        try:
            limit = max(1, min(int((qs.get("limit") or ["8"])[0]), 30))
        except ValueError:
            limit = 8
        conn = db.connect(self.cfg)
        self._json(200, True, insights_mod.search(conn, q, limit), meta={"q": q})

    def _market(self) -> None:
        conn = db.connect(self.cfg)
        data = insights_mod.market_summary(conn, self._contract(), self.cfg)
        data["nav_last_date"] = conn.execute(
            "SELECT MAX(nav_date) AS d FROM fund_nav_daily").fetchone()["d"]
        self._json(200, True, data)

    def _peer(self, fcode: str) -> None:
        conn = db.connect(self.cfg)
        if not conn.execute("SELECT 1 FROM fund WHERE fcode=?", (fcode,)).fetchone():
            self._json(404, False, error={"code": "not_found", "message": fcode})
            return
        data = insights_mod.peer_rank(conn, fcode, self._contract())
        data["style_label"] = labels_mod.label_style(data.get("style_code"))
        self._json(200, True, data, meta={"min_peers": insights_mod.MIN_PEERS})

    def _drawdown(self, fcode: str, qs) -> None:
        conn = db.connect(self.cfg)
        if not conn.execute("SELECT 1 FROM fund WHERE fcode=?", (fcode,)).fetchone():
            self._json(404, False, error={"code": "not_found", "message": fcode})
            return
        try:
            days = int((qs.get("days") or ["1095"])[0])
        except ValueError:
            days = 1095
        if days < 30 or days > 3650:
            self._json(400, False, error={"code": "bad_request", "message": "days 需在 [30,3650]"})
            return
        self._json(200, True, insights_mod.drawdown_series(conn, fcode, days))

    def _compare(self, qs) -> None:
        raw = (qs.get("codes") or [""])[0]
        codes = [c.strip() for c in raw.split(",") if c.strip()][:insights_mod.MAX_COMPARE]
        if not codes:
            self._json(400, False, error={"code": "bad_request", "message": "codes 不能为空"})
            return
        conn = db.connect(self.cfg)
        data = insights_mod.compare(conn, codes, self._contract())
        self._json(200, True, data, meta={"max": insights_mod.MAX_COMPARE})

    def _validation(self) -> None:
        """跨源核对汇总（§16.1）：当前口径的不一致率 + 最近几日的核对历史。"""
        from ..validate import cross as cross_mod
        conn = db.connect(self.cfg)
        data = cross_mod.summarize(conn)
        data["period"] = cross_mod.period_summary(conn)
        data["recent"] = [dict(r) for r in conn.execute(
            "SELECT business_date, COUNT(*) AS n,"
            " SUM(CASE WHEN status='mismatch' THEN 1 ELSE 0 END) AS mismatch,"
            " SUM(CASE WHEN status='date_lag' THEN 1 ELSE 0 END) AS date_lag,"
            " SUM(CASE WHEN status LIKE 'missing%' THEN 1 ELSE 0 END) AS missing,"
            " MAX(checked_at) AS checked_at FROM nav_cross_check"
            " GROUP BY business_date ORDER BY business_date DESC LIMIT 7")]
        self._json(200, True, data)

    def _forward(self, qs) -> None:
        """前瞻验证汇总（§15）：cohort 数、成熟观测、证据等级。"""
        from .. import forward as forward_mod
        conn = db.connect(self.cfg)
        cohort = (qs.get("cohort") or [None])[0]
        data = forward_mod.summary(conn, cohort)
        data["cohort_list"] = forward_mod.cohort_list(conn)
        self._json(200, True, data)

    def _shadow_forward(self, qs) -> None:
        """v9/v10 同池影子的前瞻观察（research_only，不混入正式证据）。"""
        from .. import shadow_forward as shadow_fwd
        conn = db.connect(self.cfg)
        cohort = (qs.get("cohort") or [None])[0]
        data = shadow_fwd.summary(conn, cohort)
        data["cohort_list"] = shadow_fwd.cohort_list(conn)
        self._json(200, True, data)

    def _survivorship(self, qs) -> None:
        """幸存者偏差诊断：公开目录快照的消失候选（不等于清盘）。"""
        from .. import survivorship as sv_mod
        conn = db.connect(self.cfg)
        try:
            lookback = int((qs.get("lookback_days") or ["30"])[0])
        except ValueError:
            lookback = 30
        try:
            limit = int((qs.get("limit") or ["50"])[0])
        except ValueError:
            limit = 50
        lookback = max(2, min(lookback, 365))
        limit = max(1, min(limit, 200))
        self._json(200, True, sv_mod.summary(conn, lookback_days=lookback, limit=limit))

    def _oos(self, qs) -> None:
        """季度 OOS 研究模型（P2-15，research_only）；结果缓存 10 分钟。"""
        from ..screen import oos as oos_mod
        contract = (qs.get("contract") or [self._contract()])[0]
        try:
            test_quarters = int((qs.get("test_quarters") or ["3"])[0])
        except ValueError:
            test_quarters = 3
        test_quarters = max(1, min(test_quarters, 6))
        key = self._cache_key("research_oos", "%s|%s" % (contract, test_quarters))
        cached = cache_get(key, ttl=600.0)
        if cached is not None:
            self._json(200, True, cached)
            return
        conn = db.connect(self.cfg)
        out = oos_mod.run(conn, self.cfg, contract=contract,
                          test_quarters=test_quarters)
        cache_put(key, out)
        self._json(200, True, out)

    def _capacity(self, qs) -> None:
        """规模容量曲线（P2-11，研究）：按风格×规模桶给 1/3 年业绩中位数。"""
        from .. import capacity as cap_mod
        conn = db.connect(self.cfg)
        as_of = (qs.get("as_of") or [None])[0]
        self._json(200, True, cap_mod.summary(conn, self.cfg, as_of=as_of,
                                              contract=self._contract()))

    def _quality(self, qs) -> None:
        """质量看板（A5）：源成功率 / 字段 NULL 率 / 跨源不一致 / 门禁历史。"""
        from . import quality as quality_mod
        try:
            days = int((qs.get("days") or ["7"])[0])
        except ValueError:
            days = 7
        days = max(1, min(days, 90))
        cached = cache_get(self._cache_key("quality", str(days)), ttl=300.0)
        if cached is not None:
            self._json(200, True, cached)
            return
        conn = db.connect(self.cfg)
        data = quality_mod.build(conn, self.cfg, days=days)
        cache_put(self._cache_key("quality", str(days)), data)
        self._json(200, True, data)

    def _api_health(self, qs) -> None:
        """API 健康页：注册表全量接口 + fetch_log 成功率/延迟/新鲜度。"""
        conn = db.connect(self.cfg)
        try:
            days = int((qs.get("days") or ["7"])[0])
        except ValueError:
            days = 7
        self._json(200, True, api_health_mod.build(conn, self.cfg, days=days))

    def _alerts(self, qs) -> None:
        """产品告警：当前发布的净值/经理/规模/回撤/风格漂移扫描。"""
        from .. import alerts as alerts_mod
        conn = db.connect(self.cfg)
        board = (qs.get("board") or [""])[0]
        boards = [board] if board else alerts_mod.DEFAULT_BOARDS
        as_of = (qs.get("as_of") or [""])[0] or None
        self._json(200, True, alerts_mod.scan(conn, as_of=as_of, boards=boards))

    def _fund(self, fcode: str) -> None:
        conn = db.connect(self.cfg)
        row = conn.execute("SELECT * FROM fund WHERE fcode=?", (fcode,)).fetchone()
        if not row:
            self._json(404, False, error={"code": "not_found", "message": fcode})
            return
        profile = conn.execute("SELECT * FROM fund_profile WHERE fcode=?", (fcode,)).fetchone()
        style = conn.execute("SELECT * FROM fund_style WHERE fcode=?", (fcode,)).fetchone()
        metrics = [dict(r) for r in conn.execute(
            "SELECT period_days, annual_dec, vol_dec, mdd_dec, sharpe, sortino, calmar_dec,"
            " positive_ratio_pct, flat_ratio_pct, non_negative_ratio_pct,"
            " total_return_pct, n_obs, median_gap_days, p75_gap_days,"
            " min_points_used,"
            " undefined_reasons_json"
            " FROM fund_metric WHERE fcode=? AND formula_contract=?"
            " ORDER BY period_days", (fcode, self._contract()))]
        decision = conn.execute(
            "SELECT * FROM screen_decision WHERE fcode=? ORDER BY run_id DESC LIMIT 1",
            (fcode,)).fetchone()
        pick = conn.execute(
            "SELECT board, rank, formal_score, confidence FROM screen_pick WHERE fcode=?"
            " ORDER BY run_id DESC LIMIT 1", (fcode,)).fetchone()
        holdings = [dict(r) for r in conn.execute(
            "SELECT bond_code, bond_name, pct_nav, market_value_wan, report_period, bond_class"
            " FROM fund_holding_bond WHERE fcode=? ORDER BY pct_nav DESC LIMIT 15", (fcode,))]
        holding_summary = [{"bond_class": r["bond_class"],
                            "label": labels_mod.label_bond_class(r["bond_class"]),
                            "count": r["c"], "pct_nav": round(r["pct"] or 0.0, 4)}
                           for r in conn.execute(
            "SELECT bond_class, COUNT(*) AS c, SUM(pct_nav) AS pct FROM fund_holding_bond"
            " WHERE fcode=? GROUP BY 1 ORDER BY pct DESC", (fcode,))]
        managers = [dict(m) for m in conn.execute(
            "SELECT mgr_id, mgr_name, is_lead, career_days, tenure_return_pct, power_avr, star,"
            " fund_size_text, observed_at FROM fund_manager_evidence WHERE fcode=?"
            " ORDER BY is_lead DESC, career_days DESC", (fcode,))]
        manager_tenure = [dict(m) for m in conn.execute(
            "SELECT mgr_id, mgr_name, start_date, end_date, days, is_current"
            " FROM fund_manager_tenure WHERE fcode=? ORDER BY start_date DESC", (fcode,))]
        tail = conn.execute(
            "SELECT window_days, start_date, end_date, n_obs, mdd_dec, worst_rolling90d_pct,"
            " worst_rolling90d_start, worst_rolling90d_end, worst_rolling180d_pct, cvar5_dec,"
            " worst_day_pct, worst_day_date, worst_week_pct, positive_month_ratio_pct,"
            " months_total, months_positive, max_underwater_obs, ongoing_drawdown, completeness,"
            " undefined_reasons_json FROM fund_tail_metric WHERE fcode=?"
            " AND formula_contract=? ORDER BY computed_at DESC LIMIT 1",
            (fcode, self._tail_contract())).fetchone()
        latest_nav = conn.execute(
            "SELECT nav_date, unit_nav, acc_nav, growth_pct FROM fund_nav_daily"
            " WHERE fcode=? ORDER BY nav_date DESC LIMIT 1", (fcode,)).fetchone()
        buyable = conn.execute(
            "SELECT business_date, purchase_status, redemption_status, buyable_state,"
            " next_open_date, min_purchase, fee_pct FROM fund_buyable_daily"
            " WHERE fcode=? ORDER BY business_date DESC LIMIT 1", (fcode,)).fetchone()
        nav_bounds = conn.execute(
            "SELECT MIN(nav_date) AS first_date, MAX(nav_date) AS last_date, COUNT(*) AS n_obs"
            " FROM fund_nav_daily WHERE fcode=?", (fcode,)).fetchone()
        reasons = []
        if decision:
            try:
                reasons = json.loads(decision["reason_codes_json"] or "[]")
            except ValueError:
                reasons = []
        labels = {
            "style": labels_mod.label_style(style["style_code"] if style else None),
            "confidence": labels_mod.label_confidence(style["confidence"] if style else None),
            "scale_source": labels_mod.label_scale_source(
                profile["asset_scale_src"] if profile else None),
            "board": labels_mod.label_board(pick["board"] if pick else None),
            "layer": labels_mod.label_layer(decision["layer_reached"] if decision else None),
            "buyable": labels_mod.label_buyable(buyable["buyable_state"] if buyable else None),
            "reasons": [{"code": r, "label": labels_mod.label_reason(r)} for r in reasons],
        }
        fee_meta = conn.execute("SELECT * FROM fund_fee_meta WHERE fcode=?", (fcode,)).fetchone()
        fee_tiers = {"purchase": [dict(x) for x in conn.execute(
            "SELECT tier_no, amount_lo_cny, amount_hi_cny, rate_pct, discount_pct, fixed_cny"
            " FROM fund_fee_tier WHERE fcode=? AND kind='purchase' ORDER BY tier_no", (fcode,))],
            "redemption": [dict(x) for x in conn.execute(
                "SELECT tier_no, days_lo, days_hi, rate_pct FROM fund_fee_tier"
                " WHERE fcode=? AND kind='redemption' ORDER BY tier_no", (fcode,))]}
        comparability_row = conn.execute(
            "SELECT as_of, flags_json, is_comparable FROM fund_metric_comparability"
            " WHERE fcode=? AND period_days=365 ORDER BY as_of DESC LIMIT 1", (fcode,)).fetchone()
        comparability = None
        if comparability_row:
            try:
                flags = json.loads(comparability_row["flags_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                flags = []
            comparability = {"as_of": comparability_row["as_of"],
                             "flags": flags,
                             "is_comparable": bool(comparability_row["is_comparable"])}
        forward = [dict(r) for r in conn.execute(
            "SELECT o.cohort_id, o.horizon_days, o.status, o.start_date, o.end_date, o.n_obs,"
            " o.total_return_pct, o.annual_dec, o.reason_code, c.business_date"
            " FROM forward_observation o JOIN forward_cohort c ON c.cohort_id = o.cohort_id"
            " WHERE o.fcode=? ORDER BY c.created_at DESC, o.horizon_days LIMIT 8", (fcode,))]
        self._json(200, True, {"fund": dict(row),
                               "forward": forward,
                               "managers": managers,
                               "manager_tenure": manager_tenure,
                               "profile": dict(profile) if profile else None,
                               "style": dict(style) if style else None,
                               "metrics": metrics,
                               "metric_comparability": comparability,
                               "factors": factors_mod.latest(conn, fcode),
                               "quality_profile": quality_mod.profile(conn, self.cfg, fcode),
                               "decision": dict(decision) if decision else None,
                               "pick": dict(pick) if pick else None,
                               "holdings": holdings,
                               "holding_summary": holding_summary,
                               "holding_history": exposure_mod.fund_history(conn, fcode),
                               "buyable": dict(buyable) if buyable else None,
                               "latest_nav": dict(latest_nav) if latest_nav else None,
                               "nav_bounds": dict(nav_bounds) if nav_bounds else None,
                               "labels": labels,
                               "tail": dict(tail) if tail else None,
                               "fee_schedule": {"meta": dict(fee_meta) if fee_meta else None,
                                                **fee_tiers}})

    def _runs(self, qs) -> None:
        conn = db.connect(self.cfg)
        limit = min(int((qs.get("limit") or ["30"])[0]), 200)
        rows = []
        for r in conn.execute("SELECT run_id, mode, status, started_at, finished_at, notes"
                              " FROM run ORDER BY started_at DESC LIMIT ?", (limit,)):
            item = dict(r)
            item["mode_label"] = labels_mod.label_run_mode(item["mode"])
            item["status_label"] = labels_mod.label_run_status(item["status"])
            rows.append(item)
        self._json(200, True, rows)

    def _provenance_summary(self, conn) -> dict:
        """从最近一次发布门禁读溯源证据；附全表总行数，用于前端数据真实性卡片。"""
        from .. import data_health
        row = conn.execute(
            "SELECT checks_json FROM publication_gate ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        evidence = None
        if row and row["checks_json"]:
            try:
                checks = json.loads(row["checks_json"])
            except (TypeError, ValueError):
                checks = []
            for chk in checks:
                if chk.get("code") == "provenance_integrity":
                    evidence = chk.get("evidence")
                    break
        total = 0
        for table, _date_col, _source_col in data_health.PROVENANCE_TABLES:
            try:
                total += int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] or 0)
            except Exception:                                # noqa: BLE001
                pass
        return {
            "status": (evidence or {}).get("status", "unavailable"),
            "tables": len((evidence or {}).get("tables") or []),
            "total_rows": total,
            "checked_rows": sum(int(t.get("rows") or 0)
                                for t in ((evidence or {}).get("tables") or [])),
            "nav_since": (evidence or {}).get("nav_since"),
            "missing_raw": (evidence or {}).get("missing_total", 0),
            "bad_format": (evidence or {}).get("bad_format_total", 0),
            "unregistered": len((evidence or {}).get("unregistered_sources") or []),
        }

    def _operations(self) -> None:
        conn = db.connect(self.cfg)
        runs = []
        for r in conn.execute("SELECT run_id, mode, status, started_at, finished_at"
                              " FROM run ORDER BY started_at DESC LIMIT 10"):
            item = dict(r)
            item["mode_label"] = labels_mod.label_run_mode(item["mode"])
            item["status_label"] = labels_mod.label_run_status(item["status"])
            runs.append(item)
        fetch = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM fetch_log GROUP BY status")}
        by_source = []
        for r in conn.execute("SELECT source_id, status, COUNT(*) AS c FROM fetch_log"
                              " GROUP BY 1,2 ORDER BY source_id"):
            item = dict(r)
            item["source_label"] = labels_mod.label_source(item["source_id"])
            item["status_label"] = labels_mod.label_fetch_status(item["status"])
            by_source.append(item)
        fetch_labels = [{"code": k, "label": labels_mod.label_fetch_status(k), "count": v}
                        for k, v in fetch.items()]
        self._json(200, True, {"runs": runs, "fetch_status": fetch,
                               "fetch_status_labels": fetch_labels,
                               "by_source": by_source,
                               "provenance": self._provenance_summary(conn)})


def lan_urls(port: int) -> list[str]:
    """列出本机所有可用的 IPv4 访问地址（跳过回环）。"""
    import socket

    urls: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in urls and not ip.startswith("127."):
                urls.append(ip)
    except OSError:
        pass
    # 兜底：UDP connect 不会真的发包，只用来取默认路由网卡的地址
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in urls and not ip.startswith("127."):
            urls.append(ip)
    except OSError:
        pass
    return [f"http://{ip}:{port}/" for ip in urls]


def _prewarm(port: int, token: str = "") -> None:
    """启动后预热首页/质量页/组合体检，避免第一个用户吃到 2–4 秒冷查询。"""
    import time
    import urllib.request
    time.sleep(0.5)
    headers = {"Authorization": "Bearer " + token} if token else {}
    for path in ("overview", "quality", "portfolio/diagnostics"):
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/api/v1/%s" % (port, path),
                                         headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
        except Exception:                     # noqa: BLE001 - 预热失败不影响服务
            pass


def serve(cfg, host: str | None = None, port: int | None = None) -> None:
    Handler.cfg = cfg
    host = host or cfg.get("api", "host", default="127.0.0.1")
    port = int(port or cfg.get("api", "port", default=8091))
    web_root = (Path(cfg.root) / cfg.get("api", "web_dir", default="web")).resolve()
    # 启动失败必须说清楚"为什么打不开"，而不是甩一段 traceback 让用户猜：
    # 端口被占用、前端目录缺失、数据库路径不存在，都是"页面打不开"的常见原因。
    if not (web_root / "index.html").is_file():
        print(f"警告：前端目录缺少 index.html（{web_root}），页面会 404。", flush=True)
    if not Path(cfg.db_path).is_file():
        print(f"警告：数据库文件不存在（{cfg.db_path}），先跑 python3 -m fundana bootstrap --migrate。",
              flush=True)
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"无法监听 {host}:{port} —— {exc}", flush=True)
        print(f"  端口被占用时先看占用者：ss -ltnp | grep :{port}", flush=True)
        print(f"  或换个端口：python3 -m fundana serve --port {port + 1}", flush=True)
        print("  或直接用常驻启动器：scripts/serve.sh restart", flush=True)
        raise SystemExit(1) from exc
    print(f"fundana web on http://{host}:{port}/  (API: /api/v1/healthz)", flush=True)
    print(f"  pid {os.getpid()} · web {web_root} · db {cfg.db_path}", flush=True)
    token = str(cfg.get("api", "auth_token", default="") or "")
    _threading.Thread(target=_prewarm, args=(port, token), daemon=True).start()
    if host in ("0.0.0.0", "::"):
        print("局域网访问地址（同一网段的设备可用）：", flush=True)
        for url in lan_urls(port) or ["<本机 IP>"]:
            print(f"  {url}", flush=True)
        token_on = bool(str(cfg.get("api", "auth_token", default="") or ""))
        print("提示：只读" + ("、已启用 Bearer Token 鉴权" if token_on else "、无鉴权")
              + "，仅供内网使用；不要做端口映射暴露到公网。", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()