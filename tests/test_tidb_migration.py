from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.tidb_migration import (
    Endpoint,
    MigrationError,
    TABLE_ORDER,
    TableResult,
    _ensure_distinct,
    _identifier,
    _upsert_sql,
    digest_rows,
    ensure_target_schema,
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

    @patch("app.tidb_migration._schema_statements", return_value=["CREATE TABLE one (id INT)"])
    @patch("app.tidb_migration._connect")
    def test_ensure_target_schema_creates_database_and_tables(
        self, connect: MagicMock, schema_statements: MagicMock
    ) -> None:
        admin_connection = MagicMock()
        database_connection = MagicMock()
        connect.side_effect = [admin_connection, database_connection]
        admin_cursor = admin_connection.cursor.return_value.__enter__.return_value
        database_cursor = database_connection.cursor.return_value.__enter__.return_value
        endpoint = Endpoint("target.example", 4000, "root", "secret", "anishelf")

        ensure_target_schema(endpoint)

        self.assertEqual(connect.call_count, 2)
        connect.assert_any_call(endpoint, include_database=False)
        connect.assert_any_call(endpoint)
        self.assertIn("CREATE DATABASE IF NOT EXISTS `anishelf`", admin_cursor.execute.call_args.args[0])
        database_cursor.execute.assert_called_once_with("CREATE TABLE one (id INT)")
        admin_connection.commit.assert_called_once_with()
        database_connection.commit.assert_called_once_with()
        schema_statements.assert_called_once_with()

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

    def test_catalog_sync_tables_are_migrated_after_books(self) -> None:
        self.assertIn("catalog_changes", TABLE_ORDER)
        self.assertIn("catalog_sync_state", TABLE_ORDER)
        self.assertGreater(TABLE_ORDER.index("catalog_changes"), TABLE_ORDER.index("books"))


if __name__ == "__main__":
    unittest.main()
