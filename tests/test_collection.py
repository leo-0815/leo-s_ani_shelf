from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from app.collection import ownership_payload, migrate_collection, save_owned, remove_owned, list_collection, decorate_owned
from app.repository import delete_wishlist


class CollectionTests(unittest.TestCase):
    def test_optional_purchase_data(self):
        data = ownership_payload({})
        self.assertEqual(data["owned_format"], "paper")
        self.assertIsNone(data["paid_price"])
        self.assertIsNone(data["purchased_at"])

    def test_invalid_purchase_data(self):
        for payload in ({"owned_format": "bad"}, {"paid_price": -1}, {"purchased_at": "bad"}):
            with self.assertRaises(ValueError):
                ownership_payload(payload)

    def test_migration_copies_before_reset_and_is_idempotent(self):
        cursor = MagicMock()
        migrate_collection(cursor, cloud=True)
        calls = cursor.execute.call_args_list
        self.assertIn("INSERT IGNORE", calls[0].args[0])
        self.assertIn("SELECT user_id", calls[0].args[0])
        self.assertIn("UPDATE wishlist_items", calls[1].args[0])

    def test_save_existing_catalog_is_account_scoped(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"id": 8}
        save_owned(cursor, 42, 8, {"paid_price": "100"})
        sql, values = cursor.execute.call_args.args
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertEqual(values[:2], (42, 8))
        self.assertIn(100, values)

    def test_missing_catalog_book_rejected(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        with self.assertRaises(KeyError):
            save_owned(cursor, 42, 999, {})

    @patch("app.collection.transaction")
    def test_remove_owned_never_deletes_wishlist(self, transaction):
        cursor = transaction.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        remove_owned(42, 7)
        sql, values = cursor.execute.call_args.args
        self.assertIn("AND user_id=%s", sql)
        self.assertEqual(values, (7, 42))
        self.assertNotIn("wishlist", sql)

    @patch("app.repository.transaction")
    def test_remove_heart_never_deletes_collection_or_series(self, transaction):
        cursor = transaction.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        delete_wishlist(42, 8)
        for call in cursor.execute.call_args_list:
            self.assertNotIn("collection_items", call.args[0])
            self.assertNotIn("DELETE FROM followed_series", call.args[0])

    @patch("app.collection.transaction")
    def test_decorate_reads_only_current_user_ownership(self, transaction):
        cursor = transaction.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{"id": 7, "book_id": 8, "user_id": 42}]
        books = decorate_owned([{"id": 8}, {"id": 9}], 42)
        self.assertEqual(cursor.execute.call_args.args[1], [42, 8, 9])
        self.assertTrue(books[0]["is_owned"])
        self.assertFalse(books[1]["is_owned"])

    @patch("app.collection.transaction")
    def test_collection_empty_page_still_scoped(self, transaction):
        cursor = transaction.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"total": 0}
        cursor.fetchall.return_value = []
        self.assertEqual(list_collection(42, {})["total"], 0)
        for call in cursor.execute.call_args_list:
            self.assertIn("c.user_id = %s", call.args[0])
            self.assertEqual(call.args[1][0], 42)
