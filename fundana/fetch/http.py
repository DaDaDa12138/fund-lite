"""HTTP 采集：限频、重试、原始对象落库、fetch_log/source_state 记账、TTL 复用。

关键纪律：
- 判活看业务解析结果（parser 抛 ParseError 或返回空 → business_ok=0），不看 HTTP 200；
- 5xx/超时重试（1s/4s/9s），HTTP 200 但业务失败不重试，直接记 provider_null/parse_failed；
- 编码按注册表声明，失败回退 gb18030（腾讯/新浪 GBK，HBJJ GB18030）；
- 每个响应必须落 raw_object（sha256 内容寻址）并写 fetch_log。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import requests

from .. import db

_ENCODING_FALLBACK = ("utf-8", "gb18030")


@dataclass
class FetchResult:
    source_id: str
    key: str | None
    url: str
    http_status: int | None
    text: str
    raw_sha256: str | None
    business_ok: bool
    status: str
    error_msg: str | None = None
    from_cache: bool = False


class RateLimiter:
    """按 host 的最小间隔限频（单进程内线程安全）。"""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, qps: float) -> None:
        if qps <= 0:
            return
        gap = 1.0 / qps
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            sleep_for = gap - (now - last)
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
            self._last[host] = now


class Fetcher:
    def __init__(self, cfg, conn, run_id: str) -> None:
        self.cfg = cfg
        self.conn = conn
        self.run_id = run_id
        self.limiter = RateLimiter()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": cfg.app["fetch"]["user_agent"],
            "Accept-Encoding": "gzip, deflate",
        })

    def build_url(self, source_id: str, key: str | None,
                  variables: dict[str, Any] | None = None) -> str:
        spec = self.cfg.sources[source_id]
        url = spec.url
        for name, value in (variables or {}).items():
            url = url.replace("{" + name + "}", str(value))
        if "{code}" in url:
            if not key:
                raise ValueError(f"{source_id} 需要 key")
            url = url.replace("{code}", key)
        return url

    def is_fresh(self, source_id: str, key: str | None) -> str | None:
        spec = self.cfg.sources[source_id]
        state = db.get_source_state(self.conn, source_id, key or "-")
        if not state or not state["last_success_at"]:
            return None
        last = db.parse_ts(state["last_success_at"])
        if last is None:
            return None
        if datetime.now(db.CN) - last < timedelta(days=spec.ttl_days):
            return state["last_raw_sha256"]
        return None

    def get(self, source_id: str, key: str | None = None, *,
            force: bool = False, params: dict[str, Any] | None = None,
            allow_cache: bool = True, timeout: int | None = None,
            variables: dict[str, Any] | None = None) -> FetchResult:
        spec = self.cfg.sources[source_id]
        url = self.build_url(source_id, key, variables)
        if allow_cache and not force:
            cached_sha = self.is_fresh(source_id, key)
            if cached_sha:
                body = db.get_raw(self.conn, cached_sha)
                text = self._decode(body, spec.encoding)
                return FetchResult(source_id, key, url, 200, text, cached_sha, True, "ok",
                                   None, from_cache=True)

        cfg_fetch = self.cfg.app["fetch"]
        attempts = int(cfg_fetch.get("retries", 3))
        backoff = cfg_fetch.get("retry_backoff_s", [1, 4, 9])
        timeout = timeout or int(cfg_fetch.get("timeout_s", 20))
        last_err: str | None = None
        last_status: int | None = None

        for attempt in range(1, attempts + 1):
            started_at = db.now_iso()
            t0 = time.monotonic()
            try:
                self.limiter.wait(urlparse(url).netloc, spec.rate_qps)
                resp = self.session.get(url, headers=spec.headers, params=params, timeout=timeout)
                duration = int((time.monotonic() - t0) * 1000)
                last_status = resp.status_code
                if resp.status_code >= 500:
                    last_err = f"HTTP {resp.status_code}"
                    db.log_fetch(self.conn, self.run_id, source_id, key, url, started_at, duration,
                                 resp.status_code, False, "http_error", last_err, None, attempt)
                    if attempt < attempts:
                        time.sleep(float(backoff[min(attempt - 1, len(backoff) - 1)]))
                        continue
                    db.update_source_state(self.conn, source_id, key or "-", "http_error", None, last_err, False)
                    self.conn.commit()
                    return FetchResult(source_id, key, url, resp.status_code, "", None, False,
                                       "http_error", last_err)
                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code}"
                    sha = db.put_raw(self.conn, self.cfg, resp.content, url, resp.status_code,
                                     resp.headers.get("Content-Type"), spec.encoding)
                    db.log_fetch(self.conn, self.run_id, source_id, key, url, started_at, duration,
                                 resp.status_code, False, "http_error", last_err, sha, attempt)
                    db.update_source_state(self.conn, source_id, key or "-", "http_error", sha, last_err, False)
                    self.conn.commit()
                    return FetchResult(source_id, key, url, resp.status_code, "", sha, False,
                                       "http_error", last_err)

                body = resp.content
                text = self._decode(body, spec.encoding, resp.encoding)
                sha = db.put_raw(self.conn, self.cfg, body, url, resp.status_code,
                                 resp.headers.get("Content-Type"), spec.encoding)
                if not text.strip():
                    db.log_fetch(self.conn, self.run_id, source_id, key, url, started_at, duration,
                                 resp.status_code, False, "provider_null", "空响应体", sha, attempt)
                    db.update_source_state(self.conn, source_id, key or "-", "provider_null", sha,
                                           "空响应体", False)
                    self.conn.commit()
                    return FetchResult(source_id, key, url, resp.status_code, "", sha, False,
                                       "provider_null", "空响应体")
                db.log_fetch(self.conn, self.run_id, source_id, key, url, started_at, duration,
                             resp.status_code, True, "ok", None, sha, attempt)
                db.update_source_state(self.conn, source_id, key or "-", "ok", sha, None, True)
                self.conn.commit()
                return FetchResult(source_id, key, url, resp.status_code, text, sha, True, "ok")

            except (requests.Timeout, requests.ConnectionError) as exc:
                duration = int((time.monotonic() - t0) * 1000)
                last_err = f"{type(exc).__name__}: {exc}"
                db.log_fetch(self.conn, self.run_id, source_id, key, url, started_at, duration,
                             None, False, "timeout", last_err, None, attempt)
                if attempt < attempts:
                    time.sleep(float(backoff[min(attempt - 1, len(backoff) - 1)]))
                    continue
                db.update_source_state(self.conn, source_id, key or "-", "timeout", None, last_err, False)
                self.conn.commit()
                return FetchResult(source_id, key, url, None, "", None, False, "timeout", last_err)

        db.update_source_state(self.conn, source_id, key or "-", "http_error", None, last_err, False)
        self.conn.commit()
        return FetchResult(source_id, key, url, last_status, "", None, False, "http_error", last_err)

    def post_form(self, source_id: str, key: str | None = None, *,
                  data: dict[str, Any] | None = None, force: bool = False,
                  allow_cache: bool = True, timeout: int | None = None) -> FetchResult:
        """POST application/x-www-form-urlencoded；与 get() 同样落 raw_object/fetch_log。"""
        spec = self.cfg.sources[source_id]
        url = self.build_url(source_id, key)
        if allow_cache and not force:
            cached_sha = self.is_fresh(source_id, key)
            if cached_sha:
                body = db.get_raw(self.conn, cached_sha)
                text = self._decode(body, spec.encoding)
                return FetchResult(source_id, key, url, 200, text, cached_sha, True, "ok",
                                   None, from_cache=True)

        cfg_fetch = self.cfg.app["fetch"]
        attempts = int(cfg_fetch.get("retries", 3))
        backoff = cfg_fetch.get("retry_backoff_s", [1, 4, 9])
        timeout = timeout or int(cfg_fetch.get("timeout_s", 20))
        last_err: str | None = None
        last_status: int | None = None
        for attempt in range(1, attempts + 1):
            started_at = db.now_iso()
            t0 = time.monotonic()
            try:
                self.limiter.wait(urlparse(url).netloc, spec.rate_qps)
                resp = self.session.post(url, headers=spec.headers, data=data or {}, timeout=timeout)
                duration = int((time.monotonic() - t0) * 1000)
                last_status = resp.status_code
                if resp.status_code >= 500:
                    last_err = f"HTTP {resp.status_code}"
                    db.log_fetch(self.conn, self.run_id, source_id, key, url, started_at, duration,
                                 resp.status_code, False, "http_error", last_err, None, attempt)
                    if attempt < attempts:
                        time.sleep(float(backoff[min(attempt - 1, len(backoff) - 1)]))
                        continue
                    db.update_source_state(self.conn, source_id, key or "-", "http_error", None, last_err, False)
                    self.conn.commit()
                    return FetchResult(source_id, key, url, resp.status_code, "", None, False,
                                       "http_error", last_err)
                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code}"
                    sha = db.put_raw(self.conn, self.cfg, resp.content, url, resp.status_code,
                                     resp.headers.get("Content-Type"), spec.encoding)
                    db.log_fetch(self.conn, self.run_id, source_id, key, url, started_at, duration,
                                 resp.status_code, False, "http_error", last_err, sha, attempt)
                    db.update_source_state(self.conn, source_id, key or "-", "http_error", sha, last_err, False)
                    self.conn.commit()
                    return FetchResult(source_id, key, url, resp.status_code, "", sha, False,
                                       "http_error", last_err)

                body = resp.content
                text = self._decode(body, spec.encoding, resp.encoding)
                sha = db.put_raw(self.conn, self.cfg, body, url, resp.status_code,
                                 resp.headers.get("Content-Type"), spec.encoding)
                if not text.strip():
                    db.log_fetch(self.conn, self.run_id, source_id, key, url, started_at, duration,
                                 resp.status_code, False, "provider_null", "空响应体", sha, attempt)
                    db.update_source_state(self.conn, source_id, key or "-", "provider_null", sha,
                                           "空响应体", False)
                    self.conn.commit()
                    return FetchResult(source_id, key, url, resp.status_code, "", sha, False,
                                       "provider_null", "空响应体")
                db.log_fetch(self.conn, self.run_id, source_id, key, url, started_at, duration,
                             resp.status_code, True, "ok", None, sha, attempt)
                db.update_source_state(self.conn, source_id, key or "-", "ok", sha, None, True)
                self.conn.commit()
                return FetchResult(source_id, key, url, resp.status_code, text, sha, True, "ok")

            except (requests.Timeout, requests.ConnectionError) as exc:
                duration = int((time.monotonic() - t0) * 1000)
                last_err = f"{type(exc).__name__}: {exc}"
                db.log_fetch(self.conn, self.run_id, source_id, key, url, started_at, duration,
                             None, False, "timeout", last_err, None, attempt)
                if attempt < attempts:
                    time.sleep(float(backoff[min(attempt - 1, len(backoff) - 1)]))
                    continue
                db.update_source_state(self.conn, source_id, key or "-", "timeout", None, last_err, False)
                self.conn.commit()
                return FetchResult(source_id, key, url, None, "", None, False, "timeout", last_err)

        db.update_source_state(self.conn, source_id, key or "-", "http_error", None, last_err, False)
        self.conn.commit()
        return FetchResult(source_id, key, url, last_status, "", None, False, "http_error", last_err)

    def mark_business_failure(self, result: FetchResult, status: str, message: str) -> FetchResult:
        """HTTP 成功但业务解析失败：改写该次 fetch_log 并更新 source_state。"""
        self.conn.execute(
            "UPDATE fetch_log SET business_ok=0, status=?, error_msg=? WHERE id=("
            "SELECT MAX(id) FROM fetch_log WHERE run_id=? AND source_id=? AND COALESCE(key,'')=COALESCE(?,''))",
            (status, message, self.run_id, result.source_id, result.key))
        db.update_source_state(self.conn, result.source_id, result.key or "-", status,
                               result.raw_sha256, message, False)
        self.conn.commit()
        return FetchResult(result.source_id, result.key, result.url, result.http_status, result.text,
                           result.raw_sha256, False, status, message, result.from_cache)

    @staticmethod
    def _decode(body: bytes, declared: str, guessed: str | None = None) -> str:
        for enc in [declared, guessed, *_ENCODING_FALLBACK]:
            if not enc:
                continue
            try:
                return body.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return body.decode("utf-8", errors="replace")
