"""Bounded, resumable rating-only enrichment. Personal tables are never touched."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from typing import Any

from .db import transaction
from .repository import RATING_FIELDS, _crawler_write_data, _record_catalog_change
from .sources.chingwin_rating import RATING_PARSER_VERSION, parse_product_rating, product_identity
from .sources.common import fetch_html

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


def candidates(limit: int, keys: list[str] | None = None, *, dry_run: bool = False) -> list[dict[str, Any]]:
    values: list[Any] = []
    clause = "p.code = 'chingwin' AND b.rating_locked = FALSE AND b.content_rating = 'unknown'"
    if keys:
        clause += " AND b.source_key IN (" + ",".join(["%s"] * len(keys)) + ")"
        values.extend(keys)
    join = ""
    if not dry_run:
        join = " LEFT JOIN book_rating_checks r ON r.book_id = b.id"
        clause += " AND (r.book_id IS NULL OR r.parser_version <> %s OR r.retry_after <= NOW())"
        values.append(RATING_PARSER_VERSION)
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT b.*, p.code AS publisher_code FROM books b "
                "JOIN publishers p ON p.id = b.publisher_id" + join +
                " WHERE " + clause + " ORDER BY b.id LIMIT %s",
                [*values, min(max(limit, 1), 100)],
            )
            return list(cursor.fetchall())


def save_result(book_id: int, rating: str, raw: str | None, error: str | None = None) -> bool:
    """Result and durable per-book checkpoint commit in the same transaction."""
    status = "error" if error else "confirmed" if rating != "unknown" else "unknown"
    retry = datetime.now() + timedelta(days=1 if error else 7) if rating == "unknown" else None
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM books WHERE id = %s FOR UPDATE", (book_id,))
            existing = cursor.fetchone()
            if not existing:
                return False
            changed = False
            if rating != "unknown" and not existing.get("rating_locked"):
                incoming = dict(content_rating=rating, rating_raw=raw,
                                rating_source="publisher", rating_confidence=100)
                merged = _crawler_write_data(existing, incoming)
                changed_fields = [field for field in RATING_FIELDS if existing.get(field) != merged.get(field)]
                if changed_fields:
                    cursor.execute(
                        "UPDATE books SET content_rating=%s, rating_raw=%s, "
                        "rating_source=%s, rating_confidence=%s WHERE id=%s",
                        (merged["content_rating"], merged["rating_raw"], merged["rating_source"],
                         merged["rating_confidence"], book_id),
                    )
                    _record_catalog_change(cursor, book_id, "rating", str(existing["source_hash"]),
                                           "rating_enrichment", changed_fields)
                    changed = True
            if existing.get("rating_locked"):
                status, retry = "locked", None
            cursor.execute(
                "INSERT INTO book_rating_checks "
                "(book_id,parser_version,result,raw_label,retry_after,last_error) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE parser_version=VALUES(parser_version),result=VALUES(result),"
                "raw_label=VALUES(raw_label),retry_after=VALUES(retry_after),last_error=VALUES(last_error),"
                "checked_at=NOW(),uploaded_at=NULL,attempts=attempts+1",
                (book_id, RATING_PARSER_VERSION, status, (raw or "")[:100] or None,
                 retry, (error or "")[:300] or None),
            )
    return changed


def run_enrichment(*, minutes: float = 30, limit: int = 0, keys: list[str] | None = None,
                   dry_run: bool = False, sync: bool = False, pause_seconds: float = 1.5,
                   continuous: bool = False) -> dict[str, Any]:
    if minutes < 0 or limit < 0 or pause_seconds < 0:
        raise ValueError("minutes, limit and pause must not be negative")
    if sync:
        raise ValueError("--sync is available only in the local edition; cloud writes already target TiDB")
    if not dry_run:
        with transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(CHECK_TABLE_SQL)
    deadline = time.monotonic() + minutes * 60 if minutes else None
    counts = dict(checked=0, confirmed=0, unknown=0, errors=0, updated=0, uploaded=0,
                  interrupted=False, dry_run=dry_run)
    outgoing: list[dict[str, Any]] = []
    visited: set[int] = set()
    try:
        while not limit or counts["checked"] < limit:
            if deadline is not None and time.monotonic() >= deadline:
                break
            rows = candidates(min(50, limit-counts["checked"]) if limit else 50, keys, dry_run=dry_run)
            rows = [row for row in rows if int(row["id"]) not in visited]
            if not rows:
                if continuous and not dry_run:
                    time.sleep(30)
                    continue
                break
            for row in rows:
                if deadline is not None and time.monotonic() >= deadline:
                    return finish(counts, outgoing, sync and not dry_run)
                time.sleep(pause_seconds)
                rating, raw, error = "unknown", None, None
                try:
                    if product_identity(row["source_url"]) != row["source_key"]:
                        raise ValueError("No unambiguous product URL/SKU; kept unknown")
                    rating, raw = parse_product_rating(row["source_url"],
                        fetch_html(row["source_url"], timeout=20, attempts=2), row["source_key"])
                except Exception as exc:
                    error = str(exc)
                # Ctrl+C before this transaction preserves the uncommitted item.
                changed = False if dry_run else save_result(int(row["id"]), rating, raw, error)
                visited.add(int(row["id"]))
                counts["checked"] += 1
                counts["confirmed" if rating != "unknown" else "unknown"] += 1
                counts["errors"] += bool(error)
                counts["updated"] += bool(changed)
                if changed:
                    outgoing.append(dict(row,content_rating=rating,rating_raw=raw,
                                         rating_source="publisher",rating_confidence=100))
                print(json.dumps(dict(book_id=row["id"],source_key=row["source_key"],
                    rating=rating,raw=raw,updated=changed,error=error),ensure_ascii=False),flush=True)
                if limit and counts["checked"] >= limit:
                    break
            if dry_run:
                break
    except KeyboardInterrupt:
        counts["interrupted"] = True
        print("Stopped safely: committed per-book checkpoints are preserved.",flush=True)
    return finish(counts, outgoing, sync and not dry_run)


def finish(counts: dict, outgoing: list[dict], sync: bool) -> dict:
    if sync:
        raise ValueError("--sync is available only in the local edition")
    return counts


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode",choices=("30m","60m","complete","continuous"),default="30m")
    parser.add_argument("--minutes",type=float,default=None)
    parser.add_argument("--limit",type=int,default=0)
    parser.add_argument("--keys",default="")
    parser.add_argument("--dry-run",action="store_true")
    parser.add_argument("--sync",action="store_true")
    args=parser.parse_args()
    if args.sync:
        parser.error("--sync is available only in the local edition")
    minutes=args.minutes if args.minutes is not None else {"30m":30,"60m":60,"complete":0,"continuous":0}[args.mode]
    keys=[key.strip() for key in args.keys.split(",") if key.strip()] or None
    result=run_enrichment(minutes=minutes,limit=args.limit,keys=keys,dry_run=args.dry_run,sync=args.sync,
                          continuous=args.mode=='continuous')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()

