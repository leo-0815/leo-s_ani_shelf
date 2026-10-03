from __future__ import annotations

from datetime import date
from typing import Any
from .db import transaction


def migrate_collection(cursor: Any, *, cloud: bool) -> None:
    user = "user_id" if cloud else "0"
    cursor.execute(
        "INSERT IGNORE INTO collection_items "
        "(user_id, book_id, owned_format, purchased_at, store_name, order_number, paid_price, notes) "
        f"SELECT {user}, book_id, owned_format, purchased_at, store_name, order_number, paid_price, notes "
        "FROM wishlist_items WHERE state = 'purchased'"
    )
    # Convert only after copying, so restart never resurrects an explicitly removed collection.
    cursor.execute("UPDATE wishlist_items SET state = 'wanted' WHERE state = 'purchased'")


def ownership_payload(payload: dict[str, Any]) -> dict[str, Any]:
    fmt = str(payload.get("owned_format") or "paper")
    if fmt not in {"paper", "digital", "both"}:
        raise ValueError("無效的收藏格式")
    value = payload.get("paid_price")
    price = None if value in {None, ""} else int(value)
    if price is not None and price < 0:
        raise ValueError("實付價格不可為負數")
    purchased = payload.get("purchased_at")
    return dict(
        owned_format=fmt,
        purchased_at=date.fromisoformat(str(purchased)) if purchased else None,
        store_name=str(payload.get("store_name") or "")[:200] or None,
        order_number=str(payload.get("order_number") or "")[:200] or None,
        paid_price=price,
        notes=str(payload.get("notes") or "")[:5000] or None,
    )


def save_owned(cursor: Any, user_id: int, book_id: int, payload: dict[str, Any]) -> None:
    data = ownership_payload(payload)
    cursor.execute("SELECT id FROM books WHERE id = %s", (book_id,))
    if not cursor.fetchone():
        raise KeyError("找不到書籍")
    cursor.execute(
        "INSERT INTO collection_items "
        "(user_id, book_id, owned_format, purchased_at, store_name, order_number, paid_price, notes) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
        "owned_format = VALUES(owned_format), purchased_at = VALUES(purchased_at), "
        "store_name = VALUES(store_name), order_number = VALUES(order_number), "
        "paid_price = VALUES(paid_price), notes = VALUES(notes)",
        (user_id, book_id, *data.values()),
    )


def set_owned(user_id: int, book_id: int, payload: dict[str, Any]) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            save_owned(cursor, user_id, book_id, payload)


def decorate_owned(items: list[dict[str, Any]], user_id: int) -> list[dict[str, Any]]:
    ids = [int(item["id"]) for item in items if item]
    if not ids:
        return items
    with transaction() as connection:
        with connection.cursor() as cursor:
            marks = ",".join(["%s"] * len(ids))
            cursor.execute(
                f"SELECT * FROM collection_items WHERE user_id = %s AND book_id IN ({marks})",
                [user_id, *ids],
            )
            owned = {row["book_id"]: row for row in cursor.fetchall()}
    for item in items:
        row = owned.get(item["id"])
        item["is_owned"] = bool(row)
        item["collection_id"] = row["id"] if row else None
        item["collection"] = row
    return items


def list_collection(user_id: int, filters: dict[str, str], limit=100, offset=0, *, cloud=True) -> dict[str, Any]:
    from .repository import serialize_row, search_terms
    limit, offset = min(max(int(limit), 1), 200), max(int(offset), 0)
    where = ["c.user_id = %s"]
    values: list[Any] = [user_id]
    for term in search_terms(filters.get("q", "")):
        where.append("(COALESCE(b.title,c.title) LIKE %s OR COALESCE(b.author,c.author) LIKE %s OR COALESCE(b.isbn,c.isbn) LIKE %s)")
        values.extend(["%" + term + "%"] * 3)
    for key, expr in (("publisher", "p.code"), ("media_type", "COALESCE(b.media_type,c.media_type)"),
                      ("owned_format", "c.owned_format"), ("edition", "COALESCE(b.edition_type,c.edition_type)"),
                      ("status", "b.release_status")):
        if filters.get(key):
            where.append(expr + " = %s")
            values.append(filters[key])
    for key, op in (("date_from", ">="), ("date_to", "<=")):
        if filters.get(key):
            where.append("COALESCE(b.release_date,c.release_date) " + op + " %s")
            values.append(date.fromisoformat(filters[key]))
    scope = " FROM collection_items c LEFT JOIN books b ON b.id=c.book_id LEFT JOIN publishers p ON p.id=b.publisher_id WHERE " + " AND ".join(where)
    order = {"title_asc": "COALESCE(b.title,c.title),c.id",
             "release_asc": "COALESCE(b.release_date,c.release_date) IS NULL,COALESCE(b.release_date,c.release_date),c.id",
             "release_desc": "COALESCE(b.release_date,c.release_date) DESC,c.id"}.get(filters.get("sort"), "c.purchased_at IS NULL,c.purchased_at DESC,c.id DESC")
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total" + scope, values)
            total = int(cursor.fetchone()["total"])
            cursor.execute("SELECT c.id AS collection_id,c.*," 
                           "b.title AS catalog_title,b.author AS catalog_author,b.media_type AS catalog_media_type,"
                           "b.isbn AS catalog_isbn,b.edition_type AS catalog_edition_type,b.release_date AS catalog_release_date,"
                           "b.cover_url,b.release_status,b.release_precision,b.series_title,b.volume_label,"
                           "b.source_key,b.source_url,b.list_price,b.first_seen_at,"
                           "p.code AS publisher_code,p.name AS catalog_publisher_name" + scope +
                           " ORDER BY " + order + " LIMIT %s OFFSET %s", [*values, limit, offset])
            rows = cursor.fetchall()
    result = []
    for row in rows:
        item = serialize_row(row)
        item["id"] = item["book_id"] or -int(item["collection_id"])
        item["is_owned"] = True
        item["is_custom"] = item["book_id"] is None
        for name in ("title","author","media_type","isbn","edition_type","release_date","publisher_name"):
            item[name] = item.pop("catalog_" + name, None) or item.get(name)
        item["collection"] = {key: item.get(key) for key in ("owned_format","purchased_at","store_name","order_number","paid_price","notes")}
        item.update(wishlist_format=item["owned_format"], wishlist_purchased_at=item["purchased_at"],
                    wishlist_store=item["store_name"], wishlist_order_number=item["order_number"],
                    wishlist_paid_price=item["paid_price"], wishlist_notes=item["notes"])
        result.append(item)
    # Hearts are independent of ownership.
    ids = [row["id"] for row in result if row["id"] > 0]
    if ids:
        with transaction() as connection:
            with connection.cursor() as cursor:
                marks = ",".join(["%s"] * len(ids))
                user_clause = " AND user_id=%s" if cloud else ""
                cursor.execute(f"SELECT book_id,state FROM wishlist_items WHERE book_id IN ({marks})" + user_clause,
                               [*ids, *([user_id] if user_clause else [])])
                wishes = {row["book_id"]: row["state"] for row in cursor.fetchall()}
        for item in result:
            item["wishlist_state"] = wishes.get(item["id"])
    return dict(items=result,total=total,limit=limit,offset=offset)


def remove_owned(user_id: int, collection_id: int) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM collection_items WHERE id=%s AND user_id=%s", (collection_id,user_id))


def collection_stats(user_id: int) -> dict[str, int]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS purchased,SUM(c.owned_format IN ('paper','both')) AS purchased_paper,"
                "SUM(c.owned_format IN ('digital','both')) AS purchased_digital,"
                "COUNT(DISTINCT CONCAT(b.publisher_id,':',b.series_key,':',b.media_type)) AS purchased_series,"
                "COALESCE(SUM(c.paid_price),0) AS purchased_spend FROM collection_items c "
                "LEFT JOIN books b ON b.id=c.book_id WHERE c.user_id=%s", (user_id,))
            return {key:int(value or 0) for key,value in cursor.fetchone().items()}


def custom_payload(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    if not title or len(title) > 500:
        raise ValueError("請輸入書名（最多 500 字）")
    media = str(payload.get("media_type") or "unknown")
    if media not in {"manga", "novel", "unknown"}:
        raise ValueError("書籍類型只能是漫畫、輕小說或未分類")
    release = payload.get("release_date")
    return dict(
        title=title, author=str(payload.get("author") or "")[:500] or None,
        publisher_name=str(payload.get("publisher_name") or "")[:200] or None,
        media_type=media, isbn=str(payload.get("isbn") or "")[:30] or None,
        edition_type=str(payload.get("edition_type") or "")[:100] or None,
        release_date=date.fromisoformat(str(release)) if release else None,
        **ownership_payload(payload),
    )


def save_custom(user_id: int, payload: dict[str, Any], collection_id: int | None = None) -> int:
    data = custom_payload(payload)
    with transaction() as connection:
        with connection.cursor() as cursor:
            if collection_id is None:
                columns = ",".join(data)
                marks = ",".join(["%s"] * len(data))
                cursor.execute(f"INSERT INTO collection_items (user_id,{columns}) VALUES (%s,{marks})",
                               [user_id, *data.values()])
                return int(cursor.lastrowid)
            cursor.execute("SELECT id FROM collection_items WHERE id=%s AND user_id=%s AND book_id IS NULL",
                           (collection_id, user_id))
            if not cursor.fetchone():
                raise KeyError("找不到你的自建藏書")
            columns = ",".join(key + "=%s" for key in data)
            cursor.execute(f"UPDATE collection_items SET {columns} WHERE id=%s AND user_id=%s AND book_id IS NULL",
                           [*data.values(), collection_id, user_id])
            return collection_id


def get_custom(user_id: int, collection_id: int) -> dict[str, Any]:
    from .repository import serialize_row
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM collection_items WHERE id=%s AND user_id=%s AND book_id IS NULL",
                           (collection_id, user_id))
            row = cursor.fetchone()
            if not row:
                raise KeyError("找不到你的自建藏書")
            return serialize_row(row)
