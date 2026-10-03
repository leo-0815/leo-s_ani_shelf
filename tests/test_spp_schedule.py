import unittest
from app.sources.spp_schedule import sheet_tabs, schedule_rows


class OfficialScheduleTests(unittest.TestCase):
    def test_discovers_only_anime_tabs_and_converts_roc_year(self):
        markup = 'items.push({name: "11510(動漫)", pageUrl: "x", gid: "123"});items.push({name: "11510(圖書)", gid: "456"});'
        self.assertEqual(sheet_tabs(markup), [(2026, 10, "123")])

    def test_csv_quotes_and_dates(self):
        rows = schedule_rows('上市日,動漫(新),動漫(再),尖端書碼\n10/1,"【輕小說】你好，魔女",,7B001189\n10/2,,漫畫(02),6J001751\n', 2026)
        self.assertEqual(rows[0], ["2026/10/01", "【輕小說】你好，魔女", "", "7B001189"])
        self.assertEqual(len(rows), 2)

    def test_invalid_document_is_not_silent_success(self):
        with self.assertRaises(RuntimeError):
            schedule_rows("<html>Sign in</html>", 2026)

