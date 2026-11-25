import unittest
from email.message import Message
from urllib.error import HTTPError

from app.chingwin_coverage import (
    assert_robots_allowed,
    collect_kingstone,
    match_baseline_row,
    parse_kingstone_detail,
    parse_kingstone_listing,
    parse_ncl_csv,
)
from app.publisher_shadow import Restricted

LISTING_FIXTURE = """
<div class="mod_prod_card shadow-med" data-prodno="2019462329381">
  <h3 class="pdnamebox height2">
    <a href="/basic/2019462329381/">別看我，老師(04)</a>
  </h3>
  <div class="pubdate">2026/11/19&nbsp;出版</div>
</div>
"""


class ChingwinCoverageTest(unittest.TestCase):
    def test_robots_disallowed_paths_rejected(self):
        for bad in (
            "https://www.kingstone.com.tw/api/book/123",
            "https://www.kingstone.com.tw/cart/",
            "https://www.kingstone.com.tw/Refund/apply",
            "https://www.kingstone.com.tw/wp-admin/",
        ):
            with self.assertRaises(Restricted):
                assert_robots_allowed(bad)
        assert_robots_allowed("https://www.kingstone.com.tw/bookpublish/publist/08025/")
        assert_robots_allowed("https://www.kingstone.com.tw/basic/2019462329381/")

    def test_471_gtin_not_treated_as_isbn_and_shipping_date_ignored(self):
        html = """
        <h1>測試漫畫(01)</h1>
        <div>條碼：4711234567890 裝訂：紙本平裝 預計出貨：2026/11/30</div>
        <script>出版日期：2026/11/01</script>
        <div>出版日：2026/11/19</div>
        """
        parsed = parse_kingstone_detail(html)
        self.assertIsNone(parsed["isbn"])
        self.assertEqual(parsed["gtin"], "4711234567890")
        self.assertEqual(parsed["retailer_publication_date"], "2026-11-19")
        self.assertEqual(parsed["media"], "print")

    def test_same_isbn_print_vs_ebook_not_merged(self):
        base_ebook = {
            "title": "測試作品(01)(電子書)",
            "isbn": "9786264499903",
            "edition_type": "digital",
            "media_type": "ebook",
            "volume_label": "1",
        }
        print_candidate = {
            "title": "測試作品(01)",
            "isbn": "9786264499903",
            "media": "print",
            "edition_type": "standard",
            "volume": "1",
            "title_key": "測試作品",
        }
        outcome, matched = match_baseline_row(base_ebook, [print_candidate])
        self.assertEqual(outcome, "miss")
        self.assertIsNone(matched)

    def test_special_vs_standard_not_merged(self):
        base_special = {
            "title": "測試作品(02)特裝版",
            "isbn": None,
            "edition_type": "special",
            "media_type": "manga",
            "volume_label": "2",
        }
        standard_candidate = {
            "title": "測試作品(02)",
            "isbn": "9786260000001",
            "media": "print",
            "edition_type": "standard",
            "volume": "2",
            "title_key": "測試作品",
        }
        outcome, matched = match_baseline_row(base_special, [standard_candidate])
        self.assertEqual(outcome, "miss")
        self.assertIsNone(matched)

    def test_ncl_csv_month_precision_and_publisher_filter(self):
        csv_text = (
            "\ufeff申請書名,作者,出版機構,版次,預訂出版日,ISBN,出版形式,裝訂方式,分級註記\n"
            "測試作品(03),作者A,青文,初版,2026-11-00,978-626-03-0377-8,紙本,平裝,限制級\n"
            "別家作品(01),作者B,東立,初版,2026-11-00,9786260000002,紙本,平裝,\n"
        )
        rows = parse_ncl_csv(csv_text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["planned_ym"], "2026-11")
        self.assertEqual(rows[0]["release_precision"], "month")
        self.assertEqual(rows[0]["isbn"], "9786260303778")
        self.assertEqual(rows[0]["media"], "print")

    def test_listing_fixture_and_challenge_stops_kingstone(self):
        items = parse_kingstone_listing(LISTING_FIXTURE)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["prodno"], "2019462329381")
        self.assertEqual(items[0]["listing_pub_date"], "2026-11-19")

        headers = Message()
        headers["Cf-Mitigated"] = "challenge"

        def challenged(url):
            raise HTTPError(url, 403, "Forbidden", headers, None)

        res = collect_kingstone(load=challenged, pause=lambda _: None)
        self.assertEqual(res["status"], "challenge")
        self.assertEqual(res["listing_pages_fetched"], 0)
        self.assertEqual(res["records"], [])


if __name__ == "__main__":
    unittest.main()
