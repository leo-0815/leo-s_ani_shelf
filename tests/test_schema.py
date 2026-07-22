from __future__ import annotations

import unittest

from app.config import ROOT


class CloudSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = (ROOT / "app" / "schema.sql").read_text(encoding="utf-8")

    def test_personal_tables_are_scoped_by_user(self) -> None:
        self.assertIn("PRIMARY KEY (user_id, book_id)", self.schema)
        self.assertIn("PRIMARY KEY (user_id, publisher_id, normalized_series)", self.schema)
        self.assertIn("fk_recommendation_dismissal_user", self.schema)

    def test_sessions_reference_users_and_store_hashes(self) -> None:
        self.assertIn("token_hash CHAR(64) NOT NULL", self.schema)
        self.assertIn("csrf_token CHAR(64) NOT NULL", self.schema)
        self.assertIn("fk_sessions_user", self.schema)

    def test_notification_deliveries_have_an_idempotency_key(self) -> None:
        self.assertIn("CREATE TABLE IF NOT EXISTS notification_deliveries", self.schema)
        self.assertIn("UNIQUE KEY uq_notification_delivery (channel, event_key)", self.schema)
        self.assertIn("fk_notification_user", self.schema)

    def test_each_user_has_independent_email_preferences(self) -> None:
        self.assertIn("CREATE TABLE IF NOT EXISTS notification_preferences", self.schema)
        self.assertIn("email_enabled BOOLEAN NOT NULL DEFAULT FALSE", self.schema)
        self.assertIn("PRIMARY KEY (user_id)", self.schema)
        self.assertIn("fk_notification_preferences_user", self.schema)

    def test_books_store_explicit_ratings_and_manual_locks(self) -> None:
        self.assertIn("content_rating VARCHAR(30) NOT NULL DEFAULT 'unknown'", self.schema)
        self.assertIn("rating_raw VARCHAR(100) NULL", self.schema)
        self.assertIn("rating_locked BOOLEAN NOT NULL DEFAULT FALSE", self.schema)
        self.assertIn("KEY idx_books_rating (content_rating, rating_locked)", self.schema)

    def test_catalog_changes_provide_an_incremental_sync_cursor(self) -> None:
        self.assertIn("CREATE TABLE IF NOT EXISTS catalog_changes", self.schema)
        self.assertIn("change_origin VARCHAR(30) NOT NULL DEFAULT 'crawler'", self.schema)
        self.assertIn("KEY idx_catalog_changes_origin (change_origin, id)", self.schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS catalog_sync_state", self.schema)
        self.assertIn("last_pulled_change_id BIGINT UNSIGNED NOT NULL DEFAULT 0", self.schema)


if __name__ == "__main__":
    unittest.main()
