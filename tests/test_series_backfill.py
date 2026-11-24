import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import history_cli, series_backfill as series


class SeriesBackfillTests(unittest.TestCase):
    def test_menu_waits_for_manual_choice(self):
        with patch("builtins.input", return_value="Q"), patch.object(history_cli, "ensure_schema") as db, \
                patch.object(history_cli, "run_history_session") as crawl:
            history_cli.main()
        db.assert_not_called()
        crawl.assert_not_called()

    def test_full_menu_still_dispatches_existing_mode(self):
        with patch("builtins.input", side_effect=["2", "2"]), patch.object(history_cli, "ensure_schema"), \
                patch.object(history_cli, "run_history_session") as crawl:
            history_cli.main()
        crawl.assert_called_once_with("60m")

    def test_official_url_and_isbn_boundaries(self):
        for url in ("https://example.com/categories/x", "https://www.kadokawa.com.tw@other.test/categories/x",
                    "https://www.kadokawa.com.tw/categories/%2e%2e%2fproducts"):
            with self.assertRaises(ValueError):
                series.official_url(url, "categories")
        self.assertTrue(series.valid_isbn("9786264154291"))
        self.assertFalse(series.valid_isbn("9786264154292"))

    def test_lookup_ignores_foreign_products_bundles_and_other_media(self):
        names = ["https://www.kadokawa.com.tw/products/one", "/products/two", "/products/bundle",
                 "https://example.test/products/three"]
        markup = "".join(f'<a href="{url}">示範 (2) {"套書 " if "bundle" in url else ""}NT$250 加入購物車</a>'
                         for url in names)
        def make_book(url, _markup):
            return SimpleNamespace(media_type="novel" if url.endswith("/one") else "manga",
                                   publisher_code="kadokawa", series_title="示範", volume_label="2",
                                   isbn="9786264154291", author="作者", title="示範 (2)", source_url=url)
        row = dict(code="kadokawa", title="示範", series_key=series.series_key("示範", "kadokawa"), media_type="novel")
        with patch.object(series, "polite_pause"), patch.object(series.KadokawaSource, "_parse_detail", side_effect=make_book):
            items, errors, capped = series.lookup(row, "https://www.kadokawa.com.tw/categories/demo",
                                                  load=lambda url, **_: markup)
        self.assertEqual([item.source_url for item in items], [names[0]])
        self.assertEqual(errors, [])
        self.assertFalse(capped)
