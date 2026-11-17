"""榜单行 SQL 的回归测试：多版本指标不能让同一只基金出两行。

线上事故：fund_tail_metric 同时存在 fundana-tail-2026.11.1 / .2 两个契约，
ROW_SQL 漏锁一处契约 → 同一只基金 JOIN 出两行，榜单序号变成 1、1、2、2。
"""
from __future__ import annotations

import sqlite3
import unittest

from fundana import db
from fundana.api import rankings


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


class RankingRowContractTest(unittest.TestCase):
    """ROW_SQL 必须同时锁住风险契约与尾部契约。"""

    def setUp(self):
        self.conn = _conn()
        c = self.conn
        c.execute("INSERT INTO fund(fcode, name, identity_status) VALUES('000001','甲','ok')")
        c.execute("INSERT INTO screen_run(run_id, profile, config_hash, code_version,"
                  " formula_contract, created_at, business_date)"
                  " VALUES('r1','bond_v1','h','0.1.0','risk-A','2026-11-01T10:00:00','2026-11-01')")
        c.execute("INSERT INTO screen_pick(run_id, board, rank, fcode, formal_score)"
                  " VALUES('r1','bond_stable',1,'000001',80.0)")
        c.execute("INSERT INTO screen_decision(run_id, fcode, layer_reached, passed,"
                  " reason_codes_json, style_code)"
                  " VALUES('r1','000001','L6C',1,'[]','bond_pure')")
        for contract, annual in (("risk-A", 0.02), ("risk-B", 0.99)):
            c.execute("INSERT INTO fund_metric(fcode, period_days, formula_contract, annual_dec,"
                      " end_date) VALUES('000001', 365, ?, ?, '2026-11-01')", (contract, annual))
        for contract, mdd in (("tail-A", 0.05), ("tail-B", 0.09)):
            c.execute("INSERT INTO fund_tail_metric(fcode, window_days, formula_contract,"
                      " mdd_dec, completeness, end_date)"
                      " VALUES('000001', 1095, ?, ?, 1.0, '2026-11-01')", (contract, mdd))
        # 000002 只有 2026-11-03 的指标：as-of 2026-11-02 的榜单不能读到它。
        c.execute("INSERT INTO fund(fcode, name, identity_status) VALUES('000002','乙','ok')")
        c.execute("INSERT INTO screen_pick(run_id, board, rank, fcode, formal_score)"
                  " VALUES('r1','bond_stable',2,'000002',70.0)")
        c.execute("INSERT INTO fund_metric(fcode, period_days, formula_contract, annual_dec,"
                  " end_date) VALUES('000002', 365, 'risk-A', 0.99, '2026-11-03')")
        c.execute("INSERT INTO fund_nav_daily(fcode, nav_date, growth_pct, nav_source)"
                  " VALUES('000002', '2026-11-03', 0.1, 'test')")
        c.commit()

    def tearDown(self):
        self.conn.close()

    def _rows(self, risk="risk-A", tail="tail-A", cutoff="2026-11-30"):
        sql = rankings.ROW_SQL + " WHERE p.run_id=? AND p.board=? ORDER BY p.rank"
        params = [cutoff]
        for _ in range(7):
            params += [risk, cutoff]
        params += [1095, tail, cutoff, "r1", "bond_stable"]
        return self.conn.execute(sql, params).fetchall()

    def _row(self, fcode, **kw):
        return next(r for r in self._rows(**kw) if r["fcode"] == fcode)

    def test_two_contracts_do_not_duplicate_rows(self):
        rows = self._rows()
        self.assertEqual(len(rows), 2, "多契约共存时同一只基金只能出一行")
        row = self._row("000001")
        self.assertAlmostEqual(row["annual_1y"], 0.02)
        self.assertAlmostEqual(row["mdd_3y"], 0.05)

    def test_tail_contract_selects_its_own_version(self):
        row = self._row("000001", tail="tail-B")
        self.assertAlmostEqual(row["mdd_3y"], 0.09)
        self.assertAlmostEqual(row["annual_1y"], 0.02)

    def test_asof_cutoff_excludes_future_metric(self):
        """as-of 2026-11-02 的榜单不能读到 2026-11-03 的指标。"""
        row = self._row("000002", cutoff="2026-11-02")
        self.assertIsNone(row["annual_1y"])
        self.assertIsNone(row["metric_end_date"])
        self.assertIsNone(row["nav_date"])

    def test_placeholder_count_is_eighteen(self):
        """净值 as-of + 7×(契约+指标 as-of) + 尾部窗口/契约/as-of：漏一处就会前视或复制行。"""
        self.assertEqual(rankings.ROW_SQL.count("?"), 18)


if __name__ == "__main__":
    unittest.main()
