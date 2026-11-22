import unittest
from datetime import date, datetime
from unittest.mock import patch
from app.sources import spp_schedule as schedule
from app.sources.spp import SppSource
from app.models import BookRecord
from app.repository import _crawler_write_data
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

    def test_recent_catalog_limit_is_numeric_not_lexicographic(self):
        from unittest.mock import patch
        from app.sources.spp import SppSource
        source=SppSource()
        with patch.object(source,"_sitemap_urls",return_value=[
            "https://www.spp.com.tw/SalePage/Index/9",
            "https://www.spp.com.tw/SalePage/Index/100",
            "https://www.spp.com.tw/SalePage/Index/20",
        ]):
            self.assertTrue(source._recent_catalog_urls(1)[0].endswith("/100"))

    def test_product_dates_win_while_product_metadata_is_enriched(self):
        from unittest.mock import patch
        from datetime import date
        from app.models import BookRecord
        from app.sources.spp import SppSource
        source=SppSource()
        scheduled=BookRecord("spp","A1","測試小說","novel","https://docs.google.com/spreadsheets/test",release_date=date(2026,10,5),release_precision="day")
        detail=BookRecord("spp","A1","測試小說","novel","https://www.spp.com.tw/SalePage/Index/1",release_date=date(2026,9,1),cover_url="https://example.test/cover.jpg",isbn="9781234567890")
        with patch.object(source,"_recent_catalog_urls",return_value=["url"]),patch.object(source,"_fetch_catalog_batch",return_value=[(detail,detail.release_date)]):
            result=source._enrich_schedule([scheduled])
        self.assertEqual(result[0].release_date,date(2026,9,1))
        self.assertEqual(result[0].cover_url,detail.cover_url)

    def test_product_rating_is_parsed(self):
        import json
        from datetime import date
        from app.sources.spp import SppSource
        data={"Title":"测试漫畫(1)","Id":1,"CategoryLevelName":{"Level1_ShopCategory_Name":"漫畫"},
              "SellingStartDateTime":date.today().isoformat(),"ShortDescription":"書 號：A1 等 級：限制級"}
        markup='SalePageIndexViewModel"] = '+json.dumps(data)
        record,_date=SppSource()._parse_detail("https://www.spp.com.tw/SalePage/Index/1",markup)
        self.assertEqual(record.prepared()["content_rating"],"restricted_18")


class ScheduleRecoveryTests(unittest.TestCase):
    def setUp(self):
        class Today(date):
            @classmethod
            def today(cls): return cls(2026,11,22)
        p = patch.object(schedule, 'date', Today)
        p.start(); self.addCleanup(p.stop)
        self.table = '<table><tr><th>上市日</th><th>漫畫/輕小說</th><th>再版</th><th>書號</th></tr><tr><td>2026/11/23</td><td>測試漫畫(02)</td><td></td><td>6J001729</td></tr></table>'
        self.parser = SppSource()._parse_schedule

    @patch.object(schedule, 'fetch_html')
    def test_live_official_table_never_reads_deleted_sheet(self, fetch):
        fetch.return_value = self.table
        rows = schedule.collect_official_schedule(self.parser)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_key, '6J001729')
        self.assertEqual(rows[0].source_url, schedule.PUBLISH)
        fetch.assert_called_once_with(schedule.PUBLISH,timeout=20,attempts=1)

    @patch.object(schedule, 'fetch_html')
    def test_live_table_failure_discovers_only_current_official_sheet(self, fetch):
        doc = 'CURRENT_OFFICIAL_DOCUMENT_12345'
        fetch.side_effect = [RuntimeError('HTTP 500'),
                             f'<a href="https://docs.google.com/spreadsheets/d/{doc}/edit">出書表</a>',
                             'items.push({name: "11511(動漫)", gid: "123"});',
                             '上市日,動漫(新),動漫(再),尖端書碼\n11/24,測試漫畫(01),,6J001728\n']
        rows = schedule.collect_official_schedule(self.parser)
        self.assertEqual(rows[0].release_date, date(2026,11,24))
        self.assertIn(doc, rows[0].source_url)
        self.assertEqual(fetch.call_count, 4)

    @patch.object(schedule, 'fetch_html')
    def test_replaced_sheet_can_fail_without_hiding_next_discovered_document(self, fetch):
        fetch.side_effect = [RuntimeError('HTTP 500'),
            '<a href="https://docs.google.com/spreadsheets/d/DELETED_DOC_123/edit">舊表</a><a href="https://docs.google.com/spreadsheets/d/CURRENT_DOC_123/edit">新表</a>',
            RuntimeError('HTTP 410'), 'items.push({name: "11511(動漫)", gid: "123"});',
            '上市日,動漫(新),動漫(再),尖端書碼\n11/24,測試漫畫(01),,6J001728\n']
        self.assertEqual(len(schedule.collect_official_schedule(self.parser)), 1)

    @patch.object(schedule, 'fetch_html')
    def test_stale_table_and_missing_discovery_are_explicit_failure(self, fetch):
        fetch.side_effect = [self.table.replace('2026/11/23','2026/04/23'), '<html>官網</html>']
        with self.assertRaisesRegex(RuntimeError, '官方出書表不可用'):
            schedule.collect_official_schedule(self.parser)
        self.assertEqual(fetch.call_count, 2)

    @patch.object(schedule, 'fetch_html')
    def test_login_or_wrong_schema_is_not_schedule_success(self, fetch):
        fetch.side_effect = ['<html>請登入</html>'+self.table.replace('漫畫/輕小說','生活圖書'), '<html></html>']
        with self.assertRaisesRegex(RuntimeError, '缺少動漫出書表欄位'):
            schedule.collect_official_schedule(self.parser)

    def test_duplicate_book_code_uses_latest_announcement_date(self):
        rows = self.parser(schedule.PUBLISH,self.table)
        older = BookRecord('spp','6J001729','測試漫畫(02)','manga',schedule.PUBLISH,release_date=date(2026,10,1))
        self.assertEqual(schedule._recent([*rows,older])[0].release_date,date(2026,11,23))

    def test_sparse_official_table_keeps_product_metadata_and_product_date(self):
        old = BookRecord('spp','6J001729','測試漫畫(02)','manga','https://www.spp.com.tw/SalePage/Index/123',
                         release_date=date(2026,11,24), release_date_source='product',
                         release_checked_at=datetime(2026,11,22), isbn='9781234567890',author='作者',
                         cover_url='https://example.test/cover.jpg',list_price=150,
                         content_rating='restricted_18',rating_raw='限制級').prepared()
        incoming = self.parser(schedule.PUBLISH,self.table)[0].prepared()
        merged = _crawler_write_data(old,incoming)
        for key in ('author','isbn','cover_url','list_price','release_date','release_checked_at','source_url','content_rating'):
            self.assertEqual(merged[key],old[key],key)

    def test_all_schedule_paths_fail_catalog_fallback_is_degraded_not_fake_success(self):
        source = SppSource()
        book = BookRecord('spp','6J001729','測試漫畫','manga','https://www.spp.com.tw/SalePage/Index/123')
        with (patch.object(schedule,'collect_official_schedule',side_effect=RuntimeError('HTTP 410')),
             patch.object(source,'_recent_catalog_urls',return_value=[book.source_url]) as urls,
             patch.object(source,'_fetch_catalog_batch',return_value=[(book,None)])):
            rows = source.collect()
        self.assertEqual(rows,[book])
        urls.assert_called_once_with(80)
        self.assertIn('官方出書表不可用',source.schedule_error)
        self.assertEqual(source.errors,[source.schedule_error])

