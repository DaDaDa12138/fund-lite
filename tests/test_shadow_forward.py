"""v9/v10 同池影子前瞻：冻结、隔离、成熟收益与 not_ready 边界。"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fundana import db, forward as forward_mod, shadow_forward


class StubCfg:
    def __init__(self, root: Path):
        self.app = {"db": {"busy_timeout_ms": 15000, "wal": False}}
        self.root = root
        self.report_root = root / "reports"
        self.report_root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "t.db"
        self.raw_dir = root / "raw"
        self.freeze_dir = root / "freeze"
        self.code_version = "0.1.0"
        self.sources = {}

    def get(self, *keys, default=None):
        if keys and keys[0] == "quality":
            return {"nav_age_limit_days": 10}
        if keys and keys[0] == "forward":
            return default if default is not None else {}
        return default


class ShadowForwardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = StubCfg(Path(self.tmp.name))
        self.conn = db.connect(self.cfg)
        db.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO screen_run(run_id, created_at, profile, formula_contract, screen_contract,"
            " config_hash, code_version, universe_count, base_pass_count, formal_count,"
            " locked_count, business_date, status) VALUES"
            "('r1','2026-01-01T00:00:00','bond_v1','f','s','h','0.1',10,5,4,0,'2026-01-01','done')")
        for code, style in (("A", "bond_pure"), ("B", "bond_short"), ("C", "bond_pure"),
                            ("D", "bond_credit")):
            self.conn.execute(
                "INSERT INTO screen_decision(run_id, fcode, layer_reached, passed,"
                " reason_codes_json, style_code, scores_json) VALUES('r1',?,'L9',1,'[]',?,'{}')",
                (code, style))
        for board, codes in (("bond_core10", ("A", "B")), ("bond_core", ("A", "B", "C"))):
            for rank, code in enumerate(codes, 1):
                self.conn.execute(
                    "INSERT INTO screen_pick(run_id, board, rank, fcode, formal_score, rank_score)"
                    " VALUES('r1',?,?,?,80,?)", (board, rank, code, 90 - rank))
        self.conn.commit()
        self.shadow = {
            "meta": {"run_id": "r1", "business_date": "2026-01-01",
                     "formula_contract": "f", "screen_contract": "s",
                     "v9_weights": {"annual_3y": 0.30},
                     "core10": {"v10": ["A", "B"], "v9": ["B", "C"],
                                "overlap": 1, "added": ["C"], "removed": ["A"], "jaccard": 0.3333},
                     "core50": {"v10": ["A", "B", "C"], "v9": ["B", "C", "D"],
                                "overlap": 2, "added": ["D"], "removed": ["A"], "jaccard": 0.5}},
            "rows": [{"fcode": "A", "v9_rank_score": 70.0},
                     {"fcode": "B", "v9_rank_score": 80.0},
                     {"fcode": "C", "v9_rank_score": 75.0},
                     {"fcode": "D", "v9_rank_score": 60.0}],
        }

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _nav(self, code: str, growth: float, days: int = 100):
        d0 = date(2026, 1, 2)
        rows = []
        for i in range(days):
            d = (d0 + timedelta(days=i)).isoformat()
            rows.append((code, d, 1.0 + i * 0.0001, growth))
        self.conn.executemany(
            "INSERT OR REPLACE INTO fund_nav_daily(fcode, nav_date, unit_nav, acc_nav,"
            " growth_pct, nav_source) VALUES(?,?,?,NULL,?,'test')", rows)
        self.conn.commit()

    def test_record_freezes_both_boards_and_is_idempotent(self):
        out = shadow_forward.record(self.conn, self.cfg, "r1", shadow=self.shadow)
        self.assertEqual(out["cohort_id"], "shadow-v9-r1")
        self.assertEqual(out["members"], 10)
        self.assertEqual(out["core10"], {"v10": 2, "v9": 2})
        self.assertEqual(out["core50"], {"v10": 3, "v9": 3})
        shadow_forward.record(self.conn, self.cfg, "r1", shadow=self.shadow)
        n = self.conn.execute("SELECT COUNT(*) FROM shadow_member WHERE cohort_id='shadow-v9-r1'").fetchone()[0]
        self.assertEqual(n, 10)

    def test_evaluate_and_summary_compare_same_pool(self):
        shadow_forward.record(self.conn, self.cfg, "r1", shadow=self.shadow)
        self._nav("A", 0.01)
        self._nav("B", 0.02)
        self._nav("C", 0.03)
        self._nav("D", 0.04)
        ev = shadow_forward.evaluate(self.conn, self.cfg, "shadow-v9-r1")
        self.assertGreater(ev["matured"], 0)
        self.assertGreater(ev["not_ready"], 0)
        sm = shadow_forward.summary(self.conn, "shadow-v9-r1")
        c30 = [c for c in sm["comparisons"]
               if c["horizon_days"] == 30 and c["board"] == "core10"][0]
        self.assertEqual(c30["v10_n"], 2)
        self.assertEqual(c30["v9_n"], 2)
        # v10 core10 A/B，v9 core10 B/C；B 共同，C 的收益高于 A，所以 v9 中位数更高
        self.assertGreater(c30["v9_minus_v10_median_pct"], 0)
        self.assertGreater(c30["v9_win_rate_vs_v10_median_pct"], 0)

    def test_record_clears_stale_observations_when_members_change(self):
        shadow_forward.record(self.conn, self.cfg, "r1", shadow=self.shadow)
        self._nav("A", 0.01)
        shadow_forward.evaluate(self.conn, self.cfg, "shadow-v9-r1")
        before = self.conn.execute(
            "SELECT COUNT(*) FROM shadow_observation WHERE cohort_id='shadow-v9-r1'").fetchone()[0]
        self.assertGreater(before, 0)
        smaller = {"meta": dict(self.shadow["meta"]), "rows": self.shadow["rows"]}
        smaller["meta"]["core10"] = {"v10": ["A"], "v9": ["A"], "overlap": 1,
                                     "added": [], "removed": [], "jaccard": 1.0}
        smaller["meta"]["core50"] = {"v10": ["A"], "v9": ["A"], "overlap": 1,
                                     "added": [], "removed": [], "jaccard": 1.0}
        shadow_forward.record(self.conn, self.cfg, "r1", shadow=smaller)
        after = self.conn.execute(
            "SELECT COUNT(*) FROM shadow_observation WHERE cohort_id='shadow-v9-r1'").fetchone()[0]
        self.assertEqual(after, 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM shadow_member WHERE cohort_id='shadow-v9-r1'").fetchone()[0], 4)

    def test_shadow_observations_do_not_enter_formal_forward_summary(self):
        shadow_forward.record(self.conn, self.cfg, "r1", shadow=self.shadow)
        self._nav("A", 0.01)
        shadow_forward.evaluate(self.conn, self.cfg, "shadow-v9-r1")
        self.assertEqual(forward_mod.summary(self.conn)["cohorts"], 0)
