"""Opt-in, rollback-only MySQL smoke test using session-local temporary tables."""
import os
import unittest
from pathlib import Path
from contextlib import contextmanager, ExitStack
from datetime import date, datetime, timedelta
from unittest.mock import patch


@unittest.skipUnless(os.getenv("ANISHELF_TEST_LOCAL_MYSQL") == "1", "opt-in local MySQL test")
class CollectionMySQLTests(unittest.TestCase):
    def test_full_flow_without_changing_real_user_data(self):
        from app.config import load_dotenv, get_settings
        from app.db import connect
        from app.models import BookRecord
        from app.repository import (upsert_book, set_series_follow, set_wishlist, delete_wishlist,
                                    get_book, list_series, get_series, list_recommendations, stats)
        from app.collection import (set_owned,list_collection,save_custom,get_custom,remove_owned)
        from app.notifications import collect_email_events
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
        settings=get_settings()
        self.assertIn(settings.db_host,{"127.0.0.1","localhost"}, "Never run this smoke test against a cloud DB")
        connection=connect(settings)
        @contextmanager
        def local_transaction():
            yield connection
        try:
            with connection.cursor() as cursor:
                for table in ("books","collection_items","catalog_changes","release_history","recommendation_dismissals",
                              "wishlist_items","followed_series"):
                    cursor.execute(f"CREATE TEMPORARY TABLE _smoke_{table} LIKE {table}")
                    cursor.execute(f"ALTER TABLE _smoke_{table} RENAME TO {table}")
                for table in ("wishlist_items","followed_series","recommendation_dismissals"):
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN user_id BIGINT UNSIGNED NOT NULL DEFAULT 0")
                cursor.execute("ALTER TABLE wishlist_items DROP PRIMARY KEY, ADD PRIMARY KEY(user_id,book_id)")
                cursor.execute("ALTER TABLE recommendation_dismissals DROP PRIMARY KEY, ADD PRIMARY KEY(user_id,book_id)")
                cursor.execute("ALTER TABLE followed_series DROP PRIMARY KEY, ADD PRIMARY KEY(user_id,publisher_id,normalized_series,media_type)")
                cursor.execute("CREATE TEMPORARY TABLE users(id BIGINT PRIMARY KEY,email VARCHAR(200),display_name VARCHAR(100),role VARCHAR(20),is_active BOOLEAN)")
                cursor.execute("INSERT INTO users VALUES(1,'admin@example.test','admin','admin',TRUE),(2,'reader@example.test','reader','user',TRUE)")
                cursor.execute("CREATE TEMPORARY TABLE notification_preferences(user_id BIGINT,email_enabled BOOLEAN,lead_days VARCHAR(100),notify_release_date_changes BOOLEAN,notify_followed_series BOOLEAN)")
                cursor.execute("INSERT INTO notification_preferences VALUES(1,TRUE,'7,3,1,0',TRUE,TRUE)")
            with ExitStack() as stack:
                for module in ("app.repository","app.collection","app.notifications"):
                    stack.enter_context(patch(module+".transaction",local_transaction))
                title="__verify_collection_pr26__"
                for key,media,release in (("v1","novel",date.today()+timedelta(days=7)),("v2","novel",date(2020,1,1)),("m1","manga",date.today()+timedelta(days=7))):
                    upsert_book(BookRecord(publisher_code="spp",source_key=key,title=title+" "+key,series_title=title,media_type=media,author="驗證作者",release_date=release,release_precision="day",source_url="https://example.test/"+key))
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id,source_key FROM books")
                    ids={row["source_key"]:row["id"] for row in cursor.fetchall()}
                self.assertEqual(set_series_follow(1,"spp",title,"novel",True,"future"),1)
                self.assertEqual(set_series_follow(1,"spp",title,"novel",True,"all"),1)
                set_wishlist(1,ids["v2"],"preordered","keep me",None,priority=3)
                set_series_follow(1,"spp",title,"novel",True,"all")
                self.assertEqual(get_book(ids["v2"],1)["wishlist_notes"],"keep me")
                set_owned(1,ids["v2"],{"paid_price":200})
                delete_wishlist(1,ids["v2"])
                self.assertTrue(get_book(ids["v2"],1)["is_owned"])
                self.assertFalse(get_book(ids["v2"],2)["is_owned"])
                self.assertEqual(list_collection(2,{})["total"],0)
                custom_id=save_custom(1,{"title":"私人測試書"})
                with self.assertRaises(KeyError):
                    get_custom(2,custom_id)
                self.assertEqual(list_collection(1,{})["total"],2)
                self.assertEqual(stats(1)["purchased"],2)
                series=list_series({},1,100)
                self.assertEqual(series["total"],2)
                owned_series=[row for row in series["items"] if row["media_type"]=="novel"][0]
                self.assertEqual(owned_series["owned_count"],1)
                self.assertEqual(len(get_series(1,"spp",title,"novel")["items"]),2)
                # MySQL forbids reopening temporary tables in self-joins. Validate the
                # recommendation SQL read-only against real catalog/ownership tables.
                with connection.cursor() as cursor:
                    cursor.execute("ALTER TABLE books RENAME TO _smoke_books")
                    cursor.execute("ALTER TABLE collection_items RENAME TO _smoke_collection_items")
                try:
                    self.assertIn("items",list_recommendations(1))
                finally:
                    with connection.cursor() as cursor:
                        cursor.execute("ALTER TABLE _smoke_books RENAME TO books")
                        cursor.execute("ALTER TABLE _smoke_collection_items RENAME TO collection_items")
                events=collect_email_events(datetime(2000,1,1))
                self.assertFalse(any(event.book_id==ids["v2"] for event in events), "Old/owned backfill must not send new-book notices")
                set_series_follow(1,"spp",title,"novel",False)
                self.assertEqual(list_collection(1,{})["total"],2)
                remove_owned(2,custom_id)
                self.assertEqual(get_custom(1,custom_id)["title"],"私人測試書")
        finally:
            connection.rollback()
            connection.close()
