from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.database_check import run_database_smoke_test


class DatabaseSmokeTestTests(unittest.TestCase):
    @patch("app.database_check.list_series")
    @patch("app.database_check.connect")
    def test_reports_database_identity_and_catalog_counts(
        self,
        connect: MagicMock,
        list_series: MagicMock,
    ) -> None:
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"version": "TiDB-v8", "database_name": "anishelf"},
            {"count": 6},
            {"count": 9220},
            {"with_series_title": 8000, "with_series_key": 7990},
            {"id": 2},
        ]
        connection = connect.return_value
        connection.cursor.return_value.__enter__.return_value = cursor
        list_series.return_value = {"total": 1234, "items": [{"series_title": "A"}]}

        result = run_database_smoke_test()

        self.assertEqual(result["database"], "anishelf")
        self.assertEqual(result["publishers"], 6)
        self.assertEqual(result["books"], 9220)
        self.assertEqual(result["books_with_series_key"], 7990)
        self.assertEqual(result["series_query_total"], 1234)
        self.assertEqual(cursor.execute.call_count, 5)
        list_series.assert_called_once_with({}, 2, limit=1)
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
