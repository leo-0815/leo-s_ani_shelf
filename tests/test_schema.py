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


if __name__ == "__main__":
    unittest.main()
