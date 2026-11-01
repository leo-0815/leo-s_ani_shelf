"""Bounded recent-window safety sweep, not a historical backfill.

Listing discovery is durable before product fetches. Daily failures use the same
queue, so URLs remain retryable even after leaving a publisher's recent list.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from .db import transaction
from .models import BookRecord
from .sources.common import fetch_html, one_year_cutoff, parse_page, polite_pause

INTERVAL_DAYS = 4
PAGES = 15
SPP_ITEMS = 200  # Sitemap equivalent; never interpret this as 15 HTML pages.
DAILY_PRODUCTS = 5  # Per publisher, shared across repeated/manual invocations.
SOURCE_SECONDS = 240
SOURCES = ("tohan", "chingwin", "kadokawa", "tongli", "spp")


def canonical_product(code: str, url: str) -> str | None:
    """Only exact, public official product URLs; no user-controlled destinations."""
    hosts = {"tohan": {"www.tohan.com.tw"}, "chingwin": {"www.ching-win.com.tw"},
             "kadokawa": {"www.kadokawa.com.tw", "kadokawa.com.tw"},
             "tongli": {"www.tongli.com.tw"}, "spp": {"www.spp.com.tw"}}
    try:
        p = urlsplit(url)
        if (code not in hosts or p.scheme not in {"http", "https"} or p.hostname not in hosts[code]
                or p.port or p.username or p.password or len(url) > 1000):
            return None
        query = parse_qs(p.query)
        path, search = p.path.rstrip("/"), ""
        if code in {"chingwin", "kadokawa"}:
            prefix = "product-detail" if code == "chingwin" else "products"
            if not re.fullmatch(rf"/{prefix}/[A-Za-z0-9_%.-]+", path):
                return None
        elif code == "spp":
            match = re.fullmatch(r"/(?:v2/official/)?SalePage/Index/(\d+)", path, re.I)
            if not match:
                return None
            path = "/SalePage/Index/" + match[1]
        elif code == "tongli":
            key = next((v[0] for k, v in query.items() if k.lower() == "bd" and len(v) == 1), "")
            if path.lower() != "/booksdetail.aspx" or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                return None
            path, search = "/BooksDetail.aspx", urlencode({"Bd": key})
        else:
            key = query.get("id", [""])[0]
            if path != "/product.php" or query.get("act") != ["view"] or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                return None
            search = urlencode({"act": "view", "id": key})
        host = "www.kadokawa.com.tw" if code == "kadokawa" else p.hostname
        return urlunsplit(("https", host, path, search, ""))
    except (ValueError, TypeError):
        return None


def enqueue(code: str, urls: list[tuple[str, str | None, dict | None]], *, error: str | None = None,
            now: datetime | None = None, known: set[str] | None = None) -> int:
    now = now or datetime.utcnow()
    count = 0
    with transaction() as connection:
        with connection.cursor() as cursor:
            for url, hint, seed in urls:
                url = canonical_product(code, url)
                if not url or (not error and known is not None and url in known):
                    continue
                digest = hashlib.sha256(url.encode()).hexdigest()
                payload = json.dumps(seed, ensure_ascii=False, default=str) if seed else None
                cursor.execute(
                    "INSERT IGNORE INTO catalog_product_retries "
                    "(publisher_code,url_hash,source_url,media_hint,seed_json,first_seen_at,next_attempt_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (code, digest, url, hint if hint in {"manga", "novel"} else None, payload, now,
                     now + timedelta(days=1) if error else now))
                count += max(cursor.rowcount, 0)
                if error:
                    # A fresh daily failure can reopen a previously successful URL;
                    # an existing pending retry retains its backoff, never gets reset.
                    cursor.execute(
                        "UPDATE catalog_product_retries SET last_error=%s, "
                        "next_attempt_at=CASE WHEN status='done' THEN %s ELSE next_attempt_at END, "
                        "status='pending' WHERE publisher_code=%s AND url_hash=%s",
                        (error[:1000], now + timedelta(days=1), code, digest))
    return count


def attach_failure_recorder(source) -> None:
    # Normal daily sources get this hook; source parser/unit tests and historical
    # tools remain independent of the new queue and its database tables.
    def record(url: str, error: str, seed: BookRecord | None = None, hint: str | None = None):
        enqueue(source.code, [(url, hint or getattr(seed, "media_type", None), asdict(seed) if seed else None)], error=error)
    source.record_product_failure = record


def acknowledge_saved(source, record: BookRecord) -> None:
    # Listing-only metadata must not acknowledge a previously failed product fetch.
    if record.source_url not in getattr(source, "product_fetch_successes", set()):
        return
    url = canonical_product(source.code, record.source_url)
    if not url:
        return
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catalog_product_retries SET status='done',last_error=NULL "
                           "WHERE publisher_code=%s AND url_hash=%s AND status='pending'",
                           (source.code, hashlib.sha256(url.encode()).hexdigest()))


def known_urls(code: str) -> set[str]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT b.source_url FROM books b JOIN publishers p ON p.id=b.publisher_id WHERE p.code=%s", (code,))
            return {url for row in cursor.fetchall() if (url := canonical_product(code, row["source_url"] or ""))}


def claim_scan(code: str, now: datetime) -> bool:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT IGNORE INTO catalog_recent_scans (publisher_code) VALUES (%s)", (code,))
            cursor.execute("SELECT * FROM catalog_recent_scans WHERE publisher_code=%s FOR UPDATE", (code,))
            row = cursor.fetchone()
            today = (now + timedelta(hours=8)).date()
            if ((row["last_success_at"] and (row["last_success_at"] + timedelta(hours=8)).date() > today - timedelta(days=INTERVAL_DAYS))
                    or (row["lease_until"] and row["lease_until"] > now)):
                return False
            cursor.execute("UPDATE catalog_recent_scans SET last_attempt_at=%s,lease_until=%s WHERE publisher_code=%s",
                           (now, now + timedelta(hours=4), code))
            return True


def finish_scan(code: str, now: datetime, pages: int, queued: int, error: str | None) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catalog_recent_scans SET lease_until=NULL,last_error=%s,pages_scanned=%s,"
                           "queued_count=%s,last_success_at=CASE WHEN %s IS NULL THEN %s ELSE last_success_at END "
                           "WHERE publisher_code=%s", (error[:1000] if error else None, pages, queued, error, now, code))


def scan_recent(source, *, deadline: float, progress: dict | None = None) -> dict:
    """Always starts at page one; no historical cursor is read or advanced."""
    code = source.code
    known = known_urls(code)
    result = progress if progress is not None else {"pages": 0, "queued": 0}

    def save(url: str, hint: str | None = None):
        if time.monotonic() >= deadline:
            raise TimeoutError("近期複查達到執行時間上限，下次仍從近期第一頁重掃")
        polite_pause(0.6)
        markup = fetch_html(url, timeout=20, attempts=1)
        result["pages"] += 1
        print(f"[recent-recheck] {code} listing {result['pages']}: {url}", flush=True)
        return markup

    def put(rows):
        result["queued"] += enqueue(code, rows, known=known)

    if code == "chingwin":
        for _, base, hint in source.category_urls:
            for page in range(1, PAGES + 1):
                url = f"{base}?page={page}"
                records, total = source._parse_catalog_page(url, save(url), hint, one_year_cutoff())
                if page == 1 and not records:
                    raise ValueError("青文近期清單未辨識到書目；保留已入列商品，不標記複查完成")
                put([(r.source_url, hint, asdict(r)) for r in records])
                if page >= total:
                    break
    elif code == "kadokawa":
        # Upcoming remains in daily scan; only broaden manga/novel recent lists.
        for hint, base in source.category_urls:
            if hint == "upcoming":
                continue
            for page in range(1, PAGES + 1):
                url = source._page_url(base, page)
                listing = parse_page(save(url))
                urls = source._product_urls(url, listing.links)
                if page == 1 and not urls:
                    raise ValueError("角川近期清單未辨識到商品，不標記複查完成")
                put([(u, hint, None) for u in urls])
                if page + 1 not in source._page_numbers(listing.links) and len(urls) < source.page_size:
                    break
    elif code == "tongli":
        for base in source.list_urls:
            for page in range(1, PAGES + 1):
                url = re.sub(r"([?&])page=\d+", rf"\g<1>page={page}", base, flags=re.I)
                listing = parse_page(save(url))
                urls = [urljoin(url, href) for href, _ in listing.links if canonical_product(code, urljoin(url, href))]
                if page == 1 and not urls:
                    raise ValueError("東立近期清單未辨識到商品，不標記複查完成")
                put([(u, "novel" if "NovelDetail" in base else "manga", None) for u in urls])
                # Bound to 15 even if pagination controls are incomplete. Empty
                # product pages are the only early stop for this source.
                if not urls:
                    break
    elif code == "tohan":
        pending = [source.list_url]
        seen = set()
        while pending and len(seen) < PAGES:
            url = pending.pop(0)
            if url in seen:
                continue
            seen.add(url)
            listing = parse_page(save(url))
            products = [urljoin(url, href) for href, _ in listing.links if canonical_product(code, urljoin(url, href))]
            if len(seen) == 1 and not products:
                raise ValueError("東販近期清單未辨識到商品，不標記複查完成")
            put([(u, None, None) for u in products])
            # Follow actual same-category pagination, never guess a catalog URL.
            for href, _ in listing.links:
                target = urlsplit(urljoin(url, href))
                query = parse_qs(target.query)
                if (target.scheme in {"http", "https"} and target.netloc == "www.tohan.com.tw"
                        and target.path == "/product.php" and query.get("cid") == ["107"]
                        and "page" in query and query.get("act", ["list"]) == ["list"]):
                    try:
                        page = int(query["page"][0])
                    except ValueError:
                        continue
                    if 2 <= page <= PAGES:
                        page_url = "https://www.tohan.com.tw/product.php?" + urlencode({"act": "list", "cid": "107", "page": page})
                        if page_url not in seen and page_url not in pending:
                            pending.append(page_url)
    elif code == "spp":
        if time.monotonic() >= deadline:
            raise TimeoutError("近期複查時間不足")
        urls = source._recent_catalog_urls(SPP_ITEMS)
        if not urls:
            raise ValueError("尖端近期 sitemap 沒有商品，不標記複查完成")
        result["pages"] = 1
        put([(u, None, None) for u in urls])
    return result


def claim_product(code: str, now: datetime) -> dict | None:
    today = (now + timedelta(hours=8)).date()
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT IGNORE INTO catalog_recent_scans (publisher_code) VALUES (%s)", (code,))
            cursor.execute("SELECT * FROM catalog_recent_scans WHERE publisher_code=%s FOR UPDATE", (code,))
            state = cursor.fetchone()
            used = state["attempted_today"] if state["budget_day"] == today else 0
            if used >= DAILY_PRODUCTS:
                return None
            cursor.execute("SELECT * FROM catalog_product_retries WHERE publisher_code=%s AND status='pending' "
                           "AND next_attempt_at<=%s ORDER BY next_attempt_at,first_seen_at,url_hash LIMIT 1 FOR UPDATE", (code, now))
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute("UPDATE catalog_recent_scans SET budget_day=%s,attempted_today=%s WHERE publisher_code=%s",
                           (today, used + 1, code))
            cursor.execute("UPDATE catalog_product_retries SET attempts=attempts+1,last_attempt_at=%s,next_attempt_at=%s "
                           "WHERE publisher_code=%s AND url_hash=%s", (now, now + timedelta(hours=1), code, row["url_hash"]))
            row["attempts"] += 1
            return row


def finish_product(row: dict, *, error: str | None = None, ignored: bool = False) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE catalog_product_retries SET status=%s,last_error=%s,next_attempt_at=%s "
                           "WHERE publisher_code=%s AND url_hash=%s",
                           ("pending" if error else "ignored" if ignored else "done", error[:1000] if error else None,
                            datetime.utcnow() + timedelta(days=min(2 ** min(row["attempts"] - 1, 3), 7)),
                            row["publisher_code"], row["url_hash"]))


def fetch_product(source, row: dict) -> BookRecord | None:
    code, url = source.code, row["source_url"]
    if canonical_product(code, url) != url:
        raise ValueError("待補抓網址不是核准的官方商品頁")
    polite_pause(1)
    markup = fetch_html(url, timeout=20, attempts=1)
    if code == "chingwin":
        from .release_dates import parse_product, PublicationDateMissing
        from .sources.product_rating import rating_fields
        seed = json.loads(row["seed_json"] or "{}")
        for field in ("release_date",):
            if seed.get(field):
                seed[field] = date.fromisoformat(seed[field])
        for field in ("release_checked_at", "rating_checked_at"):
            if seed.get(field):
                seed[field] = datetime.fromisoformat(seed[field])
        record = BookRecord(**seed)
        try:
            record = parse_product(source, asdict(record), markup)
        except ValueError as exc:
            # Some official SKU pages have specifications but no Book JSON/date.
            # Keep the seed only with exact on-page SKU evidence, never a generic
            # error/captcha/other product page or an ISBN mismatch.
            from .sources.common import parse_page
            own_sku = re.search(rf"產品編號\s*[:：]\s*{re.escape(record.source_key)}(?:\s|$)", parse_page(markup).flat_text)
            if not isinstance(exc, PublicationDateMissing) and not (str(exc) == "Product identity mismatch" and own_sku):
                raise
        return replace(record, **rating_fields(code, url, markup, record.source_key, record.isbn))
    if code == "kadokawa":
        record = source._parse_detail(url, markup, media_hint=row["media_hint"])
    elif code == "spp":
        record, _ = source._parse_detail(url, markup, enforce_cutoff=False)
    else:
        record = source._parse_detail(url, markup)
    return record if record and record.media_type in {"manga", "novel"} else None


def backlog(code: str, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS pending,MIN(first_seen_at) AS oldest,"
                           "SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) AS failed "
                           "FROM catalog_product_retries WHERE publisher_code=%s AND status='pending'", (code,))
            row = cursor.fetchone()
    oldest = row["oldest"]
    if isinstance(oldest, str):
        oldest = datetime.fromisoformat(oldest)
    return {"pending": int(row["pending"]), "failed": int(row["failed"] or 0),
            "oldest_days": max(0, (now - oldest).days) if oldest else 0}


def run_recent_rechecks(*, scan: bool = True) -> dict:
    from .crawler import SOURCES as CLASSES
    from .repository import upsert_book
    results = {}
    for code in SOURCES:
        source = CLASSES[code]()
        result = {"scan": "not_due", "checked": 0, "inserted": 0, "updated": 0, "ignored": 0, "errors": []}
        deadline = time.monotonic() + SOURCE_SECONDS

        def drain():
            while time.monotonic() < deadline:
                row = claim_product(code, datetime.utcnow())
                if not row:
                    break
                result["checked"] += 1
                try:
                    record = fetch_product(source, row)
                    if record:
                        outcome = upsert_book(record)
                        if outcome in {"inserted", "updated"}:
                            result[outcome] += 1
                    else:
                        result["ignored"] += 1
                    finish_product(row, ignored=record is None)
                except Exception as exc:
                    finish_product(row, error=str(exc))
                    result["errors"].append(str(exc)[:1000])

        # Existing retries get priority: a slow listing scan cannot starve them.
        drain()
        if scan and time.monotonic() < deadline and claim_scan(code, datetime.utcnow()):
            progress = {"pages": 0, "queued": 0}
            try:
                scan_recent(source, deadline=deadline, progress=progress)
                result.update(progress, scan="completed")
                finish_scan(code, datetime.utcnow(), progress["pages"], progress["queued"], None)
            except Exception as exc:
                result["scan"] = "partial"
                result["errors"].append(str(exc)[:1000])
                result.update(progress)
                finish_scan(code, datetime.utcnow(), progress["pages"], progress["queued"], str(exc))
        drain()
        result.update(backlog(code))
        result["daily_limit"] = DAILY_PRODUCTS
        result["warning"] = bool(result["errors"] or result["oldest_days"] >= 7)
        results[code] = result
        print(f"[recent-recheck] {code}: scan={result['scan']}, checked={result['checked']}, "
              f"pending={result['pending']}, errors={len(result['errors'])}", flush=True)
    return results


def main() -> None:
    import argparse
    from .db import ensure_schema
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="Read queue/scan status without crawling or notifications")
    args = parser.parse_args()
    if args.status:
        with transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM catalog_recent_scans ORDER BY publisher_code")
                result = {row["publisher_code"]: {**row, **backlog(row["publisher_code"])} for row in cursor.fetchall()}
    else:
        ensure_schema()
        result = run_recent_rechecks()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not args.status and any(row.get("warning") for row in result.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
