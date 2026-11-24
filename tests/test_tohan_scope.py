import unittest
from dataclasses import replace
from unittest.mock import patch, MagicMock
from app.sources.tohan import TohanSource
from app.sources.tohan_public import public_seeds, detail_media_type, UnsupportedTohanProduct, ListingRecord
from app.repository import _crawler_write_data, _tohan_listing_record
from app.models import BookRecord, book_content_hash
from app.tohan_scope_audit import inspect
from tests.test_tohan_recovery import CARD, LIST, URL, GATE
from app import recent_recheck


def title(name='漫畫裡的小說家', child='本月新刊', parent='漫畫 / 輕小說'):
    return f'<title>{name}_{child}_{parent} | 台灣東販</title>'


class TohanScopeTests(unittest.TestCase):
    def test_only_active_main_grid_cards_not_recommendations_or_hidden_tabs(self):
        outside = CARD.replace('<div class="tab-pane active gobal-warp">','<div class="recommendations">').replace('8768','8752')
        hidden = CARD.replace('tab-pane active','tab-pane').replace('8768','9999')
        self.assertEqual(set(public_seeds(LIST,outside+hidden+CARD)),{'8768'})

    def test_main_grid_close_does_not_leak_scope_into_recommendations(self):
        other = CARD.replace('<div class="tab-pane active gobal-warp">','<div>').replace('8768','8752')
        self.assertEqual(set(public_seeds(LIST,CARD+other)),{'8768'})

    def test_script_data_cannot_change_card_title(self):
        html = CARD.replace('測試漫畫 2 特裝版','測試<script>不屬於書名</script>漫畫')
        self.assertEqual(public_seeds(LIST,html)['8768'].title,'測試漫畫')

    def test_typed_listing_seed_still_has_no_invented_grade_or_date(self):
        seed = public_seeds(LIST,CARD,'novel')['8768']
        self.assertIsInstance(seed,ListingRecord)
        self.assertTrue(_tohan_listing_record(seed))
        self.assertEqual(seed.media_type,'novel')
        self.assertEqual(seed.content_rating,'unknown')
        self.assertIsNone(seed.release_date)
        self.assertIsNone(seed.bl_category)

    def test_novel_word_in_book_title_is_not_media_evidence(self):
        self.assertEqual(detail_media_type(title()),'unknown')
        self.assertEqual(detail_media_type(title(child='分類漫畫')),'manga')
        self.assertEqual(detail_media_type(title(child='翻譯輕小說')),'novel')
        self.assertEqual(detail_media_type(title(child='華文輕小說')),'novel')

    def test_global_navigation_is_not_product_type_evidence(self):
        self.assertEqual(detail_media_type('<title>書名</title><nav>翻譯輕小說 分類漫畫 BL</nav>'),'unknown')

    def test_explicit_general_book_is_rejected_even_with_typed_hint(self):
        for hint in (None,'manga','novel'):
            with self.subTest(hint=hint), self.assertRaises(UnsupportedTohanProduct):
                detail_media_type(title('AI小說創作術','本月新書','書籍'),hint)

    def test_conflicting_product_and_listing_type_is_error_not_guess(self):
        with self.assertRaisesRegex(ValueError,'衝突'):
            detail_media_type(title(child='翻譯輕小說'),'manga')

    def test_product_proof_wins_over_a_stale_database_or_retry_hint(self):
        self.assertEqual(detail_media_type(title(child='翻譯輕小說'),'manga',strict_hint=False),'novel')

    def test_type_from_matching_category_seed_fills_ambiguous_monthly_type(self):
        self.assertEqual(detail_media_type(title(),'manga'),'manga')
        self.assertEqual(detail_media_type(title(),'novel'),'novel')

    def test_listing_type_upgrade_keeps_all_existing_product_fields(self):
        old = BookRecord('tohan','8768','原本的完整書名','unknown',URL,author='作者',isbn='9781234567890',
                         list_price=250,content_rating='restricted_18',rating_raw='限制級',bl_category='BL').prepared()
        seed = public_seeds(LIST,CARD,'novel')['8768'].prepared()
        merged = _crawler_write_data(old,seed,listing_only=True)
        self.assertEqual(merged['media_type'],'novel')
        for key in ('title','normalized_title','author','isbn','list_price','content_rating','bl_category'):
            self.assertEqual(merged[key],old[key],key)
        old['media_type']='manga'
        self.assertEqual(_crawler_write_data(old,seed,listing_only=True)['media_type'],'manga')

    def test_same_listing_seed_does_not_churn_hash_when_product_title_differs(self):
        old = BookRecord('tohan','8768','原書名','manga',URL,author='作者',cover_url='https://www.tohan.com.tw/data/goods/old.jpg').prepared()
        seed = public_seeds(LIST,CARD,'manga')['8768'].prepared()
        self.assertEqual(book_content_hash(_crawler_write_data(old,seed,listing_only=True)),book_content_hash(old))

    @patch('app.sources.tohan.polite_pause')
    @patch('app.sources.tohan.fetch_html')
    def test_collect_ignores_all_link_recommendation_and_deduplicates_typed_lists(self, fetch, pause):
        source=TohanSource()
        source.category_urls=((None,LIST),('manga','https://www.tohan.com.tw/product.php?cid=3'))
        fetch.side_effect=[CARD+'<a href="/product.php?act=view&id=8752">一般書</a>',CARD,title()+'<p>作者</p><p>作者一</p><p>ISBN</p><p>9781234567890</p>']
        rows=source.collect()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0].media_type,'manga')
        self.assertEqual(fetch.call_count,3)
        self.assertNotIn('8752',fetch.call_args.args[0])

    @patch('app.sources.tohan.polite_pause')
    @patch('app.sources.tohan.fetch_html',return_value=CARD)
    def test_existing_unknown_seed_upgrades_without_new_product_request(self, fetch, pause):
        source=TohanSource();source.category_urls=(('novel',LIST),)
        source.listing_upgrade_keys={'8768'}
        rows=source.collect(known_keys={'8768'})
        self.assertEqual(rows[0].media_type,'novel')
        self.assertIsInstance(rows[0],ListingRecord)
        fetch.assert_called_once()

    @patch('app.recent_recheck.fetch_html',return_value=title(parent='書籍')+'<p>作者</p><p>作者一</p><p>ISBN</p><p>9781234567890</p>')
    @patch('app.recent_recheck.polite_pause')
    def test_out_of_scope_retry_finishes_ignored_not_eternal_parse_failure(self, pause, fetch):
        row={'source_url':URL,'media_hint':None}
        self.assertIsNone(recent_recheck.fetch_product(TohanSource(),row))

    def test_audit_is_explicit_about_unknown_gate_and_bad_type(self):
        row=dict(id=1,source_key='8768',title='舊書名',media_type='manga')
        self.assertEqual(inspect(row,title(parent='書籍'))['status'],'out_of_scope')
        self.assertEqual(inspect(row,title(child='翻譯輕小說'))['status'],'type_mismatch')
        self.assertEqual(inspect(row,title())['status'],'needs_review')
        self.assertEqual(inspect(row,GATE)['status'],'requires_login')

    def test_html_error_title_cannot_be_saved_as_a_book_with_a_listing_hint(self):
        with self.assertRaisesRegex(ValueError,'缺少單本商品欄位'):
            TohanSource()._parse_detail(URL,'<title>暫時無法提供服務</title><nav>作者 ISBN</nav>',media_hint='manga')

    def test_historical_pagination_stays_on_same_official_category(self):
        markup='<a href="?act=list&cid=17&page=2">2</a><a href="?act=list&cid=22&page=2">other</a><a href="https://evil.test/product.php?cid=17&page=3">3</a>'
        self.assertEqual(TohanSource.page_links('https://www.tohan.com.tw/product.php?cid=17',markup),
                         ['https://www.tohan.com.tw/product.php?act=list&cid=17&page=2'])

    @patch('app.recent_recheck.transaction',side_effect=RuntimeError('TiDB unavailable'))
    def test_attaching_hooks_does_not_read_db_outside_source_error_boundary(self, transaction):
        source=TohanSource()
        recent_recheck.attach_failure_recorder(source)
        transaction.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'TiDB unavailable'):
            source.listing_upgrade_keys()
