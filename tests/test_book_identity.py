"""Identity regression tests run actual repository SQL without a cloud DB."""
import sqlite3
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime
from unittest.mock import patch

from app.book_identity import chingwin_source_kind
from app.models import BookRecord, book_content_hash
from app.repository import BOOK_FIELDS, upsert_book
from app.sources.chingwin import ChingWinSource
from tests.test_release_dates import Connection


class IdentitySqlTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.db.row_factory = sqlite3.Row
        types = {"release_date": "DATE", "release_checked_at": "TIMESTAMP", "list_price": "INTEGER",
                 "rating_confidence": "INTEGER", "rating_checked_at": "TIMESTAMP"}
        columns = ", ".join(f"{f} {types.get(f, 'TEXT')}" for f in BOOK_FIELDS)
        self.db.executescript(
            "CREATE TABLE publishers (id INTEGER PRIMARY KEY, code TEXT);"
            "INSERT INTO publishers VALUES (1, 'chingwin');"
            f"CREATE TABLE books (id INTEGER PRIMARY KEY, publisher_id INTEGER, source_key TEXT, {columns}, "
            "rating_locked INTEGER DEFAULT 0, last_seen_at TIMESTAMP, UNIQUE(publisher_id,source_key));"
            "CREATE TABLE series_aliases (publisher_id INTEGER, alias_key TEXT, canonical_key TEXT, approved INTEGER);"
            "CREATE TABLE book_source_aliases (publisher_id INTEGER, alias_key TEXT, book_id INTEGER, "
            "source_kind TEXT, match_method TEXT, PRIMARY KEY(publisher_id,alias_key));"
            "CREATE TABLE catalog_changes (id INTEGER PRIMARY KEY, book_id INTEGER, change_type TEXT, "
            "source_hash TEXT, change_origin TEXT, changed_fields TEXT);"
            "CREATE TABLE release_history (id INTEGER PRIMARY KEY, book_id INTEGER, field_name TEXT, old_value TEXT, new_value TEXT);"
            "CREATE TABLE private_refs (book_id INTEGER, kind TEXT, notes TEXT);"
        )
        @contextmanager
        def transaction():
            try:
                yield Connection(self.db)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        self.patches = [patch("app.repository.transaction", transaction),
                        patch("app.series_follow.add_discovered_book")]
        for p in self.patches:
            p.start()
        self.schedule = BookRecord(
            "chingwin", "schedule-old", "聖母的制裁(02)", "manga",
            "https://www.ching-win.com.tw/about-news-detail/208", author="カズキ、亀島潤斗",
            release_date=date(2030, 10, 1), release_precision="month", release_date_source="schedule")
        self.product = replace(
            self.schedule, source_key="10526202", source_url="https://www.ching-win.com.tw/product-detail/10526202",
            author="カズキ", isbn="9786260302719", cover_url="https://example.test/cover.jpg", list_price=140,
            release_date=date(2030, 11, 5), release_precision="day", release_date_source="product",
            release_checked_at=datetime(2026, 10, 1), content_rating="general", rating_raw="普")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.db.close()

    def rows(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM books ORDER BY id")]

    def assert_consistent_hash(self, row):
        data = {field: row[field] for field in BOOK_FIELDS}
        data.update(publisher_code="chingwin", source_key=row["source_key"])
        self.assertEqual(row["source_hash"], book_content_hash(data))

    def test_schedule_to_product_keeps_id_private_refs_and_all_old_aliases(self):
        upsert_book(self.schedule)
        book_id = self.rows()[0]["id"]
        for kind in ("wishlist", "collection", "release_history", "notification"):
            self.db.execute("INSERT INTO private_refs VALUES (?, ?, 'keep')", (book_id, kind))
        self.db.commit()
        self.assertEqual(upsert_book(self.product), "updated")
        self.assertEqual(len(self.rows()), 1)
        row = self.rows()[0]
        self.assertEqual(row["id"], book_id)
        self.assertEqual(row["source_key"], self.product.source_key)
        self.assertEqual(row["release_date"], self.product.release_date)  # cross-month delay
        self.assertEqual(row["cover_url"], self.product.cover_url)
        self.assertEqual(row["author"], self.schedule.author)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM private_refs WHERE book_id=?", (book_id,)).fetchone()[0], 4)
        self.assertEqual({r[0] for r in self.db.execute("SELECT alias_key FROM book_source_aliases")},
                         {self.schedule.source_key, self.product.source_key})
        self.assert_consistent_hash(row)

    def test_old_schedule_replay_and_old_client_upload_cannot_downgrade_product(self):
        upsert_book(self.schedule)
        upsert_book(self.product)
        for origin in ("crawler", "sync_upload", "cloud_pull"):
            self.assertEqual(upsert_book(self.schedule, change_origin=origin), "unchanged")
        row = self.rows()[0]
        self.assertEqual(row["source_url"], self.product.source_url)
        self.assertEqual(row["source_key"], self.product.source_key)
        self.assertEqual(row["isbn"], self.product.isbn)
        self.assertEqual(row["release_precision"], "day")
        self.assertEqual(row["content_rating"], "general")
        self.assert_consistent_hash(row)

    def test_product_first_then_schedule_enriches_credits_not_source_or_date(self):
        upsert_book(self.product)
        book_id = self.rows()[0]["id"]
        self.assertEqual(upsert_book(self.schedule), "updated")
        row = self.rows()[0]
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(row["id"], book_id)
        self.assertEqual(row["author"], self.schedule.author)
        self.assertEqual(row["source_key"], self.product.source_key)
        self.assertEqual(row["release_date"], self.product.release_date)
        self.assert_consistent_hash(row)

    def test_legacy_product_without_verified_date_is_also_not_replaced_by_month(self):
        legacy = replace(self.product, release_date_source="unknown", release_checked_at=None)
        upsert_book(legacy)
        upsert_book(self.schedule)
        self.assertEqual(self.rows()[0]["release_date"], legacy.release_date)
        self.assertEqual(self.rows()[0]["release_date_source"], "unknown")  # no invented provenance

    def test_product_without_publication_date_can_still_use_schedule_fallback(self):
        upsert_book(replace(self.product, release_date=None, release_precision="unknown",
                            release_date_source="unknown", release_checked_at=None, list_price=None))
        upsert_book(replace(self.schedule, list_price=150))
        row = self.rows()[0]
        self.assertEqual(row["source_url"], self.product.source_url)
        self.assertEqual(row["release_date"], self.schedule.release_date)
        self.assertEqual(row["release_precision"], "month")
        self.assertEqual(row["list_price"], 150)
        revised = replace(self.schedule, release_date=date(2030, 12, 1))
        upsert_book(revised)
        self.assertEqual(self.rows()[0]["release_date"], revised.release_date)

    def test_legacy_hash_already_linked_to_same_product_promotes_to_official_sku(self):
        upsert_book(replace(self.product, source_key="old-hash"))
        book_id = self.rows()[0]["id"]
        upsert_book(self.product)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["id"], book_id)
        self.assertEqual(self.rows()[0]["source_key"], self.product.source_key)
        self.assert_consistent_hash(self.rows()[0])

    def test_monthly_republication_and_changed_author_hash_reuse_same_id(self):
        upsert_book(self.schedule)
        revised = replace(self.schedule, source_key="schedule-next", author="カズキ 、 亀島潤斗",
                          source_url="https://www.ching-win.com.tw/about-news-detail/209",
                          release_date=date(2030, 12, 1))
        upsert_book(revised)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["source_key"], self.schedule.source_key)
        self.assertEqual(self.rows()[0]["release_date"], revised.release_date)
        self.assert_consistent_hash(self.rows()[0])
        upsert_book(self.product)
        upsert_book(revised)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM book_source_aliases").fetchone()[0], 3)

    def test_media_prefix_parentheses_spacing_and_zero_padded_volume(self):
        upsert_book(replace(self.schedule, title="小說 測試故事（02）", media_type="novel"))
        upsert_book(replace(self.product, title="測試故事 (2)", media_type="novel"))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["title"], "測試故事 (2)")

    def test_different_volume_media_edition_isbn_or_author_do_not_match(self):
        cases = [replace(self.product, title="聖母的制裁(03)"),
                 replace(self.product, media_type="novel"),
                 replace(self.product, edition_type="special", title="聖母的制裁(02)特裝版"),
                 replace(self.product, edition_type="digital"),
                 replace(self.product, author="完全不同作者")]
        for product in cases:
            with self.subTest(product=product):
                self.db.execute("DELETE FROM books")
                self.db.execute("DELETE FROM book_source_aliases")
                self.db.commit()
                upsert_book(self.schedule)
                upsert_book(product)
                self.assertEqual(len(self.rows()), 2)
        self.db.execute("DELETE FROM books")
        self.db.execute("DELETE FROM book_source_aliases")
        self.db.commit()
        upsert_book(replace(self.schedule, isbn="9786264496933"))
        upsert_book(self.product)
        self.assertEqual(len(self.rows()), 2)

    def test_missing_author_needs_matching_valid_isbn(self):
        upsert_book(replace(self.schedule, author=None))
        upsert_book(self.product)
        self.assertEqual(len(self.rows()), 2)

    def test_isbn_can_match_schedule_with_corrected_title_and_no_author(self):
        upsert_book(replace(self.schedule, title="舊標題(02)", author=None, isbn=self.product.isbn))
        upsert_book(self.product)
        self.assertEqual(len(self.rows()), 1)

    def test_different_product_skus_are_not_merged_even_with_same_isbn(self):
        upsert_book(self.product)
        upsert_book(replace(self.product, source_key="10526202-new",
                           source_url="https://www.ching-win.com.tw/product-detail/10526202-new"))
        self.assertEqual(len(self.rows()), 2)

    def test_ambiguous_candidates_stay_separate_not_arbitrarily_selected(self):
        upsert_book(self.schedule)
        # Simulate an existing historical duplicate; PR47 will repair it.
        self.db.execute("UPDATE books SET source_url='https://example.test/unknown'")
        self.db.commit()
        upsert_book(replace(self.schedule, source_key="other-schedule"))
        self.db.execute("UPDATE books SET source_url=? WHERE source_key=?",
                        (self.schedule.source_url, self.schedule.source_key))
        self.db.commit()
        upsert_book(self.product)
        self.assertEqual(len(self.rows()), 3)

    def test_existing_dual_keys_are_not_batch_merged(self):
        upsert_book(replace(self.schedule, author="other author"))
        upsert_book(self.product)
        upsert_book(self.product)
        upsert_book(replace(self.schedule, author="カズキ"))
        self.assertEqual(len(self.rows()), 2)

    def test_alias_structural_mismatch_rolls_back(self):
        upsert_book(self.schedule)
        upsert_book(self.product)
        before = self.rows()
        with self.assertRaises(ValueError):
            upsert_book(replace(self.schedule, media_type="novel"), change_origin="sync_upload")
        self.assertEqual(self.rows(), before)

    def test_known_alias_or_key_cannot_retarget_another_official_product(self):
        upsert_book(self.schedule)
        upsert_book(self.product)
        before = self.rows()
        for key in (self.schedule.source_key, self.product.source_key):
            with self.subTest(key=key), self.assertRaises(ValueError):
                upsert_book(replace(self.product, source_key=key,
                                    source_url="https://www.ching-win.com.tw/product-detail/other-sku"),
                            change_origin="sync_upload")
            self.assertEqual(self.rows(), before)

    def test_old_alias_with_non_product_url_does_not_erase_product_link(self):
        upsert_book(self.schedule)
        upsert_book(self.product)
        upsert_book(replace(self.schedule, source_url="https://docs.google.com/spreadsheets/old"),
                    change_origin="sync_upload")
        self.assertEqual(self.rows()[0]["source_url"], self.product.source_url)
        self.assertEqual(self.rows()[0]["release_date"], self.product.release_date)

    def test_alias_conflict_rolls_back_metadata_and_change_feed(self):
        upsert_book(self.schedule)
        upsert_book(self.product)
        self.db.execute("UPDATE book_source_aliases SET book_id=999 WHERE alias_key=?", (self.product.source_key,))
        self.db.commit()
        before = self.rows()
        events = self.db.execute("SELECT COUNT(*) FROM catalog_changes").fetchone()[0]
        with self.assertRaises(ValueError):
            upsert_book(replace(self.product, list_price=200))
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM catalog_changes").fetchone()[0], events)

    def test_sparse_refresh_preserves_cover_isbn_ratings_and_manual_lock(self):
        upsert_book(replace(self.product, bl_category="BL", content_rating="restricted_18", rating_raw="限制級"))
        self.db.execute("UPDATE books SET rating_locked=1")
        self.db.commit()
        upsert_book(replace(self.product, cover_url=None, isbn=None, content_rating="general", rating_raw="普"))
        row = self.rows()[0]
        self.assertEqual(row["cover_url"], self.product.cover_url)
        self.assertEqual(row["isbn"], self.product.isbn)
        self.assertEqual(row["content_rating"], "restricted_18")
        self.assertEqual(row["bl_category"], "BL")


class SourceRefreshTests(unittest.TestCase):
    def test_official_source_kind_does_not_accept_lookalike_hosts(self):
        for url in ("https://www.ching-win.com.tw.evil.test/product-detail/1",
                    "https://example.test/about-news-detail/208", "https://www.ching-win.com.tw/products/"):
            self.assertEqual(chingwin_source_kind({"source_url": url}), "other")

    def collect(self, backfill):
        source = ChingWinSource()
        source.category_urls = (("comic", "https://example.test/comic", "manga"),)
        known = BookRecord("chingwin", "known", "已收錄(1)", "manga", "https://www.ching-win.com.tw/product-detail/known")
        new = replace(known, source_key="new", title="新書(1)")
        with patch("app.repository.get_backfill_progress", return_value={}), \
                patch.object(source, "_collect_schedule", return_value=[]), \
                patch("app.sources.chingwin.fetch_html", return_value="html"), \
                patch("app.sources.chingwin.polite_pause"), \
                patch.object(source, "_parse_catalog_page", return_value=([known, new], 2)), \
                patch.object(source, "_with_product_rating", side_effect=lambda r: r) as detail:
            batches = list(source.collect_batches(known_keys={"known"}, backfill=backfill))
        return batches, detail

    def test_daily_refresh_known_skus_without_extra_detail_fetch_or_duplicate_pages(self):
        batches, detail = self.collect(False)
        self.assertEqual([r.source_key for batch, _ in batches for r in batch], ["known", "new"])
        self.assertEqual([call.args[0].source_key for call in detail.call_args_list], ["new"])

    def test_historical_backfill_still_skips_known_products(self):
        batches, detail = self.collect(True)
        self.assertEqual([r.source_key for batch, _ in batches for r in batch], ["new"])
        self.assertEqual(detail.call_count, 1)


if __name__ == "__main__":
    unittest.main()
