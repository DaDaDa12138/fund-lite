"""P1-10 告警落盘与 webhook 失败不吞主流程。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from fundana import notify


class _Cfg:
    def __init__(self, root, webhook=""):
        self.root = Path(root)
        self.webhook = webhook

    def get(self, section, key, default=None, **_kw):
        if section == "paths" and key == "state_dir":
            return str(self.root / "state")
        if section == "ops" and key == "webhook_url":
            return self.webhook
        return default


class TestNotify(unittest.TestCase):
    def test_writes_jsonl_without_webhook(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = notify.send(_Cfg(tmp), "daily failed", "timeout", unit="u")
            self.assertTrue(got["logged"])
            self.assertFalse(got["pushed"])
            path = Path(tmp) / "state" / "alerts.jsonl"
            item = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(item["title"], "daily failed")
            self.assertEqual(item["unit"], "u")

    def test_webhook_failure_still_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(notify.requests, "post", side_effect=requests.RequestException("down")):
                got = notify.send(_Cfg(tmp, "http://example.invalid/hook"), "x", "y")
            self.assertTrue(got["logged"])
            self.assertFalse(got["pushed"])
            self.assertIn("down", got["error"])


if __name__ == "__main__":
    unittest.main()
