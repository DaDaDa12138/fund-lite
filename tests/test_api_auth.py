"""API 可选 Bearer Token 鉴权（ADR-092）。"""
import unittest

from fundana.api.server import authorized, extract_token


class _Cfg:
    def __init__(self, token=""):
        self.token = token

    def get(self, section, key, default=None):
        if section == "api" and key == "auth_token":
            return self.token
        return default


class TestApiAuth(unittest.TestCase):
    def test_empty_token_means_open(self):
        self.assertTrue(authorized(_Cfg(""), {}))
        self.assertTrue(authorized(_Cfg(""), {"Authorization": "Bearer wrong"}))

    def test_bearer_token(self):
        cfg = _Cfg("unit-test-token")
        self.assertTrue(authorized(cfg, {"Authorization": "Bearer unit-test-token"}))
        self.assertFalse(authorized(cfg, {"Authorization": "Bearer bad"}))
        self.assertFalse(authorized(cfg, {}))

    def test_x_api_key_fallback(self):
        cfg = _Cfg("unit-test-token")
        self.assertTrue(authorized(cfg, {"X-API-Key": "unit-test-token"}))

    def test_case_insensitive_scheme_and_whitespace(self):
        self.assertEqual(extract_token({"Authorization": "bEaReR  abc  "}), "abc")
        self.assertEqual(extract_token({"X-API-Key": " k "}), "k")
        self.assertEqual(extract_token({}), "")

    def test_no_token_leak_from_other_header(self):
        self.assertFalse(authorized(_Cfg("unit-test-token"), {"Cookie": "token=unit-test-token"}))


if __name__ == "__main__":
    unittest.main()
