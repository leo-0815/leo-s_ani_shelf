"""Opt-in integration test; session-local temporary tables, never real books."""
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

@unittest.skipUnless(os.getenv("ANISHELF_TEST_LOCAL_MYSQL") == "1", "local MySQL opt-in")
class RatingMySQLTests(unittest.TestCase):
    def test_atomic_checkpoint_rollback_and_manual_lock(self):
        from app.config import load_dotenv, get_settings
        from app.db import connect
        from app import rating_enrichment as r
        base = Path(__file__).resolve().parents[1]
        load_dotenv((base.parent if base.name == ".notification-worktree" else base) / ".env")
        settings = get_settings()
        self.assertIn(settings.db_host, {"localhost", "127.0.0.1"})
        conn = connect(settings)
        @contextmanager
        def tx():
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        try:
            with conn.cursor() as cur:
                for table in ("books", "catalog_changes"):
                    cur.execute(f"CREATE TEMPORARY TABLE _rating_{table} LIKE {table}")
                    cur.execute(f"ALTER TABLE _rating_{table} RENAME TO {table}")
                cur.execute(r.CHECK_TABLE_SQL.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMPORARY TABLE"))
                cur.execute("SELECT id FROM publishers WHERE code='chingwin'")
                pub = cur.fetchone()["id"]
                for i in range(1, 4):
                    cur.execute("INSERT INTO books (id,publisher_id,source_key,title,normalized_title,"
                        "source_url,source_hash,rating_locked) VALUES (%s,%s,%s,'test','test',"
                        "'https://www.ching-win.com.tw/product-detail/X',%s,%s)",
                        (i,pub,f"rating-test-{i}","a"*64,i==3))
            conn.commit()
            with patch.object(r, "transaction", tx):
                self.assertTrue(r.save_result(1, "general", "普"))
                with patch.object(r, "_record_catalog_change", side_effect=RuntimeError("rollback test")):
                    with self.assertRaises(RuntimeError):
                        r.save_result(2, "general", "普")
                self.assertFalse(r.save_result(3, "general", "普"))
            with conn.cursor() as cur:
                cur.execute("SELECT id,content_rating FROM books ORDER BY id")
                self.assertEqual([row["content_rating"] for row in cur.fetchall()],
                                 ["general","unknown","unknown"])
                cur.execute("SELECT book_id,result FROM book_rating_checks ORDER BY book_id")
                self.assertEqual(cur.fetchall(), [
                    {"book_id":1,"result":"confirmed"},{"book_id":3,"result":"locked"}])
        finally:
            conn.rollback()
            conn.close()
