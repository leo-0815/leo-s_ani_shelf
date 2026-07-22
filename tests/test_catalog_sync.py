from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.catalog_sync import CatalogSyncClient, compare_manifests, record_from_peer


class CatalogSyncTests(unittest.TestCase):
    def sample(self) -> dict[str, object]:
        return {
            "publisher_code": "kadokawa",
            "source_key": "book-1",
            "title": "Sample novel",
            "media_type": "novel",
            "source_url": "https://example.com/book-1",
            "release_date": "2026-07-22",
            "release_precision": "day",
            "release_status": "available",
            "content_rating": "general",
            "rating_source": "publisher",
            "rating_confidence": 100,
        }

    def test_peer_record_drops_internal_and_personal_fields(self) -> None:
        record = record_from_peer({**self.sample(), "id": 9, "user_id": 7})
        self.assertEqual(record.publisher_code, "kadokawa")
        self.assertEqual(record.release_date.isoformat(), "2026-07-22")
        self.assertFalse(hasattr(record, "user_id"))

    def test_manifest_comparison_uses_publisher_and_source_identity(self) -> None:
        result = compare_manifests(
            [{"publisher_code": "a", "source_key": "1", "source_hash": "old"}],
            [{"publisher_code": "a", "source_key": "1", "source_hash": "new"}],
        )
        self.assertEqual(result["same"], 0)
        self.assertEqual(result["different"], [["a", "1"]])

    @patch("app.catalog_sync.save_catalog_peer_state")
    @patch("app.catalog_sync.prune_catalog_changes", return_value=2)
    @patch("app.catalog_sync.pending_catalog_changes")
    @patch("app.catalog_sync.get_catalog_peer_state")
    def test_push_skips_cloud_echoes_and_non_book_media(
        self,
        get_state: MagicMock,
        pending: MagicMock,
        prune: MagicMock,
        save_state: MagicMock,
    ) -> None:
        settings = MagicMock(catalog_sync_configured=True)
        client = CatalogSyncClient(settings)
        client._request = MagicMock(
            return_value={
                "protocol_version": 1,
                "accepted": 1,
                "inserted": 1,
                "updated": 0,
                "unchanged": 0,
            }
        )
        get_state.return_value = {"last_pushed_change_id": 0}
        pending.return_value = {
            "items": [
                {**self.sample(), "change_id": 1, "change_origin": "cloud_pull"},
                {**self.sample(), "source_key": "game", "media_type": "game", "change_id": 2, "change_origin": "crawler"},
                {**self.sample(), "source_key": "new", "change_id": 3, "change_origin": "crawler"},
            ],
            "last_scanned_change_id": 3,
            "has_more": False,
        }

        result = client.push()

        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(result["pruned"], 2)
        sent = client._request.call_args.args[2]["items"]
        self.assertEqual([item["source_key"] for item in sent], ["new"])
        self.assertNotIn("change_id", sent[0])
        save_state.assert_called_with("cloud", last_pushed_change_id=3, error=None)


if __name__ == "__main__":
    unittest.main()
