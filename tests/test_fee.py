"""费用模型单元测试：口径、边界、缺失语义。"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import unittest

from fundana.fees import model


def _tier(**kw):
    base = {"tier_no": 1, "amount_lo_cny": 0.0, "amount_hi_cny": 1_000_000.0,
            "days_lo": None, "days_hi": None, "rate_pct": 0.8, "discount_pct": 0.08,
            "fixed_cny": None}
    base.update(kw)
    return model.Tier(**base)


class NetReturnTest(unittest.TestCase):
    def test_proportional(self) -> None:
        got = model.net_return_decimal(5.0, 0.8, 0.1)
        self.assertAlmostEqual(got * 100, 4.0625, places=4)

    def test_fixed_fee(self) -> None:
        got = model.net_return_decimal(5.0, 0.0, 0.0, fixed_cny=1000, amount_cny=10000)
        self.assertAlmostEqual(got * 100, -5.5, places=6)

    def test_fixed_fee_exceeds_amount(self) -> None:
        with self.assertRaises(ValueError):
            model.net_return_decimal(5.0, 0.0, 0.0, fixed_cny=10000, amount_cny=10000)


class TierPickTest(unittest.TestCase):
    def test_amount_boundaries(self) -> None:
        tiers = [_tier(tier_no=1, amount_hi_cny=1_000_000.0, rate_pct=0.8),
                 _tier(tier_no=2, amount_lo_cny=1_000_000.0, amount_hi_cny=2_000_000.0, rate_pct=0.5),
                 _tier(tier_no=3, amount_lo_cny=2_000_000.0, amount_hi_cny=None, rate_pct=None,
                       fixed_cny=1000.0)]
        self.assertEqual(model.pick_purchase(tiers, 999_999).tier_no, 1)
        self.assertEqual(model.pick_purchase(tiers, 1_000_000).tier_no, 2)
        self.assertEqual(model.pick_purchase(tiers, 5_000_000).tier_no, 3)

    def test_redemption_boundaries(self) -> None:
        tiers = [model.Tier(1, days_lo=0, days_hi=6, rate_pct=1.5),
                 model.Tier(2, days_lo=7, days_hi=29, rate_pct=0.75),
                 model.Tier(3, days_lo=30, days_hi=364, rate_pct=0.1),
                 model.Tier(4, days_lo=365, days_hi=729, rate_pct=0.05),
                 model.Tier(5, days_lo=730, days_hi=None, rate_pct=0.0)]
        self.assertEqual(model.pick_redemption(tiers, 6).rate_pct, 1.5)
        self.assertEqual(model.pick_redemption(tiers, 7).rate_pct, 0.75)
        self.assertEqual(model.pick_redemption(tiers, 364).rate_pct, 0.1)
        self.assertEqual(model.pick_redemption(tiers, 365).rate_pct, 0.05)
        self.assertEqual(model.pick_redemption(tiers, 9999).rate_pct, 0.0)


class QuoteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE fund_fee_meta(fcode TEXT PRIMARY KEY, mgr_fee_pct REAL, trust_fee_pct REAL,
          sales_fee_pct REAL, has_discount INTEGER, source TEXT, observed_at TEXT, raw_sha256 TEXT);
        CREATE TABLE fund_fee_tier(fcode TEXT, kind TEXT, tier_no INTEGER, amount_lo_cny REAL,
          amount_hi_cny REAL, days_lo INTEGER, days_hi INTEGER, rate_pct REAL, discount_pct REAL,
          fixed_cny REAL, observed_at TEXT, raw_sha256 TEXT);
        CREATE TABLE fund_metric(fcode TEXT, period_days INTEGER, total_return_pct REAL,
          mdd_dec REAL, computed_at TEXT);
        CREATE TABLE fund_buyable_daily(fcode TEXT, business_date TEXT, buyable_state TEXT);
        CREATE TABLE fund_holding_policy(fcode TEXT PRIMARY KEY, effective_days INTEGER);
        CREATE TABLE fund_nav_daily(fcode TEXT, nav_date TEXT, growth_pct REAL);
        """)

    def _seed(self, *, buyable="confirmed_yes", mdd=0.003, ret=4.0):
        self.conn.execute("INSERT INTO fund_fee_meta VALUES('000001',0.3,0.1,0,1,'em_jjfl','x','y')")
        self.conn.execute("INSERT INTO fund_fee_tier VALUES('000001','purchase',1,0,1000000,"
                          "NULL,NULL,0.8,0.08,NULL,'x','y')")
        self.conn.execute("INSERT INTO fund_fee_tier VALUES('000001','redemption',1,"
                          "NULL,NULL,0,6,1.5,NULL,NULL,'x','y')")
        self.conn.execute("INSERT INTO fund_fee_tier VALUES('000001','redemption',2,"
                          "NULL,NULL,30,364,0.1,NULL,NULL,'x','y')")
        self.conn.execute("INSERT INTO fund_metric VALUES('000001',365,?,?,'x')", (ret, mdd))
        self.conn.execute("INSERT INTO fund_buyable_daily VALUES('000001','2026-10-31',?)", (buyable,))

    def _seed_nav(self, days: int = 10, growth: float = 1.0) -> None:
        end = _dt.date(2026, 10, 31)
        for i in range(days):
            d = (end - _dt.timedelta(days=days - 1 - i)).isoformat()
            self.conn.execute("INSERT INTO fund_nav_daily VALUES('000001',?,?)", (d, growth))

    def test_window_matched_return(self) -> None:
        self._seed()
        self._seed_nav()
        q = model.quote(self.conn, "000001", 10000, 5)
        self.assertEqual(q.nav_return_window_days, 5)
        self.assertAlmostEqual(q.nav_return_pct, (1.01 ** 5 - 1) * 100, places=6)
        self.assertNotIn("nav_return_window_proxy:365d", q.reasons)

    def test_window_proxy_when_nav_missing(self) -> None:
        self._seed()
        q = model.quote(self.conn, "000001", 10000, 30)
        self.assertEqual(q.nav_return_window_days, 365)
        self.assertIn("nav_return_window_proxy:365d", q.reasons)

    def test_missing_schedule_is_unknown(self) -> None:
        q = model.quote(self.conn, "999999", 10000, 30)
        self.assertEqual(q.return_type, "unknown")
        self.assertEqual(q.user_fit, "compatible_with_cost_unknown")
        self.assertIn("fee_schedule_unavailable", q.reasons)

    def test_scenario_quote(self) -> None:
        self._seed()
        q = model.quote(self.conn, "000001", 10000, 30)
        self.assertEqual(q.return_type, "fee_net_scenario_assumed")
        self.assertAlmostEqual(q.net_return_pct, 3.0714, places=3)
        self.assertAlmostEqual(q.net_return_discount_pct, 3.8129, places=3)
        self.assertEqual(q.user_fit, "compatible_under_modeled_assumptions")

    def test_not_buyable(self) -> None:
        self._seed(buyable="confirmed_no")
        q = model.quote(self.conn, "000001", 10000, 30)
        self.assertEqual(q.user_fit, "unknown_or_unavailable")

    def test_lockup_conflict_wins_over_cost(self) -> None:
        self._seed()
        self.conn.execute("INSERT INTO fund_holding_policy VALUES('000001',365)")
        q = model.quote(self.conn, "000001", 10000, 30)
        self.assertEqual(q.user_fit, "conflict_lockup")
        self.assertIn("lockup_conflict:365d", q.reasons)

    def test_lockup_conflict_without_schedule(self) -> None:
        self.conn.execute("INSERT INTO fund_holding_policy VALUES('000001',365)")
        q = model.quote(self.conn, "000001", 10000, 30)
        self.assertEqual(q.return_type, "unknown")
        self.assertEqual(q.user_fit, "conflict_lockup")

    def test_lockup_satisfied_when_holding_longer(self) -> None:
        self._seed()
        self.conn.execute("INSERT INTO fund_holding_policy VALUES('000001',180)")
        self.conn.execute("INSERT INTO fund_fee_tier VALUES('000001','redemption',3,"
                          "NULL,NULL,365,729,0.05,NULL,NULL,'x','y')")
        q = model.quote(self.conn, "000001", 10000, 400)
        self.assertNotEqual(q.user_fit, "conflict_lockup")

    def test_risk_budget_conflict_wins(self) -> None:
        self._seed(mdd=0.2)
        q = model.quote(self.conn, "000001", 10000, 30, user_max_drawdown=0.05)
        self.assertEqual(q.user_fit, "risk_budget_conflict")


if __name__ == "__main__":
    unittest.main()