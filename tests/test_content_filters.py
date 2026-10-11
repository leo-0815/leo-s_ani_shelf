import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import urlparse

from app import guest, repository
from app.abuse import RequestError, validate_query
from app.filters import content_filter_sql
from app.preferences import visibility_scope
from tests.test_release_dates import Connection


class ContentFilterTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
          CREATE TABLE publishers(id INTEGER,code TEXT,name TEXT);
          INSERT INTO publishers VALUES(1,'chingwin','青文'),(2,'spp','尖端');
          CREATE TABLE books(id INTEGER,publisher_id INTEGER,title TEXT,normalized_title TEXT,
            author TEXT,isbn TEXT,series_title TEXT,series_key TEXT,media_type TEXT,
            content_rating TEXT,bl_category TEXT,release_date TEXT,release_status TEXT,
            edition_type TEXT,first_seen_at TEXT,updated_at TEXT);
          CREATE TABLE wishlist_items(book_id INTEGER,user_id INTEGER,state TEXT,notes TEXT,
            follow_series INTEGER,priority INTEGER,store_name TEXT,order_number TEXT,
            paid_price INTEGER,owned_format TEXT,purchased_at TEXT,updated_at TEXT);
          CREATE TABLE followed_series(user_id INTEGER,publisher_id INTEGER,normalized_series TEXT,
            media_type TEXT,follow_scope TEXT);
        """)
        for i, (rating, bl) in enumerate([
            ('general', None), ('general', 'BL漫畫'), ('restricted_18', ''),
            ('restricted_18', 'BL小說'), ('unknown', None), (None, 'BL小說'),
            ('guidance_15', ''), ('guidance_15', 'BL'),
        ], 1):
            self.db.execute("INSERT INTO books VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (i, 2 if i > 6 else 1, f'測試書{i}', f'測試書{i}', '作者', f'ISBN{i}',
                 '測試書', 'series', 'novel' if i in {5,6,8} else 'manga', rating, bl,
                 '2026-10-10', 'available', 'standard', '2026-01-01', '2026-10-10'))
        @contextmanager
        def tx():
            yield Connection(self.db)
        self.tx = tx

    def query(self, mode, filters, limit=100, offset=0, public=False):
        with patch('app.repository.transaction', self.tx), visibility_scope(mode), \
             patch('app.collection.decorate_owned', side_effect=lambda rows, user: rows), \
             patch('app.catalog_reconciliation.previous_ids', side_effect=lambda rows: rows):
            if public:
                return guest.public_get('books', {**filters, 'content_mode': mode,
                                                'limit': str(limit), 'offset': str(offset)})
            return repository.list_books(filters, 9, limit, offset)

    def test_count_and_page_matrix_preference_is_not_bypassed(self):
        visible = {'general': {1,5,7}, 'bl': {1,2,5,6,7,8},
                   'r18': {1,3,5,7}, 'all': set(range(1,9))}
        categories = {'': set(range(1,9)), 'general': {1,5,7}, 'bl': {2,4,6,8},
                      'r18': {3,4}, 'bl_r18': {4}, 'bl,r18': {2,3,4,6,8},
                      'general,bl': {1,2,4,5,6,7,8}, 'general,r18': {1,3,4,5,7},
                      'general,bl,r18': set(range(1,9)), 'none': set()}
        for mode, allowed in visible.items():
            for category, candidates in categories.items():
                for public in (False, True):
                    with self.subTest(mode=mode, category=category, public=public):
                        data = self.query(mode, {'content_filter': category, 'sort': 'title_asc'}, public=public)
                        expected = sorted(allowed & candidates)
                        self.assertEqual(data['total'], len(expected))
                        self.assertEqual([r['id'] for r in data['items']], expected)
                        page = self.query(mode, {'content_filter': category, 'sort': 'title_asc'}, 1, 1, public)
                        self.assertEqual(page['total'], len(expected))
                        self.assertEqual([r['id'] for r in page['items']], expected[1:2])

    def test_other_filters_intersect_not_union(self):
        data = self.query('all', {'content_filter': 'bl', 'publishers': 'chingwin,spp',
                                 'media_type': 'novel', 'q': '測試書6', 'edition': 'standard',
                                 'date_from': '2026-10-01', 'date_to': '2026-10-31'})
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['id'], 6)
        self.assertEqual(self.query('all', {'content_filter': 'bl', 'publishers': 'none'})['total'], 0)

    def test_bad_categories_rejected_before_database(self):
        for value in ('BL', 'male', "bl') OR 1=1 --", 'all', 'bl,', 'bl,bl',
                      'none,bl', 'general,bl,r18,bl_r18'):
            with self.subTest(value=value), self.assertRaises(RequestError):
                validate_query(urlparse('/api/books?content_filter=' + value))
            with self.assertRaises(ValueError):
                content_filter_sql(value)
        with self.assertRaises(ValueError):
            content_filter_sql(None)

    def test_sql_is_static_and_supports_internal_aliases(self):
        self.assertIn('seed.bl_category', content_filter_sql('bl', 'seed'))
        self.assertNotIn('b.', content_filter_sql('r18', ''))
        validate_query(urlparse('/api/guest/books?content_filter=bl_r18'))
        validate_query(urlparse('/api/guest/books?content_filter=r18,bl'))
        self.assertEqual(content_filter_sql('none'), '1 = 0')
