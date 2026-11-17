"""融合层作用域与规模层级测试：universe 过滤、purge、基金级/份额级规模不互相判冲突。"""
import json
import tempfile
import unittest
from pathlib import Path

from fundana import db
from fundana.fuse import identity, profile


class StubCfg:
    def __init__(self, root: Path):
        self.app = {"db": {"busy_timeout_ms": 15000, "wal": False}}
        self.root = root
        self.db_path = root / "t.db"
        self.raw_dir = root / "raw"
        self.freeze_dir = root / "freeze"
        self.report_root = root / "reports"
        self.code_version = "0.1.0"

    def get(self, *keys, default=None):
        return default


BD = "2026-10-31"


def seed(conn):
    conn.executemany(
        "INSERT INTO src_directory(business_date, observed_at, code, name, pinyin_abbr, type_text,"
        " pinyin_full, raw_sha256) VALUES(?,?,?,?,?,?,?,?)",
        [(BD, "t", "000001", "测试纯债债券A", "CS", "债券型-长债", "x", "sha1"),
         (BD, "t", "110022", "测试股票A", "GP", "股票型", "y", "sha2")])
    conn.execute(
        "INSERT INTO src_jbgk_profile(code, business_date, observed_at, full_name, estab_date, company_name,"
        " custodian, asset_scale, share_scale, mgr_fee_pct, trust_fee_pct, sales_fee_pct, benchmark_text,"
        " tracking_text, max_sub_fee_pct, max_red_fee_pct, raw_sha256)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("000001", BD, "t", "测试纯债债券型证券投资基金", "2018-05-01", "测试基金", "某行",
         "63.80亿元（截止至：2026年09月30日）", "40.00亿份", 0.30, 0.10, 0.0, "中债综合指数", None, 0.8, 1.5, "sha3"))
    conn.execute(
        "INSERT INTO src_rank_row(business_date, observed_at, code, name, day_pct, scale_yi, raw_sha256, page,"
        " row_ord) VALUES(?,?,?,?,?,?,?,?,?)",
        (BD, "t", "000001", "测试纯债债券A", 0.01, 61.0, "sha4", 1, 1))
    conn.execute(
        "INSERT INTO src_pzd_profile(code, business_date, observed_at, nav_points, scale_json, raw_sha256)"
        " VALUES(?,?,?,?,?,?)",
        ("000001", BD, "t", 1200, json.dumps([[1600000000, 30.0], [1700000000, 31.5]]), "sha5"))
    conn.commit()


class TestFuse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = StubCfg(Path(self.tmp.name))
        self.conn = db.connect(self.cfg)
        db.migrate(self.conn)
        seed(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_identity_only_target_types(self):
        out = identity.run_identity(self.conn, "run1", BD, {"bond"})
        self.assertEqual(out["funds"], 1)
        codes = [r["fcode"] for r in self.conn.execute("SELECT fcode FROM fund")]
        self.assertEqual(codes, ["000001"])

    def test_purge_removes_foreign_rows_and_dependents(self):
        identity.run_identity(self.conn, "run1", BD, None)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM fund").fetchone()[0], 2)
        self.conn.execute(
            "INSERT INTO fund_profile(fcode, asset_scale_billion, updated_at) VALUES('110022', 1.0, 't')")
        self.conn.commit()
        out = identity.purge_non_universe(self.conn, {"bond"})
        self.assertEqual(out["purged"], 1)
        self.assertEqual([r["fcode"] for r in self.conn.execute("SELECT fcode FROM fund")], ["000001"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM fund_profile WHERE fcode='110022'").fetchone()[0], 0)

    def test_scale_is_family_sum_and_ignores_rank(self):
        identity.run_identity(self.conn, "run1", BD, {"bond"})
        profile.run_profiles(self.conn, BD, 90)
        row = self.conn.execute("SELECT * FROM fund_profile WHERE fcode='000001'").fetchone()
        # 单成员家族：基金级 = 份额级 = 63.80；rank 第 18 段是区间涨幅，不参与规模判定
        self.assertEqual(round(row["asset_scale_billion"], 2), 63.80)
        self.assertEqual(row["asset_scale_src"], "em_jbgk_family_sum")
        self.assertIsNone(row["conflict_json"])
        src = json.loads(row["field_src_json"])
        self.assertEqual(src["asset_scale_level"], "family")
        self.assertEqual(src["share_class_scale_billion"], 63.8)
        self.assertEqual(round(row["recurring_fee_pct"], 4), 0.4)

    def test_scale_family_sums_share_classes(self):
        # 家族里再加一个 C 类份额（12.2 亿）：基金级必须是 63.80+12.20=76.00，而不是任一单一份额
        self.conn.execute(
            "INSERT INTO src_directory(business_date, observed_at, code, name, pinyin_abbr, type_text,"
            " pinyin_full, raw_sha256) VALUES(?,?,?,?,?,?,?,?)",
            (BD, "t", "000002", "测试纯债债券C", "CS", "债券型-长债", "x", "sha9"))
        self.conn.execute(
            "INSERT INTO src_jbgk_profile(code, business_date, observed_at, full_name, estab_date, company_name,"
            " custodian, asset_scale, share_scale, mgr_fee_pct, trust_fee_pct, sales_fee_pct, benchmark_text,"
            " tracking_text, max_sub_fee_pct, max_red_fee_pct, raw_sha256)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("000002", BD, "t", "测试纯债债券型证券投资基金", "2018-05-01", "测试基金", "某行",
             "12.20亿元（截止至：2026年09月30日）", "10.00亿份", 0.30, 0.10, 0.2, "中债综合指数", None, 0.0, 0.0, "sha10"))
        self.conn.commit()
        identity.run_identity(self.conn, "run1", BD, {"bond"})
        identity.run_share_classes(self.conn, BD)
        profile.run_profiles(self.conn, BD, 90)
        row = self.conn.execute("SELECT * FROM fund_profile WHERE fcode='000001'").fetchone()
        self.assertEqual(round(row["asset_scale_billion"], 2), 76.00)
        src = json.loads(row["field_src_json"])
        self.assertEqual(src["share_class_scale_billion"], 63.8)

    def _add_holding(self, code, bond_code, bond_name, pct):
        self.conn.execute(
            "INSERT INTO src_zqcc_holding(code, report_period, observed_at, bond_code, bond_name,"
            " pct_nav, market_value_wan, raw_sha256) VALUES(?,?,?,?,?,?,?,?)",
            (code, "2026Q3", "t", bond_code, bond_name, pct, 100.0, "shaz"))
        self.conn.commit()

    def test_style_uses_holdings_when_coverage_meets_threshold(self):
        from fundana.fuse import style
        identity.run_identity(self.conn, "run1", BD, {"bond"})
        self._add_holding("000001", "113050", "某转债", 30.0)
        self._add_holding("000001", "019666", "26国债01", 20.0)
        out = style.run_style(self.conn, BD, holdings_min_coverage=0.8)
        # 1/1 只覆盖 → 用持仓证据：转债占 60% ≥ 20% → bond_convertible
        self.assertTrue(out["holdings_used"])
        row = self.conn.execute("SELECT style_code, confidence FROM fund_style").fetchone()
        self.assertEqual(row["style_code"], "bond_convertible")
        self.assertEqual(row["confidence"], "structured_confirmed")
        # 持仓市值与来源 sha 必须透传到 L2（此前被写成 NULL，页面显示“—”）
        held = self.conn.execute(
            "SELECT market_value_wan, raw_sha256, bond_class FROM fund_holding_bond"
            " WHERE fcode='000001' AND bond_code='113050'").fetchone()
        self.assertEqual(held["market_value_wan"], 100.0)
        self.assertEqual(held["raw_sha256"], "shaz")
        self.assertEqual(held["bond_class"], "convertible")

    def test_style_ignores_holdings_below_coverage(self):
        from fundana.fuse import style
        self.conn.execute(
            "INSERT INTO src_directory(business_date, observed_at, code, name, pinyin_abbr, type_text,"
            " pinyin_full, raw_sha256) VALUES(?,?,?,?,?,?,?,?)",
            (BD, "t", "000002", "测试纯债债券C", "CS", "债券型-长债", "x", "sha11"))
        self.conn.commit()
        identity.run_identity(self.conn, "run1", BD, {"bond"})
        self._add_holding("000001", "113050", "某转债", 30.0)
        # 覆盖 1/2 = 0.5 < 0.8 → 整体不用持仓，两只基金同处名称/类型证据层级
        out = style.run_style(self.conn, BD, holdings_min_coverage=0.8)
        self.assertFalse(out["holdings_used"])
        self.assertEqual(out["with_holdings"], 0)
        codes = {r["fcode"]: r["style_code"] for r in self.conn.execute("SELECT * FROM fund_style")}
        self.assertEqual(codes["000001"], "bond_pure")

    def test_scale_falls_back_to_share_class_level(self):
        self.conn.execute("DELETE FROM src_jbgk_profile")
        self.conn.execute("DELETE FROM src_rank_row")
        self.conn.commit()
        identity.run_identity(self.conn, "run1", BD, {"bond"})
        profile.run_profiles(self.conn, BD, 90)
        row = self.conn.execute("SELECT * FROM fund_profile WHERE fcode='000001'").fetchone()
        self.assertEqual(row["asset_scale_billion"], 31.5)
        self.assertEqual(row["asset_scale_src"], "em_pzd")
        src = json.loads(row["field_src_json"])
        self.assertEqual(src["asset_scale_level"], "share_class")


class TestBondIndexUniverse(unittest.TestCase):
    """ADR-061：东财的「指数型-固收」按协会口径属于债券基金，必须进 universe。"""

    def test_type_group_maps_index_bond_to_bond(self):
        from fundana.collect import type_group, type_prefixes
        self.assertEqual(type_group("指数型-固收"), "bond")
        self.assertEqual(type_group("指数型-固收(ETF)"), "bond")
        self.assertEqual(type_group("指数型-股票"), "index")
        self.assertEqual(type_group("债券型-长债"), "bond")
        self.assertIn("指数型-固收", type_prefixes("bond"))
        self.assertNotIn("指数型-固收", type_prefixes("index"))

    def test_index_bond_classified_bond_index(self):
        from fundana.fuse.style import classify
        out = classify("南方中债7-10年国开行债券指数A", "指数型-固收", [])
        self.assertEqual(out["style_code"], "bond_index")
        self.assertEqual(out["confidence"], "structured_confirmed")


if __name__ == "__main__":
    unittest.main()
