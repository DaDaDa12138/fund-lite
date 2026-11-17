"""发布事务测试：manifest 校验、篡改检测、symlink 逃逸、失败保旧。"""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fundana import db
from fundana.publish import commit as commit_mod, gate as gate_mod, verify as verify_mod
from fundana.publish import PublicationError as PubError, current_revision


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

    def get(self, *keys, default=None):
        return default


def make_revision(cfg, rev_id="rev1"):
    rev = cfg.report_root / "runs" / "run1" / "revisions" / rev_id
    rev.mkdir(parents=True, exist_ok=True)
    (rev / "bond_stable.csv").write_bytes("\ufefffcode,name\n000001,A\n".encode("utf-8"))
    files = []
    for p in sorted(rev.rglob("*")):
        if p.is_file() and p.name != "validated.json":
            files.append({"path": p.relative_to(rev).as_posix(),
                          "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                          "size": p.stat().st_size})
    # 门禁段自 Batch A 起是 manifest 必需项（§16.8）：缺失即视为"未经门禁"，
    # 所以测试夹具必须显式给出一份通过的门禁结果。
    manifest = {"schema_version": 1, "publication_id": rev_id, "run_id": "run1",
                "gate": {"status": "pass", "blocks": [], "warnings": [], "checks": []},
                "counts": {"bond_stable": 1}, "files": files}
    (rev / "validated.json").write_text(json.dumps(manifest), encoding="utf-8")
    return rev, {"revision_id": rev_id, "revision_path": str(rev), "manifest": manifest}


class TestPublication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = StubCfg(Path(self.tmp.name))
        self.conn = db.connect(self.cfg)
        db.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_commit_and_verify(self):
        rev, built = make_revision(self.cfg)
        out = commit_mod(self.cfg, built, "run1", self.conn)
        self.assertEqual(out["generation"], 1)
        res = verify_mod(self.cfg, full=True)
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["checked"], 1)
        # fast 模式仍要逐文件核对存在性与大小（不能返回 checked=0 的假通过）
        fast = verify_mod(self.cfg, full=False)
        self.assertTrue(fast["ok"], fast["problems"])
        self.assertEqual(fast["checked"], 1)
        # 第二次发布 generation 递增
        rev2, built2 = make_revision(self.cfg, "rev2")
        out2 = commit_mod(self.cfg, built2, "run1", self.conn)
        self.assertEqual(out2["generation"], 2)

    def test_tamper_detected(self):
        rev, built = make_revision(self.cfg)
        commit_mod(self.cfg, built, "run1", self.conn)
        target = current_revision(self.cfg) / "bond_stable.csv"
        target.write_bytes("tampered".encode("utf-8"))
        res = verify_mod(self.cfg, full=True)
        self.assertFalse(res["ok"])
        self.assertTrue(any("SHA256" in p or "大小" in p for p in res["problems"]))

    def test_commit_rejects_older_nav_as_of(self):
        for run_id, nav, metric in (("run-new", "2026-11-03", "2026-11-03"),
                                    ("run-old", "2026-11-02", "2026-11-02")):
            self.conn.execute(
                "INSERT INTO screen_run(run_id, profile, config_hash, code_version,"
                " formula_contract, business_date, nav_as_of, metric_as_of)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (run_id, "bond_v1", "h", "v", "f", nav, nav, metric))
        self.conn.commit()
        _, built_new = make_revision(self.cfg, "rev-new")
        commit_mod(self.cfg, built_new, "run-new", self.conn)
        _, built_old = make_revision(self.cfg, "rev-old")
        with self.assertRaises(PubError) as ctx:
            commit_mod(self.cfg, built_old, "run-old", self.conn)
        self.assertIn("publication_older_than_current", str(ctx.exception))
        # 显式回滚才允许旧 as-of 覆盖
        out = commit_mod(self.cfg, built_old, "run-old", self.conn, allow_older=True)
        self.assertEqual(out["generation"], 2)

    def test_no_publication_raises(self):
        with self.assertRaises(PubError):
            verify_mod(self.cfg, full=False)

    def test_revision_escape_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        built = {"revision_id": "x", "revision_path": str(outside), "manifest": {"files": []}}
        with self.assertRaises(ValueError):
            commit_mod(self.cfg, built, "run1", self.conn)

    def test_failed_commit_keeps_old_pointer(self):
        rev, built = make_revision(self.cfg)
        commit_mod(self.cfg, built, "run1", self.conn)
        old = current_revision(self.cfg)
        bad = {"revision_id": "bad", "revision_path": str(Path(self.tmp.name) / "outside"),
               "manifest": {"files": []}}
        try:
            commit_mod(self.cfg, bad, "run1", self.conn)
        except ValueError:
            pass
        self.assertEqual(current_revision(self.cfg), old)


class TestRankScoreOrder(unittest.TestCase):
    def test_spearman_handles_reverse_and_ties(self):
        self.assertAlmostEqual(gate_mod.spearman([1, 2, 3], [3, 2, 1]), -1.0, places=9)
        self.assertIsNone(gate_mod.spearman([1, 2, 3], [10, 10, 10]))

    def test_rank_order_stats_flags_inversions_and_missing(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE screen_pick (run_id TEXT, board TEXT, rank INTEGER, rank_score REAL)")
        conn.executemany(
            "INSERT INTO screen_pick VALUES (?,?,?,?)",
            [("r", "bond_stable", 1, 90.0), ("r", "bond_stable", 2, 80.0),
             ("r", "bond_stable", 3, 70.0), ("r", "bond_stable", 4, 85.0),
             ("r", "bond_enhanced", 1, 60.0), ("r", "bond_enhanced", 2, None)])
        stats = gate_mod.rank_order_stats(conn, "r")
        self.assertEqual(stats["n"], 6)
        self.assertEqual(stats["missing"], 1)
        self.assertEqual(stats["inversions"], 2)  # 80/70 都低于后面的 85
        self.assertEqual(stats["boards"]["bond_stable"]["inversions"], 2)
        self.assertEqual(stats["boards"]["bond_enhanced"]["missing"], 1)


if __name__ == "__main__":
    unittest.main()