import unittest
import sqlite3
from datetime import date, datetime
from unittest.mock import MagicMock, patch
from app.series_follow import set_follow, add_discovered_book, validate_scope
from app.notifications import collect_email_events, collect_events


class SeriesFollowTests(unittest.TestCase):
    def test_large_series_set_insert_has_no_twenty_book_cap(self):
        # Execute the production SELECT against synthetic rows; only MySQL's
        # upsert/ignore spelling is adapted for this dependency-free SQL test.
        for size in (100, 300, 1000):
            with self.subTest(size=size), sqlite3.connect(":memory:") as db:
                db.executescript("""
                    CREATE TABLE books(id INTEGER PRIMARY KEY,publisher_id,series_key,media_type,release_status);
                    CREATE TABLE followed_series(user_id,publisher_id,normalized_series,media_type,series_title,follow_scope,
                        PRIMARY KEY(user_id,publisher_id,normalized_series,media_type));
                    CREATE TABLE wishlist_items(user_id,book_id,state,follow_series,notes,priority,
                        PRIMARY KEY(user_id,book_id));
                    CREATE TABLE collection_items(user_id,book_id);
                    INSERT INTO collection_items VALUES(42,1);
                    INSERT INTO wishlist_items VALUES(42,2,'preordered',0,'KEEP',3);
                """)
                db.executemany("INSERT INTO books VALUES(?,2,'work','novel','available')", [(i,) for i in range(1,size+1)])
                db.execute("INSERT INTO books VALUES(?,2,'work','manga','available')", (size+1,))
                class Cursor:
                    rowcount = 0
                    def execute(self, sql, values):
                        if "ON DUPLICATE KEY" in sql:
                            sql = sql.split(" ON DUPLICATE KEY")[0].replace("INSERT INTO", "INSERT OR REPLACE INTO", 1)
                        sql = sql.replace("INSERT IGNORE", "INSERT OR IGNORE").replace("%s", "?")
                        self.rowcount = db.execute(sql, values).rowcount
                cursor = Cursor()
                self.assertEqual(set_follow(cursor,42,2,"work","novel","作品",True,"all"), size-2)
                self.assertEqual(set_follow(cursor,42,2,"work","novel","作品",True,"all"), 0)
                self.assertEqual(db.execute("SELECT count(*) FROM wishlist_items WHERE user_id=42").fetchone()[0],size-1)
                self.assertEqual(db.execute("SELECT state,notes,priority FROM wishlist_items WHERE book_id=2").fetchone(),("preordered","KEEP",3))
                self.assertEqual(db.execute("SELECT count(*) FROM wishlist_items WHERE user_id<>42 OR book_id=?",(size+1,)).fetchone()[0],0)

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
