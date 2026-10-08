import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from app.rating_merge import merge_rating_fields
from app.repository import _sync_write_data
from app.models import BookRecord
from app import rating_enrichment as r

class RatingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.utcnow().replace(microsecond=0)
        self.old=BookRecord("chingwin","X","Test","novel",
            "https://www.ching-win.com.tw/product-detail/X",content_rating="general",
            rating_raw="普",rating_source="publisher",rating_confidence=100,
            rating_checked_at=self.now-timedelta(days=1),
            rating_parser_version="chingwin_rating_v2").prepared()
        self.old.update(id=1,publisher_id=1,rating_locked=False)
        self.unknown=dict(self.old,content_rating="unknown",rating_raw=None,
                          rating_source="unknown",rating_confidence=0,rating_checked_at=self.now)

    def test_fresh_ambiguous_page_revokes_general_in_crawler_and_peer_merge(self):
        for fn in (merge_rating_fields,_sync_write_data):
            new=fn(self.old,self.unknown)
            self.assertEqual((new["content_rating"],new["rating_confidence"],new["rating_raw"]),
                             ("unknown",0,None))

    def test_sparse_stale_locked_and_adult_records_remain_protected(self):
        for old,new in [
            (self.old,dict(self.unknown,rating_checked_at=None,rating_parser_version=None)),
            (self.old,dict(self.unknown,rating_checked_at=self.now-timedelta(days=2))),
            (dict(self.old,rating_locked=True),self.unknown),
            (dict(self.old,content_rating="restricted_18"),self.unknown),
        ]:
            self.assertEqual(merge_rating_fields(old,new)["content_rating"],old["content_rating"])

    def test_revocation_is_durable_outbox_and_http_failure_does_not_revoke(self):
        for error in (None,"HTTP 522"):
            cursor=MagicMock()
            cursor.fetchone.return_value=self.old
            with patch.object(r,"transaction") as tx, patch.object(r,"_record_catalog_change"):
                tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value=cursor
                changed=r.save_result(1,"unknown",None,error,checked_at=self.now)
            updates=[c for c in cursor.execute.call_args_list if c.args[0].startswith("UPDATE books")]
            if error:
                self.assertFalse(changed)
                self.assertEqual(updates,[])
            else:
                self.assertTrue(changed)
                self.assertEqual(updates[0].args[1][0],"unknown")
                self.assertEqual(cursor.execute.call_args.args[1][2],"revoked")

    def test_confirmation_only_is_emitted_to_catalog_change_feed(self):
        cursor=MagicMock()
        cursor.fetchone.return_value=self.old
        with patch.object(r,"transaction") as tx, patch.object(r,"_record_catalog_change") as event:
            tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value=cursor
            self.assertFalse(r.save_result(1,"general","普",checked_at=self.now))
        self.assertEqual(event.call_args.args[-1],["rating_confirmation"])
