from __future__ import annotations
from .rating_merge import peer_rating_time

from datetime import date
from typing import Any, Iterable
from urllib.parse import urlparse

from .models import CONTENT_RATINGS, BookRecord, normalize_text
from .repository import upsert_book


PROTOCOL_VERSION = 1
MAX_UPLOAD_ITEMS = 200
MEDIA_TYPES = {"manga", "novel"}
RELEASE_PRECISIONS = {"unknown", "day", "month", "year"}
RELEASE_STATUSES = {"unknown", "scheduled", "available"}
RATING_SOURCES = {"unknown", "publisher", "category"}


def _optional_text(value: Any, maximum: int) -> str | None:
    text = normalize_text(str(value or ""))[:maximum]
    return text or None


def _required_text(value: Any, field: str, maximum: int) -> str:
    text = _optional_text(value, maximum)
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _choice(value: Any, allowed: set[str], field: str, default: str) -> str:
    result = str(value or default).strip().lower()
    if result not in allowed:
        raise ValueError(f"Invalid {field}: {result}")
    return result


def _date(value: Any) -> date | None:
    if value in {None, ""}:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError("release_date must use YYYY-MM-DD") from exc


def _checked_at(payload: dict[str, Any]):
    from datetime import datetime, timedelta
    from .release_dates import parse_checked_at
    try:
        value = parse_checked_at(payload.get("release_checked_at"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid release_checked_at") from exc
    if value and value > datetime.utcnow() + timedelta(minutes=5):
        raise ValueError("release_checked_at cannot be in the future")
    if payload.get("release_date_source") == "product" and value is None:
        raise ValueError("Product release dates require release_checked_at")
    return value


def book_record_from_sync(payload: dict[str, Any]) -> BookRecord:
    """Accept only catalog metadata; account and internal database fields are ignored."""
    publisher_code = _required_text(payload.get("publisher_code"), "publisher_code", 100)
    source_key = _required_text(payload.get("source_key"), "source_key", 190)
    title = _required_text(payload.get("title"), "title", 500)
    media_type = _choice(payload.get("media_type"), MEDIA_TYPES, "media_type", "unknown")
    source_url = _required_text(payload.get("source_url"), "source_url", 1000)
    parsed_url = urlparse(source_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("source_url must be an http(s) URL")
    content_rating = _choice(
        payload.get("content_rating"), set(CONTENT_RATINGS), "content_rating", "unknown"
    )
    price = payload.get("list_price")
    if price in {None, ""}:
        price_value = None
    else:
        try:
            price_value = int(price)
        except (TypeError, ValueError) as exc:
            raise ValueError("list_price must be an integer") from exc
        if not 0 <= price_value <= 10_000_000:
            raise ValueError("list_price is outside the accepted range")
    try:
        confidence = min(max(int(payload.get("rating_confidence") or 0), 0), 100)
    except (TypeError, ValueError) as exc:
        raise ValueError("rating_confidence must be an integer") from exc

    return BookRecord(
        publisher_code=publisher_code,
        source_key=source_key,
        title=title,
        media_type=media_type,
        source_url=source_url,
        author=_optional_text(payload.get("author"), 500),
        isbn=_optional_text(payload.get("isbn"), 32),
        cover_url=_optional_text(payload.get("cover_url"), 1000),
        list_price=price_value,
        release_date=_date(payload.get("release_date")),
        release_precision=_choice(
            payload.get("release_precision"),
            RELEASE_PRECISIONS,
            "release_precision",
            "unknown",
        ),
        release_status=_choice(
            payload.get("release_status"), RELEASE_STATUSES, "release_status", "unknown"
        ),
        edition_type=_optional_text(payload.get("edition_type"), 40) or "standard",
        volume_label=_optional_text(payload.get("volume_label"), 80),
        series_title=_optional_text(payload.get("series_title"), 500),
        content_rating=content_rating,
        rating_raw=_optional_text(payload.get("rating_raw"), 100),
        rating_source=_choice(
            payload.get("rating_source"), RATING_SOURCES, "rating_source", "unknown"
        ),
        rating_confidence=confidence,
        release_date_source=_choice(payload.get("release_date_source"), {"unknown", "schedule", "product"}, "release_date_source", "unknown"),
        release_checked_at=_checked_at(payload),
        rating_checked_at=peer_rating_time(payload),
        rating_parser_version=_optional_text(payload.get("rating_parser_version"), 40),
        bl_category=_optional_text(payload.get("bl_category"), 100),
    )


def ingest_catalog_books(items: Any) -> dict[str, Any]:
    if not isinstance(items, list):
        raise ValueError("items must be a JSON array")
    if not items:
        raise ValueError("items cannot be empty")
    if len(items) > MAX_UPLOAD_ITEMS:
        raise ValueError(f"A batch may contain at most {MAX_UPLOAD_ITEMS} books")
    records: list[BookRecord] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}] must be a JSON object")
        try:
            records.append(book_record_from_sync(item))
        except ValueError as exc:
            raise ValueError(f"items[{index}]: {exc}") from exc

    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for record in records:
        outcome = upsert_book(record, change_origin="sync_upload")
        counts[outcome] = counts.get(outcome, 0) + 1
    return {"accepted": len(records), **counts}


def compare_catalog_manifests(
    local_items: Iterable[dict[str, Any]], remote_items: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Compare peers by publisher/source identity without touching personal data."""
    def keyed(items: Iterable[dict[str, Any]]) -> dict:
        return {
            (str(item.get("publisher_code") or ""), str(item.get("source_key") or "")): (
                str(item.get("sync_hash") or item.get("source_hash") or ""),
                bool(item["is_bl"]) if "is_bl" in item else None)
            for item in items
        }

    local = keyed(local_items)
    remote = keyed(remote_items)
    local_keys = set(local)
    remote_keys = set(remote)
    different = sorted(key for key in local_keys & remote_keys if local[key][0] != remote[key][0] or
                       (local[key][1] is not None and remote[key][1] is not None and local[key][1] != remote[key][1]))
    return {
        "same": len(local_keys & remote_keys) - len(different),
        "local_only": [list(key) for key in sorted(local_keys - remote_keys)],
        "remote_only": [list(key) for key in sorted(remote_keys - local_keys)],
        "different": [list(key) for key in different],
    }
