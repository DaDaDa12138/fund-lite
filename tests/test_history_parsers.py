"""F10 规模/经理历史解析器单测（不联网）。"""
import unittest

from fundana.fetch import parsers


class TestParseGmbd(unittest.TestCase):
    def test_rows_and_inception_partial(self):
        text = """var gmbd_apidata={ content:"<table class='w782 comm gmbd'><thead><tr>
<th>日期</th><th>期间申购（亿份）</th><th>期间赎回（亿份）</th>
<th>期末总份额（亿份）</th><th>期末净资产（亿元）</th>
<th>净资产变动率</th></tr></thead><tbody>
<tr><td>2026-09-30</td><td>0.01</td><td>0.05</td><td>0.38</td><td>0.37</td><td>-17.88%</td></tr>
<tr><td>2013-03-20</td><td>---</td><td>---</td><td>5.63</td><td>---</td><td>---</td></tr>
</tbody></table>", "ABBNAME":"X" }]"""
        out = parsers.parse_gmbd(text)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["rows"][0]["report_period"], "2013-03-20")
        self.assertEqual(out["rows"][0]["share_scale_billion"], 5.63)
        self.assertIsNone(out["rows"][0]["asset_scale_billion"])
        self.assertEqual(out["rows"][1]["asset_scale_billion"], 0.37)

    def test_missing_payload_raises(self):
        with self.assertRaises(parsers.ParseError):
            parsers.parse_gmbd("<html>no data</html>")


class TestParseJjjl(unittest.TestCase):
    def test_multi_manager_row_is_split(self):
        text = """<table class="w782 comm  jloff"><thead><tr><th>起始期</th><th>截止期</th>
<th>基金经理</th><th>任职期间</th><th>任职回报</th></tr></thead><tbody>
<tr><td>2026-09-05</td><td>至今</td>
<td><a href="http://fund.eastmoney.com/manager/30655181.html">邵强</a></td>
<td>57天</td><td class="tor grn">-6.59%</td></tr>
<tr><td>2014-03-21</td><td>2014-04-29</td>
<td><a href="http://fund.eastmoney.com/manager/30198253.html">张三</a>
<a href="http://fund.eastmoney.com/manager/30061448.html">李四</a></td>
<td>39天</td><td class="tor red">0.22%</td></tr>
</tbody></table>"""
        out = parsers.parse_jjjl(text)
        self.assertEqual(out["count"], 3)
        first = out["rows"][0]
        self.assertEqual(first["mgr_id"], "30655181")
        self.assertIsNone(first["end_date"])
        self.assertEqual(first["days"], 57)
        self.assertEqual(first["return_pct"], -6.59)
        multi = [r for r in out["rows"] if r["start_date"] == "2014-03-21"]
        self.assertEqual({r["mgr_id"] for r in multi}, {"30198253", "30061448"})

    def test_missing_table_raises(self):
        with self.assertRaises(parsers.ParseError):
            parsers.parse_jjjl("<html>no table</html>")


class TestParseZqccYear(unittest.TestCase):
    def test_four_quarter_blocks(self):
        text = """var apidata={ content:"<div class='box'>
<div class='boxitem w790'><h4>2022年4季度债券投资明细 截止至：<font>2022-12-31</font></h4>
<table><tr><td>1</td><td>113052</td><td>兴业转债</td><td>7.50%</td><td>601.22</td></tr></table></div>
<div class='boxitem w790'><h4>2022年3季度债券投资明细 截止至：<font>2022-09-30</font></h4>
<table><tr><td>1</td><td>019827</td><td>26国债01</td><td>6.29%</td><td>504.33</td></tr></table></div>
</div>",arryear:[2022],curyear:2026};"""
        out = parsers.parse_zqcc_year(text)
        self.assertEqual([b["period"] for b in out["blocks"]], ["2022Q4", "2022Q3"])
        self.assertEqual(out["blocks"][0]["rows"][0]["bond_code"], "113052")
        self.assertEqual(out["blocks"][1]["rows"][0]["pct_nav"], 6.29)

    def test_missing_payload_raises(self):
        with self.assertRaises(parsers.ParseError):
            parsers.parse_zqcc_year("<html>no data</html>")


class TestParseChinabondIndex(unittest.TestCase):
    def test_epoch_is_beijing_date_and_pct_chain(self):
        # 1010073600000 = 2002-01-03T16:00:00Z = 2002-01-04 00:00 Asia/Shanghai
        text = ('{"CFZS_00": {"1010073600000": 100.0, "1010419200000": 101.0},'
                ' "name": "中债-新综合财富(总值)指数"}')
        out = parsers.parse_chinabond_index(text)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["rows"][0]["trade_date"], "2002-01-04")
        self.assertIsNone(out["rows"][0]["pct_chg"])
        self.assertAlmostEqual(out["rows"][1]["pct_chg"], 1.0)

    def test_missing_series_raises(self):
        with self.assertRaises(parsers.ParseError):
            parsers.parse_chinabond_index('{"name": "x"}')


class TestTenureDays(unittest.TestCase):
    def test_year_and_days(self):
        self.assertEqual(parsers._tenure_days("2年又46天"), 776)

    def test_days_only(self):
        self.assertEqual(parsers._tenure_days("57天"), 57)

    def test_year_only(self):
        self.assertEqual(parsers._tenure_days("3年"), 1095)


if __name__ == "__main__":
    unittest.main()
