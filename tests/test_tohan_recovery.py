import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch, MagicMock

from app.models import BookRecord
from app.repository import _crawler_write_data
from app.sources.tohan import TohanSource
from app.sources.tohan_public import (public_seeds, require_public_product, SourceLoginRequired,
                                      ExcludedTohanPure, LOGIN_ERROR)
from app import rating_enrichment, recent_recheck, release_dates
from tests import test_recent_recheck as queue_tests


LIST = 'https://www.tohan.com.tw/product.php?cid=107'
URL = 'https://www.tohan.com.tw/product.php?act=view&id=8768'
GATE = '<script>alert("您必須先登入會員才能執行這個動作");window.location.replace("https://www.tohan.com.tw/member.php?act=login&ref=xxx");</script>'
CARD = '''<a href="product.php?act=view&amp;cid=107&amp;id=8768" title="不用此標籤猜資料">
<img src="data/goods/gallery/cover.jpg"><p class="g-bookName">測試漫畫 2 特裝版</p>
<p class="g-subTit">不是作者，也不是分級</p><p class="g-prize">優惠價 NT$119</p></a>'''
CARD = '<div class="tab-pane active gobal-warp">' + CARD + '</div>'


class TohanRecoveryTests(unittest.TestCase):
    def test_product_own_pure_category_is_excluded_even_outside_index(self):
        with self.assertRaises(ExcludedTohanPure):
            TohanSource()._parse_detail(URL, '<title>書名_PURE系列_漫畫 | 台灣東販</title>')

    def test_login_response_is_not_a_missing_title_or_a_rating(self):
        with self.assertRaisesRegex(SourceLoginRequired, '來源需登入'):
            TohanSource()._parse_detail(URL, GATE)
        require_public_product('<html><a href="member.php?act=login">登入</a><h1>書名</h1></html>')

    def test_public_card_has_only_proven_fields_and_exact_identity(self):
        row = public_seeds(LIST, CARD)['8768']
        self.assertEqual(row.source_url, URL)
        self.assertEqual(row.title, '測試漫畫 2 特裝版')
        self.assertEqual(row.cover_url, 'https://www.tohan.com.tw/data/goods/gallery/cover.jpg')
        self.assertEqual(row.media_type, 'unknown')
        self.assertEqual(row.content_rating, 'unknown')
        self.assertIsNone(row.bl_category)
        for field in ('author', 'isbn', 'list_price', 'release_date', 'rating_checked_at', 'release_checked_at'):
            self.assertIsNone(getattr(row, field))

    def test_foreign_links_navigation_and_incomplete_cards_are_not_seeds(self):
        invalid = CARD.replace('product.php?', 'https://attacker.test/product.php?')
        self.assertEqual(public_seeds(LIST, invalid + '<a href="product.php?act=view&id=1">書名</a>'), {})
        self.assertEqual(public_seeds(LIST, CARD.replace('g-bookName', 'g-subTit')), {})
        self.assertEqual(public_seeds(LIST, CARD.replace('data/goods/gallery/cover.jpg', 'data:image/png;base64,x')), {})

    @patch('app.sources.tohan.polite_pause')
    @patch('app.sources.tohan.fetch_html')
    def test_member_gate_does_not_save_seed_or_count_as_failure(self, fetch, pause):
        fetch.side_effect = [CARD, GATE]
        source = TohanSource()
        source.pure_index_url = None
        source.category_urls = ((None, LIST),)
        source.record_product_failure = MagicMock()
        rows = source.collect()
        self.assertEqual(rows, [])
        self.assertEqual(source.errors, [])
        self.assertEqual(source.skipped_count, 1)
        self.assertFalse(getattr(source, 'product_fetch_successes', set()))
        self.assertEqual(source.record_product_failure.call_args.args, (URL, LOGIN_ERROR, None, None))

    @patch('app.sources.tohan.polite_pause')
    @patch('app.sources.tohan.fetch_html')
    def test_pure_index_filters_cross_listed_id_but_real_failure_stays_visible(self, fetch, pause):
        other = CARD.replace('8768', '8769')
        fetch.side_effect = [CARD, CARD + other, RuntimeError('HTTP 503')]
        source = TohanSource()
        source.category_urls = ((None, LIST),)
        rows = source.collect()
        self.assertEqual([row.source_key for row in rows], ['8769'])
        self.assertEqual(source.skipped_count, 1)
        self.assertIn('HTTP 503', source.errors[0])
        self.assertNotIn(URL, [call.args[0] for call in fetch.call_args_list])

    @patch('app.sources.tohan.polite_pause')
    @patch('app.sources.tohan.fetch_html', return_value=CARD)
    def test_backoff_skips_product_but_retains_public_seed(self, fetch, pause):
        source = TohanSource()
        source.pure_index_url = None
        source.category_urls = ((None, LIST),)
        source.should_fetch_product = lambda url: False
        self.assertEqual(source.collect()[0].media_type, 'unknown')
        fetch.assert_called_once_with(LIST)
        self.assertEqual(source.errors, [])

    def test_sparse_seed_cannot_erase_existing_details_but_fills_cover(self):
        seed = public_seeds(LIST, CARD)['8768'].prepared()
        old = BookRecord('tohan', '8768', '完整書名', 'novel', URL, author='作者',
                         isbn='9781234567890', list_price=150, release_date=date(2026,10,2),
                         content_rating='restricted_18', bl_category='BL', rating_raw='限制級',
                         release_date_source='product', release_checked_at=datetime(2026,10,1)).prepared()
        merged = _crawler_write_data(old, seed)
        for field in ('title', 'author', 'isbn', 'list_price', 'release_date', 'media_type',
                      'content_rating', 'bl_category', 'release_checked_at'):
            self.assertEqual(merged[field], old[field], field)
        self.assertEqual(merged['cover_url'], seed['cover_url'])
        old['cover_url'] = 'https://www.tohan.com.tw/data/goods/better.jpg'
        self.assertEqual(_crawler_write_data(old, seed)['cover_url'], old['cover_url'])

    def test_real_detail_upgrades_seed_in_place(self):
        seed = public_seeds(LIST, CARD)['8768'].prepared()
        detail = BookRecord('tohan','8768','完整書名','manga',URL,author='作者',isbn='9781234567890').prepared()
        merged = _crawler_write_data(seed, detail)
        self.assertEqual(merged['source_key'], '8768')
        self.assertEqual(merged['media_type'], 'manga')
        self.assertEqual(merged['isbn'], '9781234567890')

    @patch('app.recent_recheck.enqueue')
    @patch('app.rating_enrichment.save_result', return_value=False)
    @patch('app.rating_enrichment.fetch_html', return_value=GATE)
    def test_rating_gate_is_error_not_successful_unknown(self, fetch, save, enqueue):
        row = dict(id=1, publisher_code='tohan',source_key='8768',source_url=URL)
        result = rating_enrichment.check_book(row)
        self.assertEqual(result[2], LOGIN_ERROR)
        self.assertEqual(save.call_args.args[3], LOGIN_ERROR)
        enqueue.assert_called_once()

    def test_both_release_and_rating_candidate_sql_share_backoff(self):
        from contextlib import contextmanager
        cur = MagicMock(); conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        @contextmanager
        def tx(): yield conn
        with patch.object(release_dates, 'transaction', tx):
            release_dates.candidates('tohan', 20, datetime.utcnow())
        self.assertIn(recent_recheck.tohan_retry_filter('tohan'), cur.execute.call_args.args[0])
        with patch.object(rating_enrichment, 'transaction', tx):
            rating_enrichment.candidates(20, code='tohan')
        self.assertIn(recent_recheck.tohan_retry_filter('tohan'), cur.execute.call_args.args[0])


class TohanQueueTests(queue_tests.RecentQueueTests):
    def test_member_rows_and_legacy_pending_rows_never_retry(self):
        recent_recheck.enqueue('tohan', [(URL, None, None)], error=LOGIN_ERROR, now=self.now)
        self.assertFalse(recent_recheck.product_retry_due('tohan', URL+'&cid=107', self.now))
        self.assertEqual(self.row()['status'], 'ignored')
        self.db.execute("UPDATE catalog_product_retries SET status='pending'")
        later = self.now + timedelta(days=30)
        self.assertIsNone(recent_recheck.claim_product('tohan', later))
        self.assertFalse(recent_recheck.product_retry_due('tohan', URL, later))
        self.assertEqual(recent_recheck.backlog('tohan', later)['pending'], 0)
        self.assertEqual(recent_recheck.tohan_excluded_keys(), {'8768'})
        recent_recheck.enqueue('tohan', [(URL.replace('8768', '8769'), None, None)],
                               error='HTTP 403', now=self.now)
        self.assertEqual(recent_recheck.claim_product('tohan', later)['last_error'], 'HTTP 403')
