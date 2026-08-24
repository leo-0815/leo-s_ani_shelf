from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .models import BookRecord
from .repository import set_wishlist, upsert_book


def restore_export(path: Path) -> dict[str, int]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("備份檔缺少 items 清單")
    totals = {"processed": 0, "inserted": 0, "updated": 0, "wishlist": 0, "errors": 0}
    for item in items:
        try:
            record = _record_from_export(item)
            outcome = upsert_book(record)
            totals["processed"] += 1
            if outcome in totals:
                totals[outcome] += 1
            if item.get("wishlist_state"):
                set_wishlist(
                    _book_id(record.publisher_code, record.source_key),
                    str(item["wishlist_state"]),
                    str(item.get("wishlist_notes") or ""),
                    bool(item.get("follow_series")),
                    int(item.get("wishlist_priority") or 0),
                    str(item.get("wishlist_store") or ""),
                    str(item.get("wishlist_order_number") or ""),
                    int(item["wishlist_paid_price"]) if item.get("wishlist_paid_price") is not None else None,
                    str(item.get("wishlist_format") or "paper"),
                    str(item.get("wishlist_purchased_at") or "") or None,
                )
                totals["wishlist"] += 1
        except Exception:
            totals["errors"] += 1
    return totals


def _record_from_export(item: dict[str, Any]) -> BookRecord:
    required = ("publisher_code", "source_key", "title", "media_type", "source_url")
    if any(not item.get(key) for key in required):
        raise ValueError("備份書目缺少必要欄位")
    release = date.fromisoformat(item["release_date"]) if item.get("release_date") else None
    return BookRecord(
        publisher_code=str(item["publisher_code"]),
        source_key=str(item["source_key"]),
        title=str(item["title"]),
        media_type=str(item["media_type"]),
        source_url=str(item["source_url"]),
        author=item.get("author"),
        isbn=item.get("isbn"),
        cover_url=item.get("cover_url"),
        list_price=int(item["list_price"]) if item.get("list_price") is not None else None,
        release_date=release,
        release_precision=str(item.get("release_precision") or "unknown"),
        release_status=str(item.get("release_status") or "unknown"),
        edition_type=str(item.get("edition_type") or "standard"),
        volume_label=item.get("volume_label"),
        series_title=item.get("series_title"),
    )


def _book_id(publisher_code: str, source_key: str) -> int:
    from .db import transaction

    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT b.id FROM books b JOIN publishers p ON p.id = b.publisher_id "
                "WHERE p.code = %s AND b.source_key = %s",
                (publisher_code, source_key),
            )
            row = cursor.fetchone()
    if not row:
        raise KeyError("還原後找不到書目")
    return int(row["id"])
