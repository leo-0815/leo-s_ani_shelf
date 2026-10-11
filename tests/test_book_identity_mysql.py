"""Opt-in MySQL check using only connection-local temporary tables."""
import os
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(os.getenv("ANISHELF_TEST_LOCAL_MYSQL") == "1", "opt-in local MySQL test")
class IdentityMySQLTests(unittest.TestCase):
    def test_schedule_product_alias_roundtrip_without_real_catalog_writes(self):
        from app.config import load_dotenv, get_settings
        from app.db import connect
        from app.models import BookRecord
        from app.repository import BOOK_FIELDS, upsert_book
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
        settings = get_settings()
        self.assertIn(settings.db_host, {"127.0.0.1", "localhost"}, "Never run against a cloud DB")
        connection = connect(settings)
        @contextmanager
        def temporary_transaction():
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        try:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMPORARY TABLE publishers (id BIGINT PRIMARY KEY, code VARCHAR(40))")
                cursor.execute("INSERT INTO publishers VALUES (1, 'chingwin')")
                types = {"release_date": "DATE", "release_checked_at": "DATETIME", "list_price": "INT",
                         "rating_confidence": "INT", "rating_checked_at": "DATETIME"}
                columns = ", ".join(f"{f} {types.get(f, 'TEXT')}" for f in BOOK_FIELDS)
                cursor.execute("CREATE TEMPORARY TABLE books (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
                               f"publisher_id BIGINT, source_key VARCHAR(190), {columns}, "
                               "rating_locked BOOLEAN DEFAULT FALSE, last_seen_at DATETIME, "
                               "UNIQUE KEY(publisher_id,source_key)) ENGINE=InnoDB")
                cursor.execute("CREATE TEMPORARY TABLE series_aliases (publisher_id BIGINT, alias_key VARCHAR(190), "
                               "canonical_key VARCHAR(190), approved BOOLEAN)")
                # Exercise the actual new schema's keys/types, without FK links
                # to real tables or any persistent DDL.
                schema = (Path(__file__).resolve().parents[1] / "app/schema.sql").read_text(encoding="utf-8")
                ddl = next(s for s in schema.split(';') if 'CREATE TABLE IF NOT EXISTS book_source_aliases (' in s)
                ddl = ddl.replace('CREATE TABLE IF NOT EXISTS', 'CREATE TEMPORARY TABLE')
                ddl = '\n'.join(line for line in ddl.splitlines() if 'CONSTRAINT ' not in line)
                ddl = ddl.replace('KEY idx_book_aliases_book (book_id),', 'KEY idx_book_aliases_book (book_id)')
                cursor.execute(ddl)
                cursor.execute("CREATE TEMPORARY TABLE catalog_changes (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
                               "book_id BIGINT, change_type VARCHAR(30), source_hash CHAR(64), "
                               "change_origin VARCHAR(30), changed_fields VARCHAR(500))")
                cursor.execute("CREATE TEMPORARY TABLE release_history (book_id BIGINT, field_name VARCHAR(40), "
                               "old_value TEXT, new_value TEXT)")
                cursor.execute("CREATE TEMPORARY TABLE collection_items (book_id BIGINT, notes TEXT)")
            with patch("app.repository.transaction", temporary_transaction), patch("app.series_follow.add_discovered_book"):
                schedule = BookRecord("chingwin", "old-key", "SQL 測試(02)", "manga",
                                      "https://www.ching-win.com.tw/about-news-detail/208", author="作者、繪者",
                                      release_date=date(2030, 10, 1), release_precision="month")
                self.assertEqual(upsert_book(schedule), "inserted")
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id FROM books")
                    book_id = cursor.fetchone()["id"]
                    cursor.execute("INSERT INTO collection_items VALUES (%s, 'keep')", (book_id,))
                connection.commit()
                product = replace(schedule, source_key="10500002", author="作者",
                                  source_url="https://www.ching-win.com.tw/product-detail/10500002",
                                  release_date=date(2030, 11, 5), release_precision="day", cover_url="https://example.test/cover.jpg")
                self.assertEqual(upsert_book(product), "updated")
                self.assertEqual(upsert_book(schedule, change_origin="sync_upload"), "unchanged")
                with connection.cursor() as cursor:
                    cursor.execute("SELECT * FROM books")
                    rows = cursor.fetchall()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["id"], book_id)
                    self.assertEqual(rows[0]["source_key"], product.source_key)
                    self.assertEqual(rows[0]["release_date"], product.release_date)
                    self.assertEqual(rows[0]["author"], schedule.author)
                    cursor.execute("SELECT COUNT(*) AS n FROM book_source_aliases WHERE book_id = %s", (book_id,))
                    self.assertEqual(cursor.fetchone()["n"], 2)
                    cursor.execute("SELECT notes FROM collection_items WHERE book_id = %s", (book_id,))
                    self.assertEqual(cursor.fetchone()["notes"], "keep")
        finally:
            connection.rollback()
            connection.close()  # All temporary tables disappear, even on failure.
