from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .db import transaction
from .models import BookRecord, detect_edition, extract_volume, infer_series_title, normalize_text


BOOK_FIELDS = (
    "title",
    "normalized_title",
    "series_title",
    "volume_label",
    "edition_type",
    "media_type",
    "author",
    "isbn",
    "cover_url",
    "list_price",
    "release_date",
    "release_precision",
    "release_status",
    "source_url",
    "source_hash",
)
HISTORY_FIELDS = ("title", "release_date", "release_precision", "release_status")


def serialize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (date, datetime)):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


def get_publisher_id(connection: Any, code: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM publishers WHERE code = %s", (code,))
        row = cursor.fetchone()
    if not row:
        raise ValueError(f"未知出版社：{code}")
    return int(row["id"])


def upsert_book(record: BookRecord) -> str:
    data = record.prepared()
    with transaction() as connection:
        publisher_id = get_publisher_id(connection, record.publisher_code)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM books WHERE publisher_id = %s AND source_key = %s FOR UPDATE",
                (publisher_id, record.source_key),
            )
            existing = cursor.fetchone()
            migrate_source_key = False
            if existing is None and record.publisher_code == "chingwin":
                existing = _find_chingwin_schedule_row(
                    cursor,
                    publisher_id,
                    data,
                )
                migrate_source_key = existing is not None
            if existing is None and record.publisher_code == "tongli":
                existing = _find_tongli_matching_row(cursor, publisher_id, data)
                if existing is not None and _is_tongli_schedule(data):
                    cursor.execute(
                        "UPDATE books SET last_seen_at = CURRENT_TIMESTAMP WHERE id = %s",
                        (existing["id"],),
                    )
                    return "unchanged"
                migrate_source_key = (
                    existing is not None and not _is_tongli_schedule(data)
                )
            if existing is None:
                columns = ", ".join(("publisher_id", "source_key", *BOOK_FIELDS))
                placeholders = ", ".join(["%s"] * (2 + len(BOOK_FIELDS)))
                values = [publisher_id, record.source_key, *[data[field] for field in BOOK_FIELDS]]
                cursor.execute(
                    f"INSERT INTO books ({columns}) VALUES ({placeholders})",
                    values,
                )
                book_id = int(cursor.lastrowid)
                normalized_series = normalize_text(data.get("series_title") or "").casefold()[:190]
                if normalized_series:
                    cursor.execute(
                        "INSERT IGNORE INTO wishlist_items "
                        "(user_id, book_id, state, follow_series) "
                        "SELECT user_id, %s, 'wanted', TRUE FROM followed_series "
                        "WHERE publisher_id = %s AND normalized_series = %s",
                        (book_id, publisher_id, normalized_series),
                    )
                return "inserted"

            if existing["source_hash"] == data["source_hash"]:
                cursor.execute(
                    "UPDATE books SET last_seen_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (existing["id"],),
                )
                return "unchanged"

            for field in HISTORY_FIELDS:
                old = existing.get(field)
                new = data.get(field)
                if old != new:
                    cursor.execute(
                        "INSERT INTO release_history (book_id, field_name, old_value, new_value) "
                        "VALUES (%s, %s, %s, %s)",
                        (existing["id"], field, _as_text(old), _as_text(new)),
                    )
            assignments = ", ".join(f"{field} = %s" for field in BOOK_FIELDS)
            if migrate_source_key:
                assignments = f"source_key = %s, {assignments}"
                values = [
                    record.source_key,
                    *[data[field] for field in BOOK_FIELDS],
                    existing["id"],
                ]
            else:
                values = [*[data[field] for field in BOOK_FIELDS], existing["id"]]
            cursor.execute(
                f"UPDATE books SET {assignments}, last_seen_at = CURRENT_TIMESTAMP WHERE id = %s",
                values,
            )
            return "updated"


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _find_chingwin_schedule_row(
    cursor: Any,
    publisher_id: int,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    """Reconcile old monthly-schedule hashes with stable catalog product codes."""
    cursor.execute(
        "SELECT * FROM books WHERE publisher_id = %s AND normalized_title = %s "
        "AND media_type = %s FOR UPDATE",
        (publisher_id, data["normalized_title"], data["media_type"]),
    )
    candidates = cursor.fetchall()
    compatible: list[dict[str, Any]] = []
    new_date = data.get("release_date")
    for candidate in candidates:
        old_date = candidate.get("release_date")
        if old_date and new_date:
            if candidate.get("release_precision") == "month":
                if (old_date.year, old_date.month) != (new_date.year, new_date.month):
                    continue
            elif old_date != new_date:
                continue
        compatible.append(candidate)
    return compatible[0] if len(compatible) == 1 else None


def _is_tongli_schedule(data: dict[str, Any]) -> bool:
    return "tongli.com.tw/Search1.aspx" in str(data.get("source_url") or "")


def _find_tongli_matching_row(
    cursor: Any,
    publisher_id: int,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    """Join undated official plans to the eventual stable BD product record."""
    cursor.execute(
        "SELECT * FROM books WHERE publisher_id = %s AND media_type = %s "
        "AND (normalized_title = %s OR "
        "(series_title = %s AND COALESCE(volume_label, '') = COALESCE(%s, ''))) "
        "FOR UPDATE",
        (
            publisher_id,
            data["media_type"],
            data["normalized_title"],
            data.get("series_title"),
            data.get("volume_label"),
        ),
    )
    candidates = cursor.fetchall()
    if _is_tongli_schedule(data):
        stable = [
            row
            for row in candidates
            if not str(row.get("source_key") or "").startswith("planned:")
        ]
        return stable[0] if len(stable) == 1 else None
    planned = [
        row
        for row in candidates
        if str(row.get("source_key") or "").startswith("planned:")
    ]
    return planned[0] if len(planned) == 1 else None


def list_books(
    filters: dict[str, str], user_id: int, limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    where: list[str] = []
    values: list[Any] = []
    for term in search_terms(filters.get("q", "")):
        like = f"%{term}%"
        isbn_term = term.replace("-", "") if term.replace("-", "").isalnum() else term
        where.append("(b.title LIKE %s OR b.author LIKE %s OR b.isbn LIKE %s OR b.series_title LIKE %s)")
        values.extend([like, like, f"%{isbn_term}%", like])
    if filters.get("status") == "scheduled_undated":
        where.append("b.release_status = 'scheduled' AND b.release_date IS NULL")
    for key, column in (
        ("publisher", "p.code"),
        ("media_type", "b.media_type"),
        ("edition", "b.edition_type"),
        ("wishlist_state", "w.state"),
    ):
        if filters.get(key):
            where.append(f"{column} = %s")
            values.append(filters[key])
    if filters.get("status") and filters["status"] != "scheduled_undated":
        where.append("b.release_status = %s")
        values.append(filters["status"])
    if filters.get("wishlist") == "1":
        where.append("w.book_id IS NOT NULL")
    if filters.get("date_from"):
        where.append("b.release_date >= %s")
        values.append(date.fromisoformat(filters["date_from"]))
    if filters.get("date_to"):
        where.append("b.release_date <= %s")
        values.append(date.fromisoformat(filters["date_to"]))
    missing = filters.get("missing")
    if missing == "author":
        where.append("(b.author IS NULL OR b.author = '')")
    elif missing == "isbn":
        where.append("(b.isbn IS NULL OR b.isbn = '')")
    elif missing == "date":
        where.append("b.release_date IS NULL")
    elif missing == "type":
        where.append("b.media_type = 'unknown'")
    elif missing == "suspicious_date":
        where.append("b.release_date < '2010-01-01'")
    clause = " WHERE " + " AND ".join(where) if where else ""
    base = (
        " FROM books b JOIN publishers p ON p.id = b.publisher_id "
        "LEFT JOIN wishlist_items w ON w.book_id = b.id AND w.user_id = %s"
    )
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total" + base + clause, [user_id, *values])
            total = int(cursor.fetchone()["total"])
            order_by = {
                "release_asc": "(b.release_date IS NULL), b.release_date ASC, b.title ASC",
                "title_asc": "b.normalized_title ASC, b.release_date DESC",
                "added_desc": "b.first_seen_at DESC, b.release_date DESC",
                "updated_desc": "b.updated_at DESC, b.release_date DESC",
            }.get(
                filters.get("sort"),
                "(b.release_date IS NULL), b.release_date DESC, b.updated_at DESC",
            )
            order_values: list[Any] = []
            if filters.get("q") and not filters.get("sort"):
                normalized_query = normalize_text(filters["q"]).casefold()
                order_by = (
                    "(b.normalized_title = %s) DESC, "
                    "(b.normalized_title LIKE %s) DESC, "
                    "(b.series_title = %s) DESC, "
                    "(b.release_date IS NULL), b.release_date DESC"
                )
                order_values = [normalized_query, f"{normalized_query}%", filters["q"]]
            cursor.execute(
                "SELECT b.*, p.code AS publisher_code, p.name AS publisher_name, "
                "w.state AS wishlist_state, w.notes AS wishlist_notes, w.follow_series, "
                "w.priority AS wishlist_priority, w.store_name AS wishlist_store, "
                "w.order_number AS wishlist_order_number, w.paid_price AS wishlist_paid_price, "
                "w.owned_format AS wishlist_format, "
                "EXISTS(SELECT 1 FROM followed_series fs WHERE fs.user_id = %s "
                "AND fs.publisher_id = b.publisher_id "
                "AND fs.normalized_series = LEFT(LOWER(b.series_title), 190)) AS series_following "
                + base
                + clause
                + f" ORDER BY {order_by} "
                "LIMIT %s OFFSET %s",
                [user_id, user_id, *values, *order_values, min(max(limit, 1), 200), max(offset, 0)],
            )
            items = [serialize_row(row) for row in cursor.fetchall()]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def search_terms(query: str) -> list[str]:
    """Normalize and cap independent partial-match terms for a small local catalog."""
    return normalize_text(query).casefold().split()[:8]


def get_book(book_id: int, user_id: int) -> dict[str, Any] | None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT b.*, p.code AS publisher_code, p.name AS publisher_name, "
                "w.state AS wishlist_state, w.notes AS wishlist_notes, w.follow_series, "
                "w.priority AS wishlist_priority, w.store_name AS wishlist_store, "
                "w.order_number AS wishlist_order_number, w.paid_price AS wishlist_paid_price, "
                "w.owned_format AS wishlist_format, "
                "EXISTS(SELECT 1 FROM followed_series fs WHERE fs.user_id = %s "
                "AND fs.publisher_id = b.publisher_id "
                "AND fs.normalized_series = LEFT(LOWER(b.series_title), 190)) AS series_following "
                "FROM books b JOIN publishers p ON p.id = b.publisher_id "
                "LEFT JOIN wishlist_items w ON w.book_id = b.id AND w.user_id = %s WHERE b.id = %s",
                (user_id, user_id, book_id),
            )
            book = serialize_row(cursor.fetchone())
            if book:
                cursor.execute(
                    "SELECT * FROM release_history WHERE book_id = %s ORDER BY observed_at DESC LIMIT 50",
                    (book_id,),
                )
                book["history"] = [serialize_row(row) for row in cursor.fetchall()]
            return book


def set_wishlist(
    user_id: int,
    book_id: int,
    state: str,
    notes: str,
    follow_series: bool,
    priority: int = 0,
    store_name: str = "",
    order_number: str = "",
    paid_price: int | None = None,
    owned_format: str = "paper",
) -> None:
    allowed = {"wanted", "preordered", "purchased", "paused"}
    if state not in allowed:
        raise ValueError("無效的訂選狀態")
    if owned_format not in {"paper", "digital", "both"}:
        raise ValueError("無效的收藏格式")
    priority = min(max(int(priority), 0), 3)
    if paid_price is not None:
        paid_price = max(int(paid_price), 0)
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM books WHERE id = %s", (book_id,))
            if not cursor.fetchone():
                raise KeyError("找不到書籍")
            cursor.execute(
                "INSERT INTO wishlist_items "
                "(user_id, book_id, state, notes, follow_series, priority, store_name, order_number, "
                "paid_price, owned_format) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "state = VALUES(state), notes = VALUES(notes), "
                "follow_series = VALUES(follow_series), priority = VALUES(priority), "
                "store_name = VALUES(store_name), order_number = VALUES(order_number), "
                "paid_price = VALUES(paid_price), owned_format = VALUES(owned_format)",
                (
                    user_id,
                    book_id,
                    state,
                    notes[:5000],
                    follow_series,
                    priority,
                    store_name[:200] or None,
                    order_number[:200] or None,
                    paid_price,
                    owned_format,
                ),
            )
            _set_followed_series_for_book(cursor, user_id, book_id, follow_series)


def delete_wishlist(user_id: int, book_id: int) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT follow_series FROM wishlist_items WHERE user_id = %s AND book_id = %s",
                (user_id, book_id),
            )
            item = cursor.fetchone()
            if item and item["follow_series"]:
                _set_followed_series_for_book(cursor, user_id, book_id, False)
            cursor.execute(
                "DELETE FROM wishlist_items WHERE user_id = %s AND book_id = %s",
                (user_id, book_id),
            )


def list_recommendations(user_id: int, limit: int = 60) -> dict[str, Any]:
    """Recommend unseen books from purchased books and followed series."""
    limit = min(max(int(limit), 1), 100)
    query_limit = min(limit * 6, 400)
    candidate_columns = (
        "c.*, p.code AS publisher_code, p.name AS publisher_name, "
        "seed.id AS seed_book_id, seed.title AS seed_title, "
        "seed.volume_label AS seed_volume_label, sw.updated_at AS seed_updated_at"
    )
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS purchased_count FROM wishlist_items "
                "WHERE user_id = %s AND state = 'purchased'",
                (user_id,),
            )
            purchased_count = int(cursor.fetchone()["purchased_count"])
            cursor.execute(
                f"SELECT {candidate_columns}, 'same_series' AS recommendation_type "
                "FROM wishlist_items sw "
                "JOIN books seed ON seed.id = sw.book_id "
                "JOIN books c ON c.publisher_id = seed.publisher_id "
                "AND c.series_title = seed.series_title AND c.id <> seed.id "
                "JOIN publishers p ON p.id = c.publisher_id "
                "LEFT JOIN wishlist_items cw ON cw.book_id = c.id AND cw.user_id = sw.user_id "
                "LEFT JOIN recommendation_dismissals rd ON rd.book_id = c.id AND rd.user_id = sw.user_id "
                "WHERE sw.user_id = %s AND sw.state = 'purchased' "
                "AND seed.series_title IS NOT NULL AND seed.series_title <> '' "
                "AND cw.book_id IS NULL AND rd.book_id IS NULL "
                "ORDER BY sw.updated_at DESC, "
                "(c.release_status = 'scheduled') DESC, c.release_date DESC "
                "LIMIT %s",
                (user_id, query_limit),
            )
            series_rows = cursor.fetchall()
            cursor.execute(
                f"SELECT {candidate_columns}, 'same_author' AS recommendation_type "
                "FROM wishlist_items sw "
                "JOIN books seed ON seed.id = sw.book_id "
                "JOIN books c ON c.author = seed.author AND c.id <> seed.id "
                "JOIN publishers p ON p.id = c.publisher_id "
                "LEFT JOIN wishlist_items cw ON cw.book_id = c.id AND cw.user_id = sw.user_id "
                "LEFT JOIN recommendation_dismissals rd ON rd.book_id = c.id AND rd.user_id = sw.user_id "
                "WHERE sw.user_id = %s AND sw.state = 'purchased' "
                "AND seed.author IS NOT NULL AND seed.author <> '' "
                "AND COALESCE(c.series_title, '') <> COALESCE(seed.series_title, '') "
                "AND c.normalized_title <> seed.normalized_title "
                "AND cw.book_id IS NULL AND rd.book_id IS NULL "
                "ORDER BY sw.updated_at DESC, "
                "(c.release_status = 'scheduled') DESC, c.release_date DESC "
                "LIMIT %s",
                (user_id, query_limit),
            )
            author_rows = cursor.fetchall()
            cursor.execute(
                "SELECT c.*, p.code AS publisher_code, p.name AS publisher_name, "
                "NULL AS seed_book_id, fs.series_title AS seed_title, "
                "NULL AS seed_volume_label, fs.created_at AS seed_updated_at, "
                "'followed_series' AS recommendation_type "
                "FROM followed_series fs "
                "JOIN books c ON c.publisher_id = fs.publisher_id "
                "AND c.series_title = fs.series_title "
                "JOIN publishers p ON p.id = c.publisher_id "
                "LEFT JOIN wishlist_items cw ON cw.book_id = c.id AND cw.user_id = fs.user_id "
                "LEFT JOIN recommendation_dismissals rd ON rd.book_id = c.id AND rd.user_id = fs.user_id "
                "WHERE fs.user_id = %s AND cw.book_id IS NULL AND rd.book_id IS NULL "
                "ORDER BY fs.created_at DESC, "
                "(c.release_status = 'scheduled') DESC, c.release_date DESC "
                "LIMIT %s",
                (user_id, query_limit),
            )
            followed_rows = cursor.fetchall()
            cursor.execute(
                "SELECT COUNT(*) AS followed_count FROM followed_series WHERE user_id = %s",
                (user_id,),
            )
            followed_count = int(cursor.fetchone()["followed_count"])
            cursor.execute(
                "SELECT p.code AS publisher_code, p.name AS publisher_name, "
                "seed.series_title, seed.media_type, "
                "COUNT(DISTINCT seed.id) AS purchased_count, "
                "(SELECT COUNT(*) FROM books all_books "
                " WHERE all_books.publisher_id = seed.publisher_id "
                " AND all_books.series_title = seed.series_title) AS book_count, "
                "(SELECT COUNT(*) FROM books scheduled_books "
                " WHERE scheduled_books.publisher_id = seed.publisher_id "
                " AND scheduled_books.series_title = seed.series_title "
                " AND scheduled_books.release_status = 'scheduled') AS scheduled_count "
                "FROM wishlist_items sw "
                "JOIN books seed ON seed.id = sw.book_id "
                "JOIN publishers p ON p.id = seed.publisher_id "
                "LEFT JOIN followed_series fs ON fs.user_id = sw.user_id "
                "AND fs.publisher_id = seed.publisher_id "
                "AND fs.series_title = seed.series_title "
                "WHERE sw.user_id = %s AND sw.state = 'purchased' "
                "AND seed.series_title IS NOT NULL AND seed.series_title <> '' "
                "AND fs.publisher_id IS NULL "
                "GROUP BY seed.publisher_id, p.code, p.name, "
                "seed.series_title, seed.media_type "
                "ORDER BY MAX(sw.updated_at) DESC LIMIT 20",
                (user_id,),
            )
            series_prompts = [serialize_row(row) for row in cursor.fetchall()]
    return {
        "items": merge_recommendation_rows(series_rows, author_rows, limit, followed_rows),
        "series_prompts": series_prompts,
        "purchased_count": purchased_count,
        "followed_count": followed_count,
    }


def merge_recommendation_rows(
    series_rows: list[dict[str, Any]],
    author_rows: list[dict[str, Any]],
    limit: int,
    followed_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Deduplicate recommendation evidence and keep the strongest reason."""
    merged: dict[int, dict[str, Any]] = {}
    for row in [*series_rows, *(followed_rows or []), *author_rows]:
        item = serialize_row(row) or {}
        book_id = int(item["id"])
        recommendation_type = str(item.pop("recommendation_type"))
        score = {
            "same_series": 110,
            "followed_series": 95,
            "book_series": 105,
            "same_author": 70,
            "book_author": 65,
        }.get(recommendation_type, 50)
        if item.get("release_status") == "scheduled":
            score += 12
        if (
            recommendation_type in {"same_series", "book_series"}
            and item.get("volume_label")
            and item.get("volume_label") != item.get("seed_volume_label")
        ):
            score += 10
        reason = {
            "same_series": f"因為你買了《{item.get('seed_title')}》，這是同系列的其他集數或版本",
            "followed_series": f"因為你追蹤了「{item.get('seed_title')}」，推薦這個系列的其他書目",
            "book_series": f"和《{item.get('seed_title')}》同系列的其他集數或版本",
            "same_author": f"因為你買了《{item.get('seed_title')}》，推薦同作者 {item.get('author')} 的作品",
            "book_author": f"同樣出自 {item.get('author')}，你可能也會喜歡",
        }.get(recommendation_type, "依照你的收藏偏好推薦")
        existing = merged.get(book_id)
        if existing:
            types = set(existing["recommendation_types"])
            types.add(recommendation_type)
            existing["recommendation_types"] = sorted(types)
            existing["recommendation_score"] += 2
            if score > existing["_reason_score"]:
                existing["recommendation_reason"] = reason
                existing["_reason_score"] = score
            continue
        item["recommendation_types"] = [recommendation_type]
        item["recommendation_reason"] = reason
        item["recommendation_score"] = score
        item["_reason_score"] = score
        merged[book_id] = item
    items = sorted(
        merged.values(),
        key=lambda item: (
            -int(item["recommendation_score"]),
            str(item.get("release_date") or ""),
            str(item.get("title") or ""),
        ),
        reverse=False,
    )
    for item in items:
        item.pop("_reason_score", None)
    return items[: min(max(int(limit), 1), 100)]


def list_book_recommendations(
    user_id: int, book_id: int, limit: int = 8
) -> list[dict[str, Any]]:
    """Related, unselected books shown at the bottom of a book detail."""
    limit = min(max(int(limit), 1), 20)
    query_limit = min(limit * 4, 80)
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, publisher_id, title, series_title, volume_label, author "
                "FROM books WHERE id = %s",
                (book_id,),
            )
            seed = cursor.fetchone()
            if not seed:
                raise KeyError("找不到指定書目")
            series_rows: list[dict[str, Any]] = []
            author_rows: list[dict[str, Any]] = []
            if seed["series_title"]:
                cursor.execute(
                    "SELECT c.*, p.code AS publisher_code, p.name AS publisher_name "
                    "FROM books c JOIN publishers p ON p.id = c.publisher_id "
                    "LEFT JOIN wishlist_items w ON w.book_id = c.id AND w.user_id = %s "
                    "LEFT JOIN recommendation_dismissals rd ON rd.book_id = c.id AND rd.user_id = %s "
                    "WHERE c.publisher_id = %s AND c.series_title = %s AND c.id <> %s "
                    "AND w.book_id IS NULL AND rd.book_id IS NULL "
                    "ORDER BY (c.release_status = 'scheduled') DESC, "
                    "c.release_date DESC LIMIT %s",
                    (user_id, user_id, seed["publisher_id"], seed["series_title"], book_id, query_limit),
                )
                series_rows = cursor.fetchall()
                for row in series_rows:
                    row.update(
                        {
                            "recommendation_type": "book_series",
                            "seed_title": seed["title"],
                            "seed_volume_label": seed["volume_label"],
                        }
                    )
            if seed["author"]:
                cursor.execute(
                    "SELECT c.*, p.code AS publisher_code, p.name AS publisher_name "
                    "FROM books c JOIN publishers p ON p.id = c.publisher_id "
                    "LEFT JOIN wishlist_items w ON w.book_id = c.id AND w.user_id = %s "
                    "LEFT JOIN recommendation_dismissals rd ON rd.book_id = c.id AND rd.user_id = %s "
                    "WHERE c.author = %s AND c.id <> %s "
                    "AND COALESCE(c.series_title, '') <> COALESCE(%s, '') "
                    "AND w.book_id IS NULL AND rd.book_id IS NULL "
                    "ORDER BY (c.release_status = 'scheduled') DESC, "
                    "c.release_date DESC LIMIT %s",
                    (user_id, user_id, seed["author"], book_id, seed["series_title"], query_limit),
                )
                author_rows = cursor.fetchall()
                for row in author_rows:
                    row.update(
                        {
                            "recommendation_type": "book_author",
                            "seed_title": seed["title"],
                            "seed_volume_label": seed["volume_label"],
                        }
                    )
    return merge_recommendation_rows(series_rows, author_rows, limit)


def dismiss_recommendation(user_id: int, book_id: int) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM books WHERE id = %s", (book_id,))
            if not cursor.fetchone():
                raise KeyError("找不到指定書目")
            cursor.execute(
                "INSERT INTO recommendation_dismissals (user_id, book_id, reason_type) "
                "VALUES (%s, %s, 'all') ON DUPLICATE KEY UPDATE created_at = CURRENT_TIMESTAMP",
                (user_id, book_id),
            )


def clear_recommendation_dismissals(user_id: int) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM recommendation_dismissals WHERE user_id = %s", (user_id,))


def _set_followed_series_for_book(
    cursor: Any, user_id: int, book_id: int, following: bool
) -> None:
    cursor.execute(
        "SELECT publisher_id, series_title, media_type FROM books WHERE id = %s",
        (book_id,),
    )
    book = cursor.fetchone()
    if not book or not book["series_title"]:
        return
    normalized = normalize_text(book["series_title"]).casefold()[:190]
    if following:
        cursor.execute(
            "INSERT INTO followed_series "
            "(user_id, publisher_id, series_title, normalized_series, media_type) "
            "VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
            "series_title = VALUES(series_title), media_type = VALUES(media_type)",
            (user_id, book["publisher_id"], book["series_title"], normalized, book["media_type"]),
        )
        cursor.execute(
            "INSERT IGNORE INTO wishlist_items (user_id, book_id, state, follow_series) "
            "SELECT %s, id, 'wanted', FALSE FROM books WHERE publisher_id = %s "
            "AND LEFT(LOWER(series_title), 190) = %s AND release_status = 'scheduled'",
            (user_id, book["publisher_id"], normalized),
        )
    else:
        cursor.execute(
            "DELETE FROM followed_series WHERE user_id = %s "
            "AND publisher_id = %s AND normalized_series = %s",
            (user_id, book["publisher_id"], normalized),
        )


def list_publishers() -> list[dict[str, Any]]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT p.*, s.cursor_date, s.cursor_value, s.last_mode, "
                "s.backfill_completed, s.last_success_at AS sync_success_at, "
                "(SELECT COUNT(*) FROM books b WHERE b.publisher_id = p.id) AS book_count "
                "FROM publishers p LEFT JOIN source_sync_state s ON s.source_code = p.code "
                "ORDER BY p.enabled DESC, p.name"
            )
            return [serialize_row(row) for row in cursor.fetchall()]


def stats(user_id: int) -> dict[str, int]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(release_status = 'scheduled') AS scheduled, "
                "SUM(release_status = 'available') AS available, "
                "SUM(release_status = 'unknown') AS unknown, "
                "SUM(release_status = 'scheduled' AND release_date IS NULL) "
                "AS scheduled_undated FROM books"
            )
            result = cursor.fetchone()
            cursor.execute(
                "SELECT COUNT(*) AS wishlist FROM wishlist_items WHERE user_id = %s",
                (user_id,),
            )
            result.update(cursor.fetchone())
            cursor.execute(
                "SELECT COUNT(*) AS followed_series FROM followed_series WHERE user_id = %s",
                (user_id,),
            )
            result.update(cursor.fetchone())
    return {key: int(value or 0) for key, value in result.items()}


def list_series(
    filters: dict[str, str],
    user_id: int,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    where = ["b.series_title IS NOT NULL", "b.series_title <> ''"]
    values: list[Any] = []
    if filters.get("q"):
        for term in search_terms(filters["q"]):
            where.append("(b.series_title LIKE %s OR b.author LIKE %s)")
            values.extend([f"%{term}%", f"%{term}%"])
    if filters.get("publisher"):
        where.append("p.code = %s")
        values.append(filters["publisher"])
    clause = " AND ".join(where)
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS total FROM ("
                "SELECT b.publisher_id, b.series_title, b.media_type "
                "FROM books b JOIN publishers p ON p.id = b.publisher_id "
                f"WHERE {clause} GROUP BY b.publisher_id, b.series_title, b.media_type"
                ") grouped_series",
                values,
            )
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                "SELECT p.code AS publisher_code, p.name AS publisher_name, b.series_title, "
                "b.media_type, COUNT(*) AS book_count, "
                "COUNT(DISTINCT b.volume_label) AS volume_count, "
                "MIN(b.release_date) AS first_date, MAX(b.release_date) AS last_date, "
                "SUM(b.release_status = 'scheduled') AS scheduled_count, "
                "SUM(w.state = 'purchased') AS owned_count, "
                "MAX(fs.normalized_series IS NOT NULL) AS is_following, "
                "SUBSTRING_INDEX(GROUP_CONCAT(b.cover_url ORDER BY b.release_date DESC "
                "SEPARATOR '\\n'), '\\n', 1) AS cover_url "
                "FROM books b JOIN publishers p ON p.id = b.publisher_id "
                "LEFT JOIN wishlist_items w ON w.book_id = b.id AND w.user_id = %s "
                "LEFT JOIN followed_series fs ON fs.user_id = %s AND fs.publisher_id = b.publisher_id "
                "AND fs.normalized_series = LEFT(LOWER(b.series_title), 190) "
                f"WHERE {clause} GROUP BY p.id, p.code, p.name, b.series_title, b.media_type "
                "ORDER BY MAX(b.release_date) DESC, b.series_title LIMIT %s OFFSET %s",
                [user_id, user_id, *values, min(max(limit, 1), 200), max(offset, 0)],
            )
            items = [serialize_row(row) for row in cursor.fetchall()]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_series(user_id: int, publisher_code: str, series_title: str) -> dict[str, Any] | None:
    result = list_books(
        {"publisher": publisher_code, "q": series_title, "sort": "release_asc"}, user_id,
        limit=200,
    )
    exact = [item for item in result["items"] if item["series_title"] == series_title]
    if not exact:
        return None
    numeric_volumes = sorted(
        {
            int(item["volume_label"])
            for item in exact
            if str(item.get("volume_label") or "").isdigit()
        }
    )
    missing_volumes = (
        [volume for volume in range(1, numeric_volumes[-1] + 1) if volume not in numeric_volumes]
        if numeric_volumes
        else []
    )
    return {
        "publisher_code": publisher_code,
        "publisher_name": exact[0]["publisher_name"],
        "series_title": series_title,
        "media_type": exact[0]["media_type"],
        "numeric_volumes": numeric_volumes,
        "missing_volumes": missing_volumes,
        "items": exact,
    }


def set_series_follow(
    user_id: int,
    publisher_code: str,
    series_title: str,
    media_type: str,
    following: bool,
) -> None:
    normalized = normalize_text(series_title).casefold()[:190]
    if not normalized:
        raise ValueError("系列名稱不可為空")
    with transaction() as connection:
        publisher_id = get_publisher_id(connection, publisher_code)
        with connection.cursor() as cursor:
            if following:
                cursor.execute(
                    "INSERT INTO followed_series "
                    "(user_id, publisher_id, series_title, normalized_series, media_type) "
                    "VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
                    "series_title = VALUES(series_title), media_type = VALUES(media_type)",
                    (user_id, publisher_id, series_title[:500], normalized, media_type[:40]),
                )
                cursor.execute(
                    "INSERT IGNORE INTO wishlist_items (user_id, book_id, state, follow_series) "
                    "SELECT %s, id, 'wanted', FALSE FROM books WHERE publisher_id = %s "
                    "AND LEFT(LOWER(series_title), 190) = %s "
                    "AND release_status = 'scheduled'",
                    (user_id, publisher_id, normalized),
                )
            else:
                cursor.execute(
                    "DELETE FROM followed_series WHERE user_id = %s AND publisher_id = %s "
                    "AND normalized_series = %s",
                    (user_id, publisher_id, normalized),
                )


def quality_report(limit: int = 50) -> dict[str, Any]:
    conditions = {
        "missing_author": "(b.author IS NULL OR b.author = '')",
        "missing_isbn": "(b.isbn IS NULL OR b.isbn = '')",
        "missing_date": "b.release_date IS NULL",
        "unknown_type": "b.media_type = 'unknown'",
        "suspicious_date": "b.release_date < '2010-01-01'",
    }
    with transaction() as connection:
        with connection.cursor() as cursor:
            counts: dict[str, int] = {}
            for key, condition in conditions.items():
                cursor.execute(f"SELECT COUNT(*) AS count FROM books b WHERE {condition}")
                counts[key] = int(cursor.fetchone()["count"])
            combined = " OR ".join(conditions.values())
            cursor.execute(
                "SELECT b.id, b.title, b.author, b.isbn, b.release_date, b.media_type, "
                "b.series_title, p.name AS publisher_name, "
                "CONCAT_WS(',', "
                "IF(b.author IS NULL OR b.author = '', 'missing_author', NULL), "
                "IF(b.isbn IS NULL OR b.isbn = '', 'missing_isbn', NULL), "
                "IF(b.release_date IS NULL, 'missing_date', NULL), "
                "IF(b.media_type = 'unknown', 'unknown_type', NULL), "
                "IF(b.release_date < '2010-01-01', 'suspicious_date', NULL)) AS issues "
                "FROM books b JOIN publishers p ON p.id = b.publisher_id "
                f"WHERE {combined} ORDER BY b.updated_at DESC LIMIT %s",
                (min(max(limit, 1), 200),),
            )
            items = [serialize_row(row) for row in cursor.fetchall()]
    return {"counts": counts, "items": items, "total_issues": sum(counts.values())}


def upcoming_books(user_id: int, days: int = 31) -> list[dict[str, Any]]:
    today = date.today()
    return list_books(
        {
            "date_from": today.isoformat(),
            "date_to": (today + timedelta(days=min(max(days, 1), 366))).isoformat(),
            "sort": "release_asc",
        }, user_id,
        limit=200,
    )["items"]


def export_catalog(user_id: int, wishlist_only: bool = False) -> dict[str, Any]:
    filters = {"wishlist": "1"} if wishlist_only else {}
    books = list_books(filters, user_id, limit=200, offset=0)
    items = books["items"]
    # Export all rows without raising the public page-size cap.
    if books["total"] > len(items):
        with transaction() as connection:
            with connection.cursor() as cursor:
                where = "WHERE w.book_id IS NOT NULL" if wishlist_only else ""
                cursor.execute(
                    "SELECT b.*, p.code AS publisher_code, p.name AS publisher_name, "
                    "w.state AS wishlist_state, w.notes AS wishlist_notes, w.follow_series, "
                    "w.priority AS wishlist_priority, w.store_name AS wishlist_store, "
                    "w.order_number AS wishlist_order_number, w.paid_price AS wishlist_paid_price, "
                    "w.owned_format AS wishlist_format, "
                    "EXISTS(SELECT 1 FROM followed_series fs WHERE fs.user_id = %s "
                    "AND fs.publisher_id = b.publisher_id "
                    "AND fs.normalized_series = LEFT(LOWER(b.series_title), 190)) AS series_following "
                    "FROM books b JOIN publishers p ON p.id = b.publisher_id "
                    f"LEFT JOIN wishlist_items w ON w.book_id = b.id AND w.user_id = %s {where} "
                    "ORDER BY b.release_date DESC",
                    (user_id, user_id),
                )
                items = [serialize_row(row) for row in cursor.fetchall()]
    return {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "wishlist_only": wishlist_only,
        "count": len(items),
        "items": items,
    }


def rebuild_book_metadata() -> int:
    """Recalculate lightweight title-derived fields after parser improvements."""
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, title FROM books")
            rows = cursor.fetchall()
            values = [
                (
                    infer_series_title(row["title"]),
                    extract_volume(row["title"]),
                    detect_edition(row["title"]),
                    row["id"],
                )
                for row in rows
            ]
            cursor.executemany(
                "UPDATE books SET series_title = %s, volume_label = %s, edition_type = %s "
                "WHERE id = %s",
                values,
            )
    return len(values)


def known_source_keys(source_code: str) -> set[str]:
    """Return stable publisher product keys so details are never downloaded twice."""
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT b.source_key FROM books b JOIN publishers p ON p.id = b.publisher_id "
                "WHERE p.code = %s",
                (source_code,),
            )
            return {str(row["source_key"]) for row in cursor.fetchall()}


def max_source_release_date(source_code: str) -> date | None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT MAX(b.release_date) AS latest FROM books b "
                "JOIN publishers p ON p.id = b.publisher_id WHERE p.code = %s",
                (source_code,),
            )
            return cursor.fetchone()["latest"]


def get_sync_state(source_code: str) -> dict[str, Any]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM source_sync_state WHERE source_code = %s", (source_code,))
            row = cursor.fetchone()
    return row or {
        "source_code": source_code,
        "cursor_value": None,
        "cursor_date": None,
        "backfill_completed": False,
    }


def save_sync_state(
    source_code: str,
    cursor_value: str | None,
    cursor_date: date | None,
    mode: str,
) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO source_sync_state "
                "(source_code, cursor_value, cursor_date, last_mode, backfill_completed, last_success_at) "
                "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP) "
                "ON DUPLICATE KEY UPDATE "
                "cursor_value = COALESCE(VALUES(cursor_value), cursor_value), "
                "cursor_date = CASE "
                "WHEN cursor_date IS NULL THEN VALUES(cursor_date) "
                "WHEN VALUES(cursor_date) IS NULL THEN cursor_date "
                "ELSE GREATEST(cursor_date, VALUES(cursor_date)) END, "
                "last_mode = VALUES(last_mode), "
                "backfill_completed = backfill_completed OR VALUES(backfill_completed), "
                "last_success_at = CURRENT_TIMESTAMP",
                (source_code, cursor_value, cursor_date, mode, mode == "backfill"),
            )


def get_backfill_progress(source_code: str) -> dict[str, dict[str, Any]]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT segment, next_page, completed, updated_at "
                "FROM source_backfill_progress WHERE source_code = %s",
                (source_code,),
            )
            return {
                str(row["segment"]): {
                    "next_page": int(row["next_page"]),
                    "completed": bool(row["completed"]),
                    "updated_at": row["updated_at"],
                }
                for row in cursor.fetchall()
            }


def save_backfill_progress(
    source_code: str,
    segment: str,
    next_page: int,
    completed: bool,
) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO source_backfill_progress "
                "(source_code, segment, next_page, completed) VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE next_page = VALUES(next_page), "
                "completed = VALUES(completed)",
                (source_code, segment[:100], max(int(next_page), 1), completed),
            )
