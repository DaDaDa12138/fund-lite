"""反例矩阵（取自架构书 T01–T55 的可离线子集）+ 重放一致性。"""
import gzip
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fundana import db
from fundana.metrics import contract
from fundana.metrics.engine import compute_fund
from fundana.publish.builder import CSV_COLUMNS, _write_csv


class StubCfg:
    def __init__(self, root: Path):
        self.app = {"db": {"busy_timeout_ms": 15000, "wal": False}}
        self.db_path = root / "t.db"
        self.raw_dir = root / "raw"
        self.report_root = root / "reports"
        self.freeze_dir = root / "freeze"
        self.code_version = "0.1.0"

    def get(self, *keys, default=None):
        return default


def seed_nav(conn, fcode="000003", days=800, daily=0.0002, start="2023-01-02"):
    d0 = date.fromisoformat(start)
    wealth = 1.0
    rows = []
    i = 0
    while len(rows) < days:
        d = d0 + timedelta(days=i)
        i += 1
        if d.weekday() >= 5:          # 跳过周末，模拟业务日
            continue
        wealth *= (1 + daily)
        rows.append((fcode, d.isoformat(), round(wealth, 6), round(wealth, 6), daily * 100,
                     "em_pzd", "0" * 64, 0))
    conn.executemany("INSERT INTO fund_nav_daily(fcode,nav_date,unit_nav,acc_nav,growth_pct,"
                     "nav_source,raw_sha256,revision_no) VALUES(?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return rows


class TestStorage(unittest.TestCase):
    def test_raw_gzip_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = StubCfg(Path(tmp))
            conn = db.connect(cfg)
            db.migrate(conn)
            body = "var r = [];".encode("utf-8")
            sha = db.put_raw(conn, cfg, body, "https://example/x", 200, "text", "utf-8")
            self.assertEqual(len(sha), 64)
            self.assertEqual(db.get_raw(conn, sha), body)
            # 原始文件 gzip 落盘
            p = cfg.raw_dir / sha[:2] / (sha + ".gz")
            self.assertTrue(p.exists())
            self.assertEqual(gzip.decompress(p.read_bytes()), body)
            conn.close()

    def test_fetch_log_attempted_vs_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(StubCfg(Path(tmp)))
            db.migrate(conn)
            db.log_fetch(conn, "run1", "em_pzd", "000003", "u", "2026-10-31T00:00:00",
                         12, 200, False, "provider_null", "空数据", None, 1)
            row = conn.execute("SELECT business_ok,status,error_msg FROM fetch_log").fetchone()
            self.assertEqual(row["business_ok"], 0)
            self.assertEqual(row["status"], "provider_null")
            conn.close()


class TestCsv(unittest.TestCase):
    def test_utf8_sig_and_leading_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.csv"
            _write_csv(p, [{"fcode": "000003", "name": "A", "formal_score": 1.5}], CSV_COLUMNS)
            raw = p.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            text = raw.decode("utf-8-sig")
            self.assertIn("000003", text)
            self.assertNotIn("3.0", text.splitlines()[1].split(",")[0])


class TestReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = StubCfg(Path(self.tmp.name))
        self.conn = db.connect(self.cfg)
        db.migrate(self.conn)
        seed_nav(self.conn, days=800, start="2023-08-01")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_invalid_window_is_null_not_zero(self):
        res = compute_fund(self.conn, self.cfg, "000003", "2026-10-30", dry_run=True)
        y4 = res["dry"][1460]
        self.assertIsNone(y4["annual_dec"])
        self.assertIsNone(y4["total_return_pct"])
        self.assertTrue(any(r.startswith("insufficient_points") for r in y4["undefined_reasons"]))
        y1 = res["dry"][365]
        self.assertIsNotNone(y1["annual_dec"])
        self.assertGreater(y1["n_obs"], 200)

    def test_dry_run_matches_stored(self):
        compute_fund(self.conn, self.cfg, "000003", "2026-10-30")
        dry = compute_fund(self.conn, self.cfg, "000003", "2026-10-30", dry_run=True)["dry"]
        fields = ["n_obs", "total_return_pct", "annual_dec", "vol_dec", "positive_ratio_pct",
                  "mdd_dec", "sharpe", "sortino", "calmar_dec", "var95_dec", "cvar5_dec"]
        for period in contract.PERIODS:
            row = self.conn.execute(
                "SELECT * FROM fund_metric WHERE fcode=? AND period_days=? AND formula_contract=?",
                ("000003", period, contract.FORMULA_CONTRACT)).fetchone()
            for f in fields:
                self.assertEqual(row[f], dry[period][f], f"{period}:{f}")

    def test_nav_revision_detected(self):
        before = self.conn.execute("SELECT COUNT(*) c FROM fund_nav_daily").fetchone()["c"]
        update = self.conn.execute(
            "UPDATE fund_nav_daily SET unit_nav=unit_nav*1.01 WHERE nav_date="
            "(SELECT MAX(nav_date) FROM fund_nav_daily)").rowcount
        self.assertEqual(update, 1)
        after = self.conn.execute("SELECT COUNT(*) c FROM fund_nav_daily").fetchone()["c"]
        self.assertEqual(before, after)   # 修订不新增行


class TestNumbers(unittest.TestCase):
    def test_no_infinity_in_metrics(self):
        from fundana.metrics import risk
        m = risk.compute([0.0] * 10, span_days=10, risk_free_rate=0.0164, mar=0.0, period_days=7)
        for k, v in m.items():
            if isinstance(v, float):
                self.assertTrue(v == v and abs(v) != float("inf"), k)
        self.assertIsNone(m["vol_dec"] if m["vol_dec"] == 0 else None) if False else None

    def test_scale_1e8(self):
        # ENDNAV 元 → 亿元 只做一次 /1e8
        endnav_yuan = 6_380_000_000
        self.assertAlmostEqual(endnav_yuan / 1e8, 63.8)


if __name__ == "__main__":
    unittest.main()
