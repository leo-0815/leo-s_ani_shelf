"""Bounded, edition-specific release-date revalidation (no account data)."""
from __future__ import annotations

from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit
import re

from .db import transaction
from .models import BookRecord

SOURCES = {"kadokawa", "chingwin", "spp", "tongli", "tohan"}
RELEASE_FIELDS = ("release_date", "release_precision", "release_status",
                  "release_date_source", "release_checked_at")
DEFAULT_LIMIT = 20

class PublicationDateMissing(ValueError):
    """A readable product with no date is not an HTTP/source failure."""

def parse_checked_at(value: Any) -> datetime | None:
    if not value:
        return None
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo:
        result = result.astimezone(timezone.utc).replace(tzinfo=None)
    return result.replace(microsecond=0)

def product_record(**kwargs: Any) -> BookRecord:
    """Only call after parsing an explicit publication field on the product page."""
    return BookRecord(**kwargs, release_date_source="product" if kwargs.get("release_date") else "unknown",
                      release_checked_at=datetime.utcnow().replace(microsecond=0) if kwargs.get("release_date") else None)

def merge_release_fields(existing: dict, incoming: dict) -> dict:
    result = dict(incoming)
    if "release_date" not in incoming:
        return result
    old_time = parse_checked_at(existing.get("release_checked_at"))
    new_time = parse_checked_at(incoming.get("release_checked_at"))
    old_product = existing.get("release_date_source") == "product"
    new_product = incoming.get("release_date_source") == "product" and new_time is not None
    preserve = (
        (incoming.get("release_date") is None and existing.get("release_date") is not None)
        or (old_product and not new_product)
        or (old_product and new_product and old_time is not None and new_time <= old_time)
    )
    if preserve:
        for key in RELEASE_FIELDS:
            if key in existing:
                result[key] = existing[key]
        # Do not replace a verified SKU link with a monthly announcement/sheet.
        if old_product and existing.get("source_url"):
            result["source_url"] = existing["source_url"]
    return result

def product_url_valid(code: str, url: str, key: str) -> bool:
    parsed = urlsplit(url)
    hosts = {"kadokawa": {"www.kadokawa.com.tw", "kadokawa.com.tw"},
             "chingwin": {"www.ching-win.com.tw"},
             "spp": {"www.spp.com.tw"}, "tongli": {"www.tongli.com.tw"},
             "tohan": {"www.tohan.com.tw"}}
    if code not in hosts or parsed.scheme not in {"http", "https"} or parsed.hostname not in hosts[code] or parsed.port:
        return False
    if code == "kadokawa":
        return parsed.path.rstrip("/").split("/")[-1] == key and parsed.path.startswith("/products/")
    if code == "chingwin":
        from .sources.chingwin_rating import product_identity
        return product_identity(url.replace("http://", "https://", 1)) == key
    if code == "spp":
        return bool(re.fullmatch(r"/(?:v2/official/)?SalePage/Index/\d+/?", parsed.path, re.I))
    query = parse_qs(parsed.query)
    if code == "tongli":
        return parsed.path.lower() == "/booksdetail.aspx" and next((v[0] for k, v in query.items() if k.lower() == "bd"), "") == key
    return parsed.path == "/product.php" and query.get("act") == ["view"] and query.get("id") == [key]

def parse_product(source: Any, row: dict, markup: str) -> BookRecord:
    url, key = row["source_url"].replace("http://", "https://", 1), row["source_key"]
    if not product_url_valid(source.code, url, key):
        raise ValueError("Not an edition-specific official product URL")
    if source.code == "chingwin":
        # Product specifications are scoped to this SKU, not recommendations.
        from .sources.common import parse_page
        text = parse_page(markup).flat_text
        block = re.search(rf"產品編號\s*[:：]\s*{re.escape(key)}\b(.{{0,2500}}?)(?:定價|內容簡介)", text)
        match = re.search(r"(?:出版日期|上市日期)\s*[:：]\s*(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", block[1]) if block else None
        if not match:
            # Reuse the structured Book/Product parser, matching the exact SKU.
            records, _ = source._parse_catalog_page(url, markup, row["media_type"], date.min)
            record = next((r for r in records if r.source_key == key), None)
        else:
            allowed = {f.name for f in fields(BookRecord)}
            values = {k: v for k, v in row.items() if k in allowed}
            values.update(publisher_code=source.code, release_date=date(*map(int, match.groups())),
                          release_precision="day", release_status="unknown")
            record = BookRecord(**values)
    elif source.code == "kadokawa":
        record = source._parse_detail(url, markup, media_hint=row["media_type"])
    elif source.code == "spp":
        record, _ = source._parse_detail(url, markup, enforce_cutoff=False)
    else:
        record = source._parse_detail(url, markup)
    if record is None or record.source_key != key or record.publisher_code != source.code:
        raise ValueError("Product identity mismatch")
    old_isbn = str(row.get("isbn") or "").replace("-", "")
    if old_isbn and record.isbn and old_isbn != record.isbn.replace("-", ""):
        raise ValueError("Product ISBN mismatch; retain the existing edition")
    if record.release_date is None:
        raise PublicationDateMissing("Product page has no explicit publication date")
    return replace(record, release_status="unknown", release_date_source="product",
                   release_checked_at=datetime.utcnow().replace(microsecond=0))

def candidates(code: str, limit: int, now: datetime) -> list[dict]:
    today = now.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT b.*, p.code AS publisher_code FROM books b "
                "JOIN publishers p ON p.id = b.publisher_id "
                "LEFT JOIN book_release_checks rc ON rc.book_id = b.id "
                "WHERE p.code = %s AND b.media_type IN ('manga', 'novel') "
                "AND (b.release_status = 'scheduled' OR b.release_date BETWEEN %s AND %s) "
                "AND (rc.next_check_at IS NULL OR rc.next_check_at <= %s) "
                "AND (b.release_checked_at IS NULL OR b.release_checked_at <= %s) "
                "AND (REPLACE(b.source_url, 'http://', 'https://') LIKE %s "
                "OR REPLACE(b.source_url, 'http://', 'https://') LIKE %s) "
                # Oldest attempts first prevents an overdue subset starving the rest.
                "ORDER BY COALESCE(rc.attempted_at, '1970-01-01'), "
                "(b.release_status = 'scheduled' AND b.release_date < %s) DESC, b.id "
                "LIMIT %s",
                (code, today - timedelta(days=30), today + timedelta(days=30), now,
                 now - timedelta(days=1), *url_patterns(code), today, limit),
            )
            return cursor.fetchall()

def url_patterns(code: str) -> tuple[str, str]:
    return {
        "kadokawa": ("https://www.kadokawa.com.tw/products/%", "https://kadokawa.com.tw/products/%"),
        "chingwin": ("https://www.ching-win.com.tw/product-detail/%", "https://www.ching-win.com.tw/product-detail/%"),
        "spp": ("https://www.spp.com.tw/SalePage/Index/%", "https://www.spp.com.tw/v2/official/SalePage/Index/%"),
        "tongli": ("https://www.tongli.com.tw/BooksDetail.aspx?%", "https://www.tongli.com.tw/BooksDetail.aspx?%"),
        "tohan": ("https://www.tohan.com.tw/product.php?%", "https://www.tohan.com.tw/product.php?%"),
    }[code]

def record_attempt(book_id: int, now: datetime, next_at: datetime, error: str | None) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE book_release_checks SET attempted_at = %s, next_check_at = %s, "
                "last_error = %s WHERE book_id = %s",
                (now, next_at, error[:1000] if error else None, book_id),
            )

def claim_attempt(code: str, book_id: int, now: datetime, limit: int) -> bool:
    """Serialize the daily budget across scheduled and interactive processes."""
    taipei = timezone(timedelta(hours=8))
    start = now.replace(tzinfo=timezone.utc).astimezone(taipei).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc).replace(tzinfo=None)
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM publishers WHERE code = %s FOR UPDATE", (code,))
            cursor.execute(
                "SELECT COUNT(*) AS total FROM book_release_checks rc "
                "JOIN books b ON b.id = rc.book_id JOIN publishers p ON p.id = b.publisher_id "
                "WHERE p.code = %s AND rc.attempted_at >= %s", (code, start),
            )
            if int(cursor.fetchone()["total"]) >= limit:
                return False
            cursor.execute(
                "INSERT IGNORE INTO book_release_checks (book_id, attempted_at, next_check_at) "
                "VALUES (%s, %s, %s)", (book_id, now, now),
            )
            cursor.execute(
                "UPDATE book_release_checks SET attempted_at = %s, next_check_at = %s "
                "WHERE book_id = %s AND next_check_at <= %s",
                (now, now + timedelta(days=1), book_id, now),
            )
            return cursor.rowcount == 1

def refresh_source(source: Any, limit: int = DEFAULT_LIMIT) -> dict:
    from .repository import upsert_book
    from .sources.common import RateLimiter, fetch_html
    stats = {"checked": 0, "updated": 0, "unconfirmed": 0, "errors": []}
    if source.code not in SOURCES:
        return stats
    now = datetime.utcnow().replace(microsecond=0)
    limit = min(max(limit, 0), 100)
    limiter = RateLimiter(1)
    for row in candidates(source.code, limit, now):
        if not claim_attempt(source.code, row["id"], now, limit):
            continue
        stats["checked"] += 1
        try:
            if not product_url_valid(source.code, row["source_url"], row["source_key"]):
                raise ValueError("Not an edition-specific official product URL")
            limiter.wait()
            record = parse_product(source, row, fetch_html(row["source_url"].replace("http://", "https://", 1), timeout=20, attempts=2))
            outcome = upsert_book(record)
            if outcome == "updated":
                stats["updated"] += 1
            today = now.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
            interval = 7 if record.release_date > today + timedelta(days=30) else 1
            record_attempt(row["id"], now, now + timedelta(days=interval), None)
        except PublicationDateMissing as exc:
            stats["unconfirmed"] += 1
            record_attempt(row["id"], now, now + timedelta(days=1), str(exc))
        except Exception as exc:
            message = f"{row['source_key']}: {exc}"
            stats["errors"].append(message)
            # Failed requests retain old metadata and retry no sooner than tomorrow.
            record_attempt(row["id"], now, now + timedelta(days=1), message)
    return stats

