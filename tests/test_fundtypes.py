"""类型插件注册表的契约（ADR-077）。

这一层是"加一种基金类型"的唯一入口，所以测试要守住三件事：
1. 前缀归类与 ADR-061 的历史行为逐字一致（否则 universe 会悄悄变大或变小）；
2. 注册表里的 styles / boards 与 screen/thresholds 常量一致（防止两处各写一份）；
3. 未实现的类型必须报错并给出缺口，不能静默跑成空榜。
"""
import sqlite3
import unittest

from fundana import collect, db, fundtypes
from fundana.screen import thresholds as T


class TestTypeClassification(unittest.TestCase):
    def test_prefixes_map_to_groups(self):
        cases = {
            "债券型-长债": "bond",
            "债券型-中短债": "bond",
            "货币型-普通货币": "money",
            "股票型-标准股票": "equity",
            "混合型-偏股": "hybrid",
            "指数型-股票": "index",
            "QDII-指数": "qdii",
            "FOF-稳健型": "fof",
        }
        for text, group in cases.items():
            self.assertEqual(fundtypes.type_group(text), group, text)

    def test_bond_index_type_goes_to_bond(self):
        # ADR-061：东财把 1,356 只被动债基放在「指数型-固收」，必须归 bond
        self.assertEqual(fundtypes.type_group("指数型-固收"), "bond")
        self.assertEqual(fundtypes.type_group("指数型-固收(ETF)"), "bond")

    def test_unknown_type_is_other(self):
        self.assertEqual(fundtypes.type_group(None), "other")
        self.assertEqual(fundtypes.type_group(""), "other")
        self.assertEqual(fundtypes.type_group("其他-未分类"), "other")

    def test_bond_prefixes_include_index_fixed_income(self):
        self.assertIn("指数型-固收", fundtypes.type_prefixes("bond"))
        self.assertNotIn("指数型-固收", fundtypes.type_prefixes("index"))


class TestRegistry(unittest.TestCase):
    def test_bond_is_implemented_with_profile(self):
        bond = fundtypes.get("bond")
        self.assertTrue(bond.implemented)
        self.assertEqual(bond.screen_profile, T.SCREEN_PROFILE)

    def test_registry_matches_thresholds(self):
        bond = fundtypes.get("bond")
        self.assertEqual(tuple(bond.styles), tuple(T.STYLE_ORDER))
        self.assertEqual(tuple(bond.boards), tuple(T.ALL_BOARDS))

    def test_every_registered_code_is_classifiable(self):
        groups = {g for _, g in fundtypes.TYPE_PREFIXES} | set(fundtypes.EXTRA_PREFIXES)
        for code in fundtypes.REGISTRY:
            self.assertIn(code, groups, code)

    def test_unimplemented_types_list_gaps(self):
        for code, ft in fundtypes.REGISTRY.items():
            if ft.implemented:
                continue
            self.assertTrue(ft.gaps, f"{code} 未实现却没有写缺口")
            self.assertIsNone(ft.screen_profile)


class TestResolve(unittest.TestCase):
    def test_default_is_bond(self):
        self.assertEqual(fundtypes.resolve(), ("bond",))
        self.assertEqual(fundtypes.resolve(cfg=None), ("bond",))

    def test_config_overrides_default(self):
        self.assertEqual(fundtypes.resolve(cfg={"universe": {"types": ["bond"]}}), ("bond",))

    def test_explicit_overrides_config(self):
        cfg = {"universe": {"types": ["bond"]}}
        self.assertEqual(fundtypes.resolve("bond", cfg), ("bond",))

    def test_comma_string_and_dedup(self):
        self.assertEqual(fundtypes.resolve("bond,bond"), ("bond",))
        self.assertEqual(fundtypes.resolve(["bond"]), ("bond",))

    def test_all_returns_implemented(self):
        self.assertEqual(fundtypes.resolve("all"), fundtypes.implemented_codes())

    def test_unknown_raises(self):
        with self.assertRaises(ValueError) as ctx:
            fundtypes.resolve("gold")
        self.assertIn("未知基金类型", str(ctx.exception))

    def test_unimplemented_raises_with_gaps(self):
        with self.assertRaises(ValueError) as ctx:
            fundtypes.resolve("money")
        msg = str(ctx.exception)
        self.assertIn("尚未实现", msg)
        self.assertIn("万份收益", msg)   # 缺口要能被用户读懂，不是一句"不支持"

    def test_stub_config_without_get_is_tolerated(self):
        class Stub:
            pass
        self.assertEqual(fundtypes.resolve(cfg=Stub()), ("bond",))

    def test_where_builds_placeholders(self):
        clause, params = fundtypes.where()
        self.assertEqual(clause, "type_group IN (?)")
        self.assertEqual(params, ["bond"])
        clause, params = fundtypes.where(column="f.type_group")
        self.assertEqual(clause, "f.type_group IN (?)")


class TestUncovered(unittest.TestCase):
    def test_uncovered_reports_counts_and_gaps_without_mixing(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        for code, group in (("b1", "bond"), ("h1", "hybrid"), ("h2", "hybrid"),
                            ("q1", "qdii")):
            conn.execute("INSERT INTO fund(fcode, type_group, identity_status)"
                         " VALUES(?,?,?)", (code, group, "ok"))
        conn.commit()
        out = {r["code"]: r for r in fundtypes.uncovered(conn)}
        self.assertNotIn("bond", out)
        self.assertEqual(out["hybrid"]["funds"], 2)
        self.assertEqual(out["qdii"]["funds"], 1)
        self.assertTrue(out["hybrid"]["gaps"])


class TestBackwardCompat(unittest.TestCase):
    def test_collect_reexports_old_names(self):
        self.assertEqual(collect.type_group("债券型-长债"), "bond")
        self.assertIn("指数型-固收", collect.type_prefixes("bond"))
        self.assertEqual(collect.BOND_INDEX_TYPE, fundtypes.BOND_INDEX_TYPE)


if __name__ == "__main__":
    unittest.main()
