import unittest
from email.message import Message
from urllib.error import HTTPError

from app.publisher_shadow import ISBN, OFFICIAL, RETAIL, retail_detail, run


class PublisherShadowTest(unittest.TestCase):
    def test_challenge_stops_only_affected_publisher(self):
        requests = []
        headers = Message()
        headers["Cf-Mitigated"] = "challenge"

        def load(url):
            requests.append(url)
            if "ching-win" in url:
                raise HTTPError(url, 403, "Forbidden", headers, None)
            return "<html>not validated</html>"

        report = run(load=load, pause=lambda _: None)
        self.assertEqual(set(report["sources"]), set(OFFICIAL))
        self.assertEqual(report["sources"]["chingwin"], {"schedule": {"status": "challenge", "http": 403}})
        self.assertEqual(report["sources"]["kadokawa"]["detail"]["status"], "layout_or_evidence_unverified")
        self.assertTrue(all(url not in requests for group in RETAIL.values() for url in group))
        self.assertFalse(report["actual_catalog_write"])

    def test_retailer_publication_is_distinct_from_shipping_and_requires_print(self):
        base = f"<h1>螢火蟲的出嫁(09)</h1>出版社：青文 ISBN：{ISBN} 規格：平裝"
        self.assertIsNone(retail_detail(base + " 預計出貨 2026/09/19"))
        correct = base + "<p>出版日期：2026/09/02</p> 預計出貨 2026/09/19"
        self.assertEqual(retail_detail(correct)["retailer_publication_date"], "2026-09-02")
        self.assertIsNone(retail_detail(correct.replace("規格：平裝", "電子書")))
        self.assertIsNone(retail_detail(correct.replace("出版社：青文", "出版社：別家")))
        self.assertIsNone(retail_detail(base + "<script>出版日：2026/09/02</script>"))


    def test_tongli_markers_match_schedule_table_and_hyphenated_isbn(self):
        def load(url):
            if "Search1.aspx" in url:
                return "<table><tr><th>書名／集數</th><th>作者</th></tr></table>"
            if "BooksDetail.aspx" in url:
                return "<h1>我的幸福婚姻</h1><span>ISBN：978-626-02-7999-8</span>"
            return "<html>not validated</html>"

        report = run(load=load, pause=lambda _: None)
        self.assertEqual(
            report["sources"]["tongli"],
            {
                "schedule": {"status": "sample_verified"},
                "detail": {"status": "sample_verified"},
            },
        )


if __name__ == "__main__":
    unittest.main()
