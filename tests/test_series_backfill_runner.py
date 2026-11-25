import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import history_cli, repository
from app.models import BookRecord
from app.series import series_key
from app import series_backfill_runner as runner, series_discovery as discovery


ISBN = '9786264154291'


def row(code, title='示範', *, media='manga', gaps=(2,), seeds=()):
    return dict(code=code, title=title, media_type=media, series_key=series_key(title, code),
                gaps=gaps, seeds=seeds)


def record(code='kadokawa', title='示範', *, media='manga', edition='standard',
           url='https://www.kadokawa.com.tw/products/one', key='one', isbn=ISBN):
    return BookRecord(code, key, title + ' (2)', media, url, edition_type=edition,
                      isbn=isbn, series_title=title, volume_label='2', author='作者')


class DiscoveryTests(unittest.TestCase):
    @patch.object(discovery, 'polite_pause')
    def test_kadokawa_unrelated_ranked_results_do_not_fetch_even_on_other_pages(self, _):
        urls = []

        def load(url, **kwargs):
            self.assertEqual(kwargs['attempts'], 1)
            urls.append(url)
            if 'page=2' in url:
                return ('<a href="/products/trash">示範外傳 (2) NT$200</a>'
                        '<a href="/products/one">會員獨享 加入購物車 示範 (2) NT$200</a>')
            if url.endswith('/one'):
                return 'official book'
            return ('<a href="/products/known">示範 (3) NT$100</a>'
                    '<a href="https://wrong.test/products/fake">示範 (2)</a>'
                    '<a href="/products/bundle">示範 (2)套組 NT$100</a>'
                    '<a href="/products?page=2&amp;query=示範">2</a>')

        with patch.object(discovery.KadokawaSource, '_parse_detail', return_value=record()):
            result = discovery.discover(row('kadokawa'), load=load)
        self.assertEqual(len(urls), 3)
        self.assertEqual([b.source_key for b in result.books], ['one'])

    @patch.object(discovery, 'polite_pause')
    def test_tongli_only_follows_series_carousel(self, _):
        seed = 'https://www.tongli.com.tw/BooksDetail.aspx?Bd=IC1'
        product = 'https://www.tongli.com.tw/BooksDetail.aspx?Bd=IC2'
        markup = ('<a href="BooksDetail.aspx?BD=RECOMMENDED"><img alt="示範 (2)"></a>'
                  '<div id="owl-demo2"><div><a href="BooksDetail.aspx?BD=IC2">'
                  '<img alt="示範 (2)">示範 (2)<br>作者</a></div></div>')
        with patch.object(discovery.TongLiSource, '_parse_detail', return_value=record('tongli', url=product)) as detail:
            result = discovery.discover(row('tongli', seeds=[seed]), load=lambda *_a, **_kw: markup)
        detail.assert_called_once_with(product, markup)
        self.assertEqual(len(result.books), 1)

    def test_chingwin_detail_passes_markup_and_verified_media_to_parser(self):
        url = 'https://www.ching-win.com.tw/product-detail/123'
        markup = '<nav class="breadcrumb"><a href="/manga">漫畫</a></nav>'
        book = record('chingwin', url=url)
        with patch.object(discovery.ChingWinSource, '_parse_catalog_page', autospec=True,
                          return_value=([book], 0)) as parser:
            self.assertEqual(discovery._detail(row('chingwin'), url, markup), [book])
        self.assertEqual(parser.call_args.kwargs, {
            'page_url': url, 'markup': markup, 'media_type': 'manga', 'cutoff': date.min,
        })

    def test_spp_bootstrap_requires_official_shop_and_structured_search_result(self):
        data = {'SalePageList': [{'Id': 11, 'ShopId': 41817, 'Title': '示範(2)'},
                                 {'Id': 12, 'ShopId': 111, 'Title': '示範(2)'}]}
        markup = '{"serverRenderData":{"searchProvider":"elasticsearch","searchResult":' + json.dumps(data) + '}}'
        self.assertEqual(discovery._spp_cards(markup), [('/SalePage/Index/11', ['示範(2)'])])
        with self.assertRaises(ValueError):
            discovery._spp_cards('<html>登入</html>')

    def test_tohan_public_product_type_not_sitewide_navigation(self):
        url = 'https://www.tohan.com.tw/product.php?act=view&id=12'
        novel_guess = record('tohan', url=url, media='novel')
        fake = '<title>示範 (2)_靈異 / 神怪_分類漫畫_漫畫 / 輕小說 | 台灣東販</title><div data-id="BLOCK_PRODUCT_SPEC">'
        with patch.object(discovery.TohanSource, '_parse_detail', return_value=novel_guess) as parse:
            self.assertEqual(discovery._detail(row('tohan'), url, fake)[0].media_type, 'manga')
            self.assertEqual(discovery._detail(row('tohan'), url, fake.replace('分類漫畫', 'PURE系列')), [])
            self.assertEqual(discovery._detail(row('tohan'), url, '<script>location="member.php?act=login"</script>'), [])
        parse.assert_called_once_with(url, fake)

    def test_tohan_search_uses_the_results_scope(self):
        html = ('<a href="/product.php?act=view&amp;id=11" title="站外推薦"></a>'
                '<div class="all-width search-result"><div>'
                '<a href="/product.php?act=view&amp;id=12" title="示範 (2)"><p>示範 (2)</p></a>'
                '</div></div>')
        self.assertEqual([href for href, _ in discovery._cards(html, 'tohan')], ['/product.php?act=view&id=12'])

    def test_redirect_disallows_off_origin_destination(self):
        request = discovery.Request('https://www.kadokawa.com.tw/products/one')
        with self.assertRaises(discovery.SourceBlocked):
            discovery.SameOriginRedirect().redirect_request(request, None, 302, '', {}, 'https://other.test/')

    def test_accept_rejects_other_medium_editions_and_invalid_isbn(self):
        expected = row('kadokawa')
        for book in (record(media='novel'), record(edition='collector'), record(title='示範外傳'),
                     record(isbn='9786264154290')):
            self.assertFalse(discovery._accept(expected, book))
        self.assertIsNone(discovery.product_url('kadokawa', 'https://www.kadokawa.com.tw',
                                                'https://www.kadokawa.com.tw@wrong.test/products/x'))

    @patch.object(discovery, 'polite_pause')
    def test_block_is_not_retried(self, _):
        loader = MagicMock(side_effect=RuntimeError('HTTP 429: rate limited'))
        result = discovery.discover(row('chingwin'), load=loader)
        self.assertEqual(result.status, 'source_blocked')
        loader.assert_called_once()


class QueueTests(unittest.TestCase):
    def test_review_serialization_preserves_confirmation_time(self):
        date_time = datetime(2026, 11, 26, 10, 15, 9)
        self.assertEqual(runner._serialize_date(date_time), '2026-11-26T10:15:09')
        self.assertEqual(runner._serialize_date(date_time.date()), '2026-11-26')

    def test_only_one_local_series_runner_can_hold_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'progress.db'
            with runner.open_state(data), self.assertRaisesRegex(RuntimeError, '另一個'):
                with runner.open_state(data):
                    self.fail('second process lock unexpectedly acquired')

    def test_automatic_round_robin_checkpoints_and_persistent_holds(self):
        rows = [row('chingwin'), row('chingwin', '別作'), row('kadokawa'), row('kadokawa', '下一個')]
        calls = []

        def lookup(current):
            calls.append((current['code'], current['title']))
            if current['code'] == 'chingwin':
                return discovery.Discovery([], status='source_blocked')
            return discovery.Discovery([record()] if current['title'] == '示範' else [])

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'progress.db'
            with runner.open_state(data) as conn:
                first = runner.scan(conn, rows, limit=2, lookup=lookup, now=100, emit=lambda _: None)
            self.assertEqual(first['attempted'], 2)
            with runner.open_state(data) as conn:
                second = runner.scan(conn, rows, limit=2, lookup=lookup, now=101, emit=lambda _: None)
                self.assertEqual(second['attempted'], 1)
                self.assertEqual([book.title for _, book in runner.pending(conn)], ['示範 (2)'])
            self.assertEqual([code for code, _ in calls], ['chingwin', 'kadokawa', 'kadokawa'])

    def test_first_scan_prefers_smaller_gaps_within_a_publisher(self):
        many = row('kadokawa', '很多缺口', gaps=range(1, 60))
        few = row('kadokawa', '少量缺口', gaps=[2])
        lookup = MagicMock(return_value=discovery.Discovery([]))
        with tempfile.TemporaryDirectory() as folder, runner.open_state(Path(folder) / 'progress.db') as conn:
            runner.scan(conn, [many, few], limit=1, lookup=lookup, emit=lambda _: None)
        lookup.assert_called_once_with(few)

    def test_ctrl_c_preserves_prior_series_but_not_incomplete_check(self):
        rows = [row('kadokawa'), row('spp')]
        lookup = MagicMock(side_effect=[discovery.Discovery([record()]), KeyboardInterrupt()])
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'progress.db'
            with self.assertRaises(KeyboardInterrupt), runner.open_state(data) as conn:
                runner.scan(conn, rows, lookup=lookup, now=100, emit=lambda _: None)
            with runner.open_state(data) as conn:
                self.assertEqual(conn.execute('SELECT code FROM series_checks').fetchall(), [('kadokawa',)])
                self.assertEqual(len(list(runner.pending(conn))), 1)

    def test_menu_dispatches_automatic_search_without_asking_for_series(self):
        with patch('builtins.input', return_value='1') as ask, patch.object(runner, 'run_interactive') as run:
            history_cli.main()
        ask.assert_called_once()
        run.assert_called_once()

    def test_manual_mode_still_has_an_explicit_entry(self):
        with patch('builtins.input', return_value='m'), patch('app.series_backfill.interactive_series') as manual:
            history_cli.main()
        manual.assert_called_once_with()

    def test_preview_dispatches_without_opening_the_menu(self):
        with patch('builtins.input') as ask, patch.object(runner, 'run_preview') as preview:
            history_cli.main(('--series-preview', '3'))
        ask.assert_not_called()
        preview.assert_called_once_with(3)

    def test_preview_does_not_write_or_persist_progress(self):
        rows = [row('kadokawa'), row('spp')]
        scan = runner.scan
        with patch.object(runner, 'catalogue_gaps', return_value=rows), patch.object(
            runner, 'scan', side_effect=lambda conn, rows, limit: scan(
                conn, rows, limit=limit, lookup=lambda _: discovery.Discovery([record()]), emit=lambda _: None
            )
        ), patch.object(runner, 'open_state') as disk, patch.object(
            runner, 'apply_review'
        ) as apply, patch('builtins.input') as ask, patch('builtins.print'):
            result = runner.run_preview(1)
        self.assertEqual(result['attempted'], 1)
        self.assertEqual(result['new_review'], 1)
        for forbidden in (disk, apply, ask):
            forbidden.assert_not_called()

    def test_legacy_repo_does_not_offer_unsafe_review_writes(self):
        @contextmanager
        def queue():
            yield object()

        with patch.object(runner, 'open_state', queue), patch.object(runner, 'catalogue_gaps', return_value=[]), patch.object(
            runner, 'scan', return_value={'attempted': 1, 'new_review': 1, 'source_hold': []}
        ), patch.object(runner, 'pending', return_value=[(1, record())]), patch(
            'app.repository.upsert_book', new=lambda book: self.fail('unsafe write')
        ), patch('builtins.input') as ask, patch('builtins.print'):
            runner.run_interactive()
        ask.assert_not_called()

    def test_insert_only_never_updates_existing_or_adds_subscription(self):
        @contextmanager
        def tx():
            yield connection

        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.lastrowid = 14
        with patch.object(repository, 'transaction', tx), patch.object(repository, 'get_publisher_id', return_value=1):
            cursor.fetchone.side_effect = [None, None, None, {'id': 13}]
            self.assertEqual(repository.upsert_book(record(), insert_only=True), 'unchanged')
            self.assertFalse(any('UPDATE books' in call.args[0] for call in cursor.execute.call_args_list))
            cursor.reset_mock()
            cursor.fetchone.side_effect = [None, None, None, None]
            self.assertEqual(repository.upsert_book(record(), insert_only=True), 'inserted')
            sql = ' '.join(call.args[0] for call in cursor.execute.call_args_list)
            self.assertIn('INSERT INTO catalog_changes', sql)
            self.assertNotIn('wishlist_items', sql)


if __name__ == '__main__':
    unittest.main()
