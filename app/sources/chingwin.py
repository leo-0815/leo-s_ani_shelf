from __future__ import annotations

import hashlib
import html
import json
import math
import re
from datetime import date
from dataclasses import asdict, replace
from typing import Any, Iterator
from urllib.parse import urljoin

from ..models import BookRecord, detect_edition, extract_volume, infer_series_title
from .common import fetch_html, one_year_cutoff, parse_page, polite_pause, unique


class ChingWinSource:
    code = "chingwin"
    name = "青文出版社"
    list_url = "https://www.ching-win.com.tw/about-news/"
    category_urls = (
        ("comic", "https://www.ching-win.com.tw/products/chingwin/books/comic/", "manga"),
        ("novel", "https://www.ching-win.com.tw/products/chingwin/books/novel/", "novel"),
    )
    max_articles = 6
    incremental_catalog_pages = 3
    page_size = 24
    checkpoint_version = "one_year_v1"
    latest_cursor: str | None = None

    def __init__(self) -> None:
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
        from ..repository import get_backfill_progress

        seen_keys = set(known_keys or set())
        if not backfill:
            schedule_records = self._collect_schedule(seen_keys, cursor_value)
            seen_keys.update(record.source_key for record in schedule_records)
            if schedule_records:
                yield schedule_records, None

        progress = get_backfill_progress(self.code) if backfill else {}
        cutoff = one_year_cutoff()
        for segment, base_url, media_type in self.category_urls:
            checkpoint_segment = f"{self.checkpoint_version}_{segment}"
            saved = progress.get(checkpoint_segment, {})
            if backfill and saved.get("completed"):
                print(f"青文一年期回填：略過已完成區段 {segment}", flush=True)
                continue
            page_number = int(saved.get("next_page", 1)) if backfill else 1
            max_pages = None if backfill else self.incremental_catalog_pages
            while True:
                if max_pages and page_number > max_pages:
                    break
                page_url = f"{base_url}?page={page_number}"
                markup = fetch_html(page_url, timeout=30, attempts=3)
                page_records, total_pages = self._parse_catalog_page(
                    page_url,
                    markup,
                    media_type,
                    cutoff,
                )
                records = [
                    record for record in page_records if record.source_key not in seen_keys
                ]
                seen_keys.update(record.source_key for record in records)
                records = [self._with_product_rating(record) for record in records]
                completed = page_number >= total_pages
                checkpoint = (
                    {
                        "segment": checkpoint_segment,
                        "next_page": page_number + 1,
                        "completed": completed,
                        "error_count": 0,
                    }
                    if backfill
                    else None
                )
                print(
                    f"青文 {segment} 第 {page_number}/{total_pages} 頁："
                    f"一年窗口 {len(page_records)} 筆、未收錄 {len(records)} 筆",
                    flush=True,
                )
                yield records, checkpoint
                if completed:
                    break
                page_number += 1
                polite_pause(0.35)

    def _with_product_rating(self, record: BookRecord) -> BookRecord:
        # A failed detail request remains unknown; never infer general from a title.
        polite_pause(1.5)
        try:
            markup = fetch_html(record.source_url, timeout=20, attempts=2)
            from ..release_dates import parse_product
            try:
                record = parse_product(self, asdict(record), markup)
            except ValueError:
                pass  # No explicit SKU publication field: retain catalog fallback.
            from .product_rating import rating_fields
            return replace(record, **rating_fields(self.code, record.source_url, markup,
                                                  record.source_key, record.isbn))
        except Exception as exc:
            self.errors.append(f'青文商品分級 {record.source_key}: {exc}')
            return record

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

    def _collect_schedule(
        self,
        known_keys: set[str],
        cursor_value: str | None,
    ) -> list[BookRecord]:
        first_listing = parse_page(fetch_html(self.list_url))
        article_urls = unique(
            [
                urljoin(self.list_url, href)
                for href, text in first_listing.links
                if "/about-news-detail/" in href
                and "青文出版社" in text
                and "預定出書表" in text
            ]
        )
        self.latest_cursor = article_urls[0] if article_urls else cursor_value
        if cursor_value in article_urls:
            # Revisit the cursor and one older announcement for in-place edits.
            article_urls = article_urls[: article_urls.index(cursor_value) + 2]
        else:
            article_urls = article_urls[: self.max_articles]
        records: list[BookRecord] = []
        for url in article_urls:
            polite_pause()
            records.extend(
                record
                for record in self._parse_article(url, fetch_html(url))
            )
        return records

    def _parse_catalog_page(
        self,
        page_url: str,
        markup: str,
        media_type: str,
        cutoff: date,
    ) -> tuple[list[BookRecord], int]:
        objects: list[dict[str, Any]] = []
        for raw in re.findall(
            r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            markup,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            try:
                value = json.loads(html.unescape(raw).strip())
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                objects.append(value)

        products: dict[str, dict[str, Any]] = {}
        for value in objects:
            if str(value.get("@type", "")).lower() != "product":
                continue
            name = self._clean_title(str(value.get("name", "")))
            if name:
                products[name] = value

        results: list[BookRecord] = []
        for value in objects:
            if str(value.get("@type", "")).lower() != "book":
                continue
            title = self._clean_title(str(value.get("name", "")))
            product = products.get(title, {})
            source_key = str(product.get("sku", "")).strip()
            release_text = str(value.get("datePublished", "")).strip()
            release = self._date(release_text)
            if not title or not source_key:
                self.skipped_count += 1
                continue
            if release and release < cutoff:
                continue
            author_value = value.get("author") or {}
            author_names = author_value.get("name") if isinstance(author_value, dict) else None
            if isinstance(author_names, list):
                author = str(author_names[0]).strip() if author_names else None
            else:
                author = str(author_names).strip() if author_names else None
            offers = product.get("offers") if isinstance(product.get("offers"), dict) else {}
            price_text = str(offers.get("price", "")).replace(",", "")
            list_price_match = re.search(
                rf"[\"']sku[\"']\s*:\s*[\"']{re.escape(source_key)}[\"']"
                r".{0,5000}?font-delete.{0,300}?NT\$\s*([0-9,]+)",
                markup,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if list_price_match:
                price_text = list_price_match.group(1).replace(",", "")
            isbn = re.sub(r"[^0-9Xx]", "", str(value.get("isbn", ""))) or None
            results.append(
                BookRecord(
                    publisher_code=self.code,
                    source_key=source_key,
                    title=title,
                    media_type=media_type,
                    author=author,
                    isbn=isbn,
                    cover_url=str(value.get("image") or product.get("image") or "") or None,
                    list_price=int(price_text) if price_text.isdigit() else None,
                    release_date=release,
                    release_precision="day" if release else "unknown",
                    edition_type=detect_edition(title),
                    volume_label=extract_volume(title),
                    series_title=infer_series_title(title),
                    source_url=urljoin(page_url, f"/product-detail/{source_key}"),
                )
            )

        total_match = re.search(
            r"Showing\s+\d+\s*-\s*\d+\s+of\s+([0-9,]+)\s+results",
            parse_page(markup).flat_text,
            flags=re.IGNORECASE,
        )
        total = int(total_match.group(1).replace(",", "")) if total_match else len(results)
        total_pages = max(1, math.ceil(total / self.page_size))
        return results, total_pages

    def _parse_article(self, url: str, markup: str) -> list[BookRecord]:
        page = parse_page(markup)
        title = page.meta.get("og:title", "") or page.flat_text[:300]
        month_match = re.search(r"(20\d{2})年\s*(\d{1,2})月", title)
        if not month_match:
            month_match = re.search(r"(20\d{2})年\s*(\d{1,2})月", page.flat_text)
        if not month_match:
            raise ValueError("預定表缺少年月")
        release = date(int(month_match.group(1)), int(month_match.group(2)), 1)
        flat = page.flat_text
        pattern = re.compile(
            r"類別：\s*(漫畫|小說|輕小說)\s+書名：\s*(.+?)\s+作者：\s*(.+?)\s+"
            r"書系：\s*(.*?)\s+定價：\s*([0-9,]+)\s+首刷附錄：\s*(.*?)\s*"
            r"電子書：\s*(.*?)(?=類別：|會員服務|$)"
        )
        results: list[BookRecord] = []
        for match in pattern.finditer(flat):
            category, book_title, author, _, price, _, ebook = match.groups()
            book_title = book_title.strip()
            if book_title == "書名" or not book_title:
                continue
            digest = hashlib.sha1(
                f"{url}|{category}|{book_title}|{author}".encode("utf-8")
            ).hexdigest()
            edition = "digital" if "電子" in book_title else detect_edition(book_title)
            results.append(
                BookRecord(
                    publisher_code=self.code,
                    source_key=digest,
                    title=book_title,
                    media_type="manga" if category == "漫畫" else "novel",
                    author=author.strip(),
                    list_price=int(price.replace(",", "")),
                    release_date=release,
                    release_precision="month",
                    edition_type=edition,
                    volume_label=extract_volume(book_title),
                    series_title=infer_series_title(book_title),
                    source_url=url,
                )
            )
            if "〇" in ebook and edition != "digital":
                pass
        return results

    @staticmethod
    def _clean_title(title: str) -> str:
        title = re.sub(r"^\s*[（(]?預購[）)]?\s*", "", title)
        title = re.sub(r"^\s*輕小說\s*", "", title)
        return title.strip()

    @staticmethod
    def _date(value: str) -> date | None:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
