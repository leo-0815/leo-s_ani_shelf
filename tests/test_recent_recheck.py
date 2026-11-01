from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app import recent_recheck as recent
from app.models import BookRecord
from app.sources.chingwin import ChingWinSource
from app.sources.kadokawa import KadokawaSource
from app.sources.tongli import TongLiSource
from app.sources.tohan import TohanSource
from app.sources.spp import SppSource
from tests.test_release_dates import Connection


class RecentQueueTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE catalog_recent_scans(publisher_code TEXT PRIMARY KEY,
                last_attempt_at TIMESTAMP,last_success_at TIMESTAMP,lease_until TIMESTAMP,
                last_error TEXT,pages_scanned INTEGER DEFAULT 0,queued_count INTEGER DEFAULT 0,
                budget_day DATE,attempted_today INTEGER DEFAULT 0);
            CREATE TABLE catalog_product_retries(publisher_code TEXT,url_hash TEXT,source_url TEXT,
                media_hint TEXT,seed_json TEXT,status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,
                first_seen_at TIMESTAMP,last_attempt_at TIMESTAMP,next_attempt_at TIMESTAMP,last_error TEXT,
                PRIMARY KEY(publisher_code,url_hash));
        """)
        @contextmanager
        def tx():
            try:
                yield Connection(self.db)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        p = patch.object(recent, "transaction", tx)
        p.start(); self.addCleanup(p.stop)
        self.now = datetime.utcnow().replace(microsecond=0)

    def put(self, key="one", **kwargs):
        return recent.enqueue("kadokawa", [(f"https://www.kadokawa.com.tw/products/{key}", "novel", None)], now=self.now, **kwargs)

    def row(self):
        return dict(self.db.execute("SELECT * FROM catalog_product_retries LIMIT 1").fetchone())

    def test_dedup_normalize_and_ignore_already_known(self):
        url = "https://www.kadokawa.com.tw/products/one"
        self.assertEqual(self.put(known={url}), 0)
        self.assertEqual(self.put(), 1)
        self.assertEqual(recent.enqueue("kadokawa", [(url + "?tracking=1", None, None)], now=self.now), 0)
        self.assertEqual(self.row()["attempts"], 0)

    def test_failure_is_durable_and_does_not_require_listing_to_reappear(self):
        self.put(error="timeout")
        self.assertIsNone(recent.claim_product("kadokawa", self.now))
        row = recent.claim_product("kadokawa", self.now + timedelta(days=1))
        self.assertIsNotNone(row)
        self.assertEqual(row["last_error"], "timeout")
        recent.finish_product(row, error="HTTP 522")
        self.assertEqual(self.row()["status"], "pending")
        self.assertGreater(self.row()["next_attempt_at"], self.now)
        retry_at = self.row()["next_attempt_at"]
        self.put(error="daily timeout again")
        self.assertEqual(self.row()["next_attempt_at"], retry_at)

    def test_daily_budget_survives_repeated_runs_and_resets_taipei_midnight(self):
        now = datetime(2030, 1, 1, 15, 59)
        for n in range(8):
            recent.enqueue("kadokawa", [(f"https://www.kadokawa.com.tw/products/{n}", None, None)], now=now)
        for _ in range(recent.DAILY_PRODUCTS):
            row = recent.claim_product("kadokawa", now)
            self.assertIsNotNone(row)
            recent.finish_product(row)
        self.assertIsNone(recent.claim_product("kadokawa", now))
        self.assertIsNotNone(recent.claim_product("kadokawa", now + timedelta(minutes=2)))

    def test_claim_is_a_lease_and_crash_can_resume(self):
        self.put()
        self.assertIsNotNone(recent.claim_product("kadokawa", self.now))
        self.assertIsNone(recent.claim_product("kadokawa", self.now))
        row = recent.claim_product("kadokawa", self.now + timedelta(hours=2))
        self.assertEqual(row["attempts"], 2)

    def test_four_day_success_interval_uses_dates_not_hour_jitter(self):
        now = datetime(2030, 1, 1, 6)
        self.assertTrue(recent.claim_scan("kadokawa", now))
        self.assertFalse(recent.claim_scan("kadokawa", now))
        recent.finish_scan("kadokawa", now, 30, 4, None)
        self.assertFalse(recent.claim_scan("kadokawa", now + timedelta(days=3)))
        self.assertTrue(recent.claim_scan("kadokawa", now + timedelta(days=4, hours=-1)))
        recent.finish_scan("kadokawa", now + timedelta(days=4), 2, 2, "network error")
        self.assertTrue(recent.claim_scan("kadokawa", now + timedelta(days=5)))

    def test_ack_only_after_actual_product_fetch_and_successful_save(self):
        self.put()
        url = self.row()["source_url"]
        source = SimpleNamespace(code="kadokawa")
        book = BookRecord("kadokawa", "one", "小說", "novel", url)
        recent.acknowledge_saved(source, book)
        self.assertEqual(self.row()["status"], "pending")
        source.product_fetch_successes = {url}
        recent.acknowledge_saved(source, book)
        self.assertEqual(self.row()["status"], "done")
        self.put(error="changed page failed")
        self.assertEqual(self.row()["status"], "pending")

    def test_backlog_warning_age_and_errors_are_separate(self):
        self.put(error="timeout")
        info = recent.backlog("kadokawa", self.now + timedelta(days=8))
        self.assertEqual(info, {"pending": 1, "failed": 1, "oldest_days": 8})


class RecentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.urls = []
        for p in (patch.object(recent, "known_urls", return_value=set()),
                  patch.object(recent, "polite_pause"),
                  patch.object(recent, "enqueue", side_effect=lambda code, rows, **kw: self.rows.extend(rows) or len(rows))):
            p.start(); self.addCleanup(p.stop)

    def scan(self, source, markup):
        def fetch(url, **kwargs):
            self.urls.append(url)
            return markup(url) if callable(markup) else markup
        with patch.object(recent, "fetch_html", side_effect=fetch):
            return recent.scan_recent(source, deadline=float("inf"))

    def test_chingwin_fifteen_pages_per_category_restarts_at_one(self):
        source = ChingWinSource()
        row = BookRecord("chingwin", "A1", "漫畫", "manga", "https://www.ching-win.com.tw/product-detail/A1")
        with patch.object(source, "_parse_catalog_page", return_value=([row], 100)):
            result = self.scan(source, "listing")
            self.assertEqual(result["pages"], 30)
            self.assertTrue(self.urls[0].endswith("?page=1"))
            self.assertTrue(self.urls[-1].endswith("?page=15"))
            self.urls.clear();self.scan(source, "listing")
            self.assertTrue(self.urls[0].endswith("?page=1"))
        self.assertIn('"title": "漫畫"', json.dumps(self.rows[0][2], ensure_ascii=False))

    def test_kadokawa_scan_beyond_three_and_no_upcoming_or_page_sixteen(self):
        source = KadokawaSource()
        markup = '<a href="/products/one">加入購物車</a><a href="?page=100">100</a>'
        with patch.object(source, "page_size", 1):
            result = self.scan(source, markup)
        self.assertEqual(result["pages"], 30)
        self.assertTrue(any("page=4" in url for url in self.urls))
        self.assertFalse(any("page=16" in url or "即將" in url for url in self.urls))

    def test_tongli_both_lists_bounded_fifteen_pages(self):
        result = self.scan(TongLiSource(), '<a href="/BooksDetail.aspx?Bd=A1">書</a>')
        self.assertEqual(result["pages"], 30)
        self.assertIn("novel", [row[1] for row in self.rows])
        self.assertTrue(any("page=15" in url for url in self.urls))

    def test_tohan_follows_official_act_list_links_not_catalog_or_other_hosts(self):
        markup = '<a href="/product.php?act=view&id=A1">書</a>' + ''.join(
            f'<a href="/product.php?act=list&cid=107&page={i}">{i}</a>' for i in range(1, 18)) + \
            '<a href="https://evil.test/product.php?cid=107&page=2">2</a>'
        result = self.scan(TohanSource(), markup)
        self.assertEqual(result["pages"], 15)
        self.assertEqual(len(set(self.urls)), 15)
        self.assertFalse(any("page=16" in u or "evil" in u or "cid=1&" in u for u in self.urls))

    def test_spp_latest_two_hundred_urls_no_detail_fetch(self):
        source = SppSource()
        with patch.object(source, "_recent_catalog_urls", return_value=["https://www.spp.com.tw/SalePage/Index/123"]) as listing:
            self.scan(source, "unused")
        listing.assert_called_once_with(200)
        self.assertEqual(len(self.urls), 0)

    def test_partial_fetch_keeps_queued_work_and_time_limit_never_marks_completed(self):
        source = TongLiSource()
        def markup(url):
            if "page=2" in url:
                raise RuntimeError("HTTP 522")
            return '<a href="/BooksDetail.aspx?Bd=A1">書</a>'
        with self.assertRaisesRegex(RuntimeError, "522"):
            self.scan(source, markup)
        self.assertTrue(self.rows)
        with self.assertRaises(TimeoutError):
            recent.scan_recent(source, deadline=0)

    def test_empty_first_page_not_silently_successful(self):
        for source in (KadokawaSource(), TongLiSource(), TohanSource()):
            with self.subTest(source=source.code), self.assertRaises(ValueError):
                self.scan(source, "<p>site changed</p>")


class RecentSafetyTests(unittest.TestCase):
    def test_rejects_non_product_external_credentials_ports_and_duplicate_forms(self):
        for url in ("https://evil.test/products/one", "https://www.kadokawa.com.tw:443/products/one",
                    "https://u:p@www.kadokawa.com.tw/products/one", "https://www.kadokawa.com.tw/categories/one"):
            self.assertIsNone(recent.canonical_product("kadokawa", url))
        self.assertEqual(recent.canonical_product("spp", "http://www.spp.com.tw/v2/official/SalePage/Index/123/"),
                         "https://www.spp.com.tw/SalePage/Index/123")

    def test_chingwin_keeps_seed_on_missing_explicit_product_date_and_parses_r18(self):
        seed = BookRecord("chingwin", "A1", "測試漫畫", "manga", "https://www.ching-win.com.tw/product-detail/A1", release_date=date(2030, 1, 1))
        row = {"source_url": seed.source_url, "seed_json": json.dumps(asdict(seed), default=str)}
        with patch.object(recent, "fetch_html", return_value='<p>產品編號：A1 級別：限 定價：200</p>'), patch.object(recent, "polite_pause"):
            record = recent.fetch_product(ChingWinSource(), row)
        self.assertEqual(record.title, seed.title)
        self.assertEqual(record.release_date, seed.release_date)
        self.assertEqual(record.content_rating, "restricted_18")
        with patch.object(recent, "fetch_html", return_value='<p>產品編號：OTHER 級別：普 定價：200</p>'), patch.object(recent, "polite_pause"):
            with self.assertRaisesRegex(ValueError, "identity"):
                recent.fetch_product(ChingWinSource(), row)

    def test_normal_daily_fetch_failures_call_durable_hook(self):
        for source in (KadokawaSource(), SppSource(), ChingWinSource()):
            failures = []
            source.record_product_failure = lambda *args: failures.append(args)
            with self.subTest(code=source.code):
                if source.code == "kadokawa":
                    with patch.object(source, "_fetch_record", side_effect=RuntimeError("timeout")):
                        source._fetch_records(["https://www.kadokawa.com.tw/products/one"], media_hint="novel")
                elif source.code == "spp":
                    with patch("app.sources.spp.fetch_html", side_effect=RuntimeError("timeout")):
                        source._fetch_catalog_batch(["https://www.spp.com.tw/SalePage/Index/1"])
                else:
                    seed = BookRecord("chingwin", "A1", "書", "manga", "https://www.ching-win.com.tw/product-detail/A1")
                    with patch("app.sources.chingwin.fetch_html", side_effect=RuntimeError("timeout")), patch("app.sources.chingwin.polite_pause"):
                        source._with_product_rating(seed)
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0][1], "timeout")

    def test_upsert_failure_is_not_acknowledged_or_dropped(self):
        row = {"publisher_code": "kadokawa", "source_url": "https://www.kadokawa.com.tw/products/one"}
        record = BookRecord("kadokawa", "one", "書", "manga", row["source_url"])
        with patch.object(recent, "SOURCES", ("kadokawa",)), \
             patch.object(recent, "claim_product", side_effect=[row, None, None]), \
             patch.object(recent, "fetch_product", return_value=record), \
             patch.object(recent, "finish_product") as finish, \
             patch.object(recent, "backlog", return_value={"pending": 1, "failed": 1, "oldest_days": 0}), \
             patch("app.repository.upsert_book", side_effect=RuntimeError("db failed")):
            result = recent.run_recent_rechecks(scan=False)
        finish.assert_called_once_with(row, error="db failed")
        self.assertTrue(result["kadokawa"]["warning"])

    def test_existing_retry_is_attempted_before_due_scan(self):
        order = []
        with patch.object(recent, "SOURCES", ("kadokawa",)), \
             patch.object(recent, "claim_product", side_effect=lambda *args: order.append("retry") or None), \
             patch.object(recent, "claim_scan", return_value=True), \
             patch.object(recent, "scan_recent", side_effect=lambda *args, **kw: order.append("scan")), \
             patch.object(recent, "finish_scan"), \
             patch.object(recent, "backlog", return_value={"pending": 0, "failed": 0, "oldest_days": 0}):
            result = recent.run_recent_rechecks()
        self.assertEqual(order, ["retry", "scan", "retry"])
        self.assertEqual(result["kadokawa"]["scan"], "completed")


if __name__ == "__main__":
    unittest.main()
