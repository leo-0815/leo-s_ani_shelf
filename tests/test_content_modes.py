"""Four independent BL/R18 preferences, legacy records and request isolation."""
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import urlparse

from app import guest
from app.abuse import RequestError
from app.preferences import (content_mode, get_preferences, set_preferences,
    notification_visibility_sql, visibility_scope, visible_book_sql)
from app.resource_guards import ResourceGuard
from app.server import Handler


class ContentModeTests(unittest.TestCase):
    expected = {"general": [1, 5], "bl": [1, 2, 5], "r18": [1, 3, 5], "all": [1, 2, 3, 4, 5]}

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE books(id INTEGER, content_rating TEXT, bl_category TEXT);
            INSERT INTO books VALUES(1,'general',NULL),(2,'general','BL'),
                (3,'restricted_18',NULL),(4,'restricted_18','BL'),(5,'unknown','');
            CREATE TABLE users(id INTEGER,role TEXT);
            CREATE TABLE user_preferences(user_id INTEGER,general_audience INTEGER,content_mode TEXT);
            INSERT INTO users VALUES(1,'user'),(2,'user'),(3,'user'),(4,'user'),
                (5,'user'),(6,'admin'),(7,'user'),(8,'admin');
            INSERT INTO user_preferences VALUES(1,1,'general'),(2,0,'bl'),
                (3,0,'r18'),(4,0,'all'),(7,0,NULL),(8,1,NULL);
        """)

    def test_four_modes_include_unknown_and_only_all_includes_overlap(self):
        for mode, expected in self.expected.items():
            for alias in ("", "b", "seed"):
                with self.subTest(mode=mode, alias=alias), visibility_scope(mode):
                    rows = self.db.execute(f"SELECT id FROM books {alias} WHERE {visible_book_sql(alias)} ORDER BY id")
                    self.assertEqual([row[0] for row in rows], expected)

    def test_notification_policy_per_recipient_not_current_request(self):
        sql = "SELECT b.id FROM books b CROSS JOIN users u WHERE u.id=? AND " + notification_visibility_sql()
        for user, mode in enumerate(self.expected, 1):
            with visibility_scope("all" if mode == "general" else "general"):
                self.assertEqual([row[0] for row in self.db.execute(sql, (user,))], self.expected[mode])
        for user, mode in ((5, "general"), (6, "all"), (7, "all"), (8, "general")):
            self.assertEqual([row[0] for row in self.db.execute(sql, (user,))], self.expected[mode])

    def test_nested_scopes_errors_and_threads_do_not_leak_preferences(self):
        with visibility_scope("bl"):
            original = visible_book_sql()
            with self.assertRaises(RuntimeError), visibility_scope("r18"):
                raise RuntimeError()
            self.assertEqual(visible_book_sql(), original)
        self.assertEqual(visible_book_sql(), "1 = 1")
        def policy(mode):
            with visibility_scope(mode): return visible_book_sql()
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(policy, list(self.expected)*5)), [policy(m) for m in list(self.expected)*5])
        self.assertEqual(policy(True), policy("general"))
        self.assertEqual(policy(False), policy("all"))

    def test_preferences_preserve_legacy_and_admin_choices(self):
        for row, expected in ((None,"general"), ({"role":"admin"},"all"),
                ({"role":"user","general_audience":False},"all"),
                ({"role":"admin","general_audience":True},"general"),
                ({"role":"user","general_audience":False,"content_mode":"bl"},"bl")):
            with patch("app.preferences.transaction") as tx:
                cursor = tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
                cursor.fetchone.return_value = row
                self.assertEqual(get_preferences(7)["content_mode"], expected)
        for mode in self.expected:
            with patch("app.preferences.transaction") as tx:
                result = set_preferences(7, {"content_mode":mode})
                cursor = tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
                self.assertEqual(cursor.execute.call_args.args[1], (7,mode=="general",mode))
                self.assertEqual(result, {"content_mode":mode,"general_audience":mode=="general"})

    def test_invalid_modes_rejected_before_database(self):
        for mode in ("invalid", "all' OR 1=1", True, 1, [], {}, None):
            with patch("app.preferences.transaction") as tx, self.assertRaises(ValueError):
                set_preferences(7, {"content_mode":mode})
            tx.assert_not_called()
        for mode in ("invalid", "GENERAL", [], True):
            with self.assertRaises(ValueError): content_mode(mode)
            with patch("app.guest.repo.list_books") as books, self.assertRaises(RequestError):
                guest.public_get("books", {"content_mode":mode})
            books.assert_not_called()

    def test_guest_search_and_series_use_explicit_mode(self):
        for mode in self.expected:
            with visibility_scope(mode): expected_sql = visible_book_sql()
            with patch("app.guest.repo.list_books", side_effect=lambda *a: {"policy":visible_book_sql(),"items":[]}) as books:
                result = guest.public_get("books", {"content_mode":mode,"general":"0"})
                self.assertEqual(result["policy"], expected_sql)
                self.assertEqual(books.call_args.args[0], {})
            with patch("app.guest.transaction") as tx:
                cursor = tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
                cursor.fetchall.return_value = []
                guest.resolve_series({"content_mode":mode,"series":[{"publisher":"test","series_key":"key","media_type":"novel"}]})
                self.assertIn(expected_sql, cursor.execute.call_args.args[0])

    def test_public_cache_separates_all_four_modes(self):
        guard = ResourceGuard(clock=lambda:0, max_wait=0)
        loads = []
        for mode in self.expected:
            for owner in ("one", "two"):
                data = guard.public(owner, "books", {"content_mode":mode},
                    lambda m=mode: loads.append(m) or {"mode":m})
                self.assertEqual(data["mode"], mode)
        self.assertEqual(loads, list(self.expected))
        self.assertEqual(guard.snapshot()["cache_hits"], 4)

    def test_authenticated_scope_ignores_query_override_but_keeps_backup_unfiltered(self):
        handler = object.__new__(Handler)
        for mode in self.expected:
            with visibility_scope(mode): expected = visible_book_sql()
            observed = []
            with patch.object(handler, "_visible_authenticated_get", side_effect=lambda *a: observed.append(visible_book_sql())):
                handler._authenticated_get(urlparse('/api/books?content_mode=all&general=0'), {"content_mode":mode})
                handler._authenticated_get(urlparse('/api/export.json'), {"content_mode":mode})
            self.assertEqual(observed, [expected, '1 = 1'])


if __name__ == "__main__": unittest.main()
