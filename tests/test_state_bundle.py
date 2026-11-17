"""状态包契约：瘦身只动该动的行，导出/导入必须可逆且拒绝被篡改的分片。"""
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/state_bundle.py"


def _load():
    spec = importlib.util.spec_from_file_location("state_bundle", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


AS_OF = "2026-11-16"


def _make_db(path: Path, runs: int = 10) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            "CREATE TABLE publication(publication_id TEXT, run_id TEXT, generation INTEGER, nav_as_of TEXT);"
            "CREATE TABLE screen_run(run_id TEXT, created_at TEXT);"
            "CREATE TABLE screen_run_input(run_id TEXT, fcode TEXT);"
            "CREATE TABLE screen_decision(run_id TEXT, fcode TEXT);"
            "CREATE TABLE raw_object(sha256 TEXT, bytes_gz INTEGER);"
            "CREATE TABLE fund_nav_daily(fcode TEXT, nav_date TEXT, raw_sha256 TEXT);"
            "CREATE TABLE src_zqcc_holding(fcode TEXT, raw_sha256 TEXT);"
        )
        for i in range(1, runs + 1):
            run_id = "r%02d" % i
            conn.execute("INSERT INTO publication VALUES(?,?,?,?)", ("p%02d" % i, run_id, i, AS_OF))
            conn.execute("INSERT INTO screen_run VALUES(?,?)", (run_id, "2026-11-%02dT07:30:00" % i))
            for fcode in ("000001", "000002"):
                conn.execute("INSERT INTO screen_run_input VALUES(?,?)", (run_id, fcode))
                conn.execute("INSERT INTO screen_decision VALUES(?,?)", (run_id, fcode))
        conn.commit()
    finally:
        conn.close()


def _add_raw(path: Path, rows: list[tuple[str, str | None]]) -> None:
    """rows: (tag, nav_date)；nav_date=None 表示只进 raw_object、没有任何表引用它。"""
    conn = sqlite3.connect(str(path))
    try:
        for tag, nav_date in rows:
            sha = _sha(tag)
            conn.execute("INSERT INTO raw_object VALUES(?,?)", (sha, 100))
            if nav_date:
                conn.execute("INSERT INTO fund_nav_daily VALUES(?,?,?)", ("000001", nav_date, sha))
        conn.commit()
    finally:
        conn.close()


class TestSlim(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "var/db/fundana.db"
        _make_db(self.db)
        self.mod = _load()

    def tearDown(self):
        self.tmp.cleanup()

    def _count(self, table: str) -> int:
        conn = sqlite3.connect(str(self.db))
        try:
            return int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
        finally:
            conn.close()

    def test_keeps_only_recent_runs(self):
        out = self.mod.slim(self.db, keep_publications=3, keep_runs=2)
        self.assertTrue(out["ok"])
        self.assertEqual(out["keep_run"], 3)
        self.assertEqual(out["deleted_screen_run_input"], 14)
        self.assertEqual(out["deleted_screen_decision"], 14)
        self.assertEqual(self._count("screen_run_input"), 6)
        self.assertEqual(self._count("screen_decision"), 6)

    def test_raw_kept_only_when_referenced_in_window(self):
        old = (date.fromisoformat(AS_OF) - timedelta(days=200)).isoformat()
        _add_raw(self.db, [("recent", AS_OF), ("old", old), ("orphan", None)])
        out = self.mod.slim(self.db, keep_publications=3)
        self.assertEqual(out["keep_raw"], 1)
        self.assertEqual(out["raw_rows_before"], 3)
        self.assertEqual(out["deleted_raw_object"], 2)
        conn = sqlite3.connect(str(self.db))
        try:
            left = [r[0] for r in conn.execute("SELECT sha256 FROM raw_object")]
        finally:
            conn.close()
        self.assertEqual(left, [_sha("recent")])

    def test_undated_provenance_table_kept_whole(self):
        undated = _sha("zqcc")
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("INSERT INTO raw_object VALUES(?,?)", (undated, 100))
            conn.execute("INSERT INTO src_zqcc_holding VALUES(?,?)", ("000001", undated))
            conn.commit()
        finally:
            conn.close()
        self.mod.slim(self.db, keep_publications=3)
        conn = sqlite3.connect(str(self.db))
        try:
            left = [r[0] for r in conn.execute("SELECT sha256 FROM raw_object")]
        finally:
            conn.close()
        self.assertEqual(left, [undated])


@unittest.skipUnless(shutil.which("zstd"), "需要 zstd 命令行")
class TestExportImport(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "repo"
        self.out = base / "dist"
        self.target = base / "runner"
        _make_db(self.root / "var/db/fundana.db", runs=3)
        pub_dir = self.root / "reports/runs/r03/revisions/p03"
        pub_dir.mkdir(parents=True, exist_ok=True)
        (pub_dir / "summary.md").write_text("摘要正文" + chr(10), encoding="utf-8")
        (pub_dir / "report.json").write_text("{}", encoding="utf-8")
        (self.root / "reports/publication_state.json").write_text(
            json.dumps({"generation": 3, "publication_id": "p03", "nav_as_of": AS_OF}), encoding="utf-8")
        os.symlink("runs/r03/revisions/p03", self.root / "reports/.publication_current")
        self.mod = _load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_restores_state(self):
        result = self.mod.export(self.root, self.out, part_size=512)
        manifest = result["manifest"]
        self.assertGreaterEqual(len(manifest["parts"]), 2)
        self.assertEqual(manifest["nav_as_of"], AS_OF)
        self.assertEqual(manifest["generation"], 3)
        self.assertEqual(sum(p["size"] for p in manifest["parts"]), manifest["total_bytes"])
        for part in manifest["parts"]:
            data = (self.out / part["name"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), part["sha256"])

        self.target.mkdir(parents=True, exist_ok=True)
        imported = self.mod.import_(self.target, self.out)
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["db_bytes"], (self.root / "var/db/fundana.db").stat().st_size)
        self.assertTrue((self.target / "reports/runs/r03/revisions/p03/summary.md").exists())
        self.assertTrue((self.target / "reports/.publication_current").is_symlink())
        self.assertFalse((self.out / manifest["parts"][0]["name"]).exists())

    def test_import_rejects_tampered_part(self):
        self.mod.export(self.root, self.out, part_size=512)
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        part = self.out / manifest["parts"][0]["name"]
        blob = bytearray(part.read_bytes())
        blob[0] = (blob[0] + 1) % 256
        part.write_bytes(bytes(blob))
        self.target.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(SystemExit):
            self.mod.import_(self.target, self.out)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shutil.which("zstd"), "需要 zstd 命令行")
class TestExportAlternateDb(unittest.TestCase):
    """bootstrap 场景：--db 指向副本时，归档里仍然必须是 var/db/fundana.db。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "repo"
        self.out = base / "dist"
        self.target = base / "runner"
        _make_db(self.root / "var/db/fundana.db", runs=3)
        self.alt = base / "alt/bootstrap.db"
        self.alt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.root / "var/db/fundana.db", self.alt)
        self.mod = _load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_db_path_packs_copy_under_canonical_name(self):
        result = self.mod.export(self.root, self.out, db_path=self.alt, part_size=512)
        self.assertEqual(result["manifest"]["db_bytes"], self.alt.stat().st_size)
        self.assertEqual(str(self.alt), result["db_path"])
        self.mod.import_(self.root, self.out)
        restored = self.target / "var/db/fundana.db"
        if not restored.exists():
            restored = self.root / "var/db/fundana.db"
        conn = sqlite3.connect(str(restored))
        try:
            n = int(conn.execute("SELECT COUNT(*) FROM publication").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(n, 3)

