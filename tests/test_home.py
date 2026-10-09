from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from app.home import home_snapshot
from app.preferences import visibility_scope
from app.server import Handler


class Cursor:
    def __init__(self, db): self.cursor = db.cursor()
    def __enter__(self): return self
    def __exit__(self, *args): self.cursor.close()
    def execute(self, sql, params=()): return self.cursor.execute(sql.replace("%s", "?"), params)
    def fetchone(self): return dict(self.cursor.fetchone())
    def fetchall(self): return [dict(row) for row in self.cursor.fetchall()]


class HomeTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
          CREATE TABLE publishers(id INTEGER PRIMARY KEY, name TEXT);
          CREATE TABLE books(id INTEGER PRIMARY KEY, publisher_id INTEGER, title TEXT,
            author TEXT, cover_url TEXT, media_type TEXT, edition_type TEXT, release_date TEXT,
            release_precision TEXT, release_status TEXT, series_key TEXT,
            content_rating TEXT, bl_category TEXT);
          CREATE TABLE wishlist_items(user_id INTEGER, book_id INTEGER, state TEXT);
          CREATE TABLE collection_items(user_id INTEGER, book_id INTEGER);
          CREATE TABLE followed_series(user_id INTEGER, publisher_id INTEGER,
            normalized_series TEXT, media_type TEXT);
          INSERT INTO publishers VALUES(1,'出版社');
        """)
        @contextmanager
        def tx(): yield self
        self.patcher = patch("app.home.transaction", tx)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def cursor(self): return Cursor(self.db)

    def book(self, book_id, day="2030-10-09", rating="unknown", bl="", precision="day", status="scheduled", series="s", media="novel"):
        self.db.execute("INSERT INTO books VALUES(?,1,?,'作者',NULL,?,'standard',?,?,?,?,?,?)",
                        (book_id, f"書 {book_id}", media, day, precision, status, series, rating, bl))

    def snapshot(self, **kwargs): return home_snapshot(42, today=date(2030,10,9), **kwargs)

    def test_empty_and_no_catalog_fallback(self):
        self.book(1)
        data = self.snapshot()
        self.assertEqual(data["counts"], dict(wishlist=0, collection=0, followed_series=0))
        self.assertEqual(data["items"], [])

    def test_account_isolation_and_independent_collection(self):
        for i in range(1,4): self.book(i, series=f"s{i}")
        self.db.executescript("""
          INSERT INTO wishlist_items VALUES(99,1,'wanted'),(42,2,'wanted');
          INSERT INTO collection_items VALUES(99,1),(42,3),(42,NULL);
          INSERT INTO followed_series VALUES(99,1,'s1','novel');
        """)
        data = self.snapshot()
        self.assertEqual(data["counts"], dict(wishlist=1, collection=2, followed_series=0))
        self.assertEqual([b["id"] for b in data["items"]], [2])

    def test_series_media_stays_separate(self):
        self.book(1)
        self.book(2,media="manga")
        self.db.execute("INSERT INTO followed_series VALUES(42,1,'s','novel')")
        self.assertEqual([b["id"] for b in self.snapshot()["items"]], [1])

    def test_visibility_applies_to_counts_and_upcoming(self):
        for i, rating, bl in [(1,"unknown",""),(2,"restricted_18",""),(3,"general","bl")]:
            self.book(i,rating=rating,bl=bl,series=f"s{i}")
            self.db.execute("INSERT INTO wishlist_items VALUES(42,?,'wanted')", (i,))
            self.db.execute("INSERT INTO collection_items VALUES(42,?)", (i,))
            self.db.execute("INSERT INTO followed_series VALUES(42,1,?,'novel')", (f"s{i}",))
        with visibility_scope(True):
            data = self.snapshot()
        self.assertEqual(data["counts"], dict(wishlist=1,collection=1,followed_series=1))
        self.assertEqual([b["id"] for b in data["items"]], [1])
        with visibility_scope(False):
            self.assertEqual(len(self.snapshot()["items"]), 3)

    def test_week_precision_status_and_six_book_cap(self):
        for i in range(1,12):
            self.book(i)
            self.db.execute("INSERT INTO wishlist_items VALUES(42,?,'wanted')", (i,))
        for i,kwargs in [(12,dict(day="2030-10-08")),(13,dict(day="2030-10-17")),
                         (14,dict(precision="month")),(15,dict(status="cancelled")),
                         (16,dict(status="delayed"))]:
            self.book(i,**kwargs)
            self.db.execute("INSERT INTO wishlist_items VALUES(42,?,'wanted')", (i,))
        self.assertEqual([b["id"] for b in self.snapshot()["items"]], list(range(1,7)))
        self.db.execute("DELETE FROM wishlist_items WHERE book_id < 12")
        self.assertEqual(self.snapshot()["items"], [])
        self.book(17,day="2030-10-16")
        self.db.execute("INSERT INTO wishlist_items VALUES(42,17,'wanted')")
        self.assertEqual([b["id"] for b in self.snapshot()["items"]], [17])

    def test_local_schema_without_user_columns_on_follow_and_wishlist(self):
        self.db.executescript("DROP TABLE wishlist_items; DROP TABLE followed_series; CREATE TABLE wishlist_items(book_id INTEGER,state TEXT); CREATE TABLE followed_series(publisher_id INTEGER,normalized_series TEXT,media_type TEXT);")
        self.book(1)
        self.db.execute("INSERT INTO wishlist_items VALUES(1,'wanted')")
        self.assertEqual(home_snapshot(0,cloud=False,today=date(2030,10,9))["counts"]["wishlist"], 1)


@unittest.skipUnless(hasattr(Handler, "_authenticated_get"), "cloud route")
class HomeRouteTests(unittest.TestCase):
    @patch("app.server.home_snapshot", return_value={"counts":{},"items":[]})
    def test_home_uses_session_not_query_user(self, snapshot):
        handler = object.__new__(Handler)
        handler._json = MagicMock()
        handler._authenticated_get(urlparse("/api/home?user_id=99"), {"id":42,"general_audience":True})
        snapshot.assert_called_once_with(42)

    @patch("app.server.home_snapshot")
    def test_anonymous_cannot_read_home(self, snapshot):
        handler = object.__new__(Handler)
        handler.path = "/api/home"
        handler._require_user = lambda: None
        handler.do_GET()
        snapshot.assert_not_called()
