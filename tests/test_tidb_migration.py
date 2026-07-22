from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal

from app.tidb_migration import (
    Endpoint,
    MigrationError,
    TableResult,
    _ensure_distinct,
    _identifier,
    _upsert_sql,
    digest_rows,
)


class TiDBMigrationTests(unittest.TestCase):
    def test_identifiers_are_restricted(self) -> None:
        self.assertEqual(_identifier("notification_deliveries"), "`notification_deliveries`")
        with self.assertRaises(MigrationError):
            _identifier("books; DROP TABLE users")

    def test_same_database_is_rejected(self) -> None:
        endpoint = Endpoint("example.tidbcloud.com", 4000, "root", "secret", "anishelf")
        with self.assertRaises(MigrationError):
            _ensure_distinct(endpoint, endpoint)

    def test_database_name_is_part_of_identity(self) -> None:
        source = Endpoint("example.tidbcloud.com", 4000, "root", "secret", "source")
        target = Endpoint("example.tidbcloud.com", 4000, "root", "secret", "target")
        _ensure_distinct(source, target)

    def test_endpoint_label_never_contains_password(self) -> None:
        endpoint = Endpoint("example.tidbcloud.com", 4000, "root", "top-secret", "anishelf")
        self.assertEqual(endpoint.label, "root@example.tidbcloud.com:4000/anishelf")
        self.assertNotIn("top-secret", endpoint.label)

    def test_upsert_updates_every_source_column(self) -> None:
        sql = _upsert_sql("books", ["id", "title"])
        self.assertIn("INSERT INTO `books` (`id`, `title`)", sql)
        self.assertIn("`id`=VALUES(`id`)", sql)
        self.assertIn("`title`=VALUES(`title`)", sql)

    def test_digest_is_stable_and_sensitive_to_values(self) -> None:
        rows = [
            {
                "id": 1,
                "name": "角川",
                "price": Decimal("120.00"),
                "published": date(2026, 7, 22),
                "updated": datetime(2026, 7, 22, 8, 15),
            }
        ]
        columns = ["id", "name", "price", "published", "updated"]
        first = digest_rows(rows, columns)
        second = digest_rows(rows, columns)
        changed = digest_rows([{**rows[0], "name": "青文"}], columns)
        self.assertEqual(first, second)
        self.assertNotEqual(first[1], changed[1])

    def test_table_result_requires_digest_match_when_present(self) -> None:
        self.assertTrue(TableResult("books", 10, 10).matches)
        self.assertTrue(TableResult("books", 10, 10, "abc", "abc").matches)
        self.assertFalse(TableResult("books", 10, 9, "abc", "abc").matches)
        self.assertFalse(TableResult("books", 10, 10, "abc", "def").matches)


if __name__ == "__main__":
    unittest.main()
