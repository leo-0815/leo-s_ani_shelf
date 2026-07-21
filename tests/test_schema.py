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


if __name__ == "__main__":
    unittest.main()
