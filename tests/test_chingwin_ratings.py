import unittest
from unittest.mock import patch
from app.sources.chingwin_rating import parse_product_rating, product_identity
from app.sources.chingwin import ChingWinSource
from app.models import BookRecord
from app.repository import _crawler_write_data


class ChingwinRatingTests(unittest.TestCase):
    url = "https://www.ching-win.com.tw/product-detail/ABC1"
    def html(self, label="普", key="ABC1"):
        return f"<div>產品編號:{key}</div><div>級別：{label}</div><div>開本：32開</div><div>定價 NT$200</div>"
    def test_explicit_labels(self):
        for label, expected in [("普","general"),("18限","restricted_18"),("待定","unknown")]:
            self.assertEqual(parse_product_rating(self.url,self.html(label),"ABC1"),(expected,label))
    def test_wrong_edition_foreign_host_and_missing_spec(self):
        self.assertEqual(parse_product_rating(self.url,self.html(key="ABC1A")),("unknown",None))
        self.assertEqual(parse_product_rating(self.url,self.html(),"ABC1A"),("unknown",None))
        self.assertIsNone(product_identity("https://evil.example/product-detail/ABC1"))
        self.assertEqual(parse_product_rating(self.url,"<p>本篇试閱不含18禁內容</p>"),("unknown",None))
    def test_conflicting_labels_not_whitelisted(self):
        h=self.html().replace("開本", "級別：18限 開本")
        self.assertEqual(parse_product_rating(self.url,h),("unknown",None))
    def test_related_item_not_used(self):
        h=self.html("待定")+self.html("普","OTHER")
        self.assertEqual(parse_product_rating(self.url,h)[0],"unknown")
    @patch("app.sources.chingwin.polite_pause")
    @patch("app.sources.chingwin.fetch_html")
    def test_source_enrichment_and_failure(self, fetch, pause):
        book=BookRecord(publisher_code="chingwin",source_key="ABC1",title="test",media_type="novel",source_url=self.url)
        source=ChingWinSource()
        fetch.return_value=self.html()
        self.assertEqual(source._with_product_rating(book).prepared()["content_rating"],"general")
        fetch.side_effect=RuntimeError("HTTP error")
        self.assertEqual(source._with_product_rating(book).content_rating,"unknown")
        self.assertEqual(len(source.errors),1)
    def test_unknown_and_manual_lock_preserve_existing(self):
        existing=dict(content_rating="restricted_18",rating_raw="18限",rating_source="publisher",rating_confidence=100)
        data=dict(content_rating="unknown",rating_raw=None,rating_source="unknown",rating_confidence=0)
        self.assertEqual(_crawler_write_data(existing,data)["content_rating"],"restricted_18")
        self.assertEqual(_crawler_write_data(dict(existing,rating_locked=True),dict(data,content_rating="general"))["content_rating"],"restricted_18")

