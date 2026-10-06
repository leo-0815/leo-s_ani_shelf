import unittest
from unittest.mock import MagicMock, patch
from contextlib import contextmanager
from app import rating_enrichment as r
from app import catalog_sync

class RatingEnrichmentTests(unittest.TestCase):
    def row(self): return dict(id=1,source_key="X",source_url="https://www.ching-win.com.tw/product-detail/X",rating_locked=False,content_rating="unknown",source_hash="old")
    @patch("app.rating_enrichment.finish",side_effect=lambda counts,*args:counts)
    @patch("app.rating_enrichment.time.sleep")
    @patch("app.rating_enrichment.candidates")
    @patch("app.rating_enrichment.save_result",return_value=True)
    @patch("app.rating_enrichment.fetch_html",return_value="<p>產品編號:X 級別：普 定價 NT$100</p>")
    def test_dry_run_never_saves_and_limit_is_bounded(self, fetch,save,candidates,sleep,finish):
        candidates.return_value=[self.row()]
        result=r.run_enrichment(limit=1,dry_run=True,pause_seconds=0)
        self.assertEqual(result["confirmed"],1); save.assert_not_called()
    @patch("app.rating_enrichment.finish",side_effect=lambda counts,*args:counts)
    @patch("app.rating_enrichment.time.sleep")
    @patch("app.rating_enrichment.candidates")
    @patch("app.rating_enrichment.save_result")
    @patch("app.rating_enrichment.fetch_html",side_effect=KeyboardInterrupt)
    @patch("app.rating_enrichment.transaction")
    def test_interrupt_before_write_has_no_checkpoint(self,transaction,fetch,save,candidates,sleep,finish):
        candidates.return_value=[self.row()]
        result=r.run_enrichment(limit=1,pause_seconds=0)
        self.assertTrue(result["interrupted"]); save.assert_not_called()
    def test_update_and_checkpoint_share_transaction_and_locked_rating_is_not_changed(self):
        conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value=dict(self.row(),rating_locked=True,content_rating="restricted_18")
        @contextmanager
        def tx(): yield conn
        with patch.object(r,"transaction",tx):
            self.assertFalse(r.save_result(1,"general","普"))
        sqls=[call.args[0] for call in cur.execute.call_args_list]
        self.assertFalse(any(s.startswith("UPDATE books") for s in sqls))
        self.assertTrue(any("INSERT INTO book_rating_checks" in s for s in sqls))
    def test_http_failure_retains_unknown_and_retry_checkpoint(self):
        conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value=self.row()
        @contextmanager
        def tx(): yield conn
        with patch.object(r,"transaction",tx): self.assertFalse(r.save_result(1,"unknown",None,"HTTP 522"))
        args=cur.execute.call_args.args[1]
        self.assertEqual(args[2],"error");self.assertIsNotNone(args[4])
    @unittest.skipUnless(hasattr(catalog_sync, "CatalogSyncClient"), "local sync client only")
    def test_upload_failure_leaves_outbox_unacknowledged(self):
        conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value=[dict(self.row(),publisher_code="chingwin",rating_checked_at="test",rating_check_attempts=1)]
        @contextmanager
        def tx(): yield conn
        with patch.object(r,"transaction",tx),patch("app.catalog_sync.CatalogSyncClient") as client:
            client.return_value._request.side_effect=RuntimeError("network")
            with self.assertRaises(RuntimeError):r.finish(dict(uploaded=0),[],True)
        self.assertFalse(any("UPDATE book_rating_checks" in c.args[0] for c in cur.execute.call_args_list))
    @unittest.skipUnless(hasattr(catalog_sync, "CatalogSyncClient"), "local sync client only")
    def test_upload_retry_uses_durable_rows_and_acknowledges_after_post(self):
        conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
        cur.fetchall.side_effect=[[dict(self.row(),publisher_code="chingwin",rating_checked_at="test",rating_check_attempts=1)],[]]
        @contextmanager
        def tx(): yield conn
        with patch.object(r,"transaction",tx),patch("app.catalog_sync.CatalogSyncClient") as client:
            client.return_value._request.return_value=dict(accepted=1)
            self.assertEqual(r.finish(dict(uploaded=0),[],True)["uploaded"],1)
        self.assertTrue(any("uploaded_at=NOW()" in c.args[0] for c in cur.execute.call_args_list))

