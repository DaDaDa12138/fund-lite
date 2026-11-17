"""解析器契约测试：真实响应形态（含裸键、未闭合 td、转义引号）。"""
import unittest

from fundana.fetch.parsers import (ParseError, lenient_json, parse_directory, parse_jbgk,
                                   parse_pzd, parse_rank, parse_status, parse_zqcc, pzd_nav_rows,
                                   to_float)


class TestToFloat(unittest.TestCase):
    def test_empty_is_none_not_zero(self):
        for raw in ("", " ", "-", "--", "null", None, "暂无"):
            self.assertIsNone(to_float(raw), repr(raw))

    def test_zero_and_number(self):
        self.assertEqual(to_float("0"), 0.0)
        self.assertEqual(to_float("3.11%"), 3.11)
        self.assertEqual(to_float("1,234.5"), 1234.5)


class TestLenientJson(unittest.TestCase):
    def test_bare_keys_and_trailing_comma(self):
        obj = lenient_json('{datas:[],record:"2",curpage:1,}')
        self.assertEqual(obj["record"], "2")
        self.assertEqual(obj["curpage"], 1)


class TestDirectory(unittest.TestCase):
    def test_keeps_leading_zero_code(self):
        text = ('var r = [["000003","ZHKZZZA","中海可转债债券A","债券型-混合二级","ZHONGHAI"],'
                '["000001","HXCZ","华夏成长","股票型","HUAXIA"]];')
        rows = parse_directory(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["code"], "000003")
        self.assertEqual(rows[0]["type_text"], "债券型-混合二级")


class TestStatus(unittest.TestCase):
    def test_unquoted_keys_and_13_columns(self):
        row = ('["000032","易方达信用债债券A","债券型-长债","1.2340","10-30","暂停申购",'
               '"开放赎回","","10.00","1e10","1.0","4","0.08"]')
        text = ('var reData={datas:[' + row + '],record:"1",pages:"1",curpage:"1",'
                'showday:["2026-10-30","2026-10-29"]};')
        out = parse_status(text)
        self.assertEqual(out["declared_total"], 1)
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["purchase_status"], "暂停申购")
        self.assertEqual(out["rows"][0]["flag_code"], "4")


class TestRank(unittest.TestCase):
    def test_parts_custom_pct_and_fee(self):
        parts = ["000003", "中海可转债债券A", "zhkzz", "2026-10-30", "1.2340", "2.3450",
                 "0.12", "0.34", "1.2", "2.3", "3.4", "4.5", "5.6", "6.7", "7.8", "8.9",
                 "2013-03-19", "", "12.34", "1.20", "0.08"]
        text = 'var rankData = {datas:["' + ",".join(parts) + '"],allRecords:1,pageIndex:1};'
        out = parse_rank(text)
        self.assertEqual(out["rows"][0]["code"], "000003")
        # 第 18 段是自定义区间涨幅，不是规模：必须落 custom_pct，且 scale_yi 恒为 None
        self.assertEqual(out["rows"][0]["custom_pct"], "12.34")
        self.assertIsNone(out["rows"][0]["scale_yi"])
        self.assertEqual(out["rows"][0]["fee_now_pct"], "0.08")

    def test_empty_datas_raises(self):
        with self.assertRaises(ParseError):
            parse_rank('var rankData = {datas:[],allRecords:0};')


class TestPzd(unittest.TestCase):
    def test_nav_and_growth(self):
        text = ('var fS_name = "中海可转债债券A";var Data_netWorthTrend = ['
                '{"x":1363651200000,"y":1.0,"equityReturn":0},'
                '{"x":1363737600000,"y":1.01,"equityReturn":1.0}];'
                'var Data_ACWorthTrend = [[1363651200000,1.0],[1363737600000,1.01]];'
                'var syl_1n = "3.27";var fund_Rate="0.08";')
        out = parse_pzd(text)
        self.assertEqual(out["name"], "中海可转债债券A")
        self.assertEqual(out["syl_1n_pct"], 3.27)
        rows = pzd_nav_rows(out["nav"], out["acc"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["nav_date"], "2013-03-19")
        self.assertEqual(rows[1]["growth_pct"], 1.0)
        self.assertAlmostEqual(rows[1]["acc_nav"], 1.01)


class TestJbgk(unittest.TestCase):
    def test_unclosed_td_and_fields(self):
        html = ('<table class="info w790"><tr><th>基金全称</th>'
                '<td class="thf">易方达信用债债券型证券投资基金</td><th>基金简称</th><td>A类</td></tr>'
                '<tr><th>基金代码</th><td>000032（前端）<th>基金类型</th><td>债券型-长债</td></tr>'
                '<tr><th>成立日期/规模</th><td>2013年04月24日 / 63.80亿元</td>'
                '<th>净资产规模</th><td>63.80亿元（截止至：2026年09月30日）</td></tr>'
                '<tr><th>基金管理人</th><td>易方达基金</td><th>基金托管人</th><td>中国工商银行</td></tr>'
                '<tr><th>管理费率</th><td>0.30%（每年）</td><th>托管费率</th><td>0.10%（每年）</td></tr>'
                '<tr><th>销售服务费率</th><td>0.00%（每年）</td><th>最高申购费率</th><td>0.80%</td></tr>'
                '</table>')
        out = parse_jbgk(html)
        self.assertEqual(out["full_name"], "易方达信用债债券型证券投资基金")
        self.assertEqual(out["estab_date"], "2013-04-24")
        self.assertEqual(out["asset_scale"], 63.80)
        self.assertEqual(out["mgr_fee_pct"], 0.30)
        self.assertEqual(out["trust_fee_pct"], 0.10)
        self.assertEqual(out["type_text"], "债券型-长债")


class TestZqcc(unittest.TestCase):
    def test_escaped_quotes_and_period(self):
        text = ('var apidata={ content:"<div class=\'box\'><h4>2026年3季度债券投资明细</h4>'
                '<table><tr><td>1</td><td>232680012</td><td>26邮储银行二级资本债01BC</td>'
                '<td>3.11%</td><td>12345.67</td></tr></table>",arryear:[2026,2025],curyear:2026};')
        out = parse_zqcc(text)
        self.assertEqual(out["period"], "2026Q3")
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["bond_code"], "232680012")
        self.assertAlmostEqual(out["rows"][0]["pct_nav"], 3.11)


if __name__ == "__main__":
    unittest.main()


class NavSnapshotTest(unittest.TestCase):
    """B1 全市场快照：裸键 + 尾逗号 + 名称明文（chars 只是压缩表，不用解码）。"""

    TEXT = (
        'var reData={chars:"abc",datas:['
        '["000003","中海可转债债券A","ZHKZZZ",0.936,1.146,0.949,1.159,-0.013,-1.37,'
        '"开放申购","开放赎回","",1,0,2,"",1,"0.08%","0.08%",1,"0.80%"],'
        '["019591","平安0-3年期政策性金融债债券D","PA0-3",1.1396,1.2291,1.1406,1.2301,'
        '-0.001,-0.09,"开放申购","开放赎回","",10,0,2,"",1,"0.40%","0.40%",1,"0.50%"]'
        '],count:24316,record:"24316",pages:"1",curpage:"1",indexsy:[-1.44,-0.11,],'
        'showday:["2026-10-30","2026-10-29"]};'
    )

    def test_parse_snapshot(self):
        from fundana.fetch.parsers import parse_nav_snapshot
        out = parse_nav_snapshot(self.TEXT)
        self.assertEqual(out["declared_total"], 24316)
        self.assertEqual(out["showday"], ["2026-10-30", "2026-10-29"])
        self.assertEqual(len(out["rows"]), 2)
        first = out["rows"][0]
        self.assertEqual(first["code"], "000003")
        self.assertEqual(first["unit_nav"], 0.936)
        self.assertEqual(first["growth_pct"], -1.37)
        self.assertEqual(first["purchase_status"], "开放申购")
        self.assertEqual(first["fee_max_pct"], 0.8)

    def test_ms_to_date_uses_beijing_time(self):
        # 东财 x 是"业务日零点（北京时间）"；用 UTC 解释会整体左移一天（ADR-019）
        from fundana.fetch.parsers import _ms_to_date
        self.assertEqual(_ms_to_date(1793289600000), "2026-10-30")
        self.assertEqual(_ms_to_date(1793203200000), "2026-10-29")
        self.assertIsNone(_ms_to_date(None))
