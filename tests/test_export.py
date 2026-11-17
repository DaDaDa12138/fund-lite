"""导出与反事实：CSV 字节形态、单位换算、按上一轮门槛判定。"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import unittest

from fundana.api import csv_export as ex
from fundana.api.decision import _counterfactual


def _rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))


class CsvBuildTest(unittest.TestCase):
    def test_bom_crlf_and_percent_conversion(self):
        data = ex.build([("fcode", "代码"), ("annual_1y", "1年年化(%)", ex._to_pct)],
                        [{"fcode": "000003", "annual_1y": 0.0491915346}])
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", data)
        rows = _rows(data)
        self.assertEqual(rows[0], ["代码", "1年年化(%)"])
        self.assertEqual(rows[1][0], "000003")
        self.assertEqual(rows[1][1], "4.91915346")

    def test_none_and_missing_become_empty_not_zero(self):
        data = ex.build([("a", "A"), ("b", "B"), ("c", "C", ex._to_pct)],
                        [{"a": None, "b": 0.0}])
        self.assertEqual(_rows(data)[1], ["", "0", ""])

    def test_quotes_comma_in_name(self):
        data = ex.build([("name", "名称")], [{"name": "某某,债券A"}])
        self.assertIn('"某某,债券A"', data.decode("utf-8-sig"))

    def test_bool_and_float_trimming(self):
        data = ex.build([("flag", "F"), ("v", "V")], [{"flag": True, "v": 0.1 + 0.2}])
        self.assertEqual(_rows(data)[1], ["1", "0.3"])

    def test_filename_has_date_and_optional_suffix(self):
        name = ex.filename("bond_rankings", "bond_stable")
        self.assertTrue(name.startswith("bond_rankings_bond_stable_"))
        self.assertTrue(name.endswith(".csv"))
        self.assertEqual(len(name.split("_")[-1]), 12)  # 20261101.csv

    def test_ranking_columns_keep_code_as_text(self):
        data = ex.build(ex.RANKING_COLUMNS, [{"fcode": "000003", "name": "x"}])
        self.assertEqual(_rows(data)[1][1], "000003")


class CounterfactualTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE screen_decision (run_id TEXT, fcode TEXT, thresholds_json TEXT)")
        self.conn.execute("CREATE TABLE screen_pick (run_id TEXT, board TEXT, fcode TEXT, formal_score REAL)")

    def tearDown(self):
        self.conn.close()

    def _prev(self, thresholds):
        self.conn.execute("INSERT INTO screen_decision VALUES ('prev','000001',?)",
                          (json.dumps(thresholds),))
        return {"run_id": "prev", "created_at": "2026-10-31T18:58:57"}

    def test_uses_previous_bounds_on_current_values(self):
        previous = self._prev({
            "fixed_or_admission": {"composite": {"min": 70, "value": 71},
                                   "annual_1y": {"min": 2.0, "value": 2.4},
                                   "mdd_1y": {"max": 0.02, "value": 0.019}},
            "dynamic": {"sharpe_1y": {"min": 2.5, "value": 2.6}},
        })
        current = {
            "fixed_or_admission": {"composite": {"value": 75}, "annual_1y": {"value": 1.5},
                                   "mdd_1y": {"value": 0.01}},
            "dynamic": {"sharpe_1y": {"value": 2.9}},
        }
        cf = _counterfactual(self.conn, "000001", current, 80.0, previous, "bond_stable")
        self.assertEqual(cf["run_id"], "prev")
        self.assertEqual(cf["n_items"], 4)
        self.assertEqual(cf["n_passed"], 3)
        self.assertFalse(cf["would_pass"])
        self.assertEqual(cf["failed"], ["1 年年化"])
        annual = [i for g in cf["groups"] for i in g["items"] if i["key"] == "annual_1y"][0]
        self.assertEqual(annual["op"], "min")
        self.assertEqual(annual["threshold"], 2.0)
        self.assertAlmostEqual(annual["margin"], -0.5)

    def test_rank_estimate_counts_better_scores(self):
        previous = self._prev({"fixed_or_admission": {"composite": {"min": 70}}})
        for i, score in enumerate((90.0, 85.0, 70.0)):
            self.conn.execute("INSERT INTO screen_pick VALUES ('prev','bond_stable',?,?)",
                              ("00000%d" % (i + 2), score))
        cf = _counterfactual(self.conn, "000001", {}, 80.0, previous, "bond_stable")
        est = cf["rank_estimate"]
        self.assertEqual(est["position"], 3)
        self.assertEqual(est["sample_n"], 3)
        self.assertIn("不是重跑结果", est["note"])

    def test_no_previous_returns_none(self):
        self.assertIsNone(_counterfactual(self.conn, "000001", {}, 80.0, None, "bond_stable"))

    def test_previous_without_decision_row_returns_none(self):
        cf = _counterfactual(self.conn, "000999", {}, 80.0, {"run_id": "prev"}, "bond_stable")
        self.assertIsNone(cf)

    def test_meta_keys_are_skipped(self):
        previous = self._prev({"dynamic": {"_meta": {"rule": "k_of_n"},
                                           "sharpe_1y": {"min": 2.0, "value": 2.1}}})
        cf = _counterfactual(self.conn, "000001", {"dynamic": {"sharpe_1y": {"value": 2.2}}},
                             None, previous, "bond_stable")
        self.assertEqual(cf["n_items"], 1)
        self.assertTrue(cf["would_pass"])


if __name__ == "__main__":
    unittest.main()
