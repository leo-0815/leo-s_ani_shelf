from __future__ import annotations

import unittest

from app.backup import _record_from_export


class BackupTests(unittest.TestCase):
    def test_export_row_can_be_rebuilt_as_book_record(self) -> None:
        record = _record_from_export(
            {
                "publisher_code": "tongli",
                "source_key": "TEST001",
                "title": "測試作品 (3)",
                "media_type": "manga",
                "source_url": "https://example.test/book",
                "release_date": "2026-07-16",
                "release_precision": "day",
                "edition_type": "standard",
                "content_rating": "restricted_18",
                "rating_raw": "限制級",
                "rating_source": "publisher",
                "rating_confidence": 100,
            }
        )
        self.assertEqual(record.publisher_code, "tongli")
        self.assertEqual(record.release_date.isoformat(), "2026-07-16")
        self.assertEqual(record.title, "測試作品 (3)")
        self.assertEqual(record.content_rating, "restricted_18")
        self.assertEqual(record.rating_raw, "限制級")


if __name__ == "__main__":
    unittest.main()
