import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch
from app.series_follow import set_follow, add_discovered_book, validate_scope
from app.notifications import collect_email_events, collect_events


class SeriesFollowTests(unittest.TestCase):
    def test_scope_validation(self):
        self.assertEqual(validate_scope("all"), "all")
        with self.assertRaises(ValueError):
            validate_scope("bad")

    def test_all_series_keeps_type_and_account_isolation_and_preserves_wishes(self):
        cursor=MagicMock()
        cursor.rowcount=3
        self.assertEqual(set_follow(cursor,42,2,"work","novel","作品",True,"all"),3)
        sql,values=cursor.execute.call_args.args
        self.assertIn("INSERT IGNORE",sql)
        self.assertIn("b.media_type=%s",sql)
        self.assertIn("collection_items",sql)
        self.assertNotIn("release_status='scheduled'",sql)
        self.assertEqual(values,[42,2,"work","novel",42])

    def test_future_scope_only_adds_scheduled_books(self):
        cursor=MagicMock()
        set_follow(cursor,42,2,"work","manga","作品",True)
        self.assertIn("release_status='scheduled'",cursor.execute.call_args.args[0])

    def test_stop_following_never_removes_owned_or_existing_wishlist(self):
        cursor=MagicMock()
        set_follow(cursor,42,2,"work","manga","作品",False)
        self.assertEqual(cursor.execute.call_count,1)
        self.assertIn("DELETE FROM followed_series",cursor.execute.call_args.args[0])

    def test_new_discovery_respects_scope_and_media_type(self):
        cursor=MagicMock()
        add_discovered_book(cursor,9,2,"work","novel","available")
        sql,values=cursor.execute.call_args.args
        self.assertIn("fs.follow_scope='all' OR %s='scheduled'",sql)
        self.assertIn("fs.media_type=%s",sql)
        self.assertEqual(values,(9,2,"work","novel","available",9))

    @patch("app.notifications.transaction")
    def test_email_notification_query_suppresses_old_backfill_and_owned_books(self,transaction):
        cursor=transaction.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value=[]
        collect_email_events(datetime(2026,10,1),today=date(2026,10,3))
        sql,values=cursor.execute.call_args.args
        self.assertIn("b.series_key = fs.normalized_series",sql)
        self.assertIn("b.media_type = fs.media_type",sql)
        self.assertIn("b.first_seen_at >= fs.created_at",sql)
        self.assertIn("b.release_date >= %s",sql)
        self.assertIn("collection_items",sql)
        self.assertEqual(values[0],date(2026,9,3))
