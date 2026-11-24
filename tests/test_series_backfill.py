import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import history_cli, series_backfill as series


class SeriesBackfillTests(unittest.TestCase):
    def test_spp_old_single_book_keeps_exact_series_validation(self):
        row = dict(code="spp", title="示範", series_key=series.series_key("示範", "spp"), media_type="manga")
        book = series.BookRecord("spp", "sku", "示範 (1)", "manga", "https://www.spp.com.tw/SalePage/Index/9",
                                 isbn="4711228584253", series_title="示範", volume_label="1")
        with patch.object(series, "polite_pause"), patch.object(series.SppSource, "_parse_detail",
                                                               autospec=True, return_value=(book, None)) as parse:
            results, errors, _ = series.spp_lookup(row, [book.source_url], load=lambda url, **_: "")
            self.assertEqual(results, [book])
            self.assertEqual(errors, [])
            self.assertEqual(parse.call_args.args[0]._catalog_cutoff, series.date.min)

    def test_selected_upload_skips_an_isbn_already_under_another_cloud_key(self):
        first = SimpleNamespace(publisher_code="kadokawa", source_key="a", isbn="9786263211193")
        second = SimpleNamespace(publisher_code="kadokawa", source_key="b", isbn="9786263215313")
        sent = []
        def request(method, path, body=None):
            if method == "GET":
                return {"items": [{"publisher_code": "kadokawa", "source_key": "old-a", "isbn": first.isbn}]}
            sent.extend(body["items"])
            return {"accepted": 1}
        rows = [{"publisher_code": "kadokawa", "source_key": b.source_key, "isbn": b.isbn,
                 "private_note": "should not leave local"} for b in (first, second)]
        with patch("app.repository.catalog_rows_by_keys", return_value=rows) as read:
            result = series.upload_selected([first, second], SimpleNamespace(_request=request))
        self.assertEqual(result, {"accepted": 1, "skipped": 1})
        read.assert_called_once_with([("kadokawa", "b")])
        self.assertEqual([x["source_key"] for x in sent], ["b"])
        self.assertNotIn("private_note", sent[0])

    def test_kadokawa_variant_uses_only_individual_verified_isbn(self):
        row = dict(code="kadokawa", title="示範", series_key=series.series_key("示範", "kadokawa"), media_type="novel")
        data = {"title_translations": {"zh-hant": "示範 系列 【小說】"},
                "summary_translations": {"zh-hant": "上市日期：2024/07/25"},
                "variations": [{"gtin": "9786263211193", "fields_translations": {"zh-hant": ["1"]}},
                               {"gtin": "9786264154292", "fields_translations": {"zh-hant": ["2"]}},
                               {"gtin": "9786263215313", "fields_translations": {"zh-hant": ["1~2"]}}]}
        markup = "app.value('product', JSON.parse('" + json.dumps(json.dumps(data))[1:-1] + "'));"
        result = series.kadokawa_variants(row, "https://www.kadokawa.com.tw/products/demo", markup)
        self.assertEqual([(book.volume_label, book.source_key, book.release_date) for book in result],
                         [("1", "9786263211193", None)])
        row["media_type"] = "manga"
        self.assertEqual(series.kadokawa_variants(row, "", markup), [])

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
