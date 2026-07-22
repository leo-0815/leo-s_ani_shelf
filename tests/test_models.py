from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.models import (
    BookRecord,
    detect_edition,
    extract_volume,
    infer_series_title,
    infer_status,
    normalize_content_rating,
    normalize_text,
)


class ModelTests(unittest.TestCase):
    def test_normalize_full_width_and_whitespace(self) -> None:
        self.assertEqual(normalize_text("  ＳＰＹ　×  FAMILY  "), "SPY × FAMILY")

    def test_edition_volume_and_series(self) -> None:
        title = "小林家的龍女僕(17)限定版"
        self.assertEqual(detect_edition(title), "limited")
        self.assertEqual(extract_volume(title), "17")
        self.assertEqual(infer_series_title(title), "小林家的龍女僕")

    def test_series_handles_trailing_number_and_period(self) -> None:
        self.assertEqual(extract_volume("浪人劍客 15."), "15")
        self.assertEqual(infer_series_title("浪人劍客 15."), "浪人劍客")

    def test_series_handles_vol_and_collector_labels(self) -> None:
        title = "BLUE ARCHIVE OFFICIAL ARTWORKS 蔚藍檔案美術設定集Vol.2"
        self.assertEqual(extract_volume(title), "2")
        self.assertEqual(
            infer_series_title(title),
            "BLUE ARCHIVE OFFICIAL ARTWORKS 蔚藍檔案美術設定集",
        )
        self.assertEqual(
            infer_series_title("【完全版】新世紀福音戰士 系列 【漫畫】"),
            "新世紀福音戰士 系列",
        )

    def test_volume_before_parenthesized_edition(self) -> None:
        title = "辣妹與辣妹的百合 1（首刷限定版）"
        self.assertEqual(extract_volume(title), "1")
        self.assertEqual(infer_series_title(title), "辣妹與辣妹的百合")
        self.assertEqual(extract_volume("ONE PIECE~航海王~114"), "114")
        self.assertEqual(infer_series_title("ONE PIECE~航海王~114"), "ONE PIECE~航海王")
        self.assertEqual(extract_volume("完全版 12完(首刷書盒版)"), "12")

    def test_release_status(self) -> None:
        self.assertEqual(infer_status(date.today() - timedelta(days=1), "day"), "available")
        self.assertEqual(infer_status(date.today() + timedelta(days=1), "day"), "scheduled")
        self.assertEqual(infer_status(None, "unknown"), "unknown")

    def test_record_hash_is_stable(self) -> None:
        record = BookRecord(
            publisher_code="demo",
            source_key="1",
            title="作品(01)",
            media_type="manga",
            source_url="https://example.test/1",
        )
        self.assertEqual(record.prepared()["source_hash"], record.prepared()["source_hash"])

    def test_explicit_publisher_rating_is_normalized(self) -> None:
        record = BookRecord(
            publisher_code="demo",
            source_key="adult-1",
            title="作品",
            media_type="manga",
            source_url="https://example.test/adult-1",
            rating_raw="限制級",
        )
        prepared = record.prepared()
        self.assertEqual(prepared["content_rating"], "restricted_18")
        self.assertEqual(prepared["rating_source"], "publisher")
        self.assertEqual(prepared["rating_confidence"], 100)

    def test_rating_normalizer_does_not_guess_from_unrecognized_text(self) -> None:
        self.assertEqual(normalize_content_rating("戀愛喜劇"), "unknown")
        self.assertEqual(normalize_content_rating("未滿18歲不得購買"), "restricted_18")


if __name__ == "__main__":
    unittest.main()
