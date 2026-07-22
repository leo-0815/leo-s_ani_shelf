from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from app.sources.chingwin import ChingWinSource
from app.sources.egmanga import EgMangaSource
from app.sources.kadokawa import KadokawaSource
from app.sources.spp import SppSource
from app.sources.tohan import TohanSource
from app.sources.tongli import TongLiSource


class SourceParserTests(unittest.TestCase):
    def test_tohan_backfill_resumes_with_one_page_overlap(self) -> None:
        page_one = """
        <a href="?cid=1&page=2">2</a>
        <a href="product.php?act=view&cid=1&id=one">book one</a>
        """
        page_two = '<a href="product.php?act=view&cid=1&id=two">book two</a>'
        source = TohanSource()
        with (
            patch(
                "app.repository.get_backfill_progress",
                return_value={
                    "catalog_v2_catalog": {"next_page": 3, "completed": False}
                },
            ),
            patch(
                "app.repository.get_sync_state",
                return_value={"backfill_completed": False},
            ),
            patch(
                "app.sources.tohan.fetch_html",
                side_effect=[page_one, page_two],
            ) as fetch,
        ):
            batches = list(
                source.collect_batches(known_keys={"one", "two"}, backfill=True)
            )

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][0], [])
        self.assertTrue(batches[0][1]["completed"])
        self.assertIn("page=2", fetch.call_args_list[-1].args[0])

    def test_kadokawa_backfill_resumes_with_two_page_overlap(self) -> None:
        source = KadokawaSource()
        progress = {
            "catalog_v2_upcoming": {"next_page": 2, "completed": True},
            "catalog_v2_manga": {"next_page": 4, "completed": False},
            "catalog_v2_novel": {"next_page": 2, "completed": True},
        }
        with (
            patch("app.repository.get_backfill_progress", return_value=progress),
            patch.object(source, "_fetch", return_value="<html></html>") as fetch,
        ):
            batches = list(source.collect_batches(known_keys=set(), backfill=True))

        self.assertEqual(len(batches), 1)
        self.assertIn("page=2", fetch.call_args.args[0])

    def test_tohan_detail_uses_document_title_when_og_title_is_generic(self) -> None:
        markup = """
        <html><head>
          <title>邂逅命定之人 1 首刷限定版_PURE系列_漫畫 | 台灣東販</title>
          <meta property="og:title" content="書名">
          <meta property="og:image" content="https://example.test/cover.jpg">
        </head><body>
          <nav><span>書名</span><span>作者</span><span>ISBN</span></nav>
          <h3>邂逅命定之人 1 首刷限定版</h3>
          <p>作者</p><p>あなしん</p><p>譯者</p><p>高意婷</p>
          <p>ISBN</p><p>4714453010566</p><p>出版日期</p><p>2026-03-26</p>
          <p>定價</p><p>NT$230</p>
        </body></html>
        """
        record = TohanSource()._parse_detail(
            "https://www.tohan.com.tw/product.php?act=view&cid=2&id=8481", markup
        )
        self.assertEqual(record.title, "邂逅命定之人 1 首刷限定版")
        self.assertEqual(record.author, "あなしん")
        self.assertEqual(record.isbn, "4714453010566")
        self.assertEqual(record.release_date.isoformat(), "2026-03-26")

    def test_chingwin_rows_without_space_before_digital_label(self) -> None:
        markup = """
        <html><head><meta property="og:title" content="《青文出版社》2026年7月預定出書表"></head>
        <body>
          <p>類別：漫畫 書名：月刊少女野崎同學(17) 作者：樁泉 書系：BOY
          定價：140 首刷附錄：電子書：〇(附電子特典)</p>
          <p>類別：小說 書名：測試小說(01)限定版 作者：測試作者 書系：青文文庫
          定價：250 首刷附錄：限量卡電子書：</p>
        </body></html>
        """
        records = ChingWinSource()._parse_article(
            "https://www.ching-win.com.tw/about-news-detail/202", markup
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].media_type, "manga")
        self.assertEqual(records[1].media_type, "novel")
        self.assertEqual(records[1].release_precision, "month")

    def test_chingwin_catalog_page_uses_embedded_book_data(self) -> None:
        markup = """
        <html><body>
        <script type="application/ld+json">
        {"@type":"Product","name":"(預購)測試漫畫(14)","image":"cover.jpg",
         "sku":"10521514","offers":{"price":"126"}}
        </script>
        <script type="application/ld+json">
        {"@type":"Book","name":"(預購)測試漫畫(14)","image":"cover.jpg",
         "author":{"@type":"person","name":["原作者","繪者","譯者"]},
         "isbn":"9786264498418","datePublished":"2026-07-25"}
        </script>
        <p>Showing 1-24 of 48 results</p>
        </body></html>
        """
        records, pages = ChingWinSource()._parse_catalog_page(
            "https://www.ching-win.com.tw/products/chingwin/books/comic/?page=1",
            markup,
            "manga",
            date(2025, 7, 17),
        )
        self.assertEqual(pages, 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source_key, "10521514")
        self.assertEqual(records[0].title, "測試漫畫(14)")
        self.assertEqual(records[0].author, "原作者")
        self.assertEqual(records[0].release_date.isoformat(), "2026-07-25")

    def test_chingwin_catalog_page_filters_books_older_than_one_year(self) -> None:
        markup = """
        <script type="application/ld+json">
        {"@type":"Product","name":"舊漫畫(1)","sku":"10000001","offers":{"price":"100"}}
        </script>
        <script type="application/ld+json">
        {"@type":"Book","name":"舊漫畫(1)","author":{"name":"作者"},
         "isbn":"9780000000001","datePublished":"2025-07-16"}
        </script>
        <p>Showing 1-1 of 1 results</p>
        """
        records, _pages = ChingWinSource()._parse_catalog_page(
            "https://example.test/comic?page=1",
            markup,
            "manga",
            date(2025, 7, 17),
        )
        self.assertEqual(records, [])

    def test_spp_schedule_parses_new_and_reprint_columns(self) -> None:
        markup = """
        <table><tr><th>上市日</th><th>漫畫/輕小說</th><th>再版</th><th>書號</th></tr>
        <tr><td>2026/7/3</td><td>試著愛上你(全)</td><td></td><td>7T000006</td></tr>
        <tr><td>2026/7/6</td><td></td><td>【輕小說】果然我的青春戀愛喜劇搞錯了(10)</td><td>25038754</td></tr>
        </table>
        """
        records = SppSource()._parse_schedule("https://example.test/spp", markup)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].media_type, "manga")
        self.assertEqual(records[1].media_type, "novel")
        self.assertEqual(records[1].title, "果然我的青春戀愛喜劇搞錯了(10)")

    def test_spp_detail_uses_embedded_official_product_data(self) -> None:
        markup = """
        <script>
        window.ServerRenderData["SalePageIndexViewModel"] = {
          "Id":11840077,
          "CategoryLevelName":{"Level1_ShopCategory_Name":"漫畫"},
          "Title":"天使心 2nd SEASON 典藏版(13)",
          "ShortDescription":"<ul><li>條　碼：9786264552158</li><li>書　號：6I001190</li><li>作　者：北条司</li><li>上市日：2026/06/09</li></ul>",
          "SuggestPrice":280.0,
          "SellingStartDateTime":"2026-06-09T00:00:00",
          "ImageList":[{"PicUrl":"//img.example/cover.jpg"}]
        };
        </script>
        """
        record, release = SppSource()._parse_detail(
            "https://www.spp.com.tw/SalePage/Index/11840077",
            markup,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.source_key, "6I001190")
        self.assertEqual(record.author, "北条司")
        self.assertEqual(record.isbn, "9786264552158")
        self.assertEqual(record.list_price, 280)
        self.assertEqual(record.cover_url, "https://img.example/cover.jpg")
        self.assertEqual(release, date(2026, 6, 9))

    def test_egmanga_article_keeps_exact_release_day(self) -> None:
        markup = """
        <article><time>2026-06-22</time>
        <table><tr><th>上市日期</th><th>類別</th><th>書名</th><th>作者</th><th>系列</th><th>定價</th></tr>
        <tr><td>7/2</td><td>漫畫</td><td>金絲雀夢見璀璨繁星 1</td><td>sheepD</td><td>百合GL</td><td>130</td></tr>
        </table></article>
        """
        records = EgMangaSource()._parse_article("https://example.test/july", markup)
        self.assertEqual(records[0].release_date.isoformat(), "2026-07-02")
        self.assertEqual(records[0].list_price, 130)

    def test_tongli_detail_extracts_current_book_fields(self) -> None:
        markup = """
        <html><head><meta property="og:image" content="https://example.test/book.jpg"></head><body>
        <h2>小說書籍資料</h2><h3>測試輕小說（首刷附錄版）</h3>
        <p>原文書名：</p><p>Test</p><p>集數：</p><p>第3集</p>
        <p>作者：測試作者</p><p>插畫：測試插畫</p><p>系列別：輕小說</p>
        <p>出版日期：2026/7/16</p><p>ISBN：978-626-027-949-3</p>
        <p>新台幣售價：240 元</p><h2>內容簡介</h2>
        </body></html>
        """
        record = TongLiSource()._parse_detail(
            "https://www.tongli.com.tw/BooksDetail.aspx?Bd=NE0190003A", markup
        )
        self.assertEqual(record.title, "測試輕小說（首刷附錄版） (3)")
        self.assertEqual(record.media_type, "novel")
        self.assertEqual(record.author, "測試作者")
        self.assertEqual(record.release_date.isoformat(), "2026-07-16")

    def test_tongli_schedule_is_official_undated_release(self) -> None:
        markup = """
        <table>
          <tr><th>書名／集數</th><th>作者</th><th>系列</th><th>開數</th><th>定價</th><th>備註</th></tr>
          <tr><td>#辣妹與辣妹的百合 1</td><td>INOUE</td><td>百合姬</td><td>32K</td><td>140</td><td></td></tr>
          <tr><td>克羅洛戰記7</td><td>作者甲</td><td>輕小說</td><td>32K</td><td>230</td><td></td></tr>
        </table>
        """
        records = TongLiSource()._parse_schedule(
            "https://www.tongli.com.tw/Search1.aspx?Page=1",
            markup,
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].title, "辣妹與辣妹的百合 1")
        self.assertEqual(records[0].release_status, "scheduled")
        self.assertIsNone(records[0].release_date)
        self.assertEqual(records[1].media_type, "novel")
        self.assertTrue(records[0].source_key.startswith("planned:"))

    def test_kadokawa_detail_uses_final_breadcrumb_category(self) -> None:
        markup = """
        <html><head><meta property="og:title" content="志乃與戀 Future (1)">
        <meta property="og:image" content="https://example.test/k.jpg"></head><body>
        <nav>漫畫 全系列 輕小說 新品上市 ALL</nav>
        <h1>志乃與戀 Future (1)</h1>
        <p>作者資訊：作者：日日綴郎 / 插畫：千種みのり</p>
        <p>上市日期：2026/05/13</p><p>ISBN：9786264455312</p><p>NT$260 NT$205</p>
        </body></html>
        """
        record = KadokawaSource()._parse_detail(
            "https://www.kadokawa.com.tw/products/9786264455312", markup
        )
        self.assertEqual(record.media_type, "novel")
        self.assertEqual(record.list_price, 260)
        self.assertEqual(record.isbn, "9786264455312")

    def test_kadokawa_detail_accepts_new_author_and_barcode_fields(self) -> None:
        markup = """
        <html><head><meta property="og:title" content="預購-測試漫畫 (8)（首刷特裝版）">
        <meta property="og:image" content="https://example.test/k2.jpg"></head><body>
        <nav>輕小說 全系列 漫畫 新品上市 ALL</nav>
        <h1>預購-測試漫畫 (8)（首刷特裝版）</h1>
        <p>作者：測試漫畫家</p>
        <p>上市日期：2026/07/23</p><p>條碼：4711289631484</p><p>NT$330</p>
        </body></html>
        """
        record = KadokawaSource()._parse_detail(
            "https://www.kadokawa.com.tw/products/4711289631484", markup
        )
        self.assertEqual(record.title, "測試漫畫 (8)（首刷特裝版）")
        self.assertEqual(record.media_type, "manga")
        self.assertEqual(record.author, "測試漫畫家")
        self.assertEqual(record.isbn, "4711289631484")
        self.assertEqual(record.list_price, 330)
        self.assertEqual(record.release_date.isoformat(), "2026-07-23")

    def test_kadokawa_category_page_url_uses_72_items(self) -> None:
        url = KadokawaSource._page_url(
            "https://www.kadokawa.com.tw/categories/%E6%BC%AB%E7%95%AB",
            7,
        )
        self.assertIn("limit=72", url)
        self.assertIn("page=7", url)

    def test_kadokawa_detail_keeps_catalog_item_with_missing_metadata(self) -> None:
        markup = """
        <html><head><meta property="og:title" content="資料不完整的作品 (4)">
        <meta property="og:image" content="https://example.test/k3.jpg"></head><body>
        <nav>全部商品 書籍 新品上市 ALL</nav>
        <h1>資料不完整的作品 (4)</h1>
        <p>商品說明仍在整理中</p><p>NT$180</p>
        </body></html>
        """
        record = KadokawaSource()._parse_detail(
            "https://www.kadokawa.com.tw/products/4710000000001",
            markup,
            media_hint="manga",
        )
        self.assertEqual(record.media_type, "manga")
        self.assertIsNone(record.author)
        self.assertIsNone(record.release_date)
        self.assertEqual(record.release_precision, "unknown")

    def test_kadokawa_detail_ignores_later_description_author_marker(self) -> None:
        markup = """
        <html><head><meta property="og:title" content="測試輕小說 (2)"></head><body>
        <nav>全部商品 輕小說 男性向</nav>
        <h1>測試輕小說 (2)</h1>
        <p>作者：第一作者</p><p>上市日期：2026/07/16</p>
        <p>ISBN：9786260000002</p><p>NT$250</p>
        <section>內容簡介 作者：故事中的角色，這不是商品作者欄位。</section>
        </body></html>
        """
        record = KadokawaSource()._parse_detail(
            "https://www.kadokawa.com.tw/products/9786260000002", markup
        )
        self.assertEqual(record.author, "第一作者")
        self.assertEqual(record.release_date.isoformat(), "2026-07-16")

    def test_kadokawa_detail_stops_author_before_shop_template(self) -> None:
        markup = """
        <html><head><meta property="og:title" content="待補日期的輕小說"></head><body>
        <nav>全部商品 輕小說 男性向</nav>
        <h1>待補日期的輕小說</h1>
        <p>作者：測試作者 {{ productService.variationPriceMemberTag(variationSelected) }}</p>
        <p>商品資料整理中</p>
        </body></html>
        """
        record = KadokawaSource()._parse_detail(
            "https://www.kadokawa.com.tw/products/missing-date", markup
        )
        self.assertEqual(record.author, "測試作者")
        self.assertIsNone(record.release_date)


if __name__ == "__main__":
    unittest.main()
