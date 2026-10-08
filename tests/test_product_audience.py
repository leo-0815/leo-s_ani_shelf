import json
import unittest
from unittest.mock import MagicMock, patch

from app.models import BookRecord, book_content_hash, catalog_sync_hash
from app.repository import _sync_write_data
from app.sources.product_audience import parse_bl_category
from app.sources.product_rating import rating_fields
from app import rating_enrichment as enrichment


class ProductAudienceTests(unittest.TestCase):
    def test_chingwin_breadcrumb_only_and_exact_sku(self):
        url = "https://www.ching-win.com.tw/product-detail/X"
        nav = '<nav><a href="/bl">耽美漫畫</a></nav>'
        product = '<p>產品編號:X 級別：普 定價 NT$100</p>'
        self.assertIsNone(parse_bl_category("chingwin", url, nav + product, "X"))
        html = nav + '<div id="breadcrumb"><h2>漫畫</h2><a>耽美漫畫</a></div>' + product
        self.assertEqual(parse_bl_category("chingwin", url, html, "X"), "耽美漫畫")
        self.assertIsNone(parse_bl_category("chingwin", url, html, "Y"))
        self.assertIsNone(parse_bl_category("chingwin", url, html.replace("編號:X", "編號:Y"), "X"))
        fields = rating_fields("chingwin", url, html, "X")
        self.assertEqual((fields["content_rating"], fields["bl_category"]), ("general", "耽美漫畫"))

    def test_spp_category_must_belong_to_valid_product(self):
        url = "https://www.spp.com.tw/SalePage/Index/123"
        data = dict(Id=123, CategoryName="BL小說", ShortDescription="書 號：ABC 等 級：普遍級")
        html = '<script>SalePageIndexViewModel"] = ' + json.dumps(data) + '</script>'
        self.assertEqual(parse_bl_category("spp", url, html, "ABC"), "BL小說")
        self.assertIsNone(parse_bl_category("spp", url, html, "WRONG"))
        self.assertIsNone(parse_bl_category("spp", url.replace("123", "456"), html, "ABC"))

    def test_product_metadata_not_global_or_unrelated_story_text(self):
        url = "https://www.tohan.com.tw/product.php?act=view&id=1"
        self.assertIsNone(parse_bl_category("tohan", url, "<nav>BL漫畫</nav><p>BL小說</p>", "1"))
        self.assertEqual(parse_bl_category("tohan", url,
            '<meta property="og:description" content="大人氣純愛BL漫畫第五集！">', "1"), "BL漫畫")
        self.assertIsNone(parse_bl_category("tohan", url,
            '<meta property="og:description" content="本作並非BL漫畫">', "1"))
        self.assertIsNone(parse_bl_category("tohan", "https://example.com/product.php?id=1",
            '<meta name="keywords" content="BL漫畫">', "1"))

    def test_kadokawa_and_tongli_breadcrumbs_not_generic_site_keywords(self):
        url = "https://www.kadokawa.com.tw/products/9786263786882"
        self.assertEqual(parse_bl_category("kadokawa", url,
            '<nav>BL</nav><div id="ProductList-breadcrumb"><a>BL</a></div>', "9786263786882"), "BL")
        self.assertIsNone(parse_bl_category("kadokawa", url, '<nav>BL</nav>', "9786263786882"))
        self.assertIsNone(parse_bl_category("tongli", "https://www.tongli.com.tw/BooksDetail.aspx?Bd=ABC",
            '<meta name="keywords" content="東立,BL漫畫">', "ABC"))
        self.assertEqual(parse_bl_category("tongli", "https://www.tongli.com.tw/BooksDetail.aspx?Bd=ABC",
            '<div id="breadcrumbs"><a>BL漫畫</a></div>', "ABC"), "BL漫畫")
        self.assertIsNone(parse_bl_category("chingwin", "https://www.ching-win.com.tw/product-detail/X",
            '<meta name="keywords" content="BL漫畫"><p>產品編號:X</p>', "X"))

    def test_sparse_peer_keeps_bl_and_canonical_hash_stays_compatible(self):
        old = BookRecord("chingwin", "X", "Test", "novel", "https://www.ching-win.com.tw/product-detail/X",
                         bl_category="耽夢文庫").prepared()
        new = dict(old, bl_category=None)
        self.assertEqual(_sync_write_data(old, new)["bl_category"], "耽夢文庫")
        self.assertEqual(book_content_hash(old), book_content_hash(new))
        self.assertEqual(catalog_sync_hash(old), catalog_sync_hash(new))

    def test_bl_only_change_commits_and_is_uploadable_even_when_age_unknown(self):
        old = BookRecord("chingwin", "X", "Test", "novel", "https://www.ching-win.com.tw/product-detail/X").prepared()
        old.update(id=1, rating_locked=False)
        cur = MagicMock()
        cur.fetchone.return_value = old
        with patch.object(enrichment, "transaction") as tx, patch.object(enrichment, "_record_catalog_change") as event:
            tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cur
            self.assertTrue(enrichment.save_result(1, "unknown", None, bl_category="BL小說"))
        self.assertIn("bl_category", event.call_args.args[-1])
        self.assertEqual(cur.execute.call_args.args[1][2], "classified")

    def test_bl_reuses_the_same_product_download_as_rating(self):
        row = dict(id=1, publisher_code="chingwin", source_key="X",
                   source_url="https://www.ching-win.com.tw/product-detail/X")
        html = '<div id="breadcrumb">耽美漫畫</div><p>產品編號:X 級別：普 定價 NT$100</p>'
        with patch.object(enrichment, "fetch_html", return_value=html) as fetch, patch.object(enrichment, "save_result", return_value=True) as save:
            self.assertEqual(enrichment.check_book(row)[0], "general")
        fetch.assert_called_once()
        self.assertEqual(save.call_args.kwargs["bl_category"], "耽美漫畫")

    def test_sync_ingestion_and_manifest_compare_include_bl_independently(self):
        from app import catalog_sync as sync
        decode = getattr(sync, "book_record_from_sync", None) or sync.record_from_peer
        sample = dict(publisher_code="chingwin", source_key="X", title="Test", media_type="novel",
                      source_url="https://www.ching-win.com.tw/product-detail/X", bl_category="耽美漫畫")
        self.assertEqual(decode(sample).prepared()["bl_category"], "耽美漫畫")
        compare = getattr(sync, "compare_catalog_manifests", None) or sync.compare_manifests
        left = dict(sample, sync_hash="same", is_bl=True)
        right = dict(sample, sync_hash="same", is_bl=False)
        self.assertEqual(compare([left], [right])["different"], [["chingwin", "X"]])
        self.assertEqual(compare([left], [dict(right, is_bl=True)])["same"], 1)
        self.assertEqual(compare([left], [sample | {"sync_hash": "same"}])["same"], 1)

    def test_old_cloud_rejects_bl_upload_before_any_post(self):
        from app import catalog_sync as sync
        if not hasattr(sync, "CatalogSyncClient"):
            self.skipTest("local sync client only")
        client = sync.CatalogSyncClient(MagicMock(catalog_sync_configured=True))
        client.status = MagicMock(return_value={"protocol_version": 1})
        with patch("app.catalog_sync.urllib.request.urlopen") as request:
            with self.assertRaisesRegex(RuntimeError, "Cloud must be upgraded"):
                client._request("POST", "/api/catalog-sync/books", {"items": [{"bl_category": "BL漫畫"}]})
        request.assert_not_called()

    def test_old_checkpoint_is_rechecked_once_for_bl_even_for_general_or_r18(self):
        import sqlite3
        from contextlib import contextmanager
        from tests.test_release_dates import Connection
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.create_function("UTC_TIMESTAMP", 0, lambda: "2026-10-09")
        db.executescript("""
            CREATE TABLE publishers(id INTEGER, code TEXT);
            INSERT INTO publishers VALUES(1,'chingwin');
            CREATE TABLE books(id INTEGER,publisher_id INTEGER,source_key TEXT,source_url TEXT,
                content_rating TEXT,media_type TEXT,rating_locked INTEGER);
            INSERT INTO books VALUES(1,1,'X','https://www.ching-win.com.tw/product-detail/X','general','manga',0),
                (2,1,'Y','https://www.ching-win.com.tw/product-detail/Y','general','manga',0),
                (3,1,'Z','https://www.ching-win.com.tw/product-detail/Z','restricted_18','manga',0);
            CREATE TABLE book_rating_checks(book_id INTEGER,parser_version TEXT,retry_after TEXT);
            INSERT INTO book_rating_checks VALUES(1,'chingwin_rating_v1',NULL);
        """)
        db.execute("INSERT INTO book_rating_checks VALUES(2,?,NULL)", (enrichment.CHECK_VERSIONS["chingwin"],))
        @contextmanager
        def tx(): yield Connection(db)
        try:
            with patch.object(enrichment, "transaction", tx):
                self.assertEqual([r["id"] for r in enrichment.candidates(10)], [1,3])
        finally:
            db.close()

    def test_chingwin_discount_only_page_reads_explicit_grade_not_generic_adult_modal(self):
        from app.sources.chingwin_rating import parse_product_rating
        url = "https://www.ching-win.com.tw/product-detail/X"
        for label, expected in (("18限", "restricted_18"), ("普", "general")):
            html = '<nav>限制級</nav><p>產品編號:X 級別：' + label + ' ISBN：9781234567890 90折 優惠價 NT$126</p><footer>此為限制級書籍</footer>'
            self.assertEqual(parse_product_rating(url, html, "X")[0], expected)
        self.assertEqual(parse_product_rating(url, '<p>產品編號:X 優惠價 NT$126</p><footer>級別：18限</footer>', "X")[0], "unknown")
