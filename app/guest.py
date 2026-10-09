"""Stateless guest identification and strictly public, read-only catalog access."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from . import repository as repo
from .abuse import RequestError
from .db import transaction
from .preferences import visibility_scope, visible_book_sql

COOKIE = "anishelf_guest"
_KEY = secrets.token_bytes(32)  # Rotates on service restart; browser shelves do not.
BOOK_FIELDS = set("id title normalized_title series_title series_key volume_label edition_type media_type content_rating bl_category author isbn cover_url list_price release_date release_precision release_status release_date_source release_checked_at release_checked_at_utc release_display_status first_seen_at updated_at source_url publisher_code publisher_name recommendation_types recommendation_reason".split())


def signature(value: str) -> str:
    return hmac.new(_KEY, value.encode(), hashlib.sha256).hexdigest()


def issue() -> tuple[str, str]:
    identity = secrets.token_hex(16)
    value = f"{identity}.{int(time.time()) + 86400}"
    return identity, value + "." + signature("guest|" + value)


def identity(token: str | None) -> str | None:
    if not token or not re.fullmatch(r"[a-f0-9]{32}\.[0-9]{10}\.[a-f0-9]{64}", token):
        return None
    owner, expiry, supplied = token.split(".")
    if int(expiry) <= time.time() or not hmac.compare_digest(supplied, signature("guest|" + owner + "." + expiry)):
        return None
    return owner


def cookie(token: str, secure: bool) -> str:
    return f"{COOKIE}={token}; Path=/; Max-Age=86400; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")


def public_book(book):
    return {key: value for key, value in (book or {}).items() if key in BOOK_FIELDS}


def resolve_series(payload):
    rows = payload.get("series", [])
    after = payload.get("after", 0)
    general = payload.get("general", True)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20 or type(after) is not int or not 0 <= after < 10**18 or type(general) is not bool:
        raise RequestError("無效的系列查詢")
    conditions, values = [], []
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not 1 <= len(row[key]) <= maximum for key, maximum in (("publisher", 64), ("series_key", 190), ("media_type", 40))):
            raise RequestError("系列查詢格式錯誤")
        conditions.append("(p.code=%s AND b.series_key=%s AND b.media_type=%s)")
        values.extend([row["publisher"], row["series_key"], row["media_type"]])
    with visibility_scope(general), transaction() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT b.*,p.code AS publisher_code,p.name AS publisher_name FROM books b JOIN publishers p ON p.id=b.publisher_id WHERE (" + " OR ".join(conditions) + ") AND b.id>%s AND " + visible_book_sql() + " ORDER BY b.id LIMIT 201", [*values, after])
        rows = [public_book(repo.serialize_row(row)) for row in cursor.fetchall()]
    return {"items": rows[:200], "has_more": len(rows) > 200, "next_after_id": rows[min(len(rows), 200) - 1]["id"] if rows else after}


def public_get(path: str, query: dict[str, str]):
    allowed = {"q", "publisher", "publishers", "media_type", "status", "edition", "date_from", "date_to", "sort", "limit", "offset", "missing"}
    filters = {key: value for key, value in query.items() if key in allowed}
    if filters.get("sort") == "purchased_desc":
        filters.pop("sort")
    general = query.get("general", "1")
    if general not in {"0", "1"}:
        raise RequestError("無效的瀏覽偏好")
    with visibility_scope(general == "1"):
        # SQL `user_id = NULL` NEVER matches, including legacy rows. This is not
        # a shared user 0. A response allowlist also strips every private field.
        if path == "books":
            data = repo.list_books(filters, None, int(query.get("limit", 100)), int(query.get("offset", 0)))
            data["items"] = [public_book(row) for row in data["items"]]
            return data
        if path == "book-batch":
            raw = query.get("ids", "")
            if not re.fullmatch(r"[0-9]+(?:,[0-9]+){0,99}", raw):
                raise RequestError("最多查詢 100 本書目")
            ids = list(dict.fromkeys(int(value) for value in raw.split(",")))
            if any(not 0 < value < 10**18 for value in ids):
                raise RequestError("無效的書目識別碼")
            with transaction() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT b.*,p.code AS publisher_code,p.name AS publisher_name FROM books b "
                               "JOIN publishers p ON p.id=b.publisher_id WHERE b.id IN (" +
                               ",".join(["%s"] * len(ids)) + ") AND " + visible_book_sql(), ids)
                return {"items": [public_book(repo.serialize_row(row)) for row in cursor.fetchall()]}
        if match := re.fullmatch(r"books/([0-9]+)/recommendations", path):
            return {"items": [public_book(row) for row in repo.list_book_recommendations(None, int(match[1]), min(int(query.get("limit", 8)), 20))]}
        if match := re.fullmatch(r"books/([0-9]+)", path):
            book = repo.get_book(int(match[1]), None)
            if not book:
                raise RequestError("找不到書籍或不符合目前瀏覽偏好", 404)
            return public_book(book)
        if path == "series":
            data = repo.list_series(filters, None, int(query.get("limit", 60)), int(query.get("offset", 0)))
            fields = set("publisher_code publisher_name series_title series_key media_type book_count volume_count first_date last_date scheduled_count cover_url".split())
            data["items"] = [{key: value for key, value in row.items() if key in fields} for row in data["items"]]
            return data
        if path == "series/detail":
            # Reuse paged book queries instead of returning an unbounded series.
            from .series import series_key, canonical_series_title
            publisher, title, media = query.get("publisher", ""), query.get("title", ""), query.get("media_type", "")
            key = series_key(title, publisher)
            with transaction() as connection, connection.cursor() as cursor:
                where = "p.code=%s AND b.series_key=%s AND b.media_type=%s AND " + visible_book_sql()
                params = [publisher, key, media]
                cursor.execute("SELECT COUNT(*) AS total FROM books b JOIN publishers p ON p.id=b.publisher_id WHERE " + where, params)
                total = int(cursor.fetchone()["total"])
                cursor.execute("SELECT b.*,p.code AS publisher_code,p.name AS publisher_name FROM books b JOIN publishers p ON p.id=b.publisher_id WHERE " + where + " ORDER BY (b.release_date IS NULL),b.release_date,b.id LIMIT %s OFFSET %s", [*params, 200, int(query.get("offset", 0))])
                items = [public_book(repo.serialize_row(row)) for row in cursor.fetchall()]
            if not total:
                raise RequestError("找不到系列", 404)
            offset = int(query.get("offset", 0))
            return {"publisher_code": publisher, "publisher_name": items[0]["publisher_name"] if items else "", "series_title": canonical_series_title(title, publisher), "series_key": key, "media_type": media, "items": items, "missing_volumes": [], "total": total, "has_more": offset + len(items) < total, "next_offset": offset + len(items)}
        if path == "publishers":
            return {"items": [{key: row[key] for key in ("code", "name", "enabled", "book_count") if key in row} for row in repo.list_publishers()]}
        if path == "stats":
            with transaction() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS total,SUM(release_status='scheduled') AS scheduled,SUM(release_status='available') AS available,SUM(release_status='unknown') AS unknown,SUM(release_status='scheduled' AND release_date IS NULL) AS scheduled_undated FROM books b WHERE " + visible_book_sql())
                return {key: int(value or 0) for key, value in cursor.fetchone().items()}
        if path == "upcoming":
            today = datetime.now(timezone(timedelta(hours=8))).date()
            filters = {"date_from": today.isoformat(), "date_to": (today + timedelta(days=int(query.get("days", 31)))).isoformat(), "sort": "release_asc"}
            data = repo.list_books(filters, None, 200, int(query.get("offset", 0)))
            data["items"] = [public_book(row) for row in data["items"]]
            return data
    raise RequestError("訪客無法使用這個功能", 403)


def import_token(user_id: int, payload: dict) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    value = f"{user_id}.{digest}.{int(time.time()) + 600}"
    return value + "." + signature("import|" + value)


def valid_import_token(user_id: int, payload: dict, token: str) -> bool:
    try:
        owner, digest, expiry, supplied = token.split(".")
        if int(expiry) <= time.time() or int(owner) != user_id:
            return False
        expected_digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        return hmac.compare_digest(digest, expected_digest) and hmac.compare_digest(supplied, signature("import|" + ".".join((owner, digest, expiry))))
    except (ValueError, AttributeError):
        return False
