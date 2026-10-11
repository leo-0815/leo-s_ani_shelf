import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch, MagicMock
from tests.test_book_identity import IdentitySqlTests
from tests.test_release_dates import Connection
from app.repository import upsert_book
from app import catalog_reconciliation as merger


class ReconciliationTests(unittest.TestCase):
    rows = IdentitySqlTests.rows
    assert_consistent_hash = IdentitySqlTests.assert_consistent_hash

    def setUp(self):
        IdentitySqlTests.setUp(self)
        self.db.executescript("""
        ALTER TABLE books ADD COLUMN first_seen_at TIMESTAMP DEFAULT '2026-01-01 00:00:00';
        CREATE TABLE catalog_book_merges(old_book_id INTEGER PRIMARY KEY,canonical_book_id INTEGER,
          old_book_json TEXT,canonical_before_json TEXT,references_json TEXT,aliases_json TEXT,canonical_after_hash TEXT);
        CREATE TABLE wishlist_items(user_id INTEGER,book_id INTEGER,state TEXT,notes TEXT,PRIMARY KEY(user_id,book_id));
        CREATE TABLE collection_items(id INTEGER PRIMARY KEY,user_id INTEGER,book_id INTEGER,notes TEXT,paid_price INTEGER,UNIQUE(user_id,book_id));
        CREATE TABLE recommendation_dismissals(user_id INTEGER,book_id INTEGER,reason_type TEXT,PRIMARY KEY(user_id,book_id));
        CREATE TABLE notification_deliveries(id INTEGER PRIMARY KEY,channel TEXT,event_key TEXT,book_id INTEGER,UNIQUE(channel,event_key));
        CREATE TABLE book_release_checks(book_id INTEGER PRIMARY KEY,last_error TEXT);
        CREATE TABLE book_rating_checks(book_id INTEGER PRIMARY KEY,result TEXT,uploaded_at TIMESTAMP,parser_version TEXT);
        """)
        @contextmanager
        def transaction():
            try:
                yield Connection(self.db)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        self.real_reference_tables = merger._reference_tables
        self.patches.extend([patch("app.catalog_reconciliation.transaction", transaction),
                             patch("app.catalog_reconciliation._reference_tables", return_value=list(merger.REFERENCE_TABLES))])
        for p in self.patches[-2:]: p.start()
        upsert_book(replace(self.schedule, author="other author"))
        upsert_book(self.product)
        self.old, self.canonical = [r["id"] for r in self.rows()]
        self.db.execute("UPDATE books SET author=? WHERE id=?", (self.schedule.author, self.old))
        self.db.commit()

    def tearDown(self):
        IdentitySqlTests.tearDown(self)

    def pair(self):
        return merger.preview()["pairs"][0]

    def test_merge_preserves_references_notes_product_cover_and_pending_upload(self):
        self.db.execute("INSERT INTO wishlist_items VALUES(7,?,'wanted','private-note')", (self.old,))
        self.db.execute("INSERT INTO collection_items VALUES(91,8,?,'keep ownership',123)", (self.old,))
        self.db.execute("INSERT INTO recommendation_dismissals VALUES(7,?,'all')", (self.old,))
        self.db.execute("INSERT INTO notification_deliveries VALUES(1,'email',?,?)",
                        (f"release:7:{self.old}:2030-11-05:3", self.old))
        self.db.execute("INSERT INTO book_rating_checks VALUES(?,'classified',NULL,'v1')", (self.old,))
        self.db.execute("INSERT INTO book_rating_checks VALUES(?,'unknown',NULL,'v1')", (self.canonical,))
        self.db.commit()
        pair = self.pair()
        self.assertNotIn("private-note", str(pair))
        self.assertEqual(merger.apply_pairs([pair]), {"merged": 1, "blocked": []})
        row = self.rows()[0]
        self.assertEqual(row["id"], self.canonical)
        self.assertEqual(row["cover_url"], self.product.cover_url)
        self.assertEqual(row["release_date"], self.product.release_date)
        self.assertEqual(row["author"], self.schedule.author)
        self.assert_consistent_hash(row)
        self.assertEqual(self.db.execute("SELECT notes,paid_price FROM collection_items WHERE id=91").fetchone()[:], ("keep ownership",123))
        for table in merger.REFERENCE_TABLES:
            self.assertEqual(self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE book_id=?", (self.old,)).fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT result FROM book_rating_checks").fetchone()[0], "classified")
        self.assertIn(f":{self.canonical}:", self.db.execute("SELECT event_key FROM notification_deliveries").fetchone()[0])
        self.assertEqual(merger.preview()["total_pairs"], 0)
        self.assertNotIn("private-note", str(merger.alias_page()))
        self.assertEqual(merger.alias_page()["items"][0]["aliases"], [self.schedule.source_key])
        with Connection(self.db).cursor() as cursor:
            self.assertEqual(merger.resolve_book_id(cursor,self.old),self.canonical)
        self.assertIn("private-note",self.db.execute("SELECT references_json FROM catalog_book_merges").fetchone()[0])
        upsert_book(self.schedule, change_origin="sync_upload")
        self.assertEqual(len(self.rows()),1)

    def test_conflicting_private_data_blocks_without_partial_mutation(self):
        self.db.executemany("INSERT INTO wishlist_items VALUES(7,?,'wanted',?)", [(self.old,"a"),(self.canonical,"b")])
        self.db.commit()
        before = self.rows()
        result = merger.apply_pairs([self.pair()])
        self.assertEqual(result["blocked"][0]["reason"],"private_conflict")
        self.assertEqual(self.rows(),before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM catalog_book_merges").fetchone()[0],0)

    def test_stale_preview_and_atomic_sql_failure_leave_originals(self):
        pair = self.pair()
        self.db.execute("UPDATE books SET list_price=999 WHERE id=?", (self.canonical,))
        self.db.commit()
        self.assertEqual(merger.apply_pairs([pair])["blocked"][0]["reason"],"stale_preview")
        before = self.rows()
        with patch("app.catalog_reconciliation._move_references", side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError): merger.apply_pairs([self.pair()])
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM catalog_book_merges").fetchone()[0],0)

    def test_distinct_official_skus_make_schedule_ambiguous(self):
        upsert_book(replace(self.product,source_key="other-sku",source_url="https://www.ching-win.com.tw/product-detail/other-sku"))
        self.assertEqual(merger.preview()["total_pairs"],0)
        self.assertEqual(merger.preview()["ambiguous"],1)
        with self.assertRaisesRegex(merger.MergeBlocked,"ambiguous"):
            merger.merge_pair(self.old,self.canonical,None)

    def test_manual_rating_and_explicit_bl_survive_merge(self):
        self.db.execute("UPDATE books SET rating_locked=1,content_rating='restricted_18',rating_raw='manual',rating_source='manual',bl_category='BL' WHERE id=?", (self.old,))
        self.db.commit()
        merger.apply_pairs([self.pair()])
        row=self.rows()[0]
        self.assertEqual((row["rating_locked"],row["content_rating"],row["bl_category"]),(1,"restricted_18","BL"))
        self.assert_consistent_hash(row)

    def test_identical_private_rows_and_duplicate_notification_keys_collapse(self):
        self.db.executemany("INSERT INTO wishlist_items VALUES(7,?,'wanted','same')",[(self.old,),(self.canonical,)])
        self.db.executemany("INSERT INTO notification_deliveries VALUES(?,'email',?,?)",[
            (1,f"followed-series:7:{self.old}",self.old),(2,f"followed-series:7:{self.canonical}",self.canonical)])
        self.db.commit()
        merger.apply_pairs([self.pair()])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM wishlist_items").fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0],1)

    def test_versions_and_invalid_batches_are_never_fuzzy_merged(self):
        self.db.execute("UPDATE books SET title='聖母的制裁(03)',volume_label='3' WHERE id=?", (self.old,))
        self.db.commit()
        self.assertEqual(merger.preview()["total_pairs"],0)
        for rows in ([], [{}], [{}]*51):
            with self.assertRaises(ValueError):merger.apply_pairs(rows)

    def test_unknown_foreign_reference_is_rejected(self):
        cursor=MagicMock()
        cursor.fetchall.side_effect=[[{"table_name":"wishlist_items"}],[{"table_name":"extra_private","column_name":"book_id"}]]
        with self.assertRaisesRegex(merger.MergeBlocked,"unknown_reference"):
            self.real_reference_tables(cursor)

    def test_guest_old_id_refreshes_metadata_but_cannot_bypass_preferences(self):
        from app.guest import public_get
        self.db.execute("ALTER TABLE publishers ADD COLUMN name TEXT")
        merger.apply_pairs([self.pair()])
        with patch("app.guest.transaction",merger.transaction):
            rows=public_get("book-batch",{"ids":str(self.old),"content_mode":"all"})["items"]
            self.assertEqual(rows[0]["id"],self.old)
            self.assertEqual(rows[0]["canonical_book_id"],self.canonical)
            self.assertEqual(rows[0]["cover_url"],self.product.cover_url)
            self.db.execute("UPDATE books SET content_rating='restricted_18' WHERE id=?",(self.canonical,))
            self.db.commit()
            self.assertEqual(public_get("book-batch",{"ids":str(self.old),"content_mode":"general"})["items"],[])
