from __future__ import annotations
import json
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from dataclasses import replace
from unittest.mock import patch
from app.models import BookRecord, catalog_sync_hash
from app.repository import BOOK_FIELDS, upsert_book
from app.release_dates import (merge_release_fields, parse_product, product_url_valid,
                               candidates, claim_attempt, refresh_source)
from app.sources.kadokawa import KadokawaSource
from app.sources.spp import SppSource
from app.sources.chingwin import ChingWinSource
from app.sources.tongli import TongLiSource
from app.sources.tohan import TohanSource

class Cursor:
    def __init__(self, db): self.cur = db.cursor()
    def __enter__(self): return self
    def __exit__(self, *args): self.cur.close()
    def execute(self, sql, args=()):
        sql = sql.replace("%s", "?").replace(" FOR UPDATE", "").replace("INSERT IGNORE", "INSERT OR IGNORE")
        self.cur.execute(sql, args)
    def fetchone(self):
        row = self.cur.fetchone()
        return dict(row) if row else None
    def fetchall(self): return [dict(r) for r in self.cur.fetchall()]
    @property
    def rowcount(self): return self.cur.rowcount
    @property
    def lastrowid(self): return self.cur.lastrowid

class Connection:
    def __init__(self, db): self.db = db
    def cursor(self): return Cursor(self.db)

class ReleaseSqlTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.db.row_factory = sqlite3.Row
        types = {"release_date":"DATE", "release_checked_at":"TIMESTAMP", "list_price":"INTEGER",
                 "rating_confidence":"INTEGER"}
        cols = ", ".join(f"{f} {types.get(f, 'TEXT')}" for f in BOOK_FIELDS)
        self.db.executescript(
            "CREATE TABLE publishers (id INTEGER PRIMARY KEY, code TEXT);"
            "INSERT INTO publishers VALUES (1, 'kadokawa');"
            f"CREATE TABLE books (id INTEGER PRIMARY KEY, publisher_id INTEGER, source_key TEXT, {cols}, "
            "rating_locked INTEGER DEFAULT 0, last_seen_at TIMESTAMP, UNIQUE(publisher_id,source_key));"
            "CREATE TABLE series_aliases (publisher_id INTEGER, alias_key TEXT, canonical_key TEXT, approved INTEGER);"
            "CREATE TABLE catalog_changes (id INTEGER PRIMARY KEY, book_id INTEGER, change_type TEXT, "
            "source_hash TEXT, change_origin TEXT, changed_fields TEXT);"
            "CREATE TABLE release_history (id INTEGER PRIMARY KEY, book_id INTEGER, field_name TEXT, old_value TEXT, new_value TEXT);"
            "CREATE TABLE book_release_checks (book_id INTEGER PRIMARY KEY, attempted_at TIMESTAMP, next_check_at TIMESTAMP, last_error TEXT);"
        )
        @contextmanager
        def transaction():
            try:
                yield Connection(self.db)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        self.patches = [
            patch("app.repository.transaction", transaction),
            patch("app.release_dates.transaction", transaction),
            patch("app.series_follow.add_discovered_book"),
        ]
        for p in self.patches: p.start()
        self.base = BookRecord("kadokawa", "one", "測試小說 (1)", "novel",
                               "https://www.kadokawa.com.tw/products/one",
                               release_date=date(2030,10,5), release_precision="day")
        upsert_book(self.base)
    def test_summary_executes_real_aggregate_sql_and_normalizes_zero_counts(self):
        from app.release_dates import revalidation_summary
        self.db.execute("ALTER TABLE publishers ADD COLUMN name TEXT")
        self.db.execute("UPDATE publishers SET name='角川'")
        result = revalidation_summary(datetime(2026,10,7,16))
        self.assertEqual(result['date'], '2026-10-08')
        self.assertEqual(result['items'][0]['attempted_today'], 0)
        self.assertEqual(result['items'][0]['daily_limit'], 20)

    def test_summary_classifies_missing_dates_and_errors_without_exposing_errors(self):
        from app.release_dates import revalidation_summary, record_attempt
        self.db.execute("ALTER TABLE publishers ADD COLUMN name TEXT")
        now = datetime(2026,10,8,1)
        self.assertTrue(claim_attempt('kadokawa', self.one()['id'], now, 20))
        record_attempt(self.one()['id'], now, now+timedelta(days=1), 'Product page has no explicit publication date')
        summary = revalidation_summary(now)['items'][0]
        self.assertEqual(summary['attempted_today'], 1)
        self.assertEqual(summary['undated_today'], 1)
        self.assertEqual(summary['failed_today'], 0)
        record_attempt(self.one()['id'], now, now+timedelta(days=1), 'private internal exception')
        summary = revalidation_summary(now)['items'][0]
        self.assertEqual(summary['failed_today'], 1)
        self.assertNotIn('private', str(summary))

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.db.close()
    def one(self):
        return dict(self.db.execute("SELECT * FROM books WHERE source_key='one'").fetchone())
    def dates(self):
        return self.db.execute("SELECT COUNT(*) FROM release_history WHERE field_name='release_date'").fetchone()[0]
    def test_postponement_then_same_date_has_one_history_event(self):
        checked = replace(self.base, release_date=date(2030,10,12), release_date_source="product",
                          release_checked_at=datetime(2026,10,7))
        self.assertEqual(upsert_book(checked), "updated")
        self.assertEqual(self.one()["release_date"], date(2030,10,12))
        self.assertEqual(upsert_book(replace(checked, release_checked_at=datetime(2026,10,8))), "unchanged")
        self.assertEqual(self.dates(), 1)
        self.assertEqual(self.one()["release_checked_at"], datetime(2026,10,8))
    def test_old_schedule_and_old_local_upload_cannot_undo_verified_date(self):
        checked = replace(self.base, release_date=date(2030,10,12), release_date_source="product",
                          release_checked_at=datetime(2026,10,7))
        upsert_book(checked)
        for origin in ("crawler", "sync_upload", "cloud_pull"):
            upsert_book(self.base, change_origin=origin)
        self.assertEqual(self.one()["release_date"], date(2030,10,12))
        self.assertEqual(self.dates(), 1)
        # A newer SKU-specific observation is allowed to bring publication forward.
        upsert_book(replace(checked, release_date=date(2030,10,3),
                            release_checked_at=datetime(2026,10,8)), change_origin="sync_upload")
        self.assertEqual(self.one()["release_date"], date(2030,10,3))
        self.assertEqual(self.dates(), 2)
    def test_older_verified_peer_is_rejected(self):
        checked = replace(self.base, release_date=date(2030,10,12), release_date_source="product",
                          release_checked_at=datetime(2026,10,7))
        upsert_book(checked)
        upsert_book(replace(checked, release_date=date(2030,10,5),
                           release_checked_at=datetime(2026,10,6)), change_origin="cloud_pull")
        self.assertEqual(self.one()["release_date"], date(2030,10,12))
    def test_editions_are_not_updated_as_a_series(self):
        other = replace(self.base, source_key="two", title="測試小說 (1)(特裝版)",
                        source_url="https://www.kadokawa.com.tw/products/two", edition_type="special")
        upsert_book(other)
        upsert_book(replace(self.base, release_date=date(2030,10,12),
                           release_date_source="product", release_checked_at=datetime(2026,10,7)))
        self.assertEqual(self.db.execute("SELECT release_date FROM books WHERE source_key='two'").fetchone()[0], date(2030,10,5))
    def test_overdue_scheduled_survives_recent_window_and_daily_claim_persists(self):
        now = datetime(2030,12,1)
        rows = candidates("kadokawa", 20, now)
        self.assertEqual([r["source_key"] for r in rows], ["one"])
        self.assertTrue(claim_attempt("kadokawa", rows[0]["id"], now, 1))
        self.assertFalse(claim_attempt("kadokawa", rows[0]["id"], now, 1))
        self.assertEqual(candidates("kadokawa", 20, now), [])
        self.assertEqual(len(candidates("kadokawa", 20, now + timedelta(days=1))), 1)
    def test_fetch_failure_preserves_date_and_persists_retry(self):
        row = self.one()
        with patch("app.release_dates.candidates", return_value=[row]), \
             patch("app.sources.common.fetch_html", side_effect=RuntimeError("HTTP 522")):
            result = refresh_source(KadokawaSource())
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.one()["release_date"], self.base.release_date)
        checkpoint = dict(self.db.execute("SELECT * FROM book_release_checks").fetchone())
        self.assertIn("HTTP 522", checkpoint["last_error"])
        self.assertGreater(checkpoint["next_check_at"], checkpoint["attempted_at"])
        self.assertEqual(self.dates(), 0)
    def test_daily_budget_is_shared_across_books(self):
        upsert_book(replace(self.base, source_key="two", source_url="https://www.kadokawa.com.tw/products/two"))
        now = datetime(2030,10,6)
        ids = [r["id"] for r in candidates("kadokawa", 20, now)]
        self.assertTrue(claim_attempt("kadokawa", ids[0], now, 1))
        self.assertFalse(claim_attempt("kadokawa", ids[1], now, 1))
        self.assertTrue(claim_attempt("kadokawa", ids[1], now + timedelta(days=1), 1))
    def test_missing_date_retains_fallback_without_source_error(self):
        row = self.one()
        markup = '<meta property="og:title" content="測試小說 (1)"><p>作者：作者</p>'
        with patch("app.release_dates.candidates", return_value=[row]), patch("app.sources.common.fetch_html", return_value=markup):
            result = refresh_source(KadokawaSource())
        self.assertEqual(result["unconfirmed"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.one()["release_date"], self.base.release_date)
        self.assertEqual(self.dates(), 0)

class ReleaseParserTests(unittest.TestCase):
    def row(self, code, key, url):
        return dict(publisher_code=code, source_key=key, source_url=url, media_type="novel",
                    title="測試小說 (1)", isbn=None)
    def test_kadokawa_explicit_sku_date(self):
        row = self.row("kadokawa","one","https://www.kadokawa.com.tw/products/one")
        markup = '<meta property="og:title" content="測試小說 (1)"><p>測試小說 (1) 作者：作者 上市日期：2030/10/12 ISBN：9781234567890 NT$250</p>'
        record = parse_product(KadokawaSource(), row, markup)
        self.assertEqual(record.release_date, date(2030,10,12))
        self.assertEqual(record.release_date_source, "product")
    def test_chingwin_scoped_product_specs_and_missing_date(self):
        row = self.row("chingwin","A1","https://www.ching-win.com.tw/product-detail/A1")
        markup = '<p>產品編號：A1 出版日期：2030-10-12 級別：普 定價：200</p>'
        record = parse_product(ChingWinSource(), row, markup)
        self.assertEqual(record.release_date, date(2030,10,12))
        with self.assertRaises(ValueError):
            parse_product(ChingWinSource(), row, '<p>相關商品 出版日期：2030-10-19</p>')
    def test_spp_publication_not_preorder_and_no_selling_time_fallback(self):
        row = self.row("spp","A1","https://www.spp.com.tw/SalePage/Index/123")
        data = dict(Id=123, Title="測試小說", CategoryLevelName={"Level1_ShopCategory_Name":"輕小說"},
                    SellingStartDateTime="2030-09-01T00:00:00",
                    ShortDescription="書 號：A1 上市日：2030/10/12")
        markup = 'SalePageIndexViewModel"] = ' + json.dumps(data)
        self.assertEqual(parse_product(SppSource(), row, markup).release_date, date(2030,10,12))
        data["ShortDescription"] = "書 號：A1"
        with self.assertRaises(ValueError):
            parse_product(SppSource(), row, 'SalePageIndexViewModel"] = ' + json.dumps(data))
    def test_spp_mismatched_product_cannot_change_book(self):
        row = self.row("spp","A1","https://www.spp.com.tw/SalePage/Index/123")
        data = dict(Id=123, Title="other", CategoryLevelName={"Level1_ShopCategory_Name":"漫畫"},
                    ShortDescription="書 號：A2 上市日：2030/10/12")
        with self.assertRaisesRegex(ValueError, "identity"):
            parse_product(SppSource(), row, 'SalePageIndexViewModel"] = ' + json.dumps(data))
    def test_product_hosts_are_allowlisted(self):
        self.assertFalse(product_url_valid("kadokawa", "https://evil.test/products/one", "one"))
        self.assertFalse(product_url_valid("kadokawa", "https://www.kadokawa.com.tw/products/two", "one"))
        self.assertFalse(product_url_valid("spp", "https://www.spp.com.tw/SalePageCategory/1", "one"))
    def test_sync_hash_v1_ignores_operational_timestamp(self):
        a = BookRecord("spp","A","title","manga","https://www.spp.com.tw/SalePage/Index/1",
                       release_date=date(2030,10,12), release_date_source="product",
                       release_checked_at=datetime(2026,10,7))
        b = replace(a, release_checked_at=datetime(2026,10,8))
        self.assertEqual(a.prepared()["source_hash"], b.prepared()["source_hash"])
        self.assertEqual(catalog_sync_hash(a.prepared()), catalog_sync_hash(b.prepared()))

    def test_tongli_and_tohan_publication_fields(self):
        row = self.row("tongli", "A1", "https://www.tongli.com.tw/BooksDetail.aspx?Bd=A1")
        markup = '<h2>小說書籍資料</h2><h3>測試小說</h3><p>原文書名：</p><p>Test</p><p>出版日期：2030/10/12</p><h2>內容簡介</h2>'
        self.assertEqual(parse_product(TongLiSource(), row, markup).release_date, date(2030,10,12))
        row = self.row("tohan", "12", "https://www.tohan.com.tw/product.php?act=view&id=12")
        markup = '<title>測試小說</title><p>作者</p><p>某作者</p><p>ISBN</p><p>9781234567890</p><p>出版日期</p><p>2030-10-12</p>'
        self.assertEqual(parse_product(TohanSource(), row, markup).release_date, date(2030,10,12))
    def test_kadokawa_whitespace_fallback_requires_one_explicit_date(self):
        row = self.row("kadokawa", "one", "https://www.kadokawa.com.tw/products/one")
        markup = '<meta property="og:title" content="測試　小說 (1)【10月中旬出貨】"><p>測試小說(1) 上市日期：2030/10/12</p>'
        self.assertEqual(parse_product(KadokawaSource(), row, markup).release_date, date(2030,10,12))
    def test_isbn_conflict_is_not_silently_merged(self):
        row = self.row("kadokawa", "one", "https://www.kadokawa.com.tw/products/one")
        row["isbn"] = "9780000000001"
        markup = '<meta property="og:title" content="測試小說"><p>測試小說 作者：作者 上市日期：2030/10/12 ISBN：9781234567890 NT$250</p>'
        with self.assertRaisesRegex(ValueError, "ISBN"):
            parse_product(KadokawaSource(), row, markup)
    def test_peer_release_provenance_roundtrip(self):
        import app.catalog_sync as sync
        parse = getattr(sync, "book_record_from_sync", None) or sync.record_from_peer
        payload = {"publisher_code":"kadokawa", "source_key":"one", "title":"測試小說",
                   "media_type":"novel", "source_url":"https://www.kadokawa.com.tw/products/one",
                   "release_date":"2030-10-12", "release_date_source":"product",
                   "release_checked_at":"2020-10-07T12:00:00Z"}
        parsed = parse(payload)
        self.assertEqual(parsed.release_checked_at, datetime(2020,10,7,12))
        self.assertEqual(parsed.release_date_source, "product")
        del payload["release_checked_at"]
        with self.assertRaises(ValueError):
            parse(payload)

    def test_chingwin_revisits_known_cursor_announcement(self):
        from types import SimpleNamespace
        url = "https://www.ching-win.com.tw/about-news-detail/123"
        row = BookRecord("chingwin","X","測試小說","novel",url, release_date=date(2030,10,1), release_precision="month")
        source = ChingWinSource()
        with patch("app.sources.chingwin.fetch_html", return_value="html"), \
             patch("app.sources.chingwin.parse_page", return_value=SimpleNamespace(links=[(url,"青文出版社 預定出書表")])), \
             patch.object(source, "_parse_article", return_value=[row]), \
             patch("app.sources.chingwin.polite_pause"):
            self.assertEqual(source._collect_schedule({"X"}, url), [row])

if __name__ == "__main__":
    unittest.main()

