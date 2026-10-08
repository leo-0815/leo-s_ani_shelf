"""Bounded, resumable rating-only enrichment. Personal tables are never touched."""
from __future__ import annotations
import argparse
import json
import time
from datetime import datetime, timedelta
from typing import Any

from .db import transaction
from .models import book_content_hash
from .repository import BOOK_FIELDS, RATING_FIELDS, _crawler_write_data, _record_catalog_change
from .sources.common import fetch_html
from .sources.product_rating import PARSER_VERSIONS, parse_rating
from .sources.product_audience import parse_bl_category
from .release_dates import product_url_valid, url_patterns

CHECK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS book_rating_checks (
 book_id BIGINT UNSIGNED NOT NULL,
 parser_version VARCHAR(40) NOT NULL,
 result VARCHAR(20) NOT NULL,
 raw_label VARCHAR(100) NULL,
 attempts INT UNSIGNED NOT NULL DEFAULT 1,
 checked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 retry_after DATETIME NULL,
 uploaded_at DATETIME NULL,
 last_error VARCHAR(300) NULL,
 PRIMARY KEY (book_id),
 KEY idx_rating_retry (result, retry_after)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Old completed age checks receive one BL inspection; age proof versions stay unchanged.
CHECK_VERSIONS = {code: version + "_bl1" for code, version in PARSER_VERSIONS.items()}


def candidates(limit: int, keys: list[str] | None = None, *, dry_run: bool = False,
               code: str = "chingwin", excluded: set[int] | None = None,
               recent_days: int | None = None) -> list[dict[str, Any]]:
    values: list[Any] = [code, *url_patterns(code)]
    clause = ("p.code = %s AND b.rating_locked = FALSE "
              "AND b.media_type IN ('novel','manga') "
              "AND (REPLACE(b.source_url,'http://','https://') LIKE %s OR REPLACE(b.source_url,'http://','https://') LIKE %s)")
    if keys:
        clause += " AND b.source_key IN (" + ",".join(["%s"] * len(keys)) + ")"
        values.extend(keys)
    if excluded:
        clause += " AND b.id NOT IN (" + ",".join(["%s"] * len(excluded)) + ")"
        values.extend(sorted(excluded))
    if recent_days is not None:
        clause += " AND (b.release_date BETWEEN %s AND %s OR (b.release_date IS NULL AND b.first_seen_at >= %s))"
        since = datetime.utcnow() - timedelta(days=recent_days)
        values.extend([since.date(), (datetime.utcnow()+timedelta(days=90)).date(), since])
    join = ""
    if not dry_run:
        join = " LEFT JOIN book_rating_checks r ON r.book_id = b.id"
        clause += " AND (r.book_id IS NULL OR r.parser_version <> %s OR r.retry_after <= UTC_TIMESTAMP())"
        values.append(CHECK_VERSIONS[code])
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT b.*, p.code AS publisher_code FROM books b "
                "JOIN publishers p ON p.id = b.publisher_id" + join +
                " WHERE " + clause + " ORDER BY b.id LIMIT %s",
                [*values, min(max(limit, 1), 100)])
            return list(cursor.fetchall())


def save_result(book_id: int, rating: str, raw: str | None, error: str | None = None,
                *, code: str = "chingwin", checked_at: datetime | None = None,
                bl_category: str | None = None) -> bool:
    """Result, content change and durable checkpoint commit atomically."""
    now = checked_at or datetime.utcnow().replace(microsecond=0)
    status = "error" if error else "confirmed" if rating != "unknown" else "unknown"
    retry = now + timedelta(days=1 if error else 7) if rating == "unknown" else None
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM books WHERE id = %s FOR UPDATE", (book_id,))
            existing = cursor.fetchone()
            if not existing:
                return False
            changed = False
            if not error and not existing.get("rating_locked"):
                incoming = {field: existing.get(field) for field in BOOK_FIELDS}
                incoming.update(publisher_code=code, source_key=existing["source_key"],
                                content_rating=rating, rating_raw=raw,
                                rating_source="publisher" if rating != "unknown" else "unknown",
                                rating_confidence=100 if rating != "unknown" else 0,
                                rating_checked_at=now, rating_parser_version=PARSER_VERSIONS[code],
                                bl_category=bl_category)
                merged = _crawler_write_data(existing, incoming)
                changed_fields = [field for field in (*RATING_FIELDS, "bl_category") if existing.get(field) != merged.get(field)]
                source_hash = book_content_hash(merged)
                cursor.execute(
                    "UPDATE books SET content_rating=%s, rating_raw=%s, rating_source=%s, "
                    "rating_confidence=%s, rating_checked_at=%s, rating_parser_version=%s, source_hash=%s, bl_category=%s WHERE id=%s",
                    (merged["content_rating"], merged["rating_raw"], merged["rating_source"],
                     merged["rating_confidence"], merged.get("rating_checked_at"),
                     merged.get("rating_parser_version"), source_hash, merged.get("bl_category"), book_id))
                confirmation_changed = any(existing.get(field) != merged.get(field)
                                           for field in ("rating_checked_at", "rating_parser_version"))
                if changed_fields or confirmation_changed:
                    _record_catalog_change(cursor, book_id, "rating", source_hash,
                                           "crawler", changed_fields or ["rating_confirmation"])
                if changed_fields:
                    changed = True
                    if rating == "unknown":
                        status = "classified" if merged.get("bl_category") else "revoked"
            if existing.get("rating_locked"):
                status, retry = "locked", None
            cursor.execute(
                "INSERT INTO book_rating_checks "
                "(book_id,parser_version,result,raw_label,retry_after,last_error,checked_at) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE parser_version=VALUES(parser_version),result=VALUES(result),"
                "raw_label=VALUES(raw_label),retry_after=VALUES(retry_after),last_error=VALUES(last_error),"
                "checked_at=VALUES(checked_at),uploaded_at=NULL,attempts=attempts+1",
                (book_id, CHECK_VERSIONS[code], status, (raw or "")[:100] or None,
                 retry, (error or "")[:300] or None, now))
    return changed


def check_book(row: dict, *, dry_run: bool = False) -> tuple[str, str | None, str | None, bool]:
    observed_at = datetime.utcnow().replace(microsecond=0)
    code = row.get("publisher_code", "chingwin")
    url = row["source_url"].replace("http://", "https://", 1)
    rating, raw, error = "unknown", None, None
    bl_category = None
    try:
        if not product_url_valid(code, url, row["source_key"]):
            raise ValueError("No edition-specific official product URL; kept unknown")
        markup = fetch_html(url, timeout=20, attempts=2)
        rating, raw = parse_rating(code, url, markup,
                                   row["source_key"], row.get("isbn"))
        bl_category = parse_bl_category(code, url, markup, row["source_key"])
    except Exception as exc:
        error = str(exc)
    # An interrupted request cannot mark the item as completed.
    changed = False if dry_run else save_result(int(row["id"]), rating, raw, error, code=code,
                                              checked_at=observed_at, bl_category=bl_category)
    return rating, raw, error, changed


def run_enrichment(*, minutes: float = 30, limit: int = 0, keys: list[str] | None = None,
                   dry_run: bool = False, sync: bool = False, pause_seconds: float = 1.5,
                   continuous: bool = False, sources: list[str] | None = None,
                   batch_size: int = 10) -> dict[str, Any]:
    sources = list(dict.fromkeys(sources or ["chingwin"]))
    if any(code not in PARSER_VERSIONS for code in sources):
        raise ValueError("Unsupported publisher")
    if minutes < 0 or limit < 0 or pause_seconds < 0 or not 1 <= batch_size <= 10:
        raise ValueError("Invalid time, count, pause or batch size")
    if dry_run and not limit:
        limit = 10
    if dry_run:
        batch_size = 1
    if not dry_run:
        with transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(CHECK_TABLE_SQL)
    deadline = time.monotonic() + minutes * 60 if minutes else None
    counts = dict(checked=0, confirmed=0, unknown=0, errors=0, updated=0, uploaded=0,
                  interrupted=False, dry_run=dry_run, publishers={code: 0 for code in sources})
    visited: set[int] = set()
    try:
        while not limit or counts["checked"] < limit:
            progressed = False
            for code in sources:
                if deadline is not None and time.monotonic() >= deadline:
                    return finish(counts, [], sync and not dry_run)
                remaining = min(batch_size, limit-counts["checked"]) if limit else batch_size
                if remaining <= 0:
                    break
                rows = candidates(remaining, keys, dry_run=dry_run, code=code,
                                  excluded=visited if dry_run else None)
                rows = [row for row in rows if int(row["id"]) not in visited]
                for row in rows:
                    if deadline is not None and time.monotonic() >= deadline:
                        return finish(counts, [], sync and not dry_run)
                    time.sleep(pause_seconds)
                    rating, raw, error, changed = check_book(row, dry_run=dry_run)
                    visited.add(int(row["id"]))
                    progressed = True
                    counts["checked"] += 1
                    counts["publishers"][code] += 1
                    counts["confirmed" if rating != "unknown" else "unknown"] += 1
                    counts["errors"] += bool(error)
                    counts["updated"] += bool(changed)
                    print(json.dumps(dict(publisher=code, book_id=row["id"], source_key=row["source_key"],
                        rating=rating, raw=raw, updated=changed, error=error), ensure_ascii=False), flush=True)
                    if limit and counts["checked"] >= limit:
                        break
                # Bound memory even in continuous sessions: committed checkpoints drive resume.
                if not dry_run:
                    visited.clear()
            if not progressed:
                if continuous and not dry_run:
                    time.sleep(30)
                    continue
                break
    except KeyboardInterrupt:
        counts["interrupted"] = True
        print("Stopped safely: committed per-book checkpoints are preserved.", flush=True)
    return finish(counts, [], sync and not dry_run)


def finish(counts: dict, outgoing: list[dict], sync: bool) -> dict:
    if sync:
        # Durable outbox: a failed POST is retried on the next run, even though
        # its book is no longer unknown and thus no longer a crawl candidate.
        from .catalog_sync import CatalogSyncClient, UPLOAD_FIELDS
        client = CatalogSyncClient()
        while True:
            with transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT b.*,p.code AS publisher_code,r.checked_at AS enrichment_checked_at, "
                        "r.attempts AS rating_check_attempts "
                        "FROM book_rating_checks r JOIN books b ON b.id=r.book_id "
                        "JOIN publishers p ON p.id=b.publisher_id "
                        "WHERE r.result IN ('confirmed','revoked','classified') AND r.uploaded_at IS NULL ORDER BY b.id LIMIT 100")
                    outgoing=list(cursor.fetchall())
            if not outgoing:
                break
            items = [{field: (row[field].isoformat() if hasattr(row.get(field),"isoformat") else row.get(field))
                      for field in UPLOAD_FIELDS} for row in outgoing]
            result = client._request("POST","/api/catalog-sync/books",{"items":items})
            if int(result.get('accepted',0)) != len(items):
                raise RuntimeError('Rating upload incomplete; checkpoints kept for retry')
            with transaction() as connection:
                with connection.cursor() as cursor:
                    for row in outgoing:
                        cursor.execute('UPDATE book_rating_checks SET uploaded_at=NOW() '
                            'WHERE book_id=%s AND checked_at=%s AND attempts=%s',
                            (row['id'],row.get('enrichment_checked_at',row.get('rating_checked_at')),row['rating_check_attempts']))
            counts["uploaded"] += int(result.get("accepted",0))
    return counts



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("30m", "60m", "complete", "continuous"), default="30m")
    parser.add_argument("--minutes", type=float, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--keys", default="")
    parser.add_argument("--sources", default="all")
    parser.add_argument("--preflight", choices=("none", "compare", "pull"), default="none")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sync", action="store_true")
    args = parser.parse_args()
    if not args.dry_run:
        from .db import ensure_schema
        ensure_schema()
    if args.preflight != "none":
        from .catalog_sync import CatalogSyncClient
        client = CatalogSyncClient()
        comparison = client.compare()  # Includes canonical sync-hash version validation.
        print(json.dumps({key: len(value) if isinstance(value, list) else value
                          for key, value in comparison.items()}, ensure_ascii=False), flush=True)
        if args.preflight == "pull" and not args.dry_run:
            print(json.dumps(client.pull(), ensure_ascii=False), flush=True)
    minutes = args.minutes if args.minutes is not None else {"30m":30,"60m":60,"complete":0,"continuous":0}[args.mode]
    sources = list(PARSER_VERSIONS) if args.sources == "all" else [code.strip() for code in args.sources.split(",") if code.strip()]
    result = run_enrichment(minutes=minutes, limit=args.limit,
        keys=[key.strip() for key in args.keys.split(",") if key.strip()] or None,
        dry_run=args.dry_run, sync=args.sync, continuous=args.mode == "continuous", sources=sources)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

