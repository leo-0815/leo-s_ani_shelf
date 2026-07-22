from __future__ import annotations

import unittest
from unittest.mock import patch

from app.catalog_sync import (
    book_record_from_sync,
    compare_catalog_manifests,
    ingest_catalog_books,
)


class CatalogSyncTests(unittest.TestCase):
    def sample(self) -> dict[str, object]:
        return {
            "publisher_code": "kadokawa",
            "source_key": "product-123",
            "title": "Sample novel 1",
            "media_type": "novel",
            "source_url": "https://example.com/product-123",
            "release_date": "2026-07-22",
            "release_precision": "day",
            "release_status": "available",
            "content_rating": "general",
            "rating_raw": "general",
            "rating_source": "publisher",
            "rating_confidence": 100,
        }

    def test_builds_catalog_only_record(self) -> None:
        record = book_record_from_sync({**self.sample(), "user_id": 999})
        self.assertEqual(record.publisher_code, "kadokawa")
        self.assertEqual(record.release_date.isoformat(), "2026-07-22")
        self.assertFalse(hasattr(record, "user_id"))

    def test_rejects_non_catalog_media_and_unsafe_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "media_type"):
            book_record_from_sync({**self.sample(), "media_type": "game"})
        with self.assertRaisesRegex(ValueError, "http"):
            book_record_from_sync({**self.sample(), "source_url": "file:///secret"})

    @patch("app.catalog_sync.upsert_book", side_effect=["inserted", "unchanged"])
    def test_upload_validates_then_marks_sync_origin(self, upsert_book) -> None:
        result = ingest_catalog_books([self.sample(), self.sample()])
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(upsert_book.call_args.kwargs["change_origin"], "sync_upload")

    def test_manifest_diff_is_keyed_by_publisher_and_source(self) -> None:
        result = compare_catalog_manifests(
            [
                {"publisher_code": "a", "source_key": "1", "source_hash": "same"},
                {"publisher_code": "a", "source_key": "2", "source_hash": "old"},
                {"publisher_code": "a", "source_key": "3", "source_hash": "local"},
            ],
            [
                {"publisher_code": "a", "source_key": "1", "source_hash": "same"},
                {"publisher_code": "a", "source_key": "2", "source_hash": "new"},
                {"publisher_code": "b", "source_key": "4", "source_hash": "remote"},
            ],
        )
        self.assertEqual(result["same"], 1)
        self.assertEqual(result["different"], [["a", "2"]])
        self.assertEqual(result["local_only"], [["a", "3"]])
        self.assertEqual(result["remote_only"], [["b", "4"]])


if __name__ == "__main__":
    unittest.main()
