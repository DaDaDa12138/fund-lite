"""P1-2/P1-3 固定线风格化/动态层影子契约测试。"""
import json
import sqlite3
import unittest

from fundana import db
from fundana.screen import style_shadow


def _fixed(**over):
    base = {
        "composite": 70.0, "annual_1y": 3.0, "sharpe": 2.0, "calmar": 3.0,
        "mdd": 0.01, "vol": 0.03, "non_negative": 85.0, "fee": 0.50,
        "history_3y": True,
    }
    base.update(over)
    return base


def _th(values):
    return {"fixed_or_admission": {k: {"value": v, "pass": True} for k, v in values.items()}}


class TestStyleShadow(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO screen_run(run_id, profile, config_hash, code_version,"
            " formula_contract, status) VALUES(?,?,?,?,?,?)",
            ("r1", "current", "cfg", "code", "contract-1", "done"))

    def _add(self, code, style, layer, fixed, composite=70.0):
        self.conn.execute(
            "INSERT INTO screen_decision(run_id, fcode, layer_reached, passed,"
            " reason_codes_json, style_code, thresholds_json, scores_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("r1", code, layer, 1, "[]", style,
             json.dumps(_th(fixed)), json.dumps({"composite": composite})))

    def test_fixed_style_replaces_only_l6c_floor(self):
        # L9 实际通过固定线，但纯债风格地板 mdd 0.018 更严 -> shadow 退出
        self._add("000001", "bond_pure", "L9", _fixed(mdd=0.019))
        # L6C 固定线 fee 0.90 失败，但纯债风格地板 fee 0.95 -> shadow 进入
        self._add("000002", "bond_pure", "L6C", _fixed(fee=0.95))
        self.conn.commit()

        out = style_shadow.fixed_style_shadow(self.conn, "r1")
        self.assertEqual(out["actual_count"], 1)
        self.assertEqual(out["shadow_count"], 1)
        self.assertEqual(out["entered"], ["000002"])
        self.assertEqual(out["exited"], ["000001"])
        self.assertEqual(out["by_style"]["bond_pure"]["candidates"], 2)

    def test_thresholds_use_per_field_quantiles_and_shift(self):
        members = [{"fcode": str(i), "style": "bond_pure",
                    "scores": {"composite": float(i)}} for i in range(1, 101)]
        metrics = {str(i): {"annual_1y": float(i), "sharpe": float(i),
                            "sortino": float(i), "calmar": float(i),
                            "non_negative": float(i), "fee": float(i)}
                   for i in range(1, 101)}
        th = style_shadow._thresholds(members, metrics, 0.60)
        self.assertEqual(th["composite_q"], 0.60)
        self.assertAlmostEqual(th["quantile_shift"], -0.10, places=4)
        # annual 的生产分位是 .55，shift -0.10 后按 .45 取；不能所有维都用 q60
        expected_annual = style_shadow._q([float(i) for i in range(1, 101)], 0.45)
        self.assertAlmostEqual(th["annual_min_pct"], expected_annual, places=4)
        self.assertNotAlmostEqual(th["annual_min_pct"],
                                  style_shadow._q([float(i) for i in range(1, 101)], 0.60),
                                  places=4)

    def test_missing_metric_is_not_counted_as_zero(self):
        row = {"fcode": "000003", "style": "bond_pure", "scores": {"composite": 80.0}}
        th = {"composite_min": 62.0, "annual_min_pct": 2.0, "sharpe_min": 0.8,
              "sortino_min": 0.8, "calmar_min": 2.8, "non_negative_min_pct": 70.0,
              "fee_max_pct": 0.95}
        ok, npass = style_shadow._dynamic_pass(row, {}, th, 4)
        self.assertFalse(ok)
        self.assertEqual(npass, 0)

    def test_dynamic_shadow_looser_quantile_does_not_reduce_pass_set(self):
        for i in range(20):
            code = "1000%02d" % i
            self._add(code, "bond_pure", "L7", _fixed(fee=0.5), composite=70.0 + i * 0.1)
            self.conn.execute(
                "INSERT INTO fund_metric(fcode, period_days, formula_contract, annual_dec,"
                " sharpe, sortino, calmar_dec, non_negative_ratio_pct) VALUES(?,?,?,?,?,?,?,?)",
                (code, 365, "contract-1", 0.01 + i / 100.0, 1.0 + i * 0.05,
                 1.0 + i * 0.05, 3.0 + i * 0.1, 70.0 + i))
        self.conn.commit()
        q70 = style_shadow.dynamic_shadow(self.conn, "r1", quantile=0.70, k=5)
        q60 = style_shadow.dynamic_shadow(self.conn, "r1", quantile=0.60, k=4)
        self.assertEqual(q70["mother"], 20)
        self.assertEqual(q70["fixed_pass"], 20)
        self.assertGreaterEqual(q60["shadow_dynamic_pass"], q70["shadow_dynamic_pass"])


if __name__ == "__main__":
    unittest.main()
