"""上海时区不变量：裸时间按 +08:00 解释，业务日与导出文件名不跟宿主机时区走。"""
from __future__ import annotations

import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fundana import collect, db
from fundana.api import csv_export as ex
from fundana.config import load_config


class ParseTimestampTest(unittest.TestCase):
    def test_naive_timestamp_is_shanghai_not_utc(self):
        dt = db.parse_ts("2026-11-02T07:30:00")
        self.assertEqual(dt.isoformat(), "2026-11-02T07:30:00+08:00")

    def test_utc_timestamp_is_converted_to_shanghai(self):
        dt = db.parse_ts("2026-11-01T23:30:00+00:00")
        self.assertEqual(dt.isoformat(), "2026-11-02T07:30:00+08:00")

    def test_invalid_or_missing_timestamp_is_none(self):
        self.assertIsNone(db.parse_ts(None))
        self.assertIsNone(db.parse_ts(""))
        self.assertIsNone(db.parse_ts("not-a-time"))


class BusinessDateTest(unittest.TestCase):
    def test_config_declares_shanghai(self):
        self.assertEqual(load_config().timezone, "Asia/Shanghai")

    def test_collect_rank_uses_passed_business_date(self):
        class FakeFetcher:
            def __init__(self):
                self.params = None

            def get(self, source_id, key=None, *, params=None, allow_cache=True):
                self.params = params
                return SimpleNamespace(business_ok=True, status="ok", text="", raw_sha256="sha")

        fetcher = FakeFetcher()
        with patch.object(collect.parsers, "parse_rank", return_value={"rows": [], "declared_total": 0}):
            out = collect.collect_rank(fetcher, "2020-01-02")
        self.assertTrue(out["ok"])
        self.assertEqual(fetcher.params["ed"], "2020-01-02")
        self.assertEqual(
            fetcher.params["sd"],
            (date(2020, 1, 2) - timedelta(days=365 * 3)).isoformat(),
        )

    def test_export_filename_uses_shanghai_today(self):
        with patch.object(ex.db, "today_cn", return_value="2026-11-02"):
            self.assertEqual(ex.filename("bond_rankings"), "bond_rankings_20261102.csv")
