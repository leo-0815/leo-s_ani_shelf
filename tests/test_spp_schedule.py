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
        record,_date=SppSource()._parse_detail("https://example.test/1",markup)
        self.assertEqual(record.prepared()["content_rating"],"restricted_18")

