"""证监会 EID 生命周期采集的解析/窗口/PDF 日期契约测试（不联网）。"""
import json
import unittest

from fundana import lifecycle_csrc as lc
from fundana.fetch.parsers import ParseError, parse_csrc_lifecycle


SAMPLE = {
    "iTotalRecords": 2,
    "aaData": [
        {"fundCode": "008516", "fundShortName": "浦银盛熙一年定开债券",
         "reportCode": "FB060010", "reportName": "清算报告",
         "reportDesp": "清算报告", "reportSendDate": "2026-11-05",
         "reportYear": "2026", "organName": "浦银", "uploadInfoId": 1617188},
        {"fundCode": "97001", "reportCode": "FB060010", "reportName": "境外",
         "reportSendDate": "2026-11-05", "uploadInfoId": 1},
    ],
}


class TestParseCsrcLifecycle(unittest.TestCase):
    def test_keeps_six_digit_and_date(self):
        out = parse_csrc_lifecycle(json.dumps(SAMPLE, ensure_ascii=False))
        self.assertEqual(out["total"], 2)
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["rows"][0]["fcode"], "008516")
        self.assertEqual(out["rows"][0]["report_send_date"], "2026-11-05")

    def test_success_false_is_business_failure(self):
        with self.assertRaises(ParseError):
            parse_csrc_lifecycle('{"success":false,"message":"仅支持30天"}')

    def test_missing_aadata_is_business_failure(self):
        with self.assertRaises(ParseError):
            parse_csrc_lifecycle('{"iTotalRecords":0}')


class TestWindows(unittest.TestCase):
    def test_windows_cover_range_and_are_at_most_30_days(self):
        ws = list(lc.windows("2026-01-01", "2026-03-15"))
        self.assertEqual(ws[0][0], "2026-01-01")
        self.assertEqual(ws[-1][1], "2026-03-15")
        for start, end in ws:
            self.assertLessEqual((lc._date(end) - lc._date(start)).days, 29)
        # 连续且不重叠
        for (_, prev_end), (next_start, _) in zip(ws, ws[1:]):
            self.assertEqual((lc._date(next_start) - lc._date(prev_end)).days, 1)


class TestAoData(unittest.TestCase):
    def test_is_datatables_array_with_paging_keys(self):
        ao = json.loads(lc._ao_data("FB060010", "2026-01-01", "2026-01-30", 100))
        self.assertIsInstance(ao, list)
        names = {x["name"]: x["value"] for x in ao}
        self.assertEqual(names["reportType"], "FB060010")
        self.assertEqual(names["iDisplayStart"], 100)
        self.assertEqual(names["iDisplayLength"], 100)
        self.assertIn("sEcho", names)


class TestPdfDates(unittest.TestCase):
    def test_extracts_last_operating_and_liquidation_start(self):
        text = ("本基金最后运作日为 2026 年 9 月 15 日，自 2026 年 9 月 16 日进\n\n"
                "入清算期。")
        out = lc.parse_pdf_dates(text)
        self.assertEqual(out["last_operating_date"], "2026-09-15")
        self.assertEqual(out["liquidation_start"], "2026-09-16")

    def test_does_not_invent_dates(self):
        self.assertEqual(lc.parse_pdf_dates("清算报告无日期"), {})

    def test_invalid_date_is_ignored(self):
        self.assertEqual(lc.parse_pdf_dates("最后运作日为 2026 年 2 月 30 日"), {})


class TestPickCandidate(unittest.TestCase):
    def test_prefers_liquidation_report(self):
        items = [
            {"report_code": "FC030010", "report_send_date": "2026-09-01", "upload_info_id": "1"},
            {"report_code": "FB060010", "report_send_date": "2026-10-01", "upload_info_id": "2"},
        ]
        self.assertEqual(lc._pick_candidate(items)["report_code"], "FB060010")


if __name__ == "__main__":
    unittest.main()
