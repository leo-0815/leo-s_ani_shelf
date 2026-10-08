"""Execute actual email/Discord collectors; no network or real account data."""
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import date, datetime
from unittest.mock import patch
from tests import test_rating_visibility as visibility_tests
from tests.test_release_dates import Cursor
from app.notifications import collect_email_events, collect_events

class NotifyCursor(Cursor):
    def execute(self,sql,args=()):
        if "information_schema.columns" in sql:
            self.cur.execute("SELECT 1 AS present")
        else:
            super().execute(sql.replace("<=>","IS"),args)

class NotificationAudienceSqlTests(unittest.TestCase):
    def setUp(self):
        visibility_tests.RatingVisibilityTests.setUp(self)
        self.db.row_factory=sqlite3.Row
        self.db.executescript("""
            ALTER TABLE users ADD COLUMN email TEXT;
            ALTER TABLE users ADD COLUMN display_name TEXT;
            ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1;
            UPDATE users SET email='test@example.invalid',display_name='Test';
            CREATE TABLE notification_preferences(user_id INTEGER,email_enabled INTEGER,
                lead_days TEXT,notify_release_date_changes INTEGER,notify_followed_series INTEGER);
            INSERT INTO notification_preferences SELECT id,1,'0',1,1 FROM users;
            ALTER TABLE books ADD COLUMN title TEXT DEFAULT 'Test';
            ALTER TABLE books ADD COLUMN release_date TEXT DEFAULT '2030-10-09';
            ALTER TABLE books ADD COLUMN release_precision TEXT DEFAULT 'day';
            ALTER TABLE books ADD COLUMN release_status TEXT DEFAULT 'scheduled';
            ALTER TABLE books ADD COLUMN first_seen_at TEXT DEFAULT '2030-10-09';
            ALTER TABLE books ADD COLUMN series_key TEXT DEFAULT 'Test';
            ALTER TABLE books ADD COLUMN media_type TEXT DEFAULT 'novel';
            CREATE TABLE wishlist_items(book_id INTEGER,user_id INTEGER,state TEXT);
            INSERT INTO wishlist_items SELECT b.id,u.id,'wanted' FROM books b CROSS JOIN users u;
            CREATE TABLE collection_items(book_id INTEGER,user_id INTEGER);
            CREATE TABLE followed_series(user_id INTEGER,publisher_id INTEGER,
                normalized_series TEXT,media_type TEXT,created_at TEXT);
            INSERT INTO followed_series SELECT u.id,p.id,'Test','novel','2030-10-01'
                FROM users u CROSS JOIN publishers p;
            CREATE TABLE release_history(id INTEGER,book_id INTEGER,field_name TEXT,
                old_value TEXT,new_value TEXT,observed_at TEXT);
            INSERT INTO release_history SELECT id,id,'release_date','2030-10-08','2030-10-09','2030-10-09'
                FROM books;
        """)
        @contextmanager
        def tx(): yield self
        self.patcher=patch("app.notifications.transaction",tx)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
    def cursor(self): return NotifyCursor(self.db)
    def test_email_and_discord_all_three_event_types_share_policy(self):
        email=collect_email_events(datetime(2030,10,8),today=date(2030,10,9))
        for user_id in (1,2,3,4):
            user_events=[e for e in email if e.user_id==user_id]
            self.assertEqual(len(user_events),105 if user_id in (1,4) else 135)
            if user_id in (1,4):
                self.assertEqual({e.book_id for e in user_events},{row[0] for row in self.allowed})
            self.assertEqual({e.event_type for e in user_events},
                             {"release_milestone","release_date_changed","followed_series_new_book"})
        discord=collect_events(datetime(2030,10,8),today=date(2030,10,9),lead_days=(0,))
        self.assertEqual({e.user_id for e in discord},{2,4})
        self.assertEqual(len(discord),240)
