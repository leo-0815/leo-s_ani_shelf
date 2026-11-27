from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.chingwin_local_import import (
    TAIPEI,
    archive_files,
    collect_inbox_files,
    dated_inbox,
    detect_html_kind,
    find_pending_details,
    import_saved_history,
    merge_listing_and_details,
    parse_detail_html,
    parse_listing_html,
    run_interactive,
    write_pending_list,
)

LISTING_HTML = """<!doctype html><html><head><title>漫畫 - 青文出版</title></head><body>
<script type="application/ld+json">
{"@type":"Product","name":"測試漫畫(01)","sku":"CW001","offers":{"price":"140"}}
</script>
<script type="application/ld+json">
{"@type":"Book","name":"測試漫畫(01)","author":{"name":"作者A"},"isbn":"9786260306090","datePublished":"2026-11-20"}
</script>
<script type="application/ld+json">
{"@type":"Product","name":"既有漫畫(02)","sku":"CW002","offers":{"price":"130"}}
</script>
<script type="application/ld+json">
{"@type":"Book","name":"既有漫畫(02)","author":{"name":"作者B"},"isbn":"9780000000002","datePublished":"2026-11-10"}
</script>
Showing 1 - 2 of 2 results
</body></html>"""

DETAIL_HTML = """<!doctype html><html><head>
<meta property="og:title" content="測試漫畫(01) - 青文出版">
<meta property="og:url" content="https://www.ching-win.com.tw/product-detail/CW001">
<meta property="og:image" content="https://www.ching-win.com.tw/upload_files/test.jpg">
</head><body>
<div id="breadcrumb"><a href="/">首頁</a> ＞ <a>漫畫</a> ＞ <a>BL漫畫</a></div>
<div>產品編號：CW001</div>
<div>作家：作者A</div>
<div>ISBN：978-626-030-609-0</div>
<div>出版日期：2026/11/25</div>
<div>級別：18限</div>
<div>定價：NT$ 150</div>
</body></html>"""


class ChingwinLocalImportTest(unittest.TestCase):
    def test_history_imports_all_dated_html_offline_and_retains_invalid_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = date(2020, 11, 27), date(2020, 11, 28)
            for day in (first, second):
                batch = dated_inbox(root, day)
                batch.mkdir(parents=True)
                (batch / "CW001.html").write_text(DETAIL_HTML, encoding="utf-8")
            invalid = dated_inbox(root, second) / "blocked.html"
            invalid.write_text("<title>Just a moment...</title>", encoding="utf-8")
            (root / "undated.html").write_text(DETAIL_HTML, encoding="utf-8")
            checked = []
            def write(records):
                checked.extend(book.release_checked_at for book in records)
                return 0
            with patch("app.chingwin_local_import._require_local_database"), \
                    patch("app.sources.chingwin.fetch_html", side_effect=AssertionError("unexpected publisher access")):
                messages = []
                outcome = import_saved_history(write, messages, inbox=root)
                again = import_saved_history(write, [], inbox=root)
            self.assertEqual(checked, [datetime(2020, 11, 26, 16), datetime(2020, 11, 27, 16)])
            self.assertEqual((outcome["archived"], outcome["file_errors"], again["discovered"]), (2, 1, 0))
            self.assertTrue(invalid.is_file())
            self.assertTrue((root / "undated.html").is_file())
            self.assertTrue((root / "done" / first.isoformat() / "CW001.html").is_file())

    def test_history_leaves_files_pending_if_write_fails(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            batch = dated_inbox(root, date(2020, 11, 28))
            batch.mkdir(parents=True)
            product = batch / "CW001.html"
            product.write_text(DETAIL_HTML, encoding="utf-8")
            with patch("app.chingwin_local_import._require_local_database"):
                result = import_saved_history(lambda records: 1, [], inbox=root)
            self.assertTrue(product.is_file())
            self.assertFalse((root / "done").exists())
            self.assertEqual(result["write_errors"], 1)

    def test_history_crawler_routes_chingwin_to_offline_input(self):
        from app.crawler import _run_job

        with patch("app.crawler._start_job"), patch("app.crawler._finish_job") as finish, \
                patch("app.crawler._update_job_progress"), \
                patch("app.chingwin_local_import.import_saved_history", return_value={
                    "batches": 0, "discovered": 0, "archived": 0,
                    "file_errors": 0, "write_errors": 0, "paused": False,
                }) as offline, \
                patch("app.sources.chingwin.ChingWinSource.collect_batches") as online:
            _run_job(11, "chingwin", mode="backfill")
        offline.assert_called_once()
        online.assert_not_called()
        self.assertEqual(finish.call_args.args[:2], (11, "completed"))

    def test_product_with_book_json_is_still_a_detail(self):
        detail = DETAIL_HTML.replace("</head>", '<script type="application/ld+json">{"@type":"Book"}</script></head>')
        self.assertEqual(detect_html_kind(detail), "detail")
        with self.assertRaisesRegex(ValueError, "不是青文官方"):
            parse_detail_html(Path("CW001.html"), detail.replace("www.ching-win.com.tw", "example.invalid"))

    def test_default_batch_uses_taipei_calendar_day(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("app.chingwin_local_import.datetime", wraps=datetime) as clock, patch("builtins.print"):
                clock.now.return_value = datetime(2026, 11, 28, 0, 1, tzinfo=TAIPEI)
                run_interactive(root)
            clock.now.assert_called_once_with(TAIPEI)
            self.assertTrue((root / "2026" / "11" / "28").is_dir())

    def test_only_reads_files_for_selected_day(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = date(2026, 11, 8)
            selected = dated_inbox(root, day)
            self.assertEqual(selected, root / "2026" / "11" / "08")
            other = dated_inbox(root, date(2026, 11, 9))
            files = [selected / "comic.htm", selected / "list" / "page.html", selected / "detail" / "book.html"]
            for path in [*files, root / "undated.html", other / "later.html", selected / "readme.txt"]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("sample", encoding="utf-8")
            self.assertEqual(set(collect_inbox_files(selected)), set(files))

    def test_pending_report_and_archive_use_input_day(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = date(2020, 11, 28)
            batch = dated_inbox(root, day)
            batch.mkdir(parents=True)
            source = batch / "comic.html"
            source.write_text(LISTING_HTML, encoding="utf-8")
            with patch("app.config.get_settings", return_value=SimpleNamespace(cloud_mode=False, db_host="127.0.0.1")), \
                    patch("app.db.ensure_schema"), patch("app.chingwin_local_import.load_existing_chingwin_status", return_value={}), \
                    patch("app.repository.upsert_book", return_value="inserted"), \
                    patch("app.catalog_sync.CatalogSyncClient", create=True) as client, \
                    patch("builtins.input", side_effect=["YES", ""]), patch("builtins.print"):
                run_interactive(root, day=day)
            client.assert_not_called()
            self.assertIn("CW001\t", (batch / "pending_chingwin_urls.txt").read_text(encoding="utf-8"))
            self.assertTrue((root / "done" / "2020-11-28" / "comic.html").is_file())
            self.assertFalse(source.exists())

    def test_empty_selected_day_does_not_access_db(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = date(2020, 11, 28)
            with patch("app.db.ensure_schema") as schema, \
                    patch("app.chingwin_local_import.load_existing_chingwin_status") as lookup, patch("builtins.print"):
                run_interactive(root, day=day)
            schema.assert_not_called()
            lookup.assert_not_called()
            self.assertTrue(dated_inbox(root, day).is_dir())

    def test_rejects_cloud_database_before_migration_or_import(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = date(2020, 11, 28)
            batch = dated_inbox(root, day)
            batch.mkdir(parents=True)
            (batch / "comic.html").write_text(LISTING_HTML, encoding="utf-8")
            with patch("app.config.get_settings", return_value=SimpleNamespace(cloud_mode=True, db_host="127.0.0.1")), \
                    patch("app.db.ensure_schema") as schema, patch("app.repository.upsert_book") as upsert:
                with self.assertRaisesRegex(ValueError, "只支援本機 MySQL"):
                    run_interactive(root, day=day)
            schema.assert_not_called()
            upsert.assert_not_called()

    def test_listing_parse_and_pending_diff(self):
        self.assertEqual(detect_html_kind(LISTING_HTML), "listing")
        records = parse_listing_html(Path("comic_p1.html"), LISTING_HTML)
        self.assertEqual([r.source_key for r in records], ["CW001", "CW002"])
        existing = {
            "CW002": {"source_key": "CW002", "content_rating": "general", "release_date_source": "product"},
        }
        pending = find_pending_details(records, existing)
        self.assertEqual([r.source_key for r in pending], ["CW001"])

        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "pending_chingwin_urls.txt"
            write_pending_list(pending, out)
            text = out.read_text(encoding="utf-8")
            self.assertIn("CW001\t測試漫畫(01)\thttps://www.ching-win.com.tw/product-detail/CW001", text)

    def test_detail_parse_and_merge_with_listing(self):
        self.assertEqual(detect_html_kind(DETAIL_HTML), "detail")
        listing = parse_listing_html(Path("comic_p1.html"), LISTING_HTML)
        detail = parse_detail_html(Path("CW001.html"), DETAIL_HTML, checked_at=datetime(2026, 11, 28, 1, 0, 0))
        self.assertEqual(detail.isbn, "9786260306090")
        self.assertEqual(detail.list_price, 150)
        self.assertEqual(detail.release_date, date(2026, 11, 25))
        self.assertEqual(detail.content_rating, "restricted_18")
        self.assertEqual(detail.bl_category, "BL漫畫")
        self.assertEqual(detail.cover_url, "https://www.ching-win.com.tw/upload_files/test.jpg")

        merged = {r.source_key: r for r in merge_listing_and_details(listing, [detail])}
        self.assertEqual(merged["CW001"].content_rating, "restricted_18")
        self.assertEqual(merged["CW001"].list_price, 150)
        self.assertEqual(merged["CW001"].release_date, date(2026, 11, 25))

    def test_challenge_rejected_and_archive_moves_files(self):
        with self.assertRaisesRegex(ValueError, "驗證"):
            detect_html_kind("<html><title>Just a moment...</title></html>")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "comic_p1.html"
            f.write_text(LISTING_HTML, encoding="utf-8")
            moved = archive_files([f], root / "done", day=date(2026, 11, 28))
            self.assertFalse(f.exists())
            self.assertTrue(moved[0].exists())
            self.assertEqual(moved[0].parent.name, "2026-11-28")


if __name__ == "__main__":
    unittest.main()
