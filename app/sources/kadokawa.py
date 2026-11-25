from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Iterator
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from ..models import BookRecord, detect_edition, extract_volume, infer_series_title
from .common import fetch_html, parse_page, unique
from .kadokawa_catalog import verified_listing


class _RateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1.0 / max(requests_per_second, 0.1)
        self.lock = threading.Lock()
        self.next_start = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_start - now)
            self.next_start = max(now, self.next_start) + self.interval
        if delay:
            time.sleep(delay)


class KadokawaSource:
    code = "kadokawa"
    name = "台灣角川"
    category_urls = (
        ("upcoming", "https://www.kadokawa.com.tw/categories/%E5%8D%B3%E5%B0%87%E4%B8%8A%E5%B8%82"),
        ("manga", "https://www.kadokawa.com.tw/categories/%E6%BC%AB%E7%95%AB"),
        ("novel", "https://www.kadokawa.com.tw/categories/%E8%BC%95%E5%B0%8F%E8%AA%AA"),
    )
    page_size = 72
    workers = 2
    requests_per_second = 2.0
    incremental_catalog_pages = 3
    checkpoint_version = "catalog_v3"
    history_page_size = 24
    max_history_pages = 500
    latest_cursor: str | None = None

    def __init__(self) -> None:
        self._limiter = _RateLimiter(self.requests_per_second)
        self.errors: list[str] = []
        self.skipped_count = 0

    def collect(
        self,
        known_keys: set[str] | None = None,
        cursor_value: str | None = None,
        backfill: bool = False,
    ) -> list[BookRecord]:
        records: list[BookRecord] = []
        for batch, _checkpoint in self.collect_batches(
            known_keys=known_keys,
            cursor_value=cursor_value,
            backfill=backfill,
        ):
            records.extend(batch)
        return records

    def collect_batches(
        self,
        known_keys: set[str] | None = None,
        cursor_value: str | None = None,
        backfill: bool = False,
    ) -> Iterator[tuple[list[BookRecord], dict[str, Any] | None]]:
        if backfill:
            yield from self._verified_history_batches(known_keys)
            return

        seen_keys = set(known_keys or set())
        self.latest_cursor = self.category_urls[0][1]
        for segment, base_url in self.category_urls:
            page_number = 1
            max_incremental_pages = (
                None if segment == "upcoming" else self.incremental_catalog_pages
            )
            while True:
                if max_incremental_pages and page_number > max_incremental_pages:
                    break
                listing_url = self._page_url(base_url, page_number)
                listing = parse_page(self._fetch(listing_url))
                product_urls = self._product_urls(listing_url, listing.links)
                page_numbers = self._page_numbers(listing.links)
                candidates: list[str] = []
                for url in product_urls:
                    source_key = self._source_key(url)
                    if not source_key or source_key in seen_keys:
                        continue
                    seen_keys.add(source_key)
                    candidates.append(url)
                media_hint = segment if segment in {"manga", "novel"} else None
                records = self._fetch_records(candidates, media_hint=media_hint)
                has_next = (
                    page_number + 1 in page_numbers
                    or len(product_urls) >= self.page_size
                )
                next_page = page_number + 1
                completed = not has_next
                print(
                    f"角川 {segment} 第 {page_number} 頁："
                    f"{len(product_urls)} 個商品、{len(candidates)} 個未收錄、"
                    f"解析 {len(records)} 筆",
                    flush=True,
                )
                yield records, None
                if completed:
                    break
                page_number = next_page

    def _verified_history_batches(self, known_keys):
        from ..repository import get_backfill_progress
        from ..series import series_key
        from ..series_backfill import VARIANT_TITLE, kadokawa_variants

        progress = get_backfill_progress(self.code)
        self.latest_cursor = self.category_urls[0][1]
        known_keys = set(known_keys or ())
        seen_variants = set()
        known_isbns = None
        folded_isbns = set()
        for segment, base in self.category_urls:
            name = f"{self.checkpoint_version}_{segment}"
            saved = progress.get(name, {})
            if saved.get("completed"):
                continue
            # v3 is 24 items/page. It does not inherit old 72-item offsets.
            page_number = max(1, int(saved.get("next_page", 1)) - 2)
            fingerprints = set()
            media_hint = segment if segment in {"manga", "novel"} else None
            while page_number <= self.max_history_pages:
                url = self._page_url(base, page_number, limit=self.history_page_size)
                cards, has_next = verified_listing(url, self._fetch(url), page_number)
                fingerprint = frozenset(c.url for c in cards)
                if fingerprint in fingerprints:
                    raise ValueError(f"角川第 {page_number} 頁：官網回傳重複商品頁")
                fingerprints.add(fingerprint)
                before_errors = len(self.errors)
                ordinary = []
                variants = []
                for card in cards:
                    variant = VARIANT_TITLE.fullmatch(card.title.strip())
                    if not variant:
                        key = self._source_key(card.url)
                        if key not in known_keys:
                            ordinary.append(card.url)
                            known_keys.add(key)
                        continue
                    variants.append((card, variant))
                records = [book for book in self._fetch_records(ordinary, media_hint=media_hint, strict=True)
                           if book.isbn not in folded_isbns]
                for card, variant in variants:
                    if card.url in seen_variants:
                        continue
                    seen_variants.add(card.url)
                    row = {"code": self.code, "media_type": "manga" if variant[2] == "漫畫" else "novel",
                           "series_key": series_key(variant[1], self.code)}
                    try:
                        books = kadokawa_variants(row, card.url, self._fetch(card.url))
                        if not books:
                            # Official item exists, but its labels/GTINs cannot identify single books.
                            self.skipped_count += 1
                            continue
                        if known_isbns is None:
                            known_isbns = self._history_isbns()
                        known_isbns.update(b.isbn for b in records if b.isbn)
                        for book in books:
                            if book.source_key in known_keys or book.isbn in known_isbns:
                                continue
                            records.append(book)
                            known_keys.add(book.source_key)
                            known_isbns.add(book.isbn)
                            folded_isbns.add(book.isbn)
                    except Exception as exc:
                        self.errors.append(f"{card.url}：{exc}")
                if known_isbns is not None:
                    known_isbns.update(b.isbn for b in records if b.isbn)
                errors = len(self.errors) - before_errors
                print(f"角川 {name} 第 {page_number} 頁：{len(cards)} 張商品卡、{len(records)} 筆、{errors} 個錯誤", flush=True)
                yield records, {"segment": name, "next_page": page_number + 1,
                                "completed": not has_next and not errors, "error_count": errors}
                if errors or not has_next:
                    break
                page_number += 1
            else:
                raise ValueError(f"角川 {name} 已達分頁安全上限，未標記完成")

    def _history_isbns(self):
        from ..db import transaction
        with transaction() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT b.isbn FROM books b JOIN publishers p ON p.id=b.publisher_id "
                           "WHERE p.code=%s AND b.isbn IS NOT NULL", (self.code,))
            return {row["isbn"].replace("-", "").strip() for row in cursor.fetchall()}

    def commit_batch(self, checkpoint: dict[str, Any] | None) -> None:
        if not checkpoint:
            return
        from ..repository import save_backfill_progress

        save_backfill_progress(
            self.code,
            str(checkpoint["segment"]),
            int(checkpoint["next_page"]),
            bool(checkpoint["completed"]),
        )

    def _fetch_records(
        self,
        urls: list[str],
        media_hint: str | None = None,
        *,
        strict: bool = False,
    ) -> list[BookRecord]:
        if not urls:
            return []
        records: list[BookRecord] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._fetch_record, url, media_hint): url for url in urls
            }
            for future in as_completed(futures):
                try:
                    record = future.result()
                    if record and record.media_type in {"manga", "novel"}:
                        records.append(record)
                    elif record:
                        self.skipped_count += 1
                except ValueError as exc:
                    if strict:
                        self.errors.append(f"{futures[future]}：{exc}")
                    else:
                        self.skipped_count += 1
                except Exception as exc:
                    self.errors.append(f"{futures[future]}：{exc}")
        records.sort(key=lambda record: (record.release_date or date.min, record.title))
        return records

    def _fetch_record(
        self,
        url: str,
        media_hint: str | None = None,
    ) -> BookRecord | None:
        return self._parse_detail(url, self._fetch(url), media_hint=media_hint)

    def _fetch(self, url: str) -> str:
        self._limiter.wait()
        return fetch_html(url, timeout=30, attempts=3)

    def _parse_detail(
        self,
        url: str,
        markup: str,
        media_hint: str | None = None,
    ) -> BookRecord:
        page = parse_page(markup)
        title = self._clean_title(page.meta.get("og:title", "").strip())
        source_key = self._source_key(url)
        if not title or not source_key:
            raise ValueError("角川商品頁缺少書名或商品編號")

        title_positions = [
            match.start() for match in re.finditer(re.escape(title), page.flat_text)
        ]
        dated_positions = [
            position
            for position in title_positions
            if "上市日期：" in page.flat_text[position : position + 2500]
        ]
        title_pos = (
            dated_positions[-1]
            if dated_positions
            else title_positions[-1]
            if title_positions
            else max(0, page.flat_text.rfind("上市日期：") - 800)
        )
        detail = page.flat_text[title_pos : title_pos + 2500]
        author = self._field(
            detail,
            r"(?:作者資訊：|作者：)\s*(.+?)(?=\s+(?:上市日期：|ISBN：|條碼：|NT\$|\{\{))",
        )
        release_text = self._field(detail, r"上市日期：\s*(20\d{2}/\d{1,2}/\d{1,2})")
        isbn = self._field(detail, r"(?:ISBN|條碼)：\s*([0-9Xx-]{8,24})")
        price_text = self._field(
            detail,
            r"(?:ISBN|條碼)：\s*[0-9Xx-]{8,24}.{0,80}?NT\$\s*([0-9,]+)",
        )
        if not price_text:
            price_text = self._field(detail, r"NT\$\s*([0-9,]+)")
        breadcrumb = page.flat_text[max(0, title_pos - 450) : title_pos]
        novel_pos, manga_pos = breadcrumb.rfind("輕小說"), breadcrumb.rfind("漫畫")
        if media_hint in {"manga", "novel"}:
            media_type = media_hint
        elif novel_pos > manga_pos:
            media_type = "novel"
        elif manga_pos >= 0:
            media_type = "manga"
        else:
            media_type = "unknown"
        if author:
            author = re.sub(r"^作者：\s*", "", author)
            author = author.split("{{", 1)[0].strip()[:500] or None
        return BookRecord(
            publisher_code=self.code,
            source_key=source_key,
            title=title,
            media_type=media_type,
            author=author,
            isbn=isbn.replace("-", "") if isbn else None,
            cover_url=page.meta.get("og:image") or None,
            list_price=int(price_text.replace(",", "")) if price_text else None,
            release_date=(
                date(*(int(part) for part in release_text.split("/")))
                if release_text
                else None
            ),
            release_precision="day" if release_text else "unknown",
            edition_type=detect_edition(title),
            volume_label=extract_volume(title),
            series_title=infer_series_title(title),
            source_url=url,
        )

    @classmethod
    def _product_urls(cls, page_url: str, links: list[tuple[str, str]]) -> list[str]:
        return unique(
            [
                urljoin(page_url, href)
                for href, text in links
                if "/products/" in href and "加入購物車" in text
            ]
        )

    @staticmethod
    def _page_numbers(links: list[tuple[str, str]]) -> set[int]:
        numbers: set[int] = set()
        for href, _text in links:
            query = parse_qs(urlparse(href).query)
            if query.get("page") and query["page"][0].isdigit():
                numbers.add(int(query["page"][0]))
        return numbers

    @classmethod
    def _page_url(cls, base_url: str, page_number: int, *, limit: int | None = None) -> str:
        parsed = urlparse(base_url)
        query = parse_qs(parsed.query)
        query.update({"limit": [str(limit or cls.page_size)], "page": [str(page_number)]})
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                urlencode(query, doseq=True),
                "",
            )
        )

    @staticmethod
    def _source_key(url: str) -> str:
        return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _clean_title(title: str) -> str:
        return re.sub(r"^預購\s*[-－]\s*", "", title).strip()

    @staticmethod
    def _field(text: str, pattern: str) -> str | None:
        match = re.search(pattern, text)
        return match.group(1).strip() if match else None
