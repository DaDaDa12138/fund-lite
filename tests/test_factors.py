"""风格因子归因（ADR-078）：解析、OLS、低 R² 门槛与分位。"""
from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date, timedelta

from fundana import factors
from fundana.fetch import parsers


def _mem():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE fund(fcode TEXT PRIMARY KEY, type_group TEXT);
        CREATE TABLE fund_nav_daily(fcode TEXT, nav_date TEXT, unit_nav REAL,
                                    acc_nav REAL, growth_pct REAL,
                                    PRIMARY KEY(fcode, nav_date));
        CREATE TABLE market_index_daily(index_code TEXT, trade_date TEXT, close REAL,
                                        pct_chg REAL, source_id TEXT, raw_sha256 TEXT,
                                        observed_at TEXT, PRIMARY KEY(index_code, trade_date));
        CREATE TABLE fund_factor_exposure(as_of TEXT, fcode TEXT, beta_duration REAL,
            beta_credit REAL, beta_convertible REAL, alpha REAL, r2 REAL, n_obs INTEGER,
            start_date TEXT, end_date TEXT, method TEXT, computed_at TEXT,
            alpha_annual_pct REAL, alpha_t REAL, alpha_reliable INTEGER,
            beta_duration_raw REAL, beta_credit_raw REAL, beta_convertible_raw REAL,
            alpha_method TEXT,
            PRIMARY KEY(as_of, fcode));
    """)
    return conn


def _days(n: int, start: str = "2025-01-01"):
    d = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _seed_indices(conn, days):
    """构造三条指数：企债≈0.9×国债+噪声，转债≈0.4×国债+0.3×信用+噪声。"""
    gov_close = 100.0
    corp_close = 100.0
    conv_close = 100.0
    prev = {}
    for i, day in enumerate(days):
        g = ((i * 37) % 19 - 9) / 10000.0          # ±0.09% 左右
        c = 0.9 * g + ((i * 53) % 11 - 5) / 20000.0
        v = 0.4 * g + 0.3 * c + ((i * 71) % 13 - 6) / 10000.0
        gov_close *= (1 + g)
        corp_close *= (1 + c)
        conv_close *= (1 + v)
        for code, close in (("000012", gov_close), ("000013", corp_close), ("000832", conv_close)):
            conn.execute(
                "INSERT INTO market_index_daily(index_code, trade_date, close, pct_chg,"
                " source_id, raw_sha256, observed_at) VALUES(?,?,?,?,?,?,?)",
                (code, day, close, None, "test", None, "2026-01-01T00:00:00+08:00"))
    conn.commit()


class ParseIndexKlineTest(unittest.TestCase):
    def test_rows_and_pct(self):
        payload = {"data": {"name": "国债指数", "klines": [
            "2026-10-29,100.00", "2026-10-30,101.00", "2026-11-02,100.00"]}}
        out = parsers.parse_index_kline(json.dumps(payload))
        self.assertEqual(out["name"], "国债指数")
        self.assertEqual(out["count"], 3)
        self.assertIsNone(out["rows"][0]["pct_chg"])
        self.assertAlmostEqual(out["rows"][1]["pct_chg"], 1.0, places=6)
        self.assertAlmostEqual(out["rows"][2]["pct_chg"], -0.990099, places=6)

    def test_empty_raises(self):
        with self.assertRaises(parsers.ParseError):
            parsers.parse_index_kline('{"data": null}')


class OlsTest(unittest.TestCase):
    def test_exact_three_factor(self):
        xs = []
        y = []
        for i in range(80):
            a = (i % 7 - 3) / 100.0
            b = (i % 5 - 2) / 100.0
            c = (i % 11 - 5) / 100.0
            xs.append((a, b, c))
            y.append(2.0 + 3.0 * a - 2.0 * b + 0.5 * c)
        coef, r2 = factors._ols(y, xs)
        self.assertAlmostEqual(coef[0], 2.0, places=9)
        self.assertAlmostEqual(coef[1], 3.0, places=9)
        self.assertAlmostEqual(coef[2], -2.0, places=9)
        self.assertAlmostEqual(coef[3], 0.5, places=9)
        self.assertAlmostEqual(r2, 1.0, places=9)

    def test_too_few_rows(self):
        with self.assertRaises(ValueError):
            factors._ols_coef([0.1, 0.2], [[0.1], [0.2]])


class FactorReturnsTest(unittest.TestCase):
    def test_orthogonal_credit(self):
        conn = _mem()
        days = _days(120)
        _seed_indices(conn, days)
        as_of, fmap = factors.factor_returns(conn, days[-1], window_days=365)
        self.assertEqual(as_of, days[-1])
        self.assertGreaterEqual(len(fmap), 60)
        gov = [v[0] for v in fmap.values()]
        credit = [v[1] for v in fmap.values()]
        mean_g = sum(gov) / len(gov)
        mean_c = sum(credit) / len(credit)
        cov = sum((g - mean_g) * (c - mean_c) for g, c in zip(gov, credit))
        sg = sum((g - mean_g) ** 2 for g in gov) ** 0.5
        sc = sum((c - mean_c) ** 2 for c in credit) ** 0.5
        self.assertLess(abs(cov / (sg * sc)), 1e-6)

    def test_missing_indices_raises(self):
        conn = _mem()
        with self.assertRaises(ValueError):
            factors.factor_returns(conn, "2026-11-02")


class ComputeTest(unittest.TestCase):
    def _seed_fund(self, conn, code, days, fn):
        conn.execute("INSERT INTO fund(fcode, type_group) VALUES(?,?)", (code, "bond"))
        prev = 1.0
        for i, day in enumerate(days):
            g = fn(i)
            prev *= (1 + g)
            conn.execute(
                "INSERT INTO fund_nav_daily(fcode, nav_date, unit_nav, acc_nav, growth_pct)"
                " VALUES(?,?,?,?,?)", (code, day, prev, prev, g * 100.0))
        conn.commit()

    def test_high_and_low_r2(self):
        conn = _mem()
        days = _days(140)
        _seed_indices(conn, days)
        # 与国债完全同步：R² 高，久期 beta 接近 1
        # 用指数收盘价还原 gov 日收益，构造同步基金
        closes = {r["trade_date"]: r["close"] for r in conn.execute(
            "SELECT trade_date, close FROM market_index_daily WHERE index_code='000012'")}
        gov_ret = {}
        prev = None
        for d in days:
            if prev is not None:
                gov_ret[d] = closes[d] / prev - 1.0
            prev = closes[d]
        self._seed_fund(conn, "000001", days[1:], lambda i: gov_ret[days[i + 1]])
        # 与三因子无关的噪声：R² 低，beta 留空
        self._seed_fund(conn, "000002", days[1:],
                        lambda i: ((i * 7919) % 101 - 50) / 10000.0)
        out = factors.compute(conn, types=("bond",), as_of=days[-1], window_days=365,
                              min_obs=60)
        self.assertEqual(out["computed"], 2)
        self.assertEqual(out["low_r2"], 1)
        hi = conn.execute("SELECT * FROM fund_factor_exposure WHERE fcode='000001'").fetchone()
        lo = conn.execute("SELECT * FROM fund_factor_exposure WHERE fcode='000002'").fetchone()
        self.assertGreater(hi["r2"], 0.9)
        self.assertGreater(hi["beta_duration"], 0.9)
        self.assertLess(lo["r2"], 0.05)
        self.assertIsNone(lo["beta_duration"])
        latest = factors.latest(conn, "000001")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["peer_n"], 1)
        self.assertGreaterEqual(latest["percentiles"]["duration"], 0.0)

    def test_stale_rows_replaced(self):
        conn = _mem()
        days = _days(80)
        _seed_indices(conn, days)
        self._seed_fund(conn, "000001", days[1:], lambda i: 0.0001)
        factors.compute(conn, types=("bond",), as_of=days[-1], window_days=365, min_obs=60)
        # 手工塞一条同 as_of 的旧行，重跑后应被清掉
        conn.execute("INSERT INTO fund_factor_exposure(as_of, fcode, n_obs, method, computed_at)"
                     " VALUES(?,?,?,?,?)", (days[-1], "999999", 1, "old", "x"))
        conn.commit()
        factors.compute(conn, types=("bond",), as_of=days[-1], window_days=365, min_obs=60)
        left = conn.execute("SELECT COUNT(*) FROM fund_factor_exposure WHERE fcode='999999'").fetchone()[0]
        self.assertEqual(left, 0)


if __name__ == "__main__":
    unittest.main()
