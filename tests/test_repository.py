from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from app.repository import (
    _crawler_write_data,
    _sync_write_data,
    merge_recommendation_rows,
    resolve_purchased_date,
    search_terms,
    set_book_rating,
)


class RepositorySearchTests(unittest.TestCase):
    def test_purchase_date_defaults_once_and_clears_outside_collection(self) -> None:
        today = date(2026, 8, 24)
        existing = date(2026, 8, 1)
        self.assertEqual(resolve_purchased_date("purchased", None, None, today), today)
        self.assertEqual(resolve_purchased_date("purchased", None, existing, today), existing)
        self.assertEqual(
            resolve_purchased_date("purchased", date(2026, 7, 2), existing, today),
            date(2026, 7, 2),
        )
        self.assertIsNone(resolve_purchased_date("wanted", existing, existing, today))

    def test_search_terms_are_partial_and_space_separated(self) -> None:
        self.assertEqual(search_terms("  月刊少女　野崎  "), ["月刊少女", "野崎"])

    def test_search_terms_normalize_width_and_case(self) -> None:
        self.assertEqual(search_terms("ＷＩＮＤ Breaker"), ["wind", "breaker"])

    def test_recommendations_prioritize_series_and_deduplicate_books(self) -> None:
        common = {
            "id": 20,
            "title": "測試作品 2",
            "author": "作者甲",
            "release_status": "scheduled",
            "release_date": "2026-08-01",
            "volume_label": "2",
            "seed_volume_label": "1",
            "seed_title": "測試作品 1",
        }
        items = merge_recommendation_rows(
            [{**common, "recommendation_type": "same_series"}],
            [{**common, "recommendation_type": "same_author"}],
            10,
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["recommendation_types"], ["same_author", "same_series"])
        self.assertIn("同系列", items[0]["recommendation_reason"])
        self.assertGreater(items[0]["recommendation_score"], 110)

    def test_recommendations_respect_limit(self) -> None:
        rows = [
            {
                "id": book_id,
                "title": f"書 {book_id}",
                "author": "作者",
                "release_status": "available",
                "release_date": None,
                "volume_label": str(book_id),
                "seed_volume_label": "1",
                "seed_title": "種子書",
                "recommendation_type": "same_series",
            }
            for book_id in range(1, 5)
        ]
        self.assertEqual(len(merge_recommendation_rows(rows, [], 2)), 2)

    def test_followed_series_is_a_recommendation_source(self) -> None:
        followed = {
            "id": 30,
            "title": "追蹤系列新書",
            "author": "作者乙",
            "release_status": "scheduled",
            "release_date": "2026-09-01",
            "volume_label": "3",
            "seed_volume_label": None,
            "seed_title": "追蹤系列",
            "recommendation_type": "followed_series",
        }
        items = merge_recommendation_rows([], [], 10, [followed])

        self.assertEqual(items[0]["recommendation_types"], ["followed_series"])
        self.assertIn("追蹤了", items[0]["recommendation_reason"])
        self.assertGreater(items[0]["recommendation_score"], 95)

    def test_locked_manual_rating_survives_crawler_updates(self) -> None:
        existing = {
            "rating_locked": True,
            "content_rating": "restricted_18",
            "rating_raw": "管理員確認",
            "rating_source": "manual",
            "rating_confidence": 100,
        }
        incoming = {
            "content_rating": "general",
            "rating_raw": "普遍級",
            "rating_source": "publisher",
            "rating_confidence": 100,
            "title": "更新後書名",
        }
        merged = _crawler_write_data(existing, incoming)
        self.assertEqual(merged["content_rating"], "restricted_18")
        self.assertEqual(merged["rating_source"], "manual")
        self.assertEqual(merged["title"], "更新後書名")

    def test_unlocked_rating_accepts_publisher_update(self) -> None:
        incoming = {
            "content_rating": "restricted_18",
            "rating_source": "publisher",
        }
        self.assertEqual(
            _crawler_write_data({"rating_locked": False}, incoming),
            incoming,
        )

    def test_sync_merge_does_not_erase_richer_existing_metadata(self) -> None:
        existing = {
            "rating_locked": False,
            "author": "Known author",
            "isbn": "9780000000000",
            "release_date": "2026-07-01",
            "release_status": "available",
            "content_rating": "guidance_15",
            "rating_confidence": 100,
        }
        incoming = {
            "author": None,
            "isbn": None,
            "release_date": None,
            "release_status": "unknown",
            "content_rating": "unknown",
            "rating_confidence": 0,
        }
        merged = _sync_write_data(existing, incoming)
        self.assertEqual(merged["author"], "Known author")
        self.assertEqual(merged["isbn"], "9780000000000")
        self.assertEqual(merged["release_status"], "available")
        self.assertEqual(merged["content_rating"], "guidance_15")

    def test_manual_rating_emits_catalog_change(self) -> None:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {
            "id": 8,
            "source_hash": "a" * 64,
            "content_rating": "unknown",
            "rating_raw": None,
            "rating_source": "unknown",
            "rating_confidence": 0,
            "rating_locked": False,
        }
        manager = MagicMock()
        manager.__enter__.return_value = connection
        with patch("app.repository.transaction", return_value=manager):
            changed = set_book_rating(
                8,
                "restricted_18",
                raw_label="管理員確認",
            )

        self.assertTrue(changed)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("UPDATE books SET content_rating" in sql for sql in statements))
        self.assertTrue(any("INSERT INTO catalog_changes" in sql for sql in statements))

    def test_manual_rating_rejects_unknown_values(self) -> None:
        with self.assertRaises(ValueError):
            set_book_rating(8, "adult")


if __name__ == "__main__":
    unittest.main()
