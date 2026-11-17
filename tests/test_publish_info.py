"""scripts/publish_info.py：当期发布信息补齐（nav_as_of 只能从 publication 表取）。"""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "publish_info", Path(__file__).resolve().parent.parent / "scripts/publish_info.py")
publish_info = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publish_info)


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "var/db").mkdir(parents=True)
        (self.root / "reports").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self):
        conn = sqlite3.connect(str(self.root / "var/db/fundana.db"))
        conn.execute("CREATE TABLE publication(publication_id TEXT, run_id TEXT, generation INTEGER,"
                     " created_at TEXT, revision_path TEXT, manifest_sha256 TEXT, status TEXT,"
                     " nav_as_of TEXT)")
        conn.execute("INSERT INTO publication VALUES(?,?,?,?,?,?,?,?)",
                     ("pub2", "run2", 88, "2026-11-17T07:23:00+08:00",
                      "runs/run2/revisions/pub2", "0" * 64, "ok", "2026-11-16"))
        conn.commit()
        conn.close()

    def _state(self):
        (self.root / "reports/publication_state.json").write_text(json.dumps({
            "generation": 88, "publication_id": "pub2", "run_id": "run2",
            "revision_path": "runs/run2/revisions/pub2", "schema_version": 1,
            "committed_at": "2026-11-17T07:23:00+08:00", "file_count": 276,
        }), encoding="utf-8")

    def test_nav_as_of_comes_from_db(self):
        self._db()
        self._state()
        info = publish_info.load(self.root)
        self.assertEqual(info["nav_as_of"], "2026-11-16")
        self.assertEqual(info["revision_path"], "reports/runs/run2/revisions/pub2")
        self.assertEqual(info["generation"], 88)

    def test_missing_db_is_not_fatal(self):
        self._state()
        info = publish_info.load(self.root)
        self.assertEqual(info["nav_as_of"], "")
        self.assertEqual(info["revision_path"], "reports/runs/run2/revisions/pub2")

    def test_github_output_lines(self):
        self._db()
        self._state()
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            publish_info.main(["--root", str(self.root), "--github-output"])
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(lines[0], "nav=2026-11-16")
        self.assertEqual(lines[1], "rev=reports/runs/run2/revisions/pub2")


if __name__ == "__main__":
    unittest.main()
