from __future__ import annotations

import inspect
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from app import repository, collection
from app.filters import add_publisher_filter
from app.preferences import (
    get_preferences, set_preferences, visibility_scope,
    publisher_visible, visible_book_sql, visible_publisher_sql,
)


class PublisherFilterTests(unittest.TestCase):
    def sql(self, filters):
        where, values = [], []
        add_publisher_filter(filters, where, values)
        return " AND ".join(where), values

    def test_all_single_multiple_none_and_duplicates(self):
        self.assertEqual(self.sql({}), ("1 = 1", []))
        self.assertEqual(self.sql({"publisher": "spp"})[1], ["spp"])
        sql, values = self.sql({"publishers": "spp, tongli,spp"})
        self.assertIn("p.code IN (%s, %s)", sql)
        self.assertEqual(values, ["spp", "tongli"])
        self.assertIn("1 = 0", self.sql({"publishers": "none"})[0])
        for count in range(1, 7):
            self.assertEqual(len(self.sql({"publishers": ",".join("p"+str(i) for i in range(count))})[1]), count)

    def test_invalid_inputs_are_rejected_not_interpolated(self):
        for raw in ("spp') OR 1=1 --", ",", "A", "x"*65, ",".join("p"+str(i) for i in range(33))):
            with self.assertRaises(ValueError):
                self.sql({"publishers": raw})

    def test_general_mode_cannot_be_overridden_by_publisher_selection(self):
        with visibility_scope(True):
            sql, values = self.sql({"publishers": "chingwin,spp", "general_audience": "false"})
            self.assertIn("<> 'restricted_18'", sql)
            self.assertIn("bl_category", sql)
            self.assertNotIn("rating_checked_at", sql)
            self.assertEqual(values, ["chingwin", "spp"])

    def test_scope_is_request_local_and_reset_even_after_errors(self):
        def observe(enabled):
            with visibility_scope(enabled):
                return visible_book_sql() == "1 = 1"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(observe, [True, False]*10)), [False, True]*10)
        try:
            with visibility_scope(True):
                raise RuntimeError("request failed")
        except RuntimeError:
            pass
        self.assertTrue(publisher_visible("chingwin"))
        with visibility_scope(True):
            self.assertTrue(publisher_visible("chingwin"))
            self.assertIn("<> 'restricted_18'", visible_book_sql())
            self.assertTrue(publisher_visible("spp"))
            self.assertIn("bl_category", visible_book_sql())

    def test_repository_filters_count_and_page_before_limit(self):
        for fn, filters in (
            (repository.list_books, {"publishers": "spp,tongli"}),
            (repository.list_books, {"publishers": "spp,tongli", "wishlist": "1"}),
            (repository.list_series, {"publishers": "spp,tongli"}),
        ):
            cursor = MagicMock()
            cursor.fetchone.return_value = {"total": 0}
            cursor.fetchall.return_value = []
            with patch("app.repository.transaction") as tx, visibility_scope(True):
                tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cursor
                kwargs = {"limit": 20, "offset": 40}
                if "user_id" in inspect.signature(fn).parameters:
                    kwargs["user_id"] = 7
                fn(filters, **kwargs)
            for call in cursor.execute.call_args_list[:2]:
                sql, params = call.args
                self.assertIn("p.code IN (%s, %s)", sql)
                self.assertIn("<> 'restricted_18'", sql)
                self.assertIn("bl_category", sql)
                self.assertIn("spp", params)
                self.assertIn("tongli", params)
            self.assertEqual(list(cursor.execute.call_args.args[1])[-2:], [20, 40])

    def test_collection_filter_is_before_count_and_limit(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"total": 0}
        cursor.fetchall.return_value = []
        with patch("app.collection.transaction") as tx, visibility_scope(True):
            tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cursor
            collection.list_collection(7, {"publishers": "spp,tongli"}, 20, 40)
        for call in cursor.execute.call_args_list[:2]:
            self.assertIn("bl_category", call.args[0])
            self.assertIn("p.code IN (%s, %s)", call.args[0])
            self.assertEqual(list(call.args[1])[:3], [7, "spp", "tongli"])

    def test_hidden_book_and_series_details_cannot_be_opened_directly(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        with patch("app.repository.transaction") as tx, visibility_scope(True):
            tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cursor
            kwargs = {"user_id": 7} if "user_id" in inspect.signature(repository.get_book).parameters else {}
            self.assertIsNone(repository.get_book(1, **kwargs))
            self.assertIn("bl_category", cursor.execute.call_args.args[0])
            kwargs = {"user_id": 7} if "user_id" in inspect.signature(repository.get_series).parameters else {}
            self.assertIsNone(repository.get_series(publisher_code="chingwin", series_title="test", **kwargs))

    def test_recommendations_filter_candidates_and_prompts_before_limit(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"purchased_count": 1, "followed_count": 1}
        cursor.fetchall.return_value = []
        with patch("app.repository.transaction") as tx, visibility_scope(True):
            tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cursor
            kwargs = {"user_id": 7} if "user_id" in inspect.signature(repository.list_recommendations).parameters else {}
            repository.list_recommendations(**kwargs)
        candidate_sql = [c.args[0] for c in cursor.execute.call_args_list if "LIMIT" in c.args[0]]
        self.assertEqual(len(candidate_sql), 4)
        for sql in candidate_sql:
            self.assertIn("bl_category", sql)
            self.assertLess(sql.index("bl_category"), sql.index("LIMIT"))

    def test_preferences_are_private_persistent_boolean_and_default_on(self):
        with patch("app.preferences.transaction") as tx:
            cursor = tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = None
            self.assertEqual(get_preferences(7), {"general_audience": True, "content_mode": "general"})
            self.assertEqual(cursor.execute.call_args.args[1], (7,))
            self.assertEqual(set_preferences(7, {"general_audience": True}), {"general_audience": True, "content_mode": "general"})
            self.assertEqual(cursor.execute.call_args.args[1], (7, True, "general"))
            cursor.fetchone.return_value = {"general_audience": False}
            self.assertFalse(get_preferences(7)["general_audience"])
            cursor.fetchone.return_value = {"role": "admin", "general_audience": None}
            self.assertFalse(get_preferences(7)["general_audience"])
            cursor.fetchone.return_value = {"role": "admin", "general_audience": True}
            self.assertTrue(get_preferences(7)["general_audience"])
            for bad in ("false", 1, None):
                with self.assertRaises(ValueError):
                    set_preferences(7, {"general_audience": bad})

    def test_default_migration_resets_existing_accounts_once(self):
        from app.preferences import migrate_general_audience_default
        cursor = MagicMock()
        cursor.fetchone.return_value = {"column_default": "0"}
        cursor.rowcount = 1
        self.assertTrue(migrate_general_audience_default(cursor, cloud=True))
        sql = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertTrue(any("SET DEFAULT 1" in q for q in sql))
        self.assertTrue(any("WHERE u.role = 'user'" in q for q in sql))
        self.assertTrue(any("SELECT id, TRUE FROM users" in q for q in sql))
        self.assertTrue(any("WHERE role = 'user'" in q for q in sql))
        cursor.reset_mock()
        cursor.fetchone.return_value = {"column_default": "1"}
        cursor.rowcount = 0
        self.assertFalse(migrate_general_audience_default(cursor, cloud=True))
        sql = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertFalse(any("UPDATE user_preferences" in q or "ALTER TABLE" in q for q in sql))

    def test_local_default_migration_uses_local_profile_only(self):
        from app.preferences import migrate_general_audience_default
        cursor = MagicMock()
        cursor.fetchone.return_value = {"column_default": "1"}
        cursor.rowcount = 1
        self.assertTrue(migrate_general_audience_default(cursor, cloud=False))
        sql = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertTrue(any("VALUES (0, TRUE)" in q for q in sql))
        self.assertFalse(any("FROM users" in q for q in sql))

