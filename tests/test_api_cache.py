import unittest

from fundana.api import server


class TestApiCache(unittest.TestCase):
    def setUp(self):
        server.cache_clear()

    def tearDown(self):
        server.cache_clear()

    def test_cache_returns_deep_copy(self):
        server.cache_put("k", {"a": [1]})
        first = server.cache_get("k")
        first["a"].append(2)
        self.assertEqual(server.cache_get("k"), {"a": [1]})

    def test_expired_entry_is_miss(self):
        server.cache_put("k", {"a": 1})
        self.assertIsNone(server.cache_get("k", ttl=0.0))


if __name__ == "__main__":
    unittest.main()
