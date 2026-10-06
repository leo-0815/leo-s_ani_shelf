import sqlite3
import unittest
from app.preferences import visibility_scope, visible_book_sql, visible_publisher_sql

class RatingVisibilityTests(unittest.TestCase):
    def test_per_book_fail_closed_and_aliases(self):
        db = sqlite3.connect(":memory:")
        db.executescript("""
            CREATE TABLE publishers(id INTEGER,code TEXT);
            INSERT INTO publishers VALUES(1,'chingwin'),(2,'spp');
            CREATE TABLE books(id INTEGER,publisher_id INTEGER,content_rating TEXT,
                rating_source TEXT,rating_confidence INTEGER);
            INSERT INTO books VALUES
              (1,1,'general','publisher',100),
              (2,1,'restricted_18','publisher',100),
              (3,1,'unknown','unknown',0),
              (4,1,'general','publisher',95),
              (5,1,'general','unknown',100),
              (6,1,'general','manual',100),
              (7,2,'unknown','unknown',0),
              (8,2,'restricted_18','publisher',100),
              (9,NULL,NULL,NULL,NULL),
              (10,1,'parental_12','publisher',100);
        """)
        try:
            with visibility_scope(True):
                for alias in ("b","c","seed"):
                    sql = f"SELECT {alias}.id FROM books {alias} LEFT JOIN publishers p ON p.id={alias}.publisher_id WHERE "
                    expected = [(1,),(6,),(7,),(9,)]
                    self.assertEqual(db.execute(sql+visible_book_sql(alias)+" ORDER BY 1").fetchall(),expected)
                    self.assertEqual(db.execute(sql+visible_publisher_sql(book_alias=alias)+" ORDER BY 1").fetchall(),expected)
                self.assertEqual(db.execute("SELECT id FROM books WHERE "+visible_book_sql("")+" ORDER BY id").fetchall(),expected)
            self.assertEqual(visible_book_sql(),"1 = 1")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM books WHERE "+visible_book_sql("")).fetchone()[0],10)
        finally:
            db.close()
