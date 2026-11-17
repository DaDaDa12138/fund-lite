"""站点构建契约：没有发布时不炸，有发布时产出可用静态页。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/site_build.py"


def _load():
    spec = importlib.util.spec_from_file_location("site_build", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_markdown():
    return importlib.util.find_spec("markdown") is not None


@unittest.skipUnless(_has_markdown(), "需要 markdown 包（requirements-ci.txt）")
class TestSiteBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.out = Path(self.tmp.name) / "site"
        self.mod = _load()

    def tearDown(self):
        self.tmp.cleanup()

    def _revision(self, run="20260101T073000-screen-aaaaaa", pub="p1",
                  generated_at="2026-01-01T07:30:00+08:00", counts=None, with_report=True):
        pub_dir = self.root / "reports/runs" / run / "revisions" / pub
        pub_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "schema_version": 3,
            "run_id": run,
            "generated_at": generated_at,
            "counts": counts if counts is not None else {
                "bond_stable": 3, "bond_enhanced": 1, "bond_core": 2, "bond_core10": 1},
            "facts": {"published": 4, "l9_pass": 9},
            "sections": [],
        }
        (pub_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        (pub_dir / "summary.md").write_text(
            "# 摘要一级标题" + chr(10) + chr(10) + "正文一行：稳健榜 3 只。" + chr(10), encoding="utf-8")
        if with_report:
            (pub_dir / "report.md").write_text(
                "# 完整报告" + chr(10) + chr(10) + "十节内容。" + chr(10), encoding="utf-8")
        (pub_dir / "bond_stable.csv").write_text(
            "fcode,name" + chr(10) + "000001,示例基金" + chr(10), encoding="utf-8")
        return pub_dir

    def test_empty_root_produces_placeholder(self):
        result = self.mod.build(self.root, self.out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["revisions"], 0)
        index = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn("暂无发布", index)
        self.assertTrue((self.out / "archive/index.html").exists())

    def test_latest_revision_drives_index(self):
        self._revision(run="20260101T073000-screen-aaaaaa", pub="p1",
                       generated_at="2026-01-01T07:30:00+08:00")
        self._revision(run="20260201T073000-screen-bbbbbb", pub="p2",
                       generated_at="2026-02-01T07:30:00+08:00",
                       counts={"bond_stable": 7, "bond_enhanced": 2})
        result = self.mod.build(self.root, self.out)
        self.assertEqual(result["revisions"], 2)
        self.assertEqual(result["latest"]["publication_id"], "p2")
        self.assertEqual(result["latest"]["day"], "2026-02-01")

        index = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn("稳健榜", index)
        self.assertIn("债券型基金筛选日报", index)
        self.assertTrue((self.out / "files/bond_stable.csv").exists())
        self.assertTrue((self.out / "report.html").exists())
        self.assertTrue((self.out / "manifest.json").exists())

        archive = sorted(p.name for p in (self.out / "archive").glob("2026-*.html"))
        self.assertEqual(archive, ["2026-01-01.html", "2026-02-01.html"])

    def test_summary_h1_is_stripped(self):
        self._revision()
        self.mod.build(self.root, self.out)
        index = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("摘要一级标题", index)
        self.assertIn("正文一行", index)

    def test_same_day_publications_do_not_collide(self):
        self._revision(run="20260101T073000-screen-aaaaaa", pub="p1")
        self._revision(run="20260101T080000-screen-cccccc", pub="p2")
        self.mod.build(self.root, self.out)
        archive = sorted(p.name for p in (self.out / "archive").glob("2026-01-01*.html"))
        self.assertEqual(archive, ["2026-01-01-2.html", "2026-01-01.html"])

    def test_revision_without_report_md_still_builds(self):
        self._revision(with_report=False)
        result = self.mod.build(self.root, self.out)
        self.assertEqual(result["revisions"], 1)
        self.assertFalse((self.out / "report.html").exists())
        self.assertTrue((self.out / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
