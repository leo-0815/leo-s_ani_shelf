"""Small, read-only home snapshot; never scans/returns the full catalog."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from .db import transaction
from .preferences import visible_book_sql
from .repository import serialize_row


def home_snapshot(user_id: int = 0, *, cloud: bool = True,
                  today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(timezone(timedelta(hours=8))).date()
    visible = visible_book_sql()
    wishlist_owner = "w.user_id = %s AND " if cloud else ""
    series_owner = "fs.user_id = %s AND " if cloud else ""
    series_match = ("fs.publisher_id = b.publisher_id AND "
                    "fs.normalized_series = b.series_key AND fs.media_type = b.media_type")
    counts_values = (user_id, user_id, user_id) if cloud else (user_id,)
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT (SELECT COUNT(*) FROM wishlist_items w JOIN books b ON b.id=w.book_id "
                f"WHERE {wishlist_owner}{visible}) AS wishlist, "
                "(SELECT COUNT(*) FROM collection_items c LEFT JOIN books b ON b.id=c.book_id "
                f"WHERE c.user_id=%s AND (c.book_id IS NULL OR {visible})) AS collection, "
                f"(SELECT COUNT(*) FROM followed_series fs WHERE {series_owner}EXISTS "
                f"(SELECT 1 FROM books b WHERE {series_match} AND {visible})) AS followed_series",
                counts_values,
            )
            counts = {key: int(value or 0) for key, value in cursor.fetchone().items()}
            cursor.execute(
                "SELECT b.id, b.title, b.author, b.cover_url, b.media_type, b.edition_type, "
                "b.release_date, b.release_precision, b.release_status, p.name AS publisher_name "
                "FROM books b JOIN publishers p ON p.id=b.publisher_id "
                "WHERE b.release_date >= %s AND b.release_date <= %s "
                "AND b.release_precision='day' AND b.release_status NOT IN ('cancelled', 'delayed') "
                f"AND {visible} AND (EXISTS (SELECT 1 FROM wishlist_items w WHERE "
                f"{wishlist_owner}w.book_id=b.id AND w.state <> 'paused') OR EXISTS "
                f"(SELECT 1 FROM followed_series fs WHERE {series_owner}{series_match})) "
                "ORDER BY b.release_date, b.id LIMIT 6",
                (today, today + timedelta(days=7), user_id, user_id) if cloud
                else (today, today + timedelta(days=7)),
            )
            items = [serialize_row(row) for row in cursor.fetchall()]
    return {"counts": counts, "items": items, "date": today.isoformat()}
