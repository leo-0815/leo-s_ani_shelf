import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from app.sources.product_rating import parse_rating, rating_fields
from app.rating_merge import merge_rating_fields, peer_rating_time
from app.models import BookRecord, catalog_sync_hash
from app import rating_enrichment as r


def spp(label="普遍級", key="A1", item=1):
    return 'SalePageIndexViewModel"] = ' + json.dumps(dict(
        Title="測試漫畫", Id=item, CategoryLevelName={"Level1_ShopCategory_Name":"漫畫"},
        ShortDescription=f"書 號：{key} 條 碼：9781234567890 等 級：{label}"))


def tongli(label="普遍級"):
    return "<div>漫畫書籍資料</div><div>測試漫畫</div><div>原文書名：測試</div><div>集數：第1集</div><div>作者：某作者 系列別：少年</div><div>圖書分級：" + label + "</div><div>ISBN：9781234567890</div><div>內容簡介</div><div>推薦商品 圖書分級：限制級</div>"


class ProductRatingTests(unittest.TestCase):
    def test_spp_scope_identity_and_conflicts(self):
        url="https://www.spp.com.tw/SalePage/Index/1"
        self.assertEqual(parse_rating("spp",url,spp(),"A1"),("general","普遍級"))
        self.assertEqual(parse_rating("spp",url,spp("限制級"),"A1")[0],"restricted_18")
        for markup,key,isbn in [(spp(), "B1", None), (spp(item=2),"A1",None),
                               (spp("普遍級 等級：限制級"),"A1",None),
                               (spp(),"A1","9780000000000"), (spp("無"),"A1",None)]:
            self.assertEqual(parse_rating("spp",url,markup,key,isbn)[0],"unknown")
        self.assertEqual(parse_rating("spp","https://evil.test/SalePage/Index/1",spp(),"A1")[0],"unknown")

    def test_tongli_scope_and_isbn(self):
        url="https://www.tongli.com.tw/BooksDetail.aspx?Bd=X"
        self.assertEqual(parse_rating("tongli",url,tongli(),"X")[0],"general")
        self.assertEqual(parse_rating("tongli",url,tongli("限制級"),"X")[0],"restricted_18")
        self.assertEqual(parse_rating("tongli",url,tongli(),"Y")[0],"unknown")
        self.assertEqual(parse_rating("tongli",url,tongli(),"X","9780000000000")[0],"unknown")
        self.assertEqual(parse_rating("tongli",url,tongli("普遍級 圖書分級：限制級"),"X")[0],"unknown")
        self.assertEqual(parse_rating("tongli",url,"<p>推薦 圖書分級：普遍級</p>","X")[0],"unknown")

    def test_incremental_detail_uses_shared_grading(self):
        from app.sources.spp import SppSource
        from app.sources.tongli import TongLiSource
        book,_=SppSource()._parse_detail("https://www.spp.com.tw/SalePage/Index/1",spp("限制級"),False)
        self.assertEqual(book.prepared()["content_rating"],"restricted_18")
        book=TongLiSource()._parse_detail("https://www.tongli.com.tw/BooksDetail.aspx?Bd=X",tongli())
        self.assertEqual(book.prepared()["content_rating"],"general")
        self.assertEqual(book.rating_parser_version,"tongli_rating_v1")
        self.assertIsNotNone(book.rating_checked_at)

    def test_confirmation_metadata_does_not_change_hash_version(self):
        values=dict(publisher_code="spp",source_key="A1",title="測試",media_type="manga",
                    source_url="https://www.spp.com.tw/SalePage/Index/1", **rating_fields(
                        "spp","https://www.spp.com.tw/SalePage/Index/1",spp(),"A1"))
        first=BookRecord(**values).prepared()
        values["rating_checked_at"]+=timedelta(days=1)
        second=BookRecord(**values).prepared()
        self.assertEqual(first["source_hash"],second["source_hash"])
        self.assertEqual(catalog_sync_hash(first),catalog_sync_hash(second))

    def test_stale_general_unknown_and_locked_never_downgrade(self):
        old=dict(content_rating="restricted_18", rating_raw="限制級", rating_source="publisher",
                 rating_confidence=100, rating_checked_at=datetime(2026,10,8), rating_parser_version="spp_rating_v1")
        for incoming in [dict(content_rating="general"), dict(content_rating="unknown"),
                         dict(content_rating="general",rating_checked_at=datetime(2026,10,7))]:
            self.assertEqual(merge_rating_fields(old,incoming)["content_rating"],"restricted_18")
        locked=dict(old,rating_locked=True)
        self.assertEqual(merge_rating_fields(locked,dict(content_rating="general",
                          rating_checked_at=datetime(2026,10,9)))["content_rating"],"restricted_18")
        with self.assertRaises(ValueError):
            peer_rating_time(dict(rating_checked_at=(datetime.utcnow()+timedelta(days=1)).isoformat(),rating_parser_version="spp_rating_v1"))

    @patch("app.rating_enrichment.finish", side_effect=lambda counts,*args:counts)
    @patch("app.rating_enrichment.time.sleep")
    @patch("app.rating_enrichment.check_book", return_value=("general","普遍級",None,False))
    @patch("app.rating_enrichment.candidates")
    def test_round_robin_and_global_limit(self,candidates,check,sleep,finish):
        ids={"chingwin":1,"spp":2,"tongli":3}
        candidates.side_effect=lambda limit,keys,**kw:[dict(id=ids[kw["code"]],source_key="X",publisher_code=kw["code"])]
        result=r.run_enrichment(dry_run=True,limit=3,sources=list(ids),batch_size=1)
        self.assertEqual(list(result["publishers"].values()),[1,1,1])
        self.assertEqual(result["checked"],3)

    @patch("app.rating_enrichment.fetch_html")
    def test_invalid_product_url_never_fetched(self,fetch):
        result=r.check_book(dict(id=1,publisher_code="tongli",source_key="X",source_url="http://127.0.0.1/private"),dry_run=True)
        self.assertIsNotNone(result[2])
        fetch.assert_not_called()

