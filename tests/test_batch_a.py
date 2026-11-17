"""Batch A（可信度层）单元测试：跨源比对 / 门禁 / 缓释 / 前瞻链式 / 质量看板。

这些用例守的都是"不能自欺"的边界：
- 缺失与真实零值必须分开；
- 门禁在没有任何正式成员时必须 block，而不是安静发布；
- 缓释只放行"恰好一项失败且满足条件"的基金，两项失败一律不放；
- 前瞻只认 cohort 之后发生的净值，样本不足写 not_ready 而不是 0；
- 质量看板对空库也必须给出结构完整的答案。
"""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fundana import db
from fundana.api import quality as quality_mod
from fundana import forward as forward_mod
from fundana.forward import _chain, _stats
from fundana.publish import gate as gate_mod
from fundana.screen import relief as relief_mod
from fundana.validate import cross as cross_mod


class StubCfg:
    def __init__(self, root: Path, sources=None, gate=None):
        self.app = {"db": {"busy_timeout_ms": 15000, "wal": False}}
        self.root = root
        self.report_root = root / "reports"
        self.report_root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "t.db"
        self.raw_dir = root / "raw"
        self.freeze_dir = root / "freeze"
        self.code_version = "0.1.0"
        self.sources = sources if sources is not None else {}
        self._gate = gate or {}

    def get(self, *keys, default=None):
        if keys and keys[0] == "gate":
            return self._gate
        if keys and keys[0] == "quality":
            return {"nav_age_limit_days": 10}
        return default


class CompareTest(unittest.TestCase):
    def test_tolerance_is_abs_or_rel(self):
        self.assertEqual(cross_mod.compare(1.0, 1.0005, 0.001, 0.01)[0], "pass")
        # 绝对差超限但相对差在 1% 内也算通过
        self.assertEqual(cross_mod.compare(1.0, 1.005, 0.001, 0.01)[0], "pass")
        self.assertEqual(cross_mod.compare(1.0, 1.02, 0.001, 0.01)[0], "mismatch")

    def test_missing_and_zero_are_different(self):
        self.assertEqual(cross_mod.compare(None, None, 0.001, 0.01)[0], "missing")
        self.assertEqual(cross_mod.compare(None, 1.0, 0.001, 0.01)[0], "missing_a")
        self.assertEqual(cross_mod.compare(1.0, None, 0.001, 0.01)[0], "missing_b")
        # 一侧真实为 0 不算缺失
        self.assertEqual(cross_mod.compare(0.0, 0.0, 0.001, 0.01)[0], "pass")

    def test_snap_date_handles_year_boundary(self):
        self.assertEqual(cross_mod._snap_date("10-30", "2026-11-01"), date(2026, 10, 30))
        # 快照里的 MM-DD 比业务日"晚"，只能是去年
        self.assertEqual(cross_mod._snap_date("12-31", "2026-01-02"), date(2025, 12, 31))
        self.assertIsNone(cross_mod._snap_date(None, "2026-11-01"))


class ReliefTest(unittest.TestCase):
    def _checks(self, **kw):
        # 门槛键与筛选引擎一致：收益类用 min，风险/费用类用 max
        out = {}
        for k, v in kw.items():
            bound = "max" if k in ("mdd", "vol", "fee") else "min"
            out[k] = {"value": v[0], bound: v[1], "pass": v[2]}
        return out

    def test_scale_relief_allowed_at_boundary(self):
        checks = self._checks(scale=(1.85, 2.0, False))
        allowed, kind, detail = relief_mod.try_relief(checks, ["scale"], {"composite": 82.0})
        self.assertTrue(allowed)
        self.assertEqual(kind, "scale")
        self.assertIn("rule", detail)

    def test_two_failures_never_relieved(self):
        checks = self._checks(scale=(1.85, 2.0, False), annual_1y=(1.0, 2.0, False))
        allowed, _kind, detail = relief_mod.try_relief(checks, ["scale", "annual_1y"], {"composite": 90.0})
        self.assertFalse(allowed)
        self.assertEqual(detail["blocked"], "not_single_failure")

    def test_composite_below_80_blocks(self):
        checks = self._checks(scale=(1.85, 2.0, False))
        allowed, _kind, detail = relief_mod.try_relief(checks, ["scale"], {"composite": 79.9})
        self.assertFalse(allowed)
        self.assertEqual(detail["blocked"], "scale_or_composite_below_relief")

    def test_annual_gap_must_be_within_015pp(self):
        ok = self._checks(annual_1y=(1.90, 2.00, False))     # 差 0.10pp
        allowed, kind, _ = relief_mod.try_relief(ok, ["annual_1y"], {"composite": 81.0})
        self.assertTrue(allowed)
        self.assertEqual(kind, "annual_1y")
        bad = self._checks(annual_1y=(1.70, 2.00, False))    # 差 0.30pp
        allowed2, _kind2, detail2 = relief_mod.try_relief(bad, ["annual_1y"], {"composite": 95.0})
        self.assertFalse(allowed2)
        self.assertEqual(detail2["blocked"], "annual_gap_or_composite_below_relief")

    def test_ineligible_type_is_recorded_not_ignored(self):
        checks = self._checks(mdd=(0.05, 0.02, False))
        allowed, _kind, detail = relief_mod.try_relief(checks, ["mdd"], {"composite": 99.0})
        self.assertFalse(allowed)
        self.assertEqual(detail["blocked"], "type_not_eligible")

    def test_non_negative_near_miss_is_eligible(self):
        """P0-1 后固定线用 non_negative；缓释必须跟同一口径。"""
        checks = self._checks(non_negative=(69.98, 70.0, False))
        allowed, kind, _ = relief_mod.try_relief(checks, ["non_negative"], {"composite": 80.0})
        self.assertTrue(allowed)
        self.assertEqual(kind, "non_negative")

    def test_positive_is_no_longer_eligible(self):
        checks = self._checks(positive=(59.99, 60.0, False))
        allowed, _kind, detail = relief_mod.try_relief(checks, ["positive"], {"composite": 99.0})
        self.assertFalse(allowed)
        self.assertEqual(detail["blocked"], "type_not_eligible")


class ForwardChainTest(unittest.TestCase):
    def _row(self, d, nav, g=None):
        return {"nav_date": d, "unit_nav": nav, "growth_pct": g}

    def test_growth_pct_is_preferred(self):
        rows = [self._row("2026-11-02", 1.0, 1.0), self._row("2026-11-03", 1.5, 1.0)]
        total, mdd, gap = _chain(rows)
        self.assertAlmostEqual(total, 2.01, places=6)      # 1.01^2-1
        self.assertAlmostEqual(mdd, 0.0, places=9)
        self.assertEqual(gap, 1)

    def test_falls_back_to_unit_nav_and_records_drawdown(self):
        rows = [self._row("2026-11-02", 1.0, None), self._row("2026-11-03", 1.2, None),
                self._row("2026-11-04", 0.9, None)]
        total, mdd, gap = _chain(rows)
        self.assertAlmostEqual(total, -10.0, places=6)
        self.assertAlmostEqual(mdd, 0.25, places=6)        # 1.2 -> 0.9
        self.assertEqual(gap, 1)

    def test_empty_series_is_none_not_zero(self):
        total, mdd, _gap = _chain([])
        self.assertIsNone(total)
        self.assertIsNone(mdd)

    def test_stats_quantiles(self):
        s = _stats([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(s["n"], 4)
        self.assertAlmostEqual(s["median"], 2.5, places=9)
        self.assertAlmostEqual(s["worst"], 1.0, places=9)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = StubCfg(self.root)
        self.conn = db.connect(self.cfg)
        db.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO screen_run(run_id, created_at, profile, formula_contract, config_hash,"
            " code_version, universe_count, base_pass_count, formal_count, locked_count)"
            " VALUES('run1','2026-11-01T00:00:00','bond_v1','f','h','0.1',0,0,0,0)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _built(self):
        rev = self.cfg.report_root / "runs" / "run1" / "revisions" / "rev1"
        rev.mkdir(parents=True, exist_ok=True)
        return {"revision_id": "rev1", "revision_path": str(rev),
                "manifest": {"schema_version": 1, "publication_id": "rev1",
                             "run_id": "run1", "files": []}}

    def test_empty_run_is_blocked_not_published(self):
        res = gate_mod.evaluate(self.conn, self.cfg, "run1", self._built())
        self.assertEqual(res["status"], "blocked")
        self.assertIn("no_formal_members", res["blocks"])
        self.assertIn("source_incomplete", res["blocks"])
        # 每一条规则都必须出现，包括 not_applicable 的，证明"检查过"
        codes = {c["code"] for c in res["checks"]}
        self.assertIn("index_repair_pending", codes)
        self.assertIn("artifact_mismatch", codes)

    def test_warn_mode_downgrades_block_to_warning(self):
        self.cfg._gate = {"mode": "warn"}
        res = gate_mod.evaluate(self.conn, self.cfg, "run1", self._built())
        self.assertEqual(res["status"], "warning")
        self.assertEqual(res["blocks"], [])

    def test_blocked_carries_evidence(self):
        res = gate_mod.evaluate(self.conn, self.cfg, "run1", self._built())
        with self.assertRaises(gate_mod.PublicationBlocked) as ctx:
            raise gate_mod.PublicationBlocked(res)
        self.assertIn("no_formal_members", str(ctx.exception))
        self.assertEqual(ctx.exception.result["status"], "blocked")

    def test_gate_result_is_persisted(self):
        gate_mod.evaluate(self.conn, self.cfg, "run1", self._built())
        row = self.conn.execute("SELECT status, blocks_json FROM publication_gate"
                                " WHERE publication_id='rev1'").fetchone()
        self.assertEqual(row["status"], "blocked")
        self.assertIn("no_formal_members", json.loads(row["blocks_json"]))


class ForwardObserveTest(unittest.TestCase):
    """前瞻只认 cohort 之后发生的净值；样本不足写 not_ready，绝不当 0 收益。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = StubCfg(Path(self.tmp.name))
        self.conn = db.connect(self.cfg)
        db.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO forward_cohort(cohort_id, run_id, publication_id, business_date, source,"
            " strategy_version, formula_contract, universe_count, investable_count, freeze_json,"
            " created_at) VALUES('c1','r1','p1','2026-01-01','live','bond_v1','f',10,5,'{}',"
            "'2026-01-01T00:00:00')")
        self.conn.execute("INSERT INTO fund(fcode, name, type_group, identity_status)"
                          " VALUES('000001','测试债基','bond','ok')")
        self.conn.commit()
        self.cohort = {"cohort_id": "c1", "business_date": "2026-01-01"}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _nav(self, start: str, n: int, growth: float = 0.01):
        d0 = date.fromisoformat(start)
        rows = [((d0 + timedelta(days=i)).isoformat(), 1.0 + i * 0.001, growth)
                for i in range(1, n + 1)]
        self.conn.executemany(
            "INSERT OR REPLACE INTO fund_nav_daily(fcode, nav_date, unit_nav, acc_nav,"
            " growth_pct, nav_source) VALUES('000001',?,?,NULL,?,'test')",
            [(d, nav, g) for d, nav, g in rows])
        self.conn.commit()

    def test_horizon_not_elapsed_is_not_ready(self):
        # 目标日 2026-01-31 还没到（今天远早于它的情况用 365 天模拟）
        self._nav("2026-01-02", 10)
        out = forward_mod._observe(self.conn, self.cohort, "000001", 365, 0.8, 10)
        self.assertEqual(out["status"], "not_ready")
        self.assertEqual(out["reason_code"], "horizon_not_elapsed")
        self.assertIsNone(out["total_return_pct"])

    def test_insufficient_points_is_not_ready_not_zero(self):
        # 净值一直更新到目标日附近（不是中断），但点数不够 -> not_ready 而非 0 收益
        self._nav("2026-01-25", 4)
        out = forward_mod._observe(self.conn, self.cohort, "000001", 30, 0.8, 10)
        self.assertEqual(out["status"], "not_ready")
        self.assertTrue(out["reason_code"].startswith("insufficient_points_"))
        self.assertIsNone(out["total_return_pct"])

    def test_matured_horizon_reports_chained_return(self):
        self._nav("2026-01-02", 25, growth=0.01)
        out = forward_mod._observe(self.conn, self.cohort, "000001", 30, 0.8, 10)
        self.assertEqual(out["status"], "matured")
        self.assertEqual(out["n_obs"], 25)
        self.assertAlmostEqual(out["total_return_pct"], (1.0001 ** 25 - 1) * 100, places=6)
        self.assertIsNotNone(out["annual_dec"])

    def test_nav_stopped_is_unverifiable_not_dropped(self):
        # 只有 cohort 后几天，之后净值中断：必须 unverifiable，不能当 0 也不能剔除
        self._nav("2026-01-02", 3, growth=0.01)
        out = forward_mod._observe(self.conn, self.cohort, "000001", 30, 0.8, 10)
        self.assertEqual(out["status"], "unverifiable")
        self.assertTrue(out["reason_code"].startswith("nav_stopped_"))
        self.assertEqual(out["n_obs"], 3)


class GateStaleTest(unittest.TestCase):
    """业务日与净值日相差过大必须 block：宁可不出新版，也不能把旧净值当今天。"""

    def test_stale_publication_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = StubCfg(Path(tmp))
            conn = db.connect(cfg)
            db.migrate(conn)
            conn.execute("INSERT INTO fund_buyable_daily(fcode, business_date, buyable_state)"
                         " VALUES('000001','2026-11-01','confirmed_yes')")
            conn.execute("INSERT INTO fund_nav_daily(fcode, nav_date, unit_nav, nav_source)"
                         " VALUES('000001','2026-10-20',1.0,'test')")
            conn.execute("INSERT INTO screen_run(run_id, created_at, profile, formula_contract,"
                         " config_hash, code_version, universe_count, base_pass_count, formal_count,"
                         " locked_count) VALUES('r1','2026-11-01T00:00:00','bond_v1','f','h','0.1',1,1,1,0)")
            conn.commit()
            rev = cfg.report_root / "runs" / "r1" / "revisions" / "rev1"
            rev.mkdir(parents=True, exist_ok=True)
            built = {"revision_id": "rev1", "revision_path": str(rev),
                     "manifest": {"schema_version": 1, "publication_id": "rev1",
                                  "run_id": "r1", "files": []}}
            res = gate_mod.evaluate(conn, cfg, "r1", built)
            conn.close()
        self.assertEqual(res["status"], "blocked")
        self.assertIn("stale_publication", res["blocks"])


class QualityTest(unittest.TestCase):
    def test_empty_database_yields_full_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = StubCfg(Path(tmp))
            conn = db.connect(cfg)
            db.migrate(conn)
            data = quality_mod.build(conn, cfg, days=7)
            conn.close()
        for key in ("sources", "field_nulls", "nav", "cross_check", "gates", "coverage_flags"):
            self.assertIn(key, data)
        self.assertEqual(data["sources"], [])
        self.assertEqual(data["nav"]["rows"], 0)
        self.assertIsNone(data["nav"]["last_date"])
        self.assertEqual(len(data["field_nulls"]), 3)


if __name__ == "__main__":
    unittest.main()
