"""P2-12 报告结构契约：缺字段/缺章节必须在写盘前失败。"""
import tempfile
import unittest
from pathlib import Path

from fundana.publish import report_schema, report_v2


def _report():
    sections = [{"id": sid, "title": sid, "markdown": "x"}
                for sid in report_schema.REQUIRED_SECTIONS]
    facts = {k: 0 for k in report_schema.REQUIRED_FACTS}
    facts["formal_by_board"] = {}
    facts["board_counts"] = {}
    facts["data_asof"] = {}
    return {
        "schema_version": 2, "run_id": "r", "generated_at": "t",
        "run": {}, "counts": {}, "facts": facts, "sections": sections,
        "markdown": "# report",
    }


class TestReportSchema(unittest.TestCase):
    def test_valid_report_passes(self):
        self.assertEqual(report_schema.validate(_report()), [])

    def test_missing_section_and_fact_are_reported(self):
        r = _report()
        r["sections"] = [s for s in r["sections"] if s["id"] != "capacity"]
        r["facts"].pop("published")
        errs = report_schema.validate(r)
        self.assertTrue(any("capacity" in e for e in errs))
        self.assertTrue(any("facts.published" in e for e in errs))

    def test_write_refuses_invalid_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                report_v2.write(Path(tmp), {"schema_version": 2})


if __name__ == "__main__":
    unittest.main()
