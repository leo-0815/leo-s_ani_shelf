"""Execute the real recommendation queries against an isolated SQL fixture."""
import inspect
import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from app import repository
from app.preferences import visibility_scope


class SqlCursor:
    def __init__(self, connection):
        self.cursor = connection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.cursor.close()

    def execute(self, sql, parameters=()):
        self.cursor.execute(sql.replace("%s", "?"), parameters)

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]


class DetailRecommendationSqlTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE publishers(id INTEGER PRIMARY KEY, code TEXT, name TEXT);
            INSERT INTO publishers VALUES (1,'chingwin','青文'), (2,'spp','尖端');
            CREATE TABLE books(
                id INTEGER PRIMARY KEY, publisher_id INTEGER, title TEXT,
                series_title TEXT, series_key TEXT, media_type TEXT,
                volume_label TEXT, author TEXT, release_status TEXT,
                release_date TEXT, content_rating TEXT, rating_source TEXT,
                rating_confidence INTEGER, rating_checked_at TEXT,
                rating_parser_version TEXT, rating_locked INTEGER, bl_category TEXT);
            CREATE TABLE wishlist_items(book_id INTEGER, user_id INTEGER);
            CREATE TABLE recommendation_dismissals(book_id INTEGER, user_id INTEGER);
        """)
        for book_id, series, rating, publisher, confidence in [
            (1,"series-a","general",1,100),
            (2,"series-a","general",1,100),
            (3,"series-a","restricted_18",1,100),
            (4,"series-a","unknown",1,0),
            (5,"series-b","general",1,100),
            (6,"series-b","restricted_18",1,100),
            (7,"series-b","unknown",1,0),
            (8,"series-b","unknown",2,0),
            (9,"series-a","general",1,95),
            (10,"series-b","general",1,95),
        ]:
            self.db.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (book_id,publisher,"Book "+str(book_id),series,series,"novel",
                 str(book_id),"Author","available","2026-01-01",rating,
                 "publisher" if rating != "unknown" else "unknown",confidence,
                 "2026-10-09", "chingwin_rating_v1" if publisher == 1 else "spp_rating_v1", 0,
                 "BL漫畫" if book_id == 9 else None))
        self.cloud = "user_id" in inspect.signature(repository.list_book_recommendations).parameters

        @contextmanager
        def transaction():
            yield self

        self.mock_tx = patch.object(repository, "transaction", transaction)
        self.mock_tx.start()
        self.addCleanup(self.mock_tx.stop)
        self.addCleanup(self.db.close)

    def cursor(self):
        return SqlCursor(self.db)

    def recommendations(self, book_id=1, limit=20):
        if self.cloud:
            return repository.list_book_recommendations(1, book_id, limit)
        return repository.list_book_recommendations(book_id, limit)

    def test_general_mode_executes_series_and_author_sql_and_filters_candidates(self):
        with visibility_scope(True):
            items = self.recommendations()
        self.assertEqual({item["id"] for item in items}, {2,4,5,7,8,10})
        types = {item["id"]: item["recommendation_types"] for item in items}
        self.assertIn("book_series", types[2])
        self.assertIn("book_author", types[5])

    def test_unrestricted_mode_still_returns_other_ratings(self):
        with visibility_scope(False):
            self.assertEqual({item["id"] for item in self.recommendations()},
                             {2,3,4,5,6,7,8,9,10})

    def test_hidden_seed_cannot_produce_recommendations(self):
        with visibility_scope(True):
            for book_id in (3,9):
                with self.subTest(book_id=book_id):
                    with self.assertRaises(KeyError):
                        self.recommendations(book_id)

    def test_wishlist_and_dismissals_remain_excluded(self):
        self.db.execute("INSERT INTO wishlist_items VALUES (2,1)")
        self.db.execute("INSERT INTO recommendation_dismissals VALUES (5,1)")
        with visibility_scope(True):
            self.assertEqual({item["id"] for item in self.recommendations()}, {4,7,8,10})

