from __future__ import annotations

from typing import Any


def validate_scope(scope: str) -> str:
    if scope not in {"future", "all"}:
        raise ValueError("無效的系列追蹤範圍")
    return scope


def set_follow(cursor: Any, user_id: int, publisher_id: int, key: str, media: str,
               title: str, following: bool, scope: str = "future", *, cloud: bool = True) -> int:
    validate_scope(scope)
    if media not in {"novel", "manga", "unknown", "mixed"}:
        raise ValueError("請指定漫畫或輕小說類型")
    user_col = "user_id, " if cloud else ""
    user_marks = "%s, " if cloud else ""
    user_values = [user_id] if cloud else []
    if not following:
        cursor.execute("DELETE FROM followed_series WHERE " + ("user_id=%s AND " if cloud else "") +
                       "publisher_id=%s AND normalized_series=%s AND media_type=%s",
                       [*user_values, publisher_id, key, media])
        return 0
    cursor.execute(
        f"INSERT INTO followed_series ({user_col}publisher_id,normalized_series,media_type,series_title,follow_scope) "
        f"VALUES ({user_marks}%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
        "series_title=VALUES(series_title),follow_scope=VALUES(follow_scope)",
        [*user_values, publisher_id, key, media, title[:500], scope],
    )
    # Ignore duplicates: never reset preorders, notes or priorities. Already-owned books need no buying reminder.
    status = "AND b.release_status='scheduled' " if scope == "future" else "AND b.release_status <> 'cancelled' "
    cursor.execute(
        f"INSERT IGNORE INTO wishlist_items ({user_col}book_id,state,follow_series) "
        f"SELECT {user_marks}b.id,'wanted',FALSE FROM books b WHERE b.publisher_id=%s "
        "AND b.series_key=%s AND b.media_type=%s " + status +
        "AND NOT EXISTS(SELECT 1 FROM collection_items c WHERE c.book_id=b.id AND c.user_id=%s)",
        [*user_values, publisher_id, key, media, user_id],
    )
    return int(cursor.rowcount)


def follow_for_book(cursor: Any, user_id: int, book_id: int, following: bool,
                    scope: str = "future", *, cloud: bool = True) -> int:
    from .series import series_key
    cursor.execute("SELECT publisher_id,series_title,series_key,media_type FROM books WHERE id=%s", (book_id,))
    book = cursor.fetchone()
    if not book or not book["series_title"]:
        return 0
    key = book["series_key"] or series_key(book["series_title"])
    return set_follow(cursor,user_id,book["publisher_id"],key,book["media_type"],book["series_title"],
                      following,scope,cloud=cloud)


def add_discovered_book(cursor: Any, book_id: int, publisher_id: int, key: str,
                        media: str, release_status: str, *, cloud: bool = True) -> None:
    if not key or release_status == "cancelled":
        return
    cursor.execute(
        "INSERT IGNORE INTO wishlist_items " +
        ("(user_id,book_id,state,follow_series) SELECT fs.user_id," if cloud else "(book_id,state,follow_series) SELECT ") +
        "%s,'wanted',FALSE FROM followed_series fs WHERE fs.publisher_id=%s AND fs.normalized_series=%s "
        "AND fs.media_type=%s AND (fs.follow_scope='all' OR %s='scheduled') "
        "AND NOT EXISTS(SELECT 1 FROM collection_items c WHERE c.book_id=%s AND c.user_id=" +
        ("fs.user_id)" if cloud else "0)"),
        (book_id,publisher_id,key,media,release_status,book_id),
    )
