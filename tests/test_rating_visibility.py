"""Non-BL/non-R18 policy across publishers and per-recipient notifications."""
import sqlite3
import unittest
from app.preferences import (visibility_scope, visible_book_sql,
                             visible_publisher_sql, notification_visibility_sql)
from app.sources.product_rating import PARSER_VERSIONS

class RatingVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE publishers(id INTEGER,code TEXT,name TEXT);
            CREATE TABLE books(id INTEGER,publisher_id INTEGER,content_rating TEXT,
                rating_source TEXT,rating_confidence INTEGER,rating_checked_at TEXT,
                rating_parser_version TEXT,rating_locked INTEGER,bl_category TEXT);
            CREATE TABLE users(id INTEGER,role TEXT);
            CREATE TABLE user_preferences(user_id INTEGER,general_audience INTEGER);
            INSERT INTO users VALUES(1,'user'),(2,'admin'),(3,'user'),(4,'admin');
            INSERT INTO user_preferences VALUES(3,0),(4,1);
        """)
        for pub_id,(code,version) in enumerate(PARSER_VERSIONS.items(),1):
            self.db.execute("INSERT INTO publishers VALUES(?,?,?)",(pub_id,code,code))
            for offset,(rating,source,confidence,stamp,parser,locked) in enumerate([
                ("general","publisher",100,"2026-10-09",version,0),
                ("restricted_18","publisher",100,"2026-10-09",version,0),
                ("unknown","unknown",0,"2026-10-09",version,0),
                ("general","publisher",95,"2026-10-09",version,0),
                ("general","publisher",100,None,None,0),
                ("general","publisher",100,"2026-10-09","wrong_parser",0),
                ("general","manual",100,None,None,1),
                ("general","manual",100,None,None,0),
                ("guidance_12","publisher",100,"2026-10-09",version,0),
            ]):
                self.db.execute("INSERT INTO books VALUES(?,?,?,?,?,?,?,?,?)",
                    (pub_id*10+offset,pub_id,rating,source,confidence,stamp,parser,locked,
                     "BL漫畫" if offset == 6 else None))
        self.allowed = [(i*10+j,) for i in range(1,6) for j in (0,2,3,4,5,7,8)]
        self.addCleanup(self.db.close)

    def test_unknown_and_guidance_visible_but_bl_general_and_r18_hidden_for_every_alias(self):
        with visibility_scope(True):
            for alias in ("b","c","seed"):
                query=f"SELECT {alias}.id FROM books {alias} LEFT JOIN publishers p ON p.id={alias}.publisher_id WHERE "
                for sql in (visible_book_sql(alias),visible_publisher_sql(book_alias=alias)):
                    self.assertEqual(self.db.execute(query+sql+" ORDER BY 1").fetchall(),self.allowed)
            self.assertEqual(self.db.execute("SELECT id FROM books WHERE "+visible_book_sql("")+" ORDER BY 1").fetchall(),self.allowed)
        self.assertEqual(visible_book_sql(),"1 = 1")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM books").fetchone()[0],45)

    def test_notification_policy_is_per_recipient_and_admin_preference_unchanged(self):
        for user_id in (1,2,3,4):
            rows=self.db.execute("SELECT b.id FROM books b CROSS JOIN users u WHERE u.id=? AND "+
                                 notification_visibility_sql()+" ORDER BY b.id",(user_id,)).fetchall()
            self.assertEqual(rows,self.allowed if user_id in (1,4) else self.db.execute("SELECT id FROM books ORDER BY id").fetchall())
        with visibility_scope(True):
            # Scheduler never inherits whichever request was last served.
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM books b CROSS JOIN users u WHERE u.id=2 AND "+
                                             notification_visibility_sql()).fetchone()[0],45)

    def test_coverage_aggregate_executes_real_sql(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        from tests.test_release_dates import Connection
        from app.repository import rating_coverage
        self.db.row_factory=sqlite3.Row
        @contextmanager
        def tx(): yield Connection(self.db)
        with patch("app.repository.transaction",tx):
            rows=rating_coverage()
        self.assertEqual(len(rows),5)
        for row in rows:
            self.assertEqual((row["bl"], row["general_audience_visible"]), (1, 7))
            self.assertEqual((row["total"],row["confirmed_general"],row["restricted"],row["unknown"],row["needs_confirmation"]),
                             (9,2,1,1,4))
