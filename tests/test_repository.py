from __future__ import annotations

import unittest
from datetime import date

from app.repository import merge_recommendation_rows, resolve_purchased_date, search_terms


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


if __name__ == "__main__":
    unittest.main()
