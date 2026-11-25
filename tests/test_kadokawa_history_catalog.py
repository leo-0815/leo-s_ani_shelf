import json
import unittest
from html import escape
from unittest.mock import patch

from app.sources.kadokawa import KadokawaSource
from app.sources.kadokawa_catalog import verified_listing


BASE = KadokawaSource.category_urls[1][1]


def listing(page, title="示範", *, slug="known", next_page=False):
    attributes = escape(json.dumps({"title": title}, ensure_ascii=False), quote=True)
    return (
        f'<link rel="canonical" href="{BASE}">'
        '<div class="ProductList-list">'
        f'<product-item product-id="test" href="/products/{slug}">'
        f'<a class="js-quick-cart-item" href="/products/{slug}" ga-product="{attributes}">售完</a>'
        '</product-item></div>'
        '<a class="js-quick-cart-item" href="/products/unrelated">推薦</a>'
        '<ul class="ProductList-paginator pagination">'
        f'<li class="active"><a href="{BASE}?page={page}">{page}</a></li>'
        + (f'<li><a href="{BASE}?page={page+1}"><i class="fa fa-angle-right"></i></a></li>' if next_page else '')
        + '</ul>'
    )


class KadokawaHistoryCatalogTests(unittest.TestCase):
    def test_real_page_marker_and_card_not_dependent_on_purchase_text(self):
        cards, more = verified_listing(BASE, listing(44, next_page=True), 44)
        self.assertEqual([c.title for c in cards], ["示範"])
        self.assertTrue(more)
        self.assertFalse(verified_listing(BASE, listing(45), 45)[1])
        with self.assertRaises(ValueError):
            verified_listing(BASE, listing(44), 45)
        with self.assertRaises(ValueError):
            verified_listing(BASE, '<html>Service unavailable</html>', 1)

    def test_v2_completion_does_not_hide_v3_or_known_variant_parent(self):
        source = KadokawaSource()
        source.category_urls = (("manga", BASE),)
        data = {"title_translations": {"zh-hant": "示範 系列 【漫畫】"},
                "variations": [
                    {"gtin": "9786263211193", "fields_translations": {"zh-hant": ["1"]}},
                    {"gtin": "9786263215313", "fields_translations": {"zh-hant": ["2"]}},
                ]}
        product = "app.value('product', JSON.parse('" + json.dumps(json.dumps(data))[1:-1] + "'));"
        with patch("app.repository.get_backfill_progress", return_value={
                "catalog_v2_manga": {"completed": True, "next_page": 44}}), \
                patch.object(source, "_fetch", side_effect=[listing(1, "示範 系列 【漫畫】"), product]) as fetch, \
                patch.object(source, "_history_isbns", return_value={"9786263211193"}):
            ((rows, checkpoint),) = list(source.collect_batches(known_keys={"known"}, backfill=True))
        self.assertIn("page=1", fetch.call_args_list[0].args[0])
        self.assertIn("limit=24", fetch.call_args_list[0].args[0])
        self.assertEqual([(r.media_type, r.volume_label, r.isbn) for r in rows],
                         [("manga", "2", "9786263215313")])
        self.assertEqual(checkpoint["segment"], "catalog_v3_manga")
        self.assertTrue(checkpoint["completed"])

    def test_repeated_page_does_not_yield_a_completion_checkpoint(self):
        source = KadokawaSource()
        source.category_urls = (("manga", BASE),)
        with patch("app.repository.get_backfill_progress", return_value={}), \
                patch.object(source, "_fetch", side_effect=[listing(1, next_page=True), listing(2)]):
            batches = source.collect_batches(known_keys={"known"}, backfill=True)
            self.assertFalse(next(batches)[1]["completed"])
            with self.assertRaisesRegex(ValueError, "重複商品"):
                next(batches)

    def test_failed_variant_prevents_terminal_checkpoint(self):
        source = KadokawaSource()
        source.category_urls = (("manga", BASE),)
        with patch("app.repository.get_backfill_progress", return_value={}), \
                patch.object(source, "_fetch", side_effect=[listing(1, "示範 系列 【漫畫】"), "<html>error</html>"]):
            ((_, checkpoint),) = list(source.collect_batches(known_keys={"known"}, backfill=True))
        self.assertEqual(checkpoint["error_count"], 1)
        self.assertFalse(checkpoint["completed"])
