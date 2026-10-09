"""Explicit, atomic, add-only guest migration into the authenticated account."""
from __future__ import annotations

import re
from .abuse import RequestError
from .collection import custom_payload, ownership_payload
from .db import transaction
from .guest import import_token, valid_import_token
from .series_follow import validate_scope


def normalize(raw):
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise RequestError("不支援的訪客備份版本")
    result = {}
    for section in ("wishlist", "collection", "custom", "follows"):
        rows = raw.get(section, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RequestError("訪客資料格式錯誤")
        result[section] = rows
    if sum(map(len, result.values())) > 1000:
        raise RequestError("目前一次最多匯入 1000 筆；尚未匯入，瀏覽器原始資料仍保留。請先下載備份")
    for section in ("wishlist", "collection", "custom"):
        cleaned = []
        for row in result[section]:
            for key, maximum in (("title", 500), ("author", 500), ("publisher_name", 200), ("isbn", 30), ("edition_type", 100), ("notes", 5000), ("store_name", 200), ("order_number", 200), ("purchased_at", 10), ("release_date", 10), ("owned_format", 20)):
                value = row.get(key)
                if value is not None and (not isinstance(value, str) or len(value) > maximum):
                    raise RequestError("匯入欄位格式或長度錯誤")
            if row.get("paid_price") not in (None, "") and (isinstance(row["paid_price"], bool) or not re.fullmatch(r"[0-9]{1,9}", str(row["paid_price"]))):
                raise RequestError("無效的實付價格")
            if section == "custom":
                clean = custom_payload(row)
            else:
                value = row.get("book_id")
                if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 10**18:
                    raise RequestError("無效的書目識別碼")
                clean = {"book_id": value, **ownership_payload(row)}
                if section == "wishlist":
                    state = row.get("state", "wanted")
                    priority = row.get("priority", 0)
                    if not isinstance(state, str) or state not in {"wanted", "preordered", "paused"} or type(priority) is not int or not 0 <= priority <= 3:
                        raise RequestError("無效的訂選狀態")
                    clean.update(state=state, priority=priority)
            cleaned.append(clean)
        result[section] = cleaned
    for row in result["follows"]:
        if any(not isinstance(row.get(key), str) or not row[key] or len(row[key]) > maximum for key, maximum in (("publisher", 64), ("series_key", 190), ("series_title", 500), ("media_type", 40))):
            raise RequestError("系列追蹤格式錯誤")
        if row["media_type"] not in {"manga", "novel", "mixed", "unknown"}:
            raise RequestError("無效的書籍類型")
        scope = row.get("scope", "future")
        if not isinstance(scope, str):
            raise RequestError("無效的系列追蹤範圍")
        validate_scope(scope)
    return result


def custom_identity(row):
    # Preserve the account's notes/purchase fields when the same manual book is
    # imported again. Editions and manga/novel remain distinct.
    return tuple(str(row.get(key) or "").strip() for key in ("title", "author", "publisher_name", "isbn", "edition_type", "media_type"))


def plan(cursor, user_id, data):
    cursor.execute("SELECT book_id FROM wishlist_items WHERE user_id=%s", (user_id,))
    wishes = {int(row["book_id"]) for row in cursor.fetchall()}
    cursor.execute("SELECT * FROM collection_items WHERE user_id=%s", (user_id,))
    owned_rows = cursor.fetchall()
    owned = {int(row["book_id"]) for row in owned_rows if row.get("book_id")}
    custom = {custom_identity(row) for row in owned_rows if not row.get("book_id")}
    cursor.execute("SELECT publisher_id,normalized_series,media_type FROM followed_series WHERE user_id=%s", (user_id,))
    follows = {(int(row["publisher_id"]), row["normalized_series"], row["media_type"]) for row in cursor.fetchall()}
    cursor.execute("SELECT id,code FROM publishers")
    publishers = {row["code"]: int(row["id"]) for row in cursor.fetchall()}
    ids = sorted({row["book_id"] for section in ("wishlist", "collection") for row in data[section]})
    valid = set()
    if ids:
        cursor.execute("SELECT id FROM books WHERE id IN (" + ",".join(["%s"] * len(ids)) + ")", ids)
        valid = {int(row["id"]) for row in cursor.fetchall()}
    selected = {section: [] for section in data}
    summary = {"new": 0, "existing": 0, "missing": 0, "wishlist": 0, "collection": 0, "custom": 0, "follows": 0}
    for section, seen in (("wishlist", wishes), ("collection", owned)):
        for row in data[section]:
            if row["book_id"] not in valid:
                summary["missing"] += 1
            elif row["book_id"] in seen:
                summary["existing"] += 1
            else:
                seen.add(row["book_id"]); selected[section].append(row)
    for row in data["custom"]:
        key = custom_identity(row)
        if key in custom:
            summary["existing"] += 1
        else:
            custom.add(key); selected["custom"].append(row)
    for row in data["follows"]:
        publisher_id = publishers.get(row["publisher"])
        key = (publisher_id, row["series_key"], row["media_type"])
        if publisher_id is None:
            summary["missing"] += 1
        elif key in follows:
            summary["existing"] += 1
        else:
            follows.add(key); selected["follows"].append({**row, "publisher_id": publisher_id})
    for section, rows in selected.items():
        summary[section] = len(rows)
        summary["new"] += len(rows)
    return selected, summary


def migrate(user_id: int, raw: dict, *, apply: bool = False, token: str = ""):
    data = normalize(raw)
    if apply and not valid_import_token(user_id, raw, token):
        raise RequestError("匯入確認已過期或資料已變更，請重新預覽", 409)
    with transaction() as connection, connection.cursor() as cursor:
        if apply:
            cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user_id,))
        selected, summary = plan(cursor, user_id, data)
        if apply:
            for section, table in (("wishlist", "wishlist_items"), ("collection", "collection_items"), ("custom", "collection_items")):
                rows = selected[section]
                if rows:
                    columns = list(rows[0])
                    cursor.executemany(f"INSERT IGNORE INTO {table} (user_id," + ",".join(columns) + ") VALUES (%s," + ",".join(["%s"] * len(columns)) + ")", [[user_id, *[row[key] for key in columns]] for row in rows])
            if selected["follows"]:
                cursor.executemany("INSERT IGNORE INTO followed_series (user_id,publisher_id,normalized_series,media_type,series_title,follow_scope) VALUES (%s,%s,%s,%s,%s,%s)", [[user_id, row["publisher_id"], row["series_key"], row["media_type"], row["series_title"], row.get("scope", "future")] for row in selected["follows"]])
                # Existing follow rules will pick up new books on later crawls.
                # Current guest wishlist was imported above, never overwritten.
    return {"summary": summary, "applied": apply, **({"token": import_token(user_id, raw)} if not apply else {})}
