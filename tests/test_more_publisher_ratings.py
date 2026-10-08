import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch
from app import rating_refresh as r
from app.sources.product_rating import parse_rating
from tests.test_release_dates import Connection

class MorePublisherRatingTests(unittest.TestCase):
    def test_kadokawa_only_product_summary_not_navigation(self):
        url="https://www.kadokawa.com.tw/products/9786263786882"
        key="9786263786882"
        nav="<nav>限制級 R18 普遍級</nav>"
        self.assertEqual(parse_rating("kadokawa",url,nav,key)[0],"unknown")
        self.assertEqual(parse_rating("kadokawa",url,nav+'<h1 class="Product-title">書</h1><p class="Product-summary Product-summary-block">🔞限制級</p>',key)[0],"restricted_18")
        self.assertEqual(parse_rating("kadokawa",url,'<p class="Product-summary">普遍級</p>',key)[0],"general")
        self.assertEqual(parse_rating("kadokawa",url,'<p class="Product-summary">普遍級 限制級</p>',key)[0],"unknown")
        self.assertEqual(parse_rating("kadokawa",url,nav+'<p class="Product-summary">普通介紹</p>',key)[0],"unknown")
    def test_tohan_no_grade_is_not_general(self):
        url="https://www.tohan.com.tw/product.php?act=view&id=1"
        for label,expected in [("無","unknown"),("普遍級","general"),("限制級","restricted_18")]:
            markup=f"<p>作者</p><p>某作者 ISBN 9781234567890 級別：{label}</p><p>相關推薦</p><p>級別：普遍級</p>"
            self.assertEqual(parse_rating("tohan",url,markup,"1")[0],expected)
            self.assertEqual(parse_rating("tohan",url,markup,"1","9780000000000")[0],"unknown")
    def test_budget_executes_real_atomic_sql_and_is_shared(self):
        db=sqlite3.connect(":memory:")
        db.execute("CREATE TABLE rating_refresh_budget (budget_day DATE PRIMARY KEY,attempted INTEGER DEFAULT 0)")
        @contextmanager
        def tx():
            try:
                yield Connection(db)
                db.commit()
            except Exception:
                db.rollback()
                raise
        now=datetime(2026,10,9,0)
        with patch.object(r,"transaction",tx):
            for _ in range(20):
                self.assertTrue(r.claim_daily_budget(now))
            self.assertFalse(r.claim_daily_budget(now))
            self.assertFalse(r.claim_daily_budget(now,1000))
            self.assertTrue(r.claim_daily_budget(now+timedelta(days=1)))
        db.close()
    @patch("app.rating_refresh.time.sleep")
    @patch("app.rating_refresh.transaction")
    @patch("app.rating_refresh.claim_daily_budget",return_value=True)
    @patch("app.rating_refresh.check_book",return_value=("unknown",None,None,False))
    @patch("app.rating_refresh.candidates")
    def test_refresh_hard_global_limit_and_all_sources(self,candidates,check,claim,tx,sleep):
        candidates.side_effect=lambda limit,**kw:[dict(id=1,source_key=kw["code"])]*limit
        result=r.refresh_recent_ratings(limit=100)
        self.assertEqual(result["checked"],20)
        self.assertEqual(claim.call_count,20)
        self.assertEqual({call.kwargs["code"] for call in candidates.call_args_list},
                         {"chingwin","spp","tongli","kadokawa","tohan"})
        self.assertTrue(all(call.kwargs["recent_days"]==90 for call in candidates.call_args_list))

