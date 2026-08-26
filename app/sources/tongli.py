from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Iterator
from urllib.parse import urljoin

from ..models import BookRecord, detect_edition, extract_volume, infer_series_title
from .common import (
    RateLimiter,
    fetch_html,
    one_year_cutoff,
    parse_page,
    parse_tables,
    polite_pause,
    unique,
)


class TongLiSource:
    code = "tongli"
    name = "東立出版社"
    list_urls = (
        "https://www.tongli.com.tw/webpagebooks.aspx?page=1&s=1",
        "https://www.tongli.com.tw/NovelDetail.aspx?page=1&s=1",
    )
    sitemap_url = "https://www.tongli.com.tw/SitemapBooks.aspx"
    schedule_url = "https://www.tongli.com.tw/Search1.aspx?Page=1"
    schedule_pages = 3
    checkpoint_segment = "one_year_v1_catalog"
    batch_size = 40
    workers = 3
    requests_per_second = 2.5
    old_batches_to_stop = 3
    max_items = 30
    latest_cursor: str | None = None

    def __init__(self) -> None:
        self._limiter = RateLimiter(self.requests_per_second)
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
        known_keys = known_keys or set()
        if backfill:
            yield from self._collect_sitemap_batches(set(known_keys))
            return

        schedule_records = self._collect_schedule()
        if schedule_records:
            yield schedule_records, None

        urls: list[str] = []
        for list_url in self.list_urls:
            listing = parse_page(fetch_html(list_url))
            for href, _text in listing.links:
                absolute = urljoin(list_url, href)
                match = re.search(r"[?&][Bb][Dd]=([^&]+)", absolute)
                if match and match.group(1) not in known_keys:
                    urls.append(absolute)
        self.latest_cursor = self.list_urls[0]
        records: list[BookRecord] = []
        for url in unique(urls)[: self.max_items]:
            polite_pause(0.35)
            try:
                records.append(self._parse_detail(url, fetch_html(url)))
            except ValueError:
                self.skipped_count += 1
            except RuntimeError as exc:
                self.errors.append(f"{url}：{exc}")
        if not records and not known_keys:
            raise RuntimeError("東立新書明細沒有解析到有效書目")
        yield records, None

    def _collect_schedule(self) -> list[BookRecord]:
        records: list[BookRecord] = []
        for page_number in range(1, self.schedule_pages + 1):
            url = f"https://www.tongli.com.tw/Search1.aspx?Page={page_number}"
            records.extend(
                self._parse_schedule(url, fetch_html(url, timeout=30, attempts=5))
            )
            if page_number < self.schedule_pages:
                polite_pause(0.35)
        return records

    def _parse_schedule(self, url: str, markup: str) -> list[BookRecord]:
        records: list[BookRecord] = []
        for row in parse_tables(markup):
            if len(row) < 5:
                continue
            title, author, series, _size, price_text = (
                row[0].strip(),
                row[1].strip(),
                row[2].strip(),
                row[3].strip(),
                row[4].strip(),
            )
            title = re.sub(r"^#+\s*", "", title)
            if not title or title in {"書名／集數", "書名/集數"}:
                continue
            media_type = "novel" if "小說" in series else "manga"
            digest = hashlib.sha1(
                f"planned|{title}|{author}|{series}".encode("utf-8")
            ).hexdigest()
            price_digits = price_text.replace(",", "")
            records.append(
                BookRecord(
                    publisher_code=self.code,
                    source_key=f"planned:{digest}",
                    title=title,
                    media_type=media_type,
                    author=author or None,
                    list_price=int(price_digits) if price_digits.isdigit() else None,
                    release_date=None,
                    release_precision="unknown",
                    release_status="scheduled",
                    edition_type=detect_edition(title),
                    volume_label=extract_volume(title),
                    series_title=infer_series_title(title),
                    source_url=url,
                )
            )
        return records

    def _collect_sitemap_batches(
        self,
        known_keys: set[str],
    ) -> Iterator[tuple[list[BookRecord], dict[str, Any]]]:
        from ..repository import get_backfill_progress

        progress = get_backfill_progress(self.code).get(self.checkpoint_segment, {})
        if progress.get("completed"):
            print("東立大型回填：一年商品區段已完成", flush=True)
            return
        markup = fetch_html(self.sitemap_url, timeout=40, attempts=3)
        urls = re.findall(r"<loc>\s*(.*?)\s*</loc>", markup, re.I)
        self.latest_cursor = urls[0] if urls else None
        start = int(progress.get("next_page", 0))
        cutoff = one_year_cutoff()
        consecutive_old_batches = 0
        for offset in range(start, len(urls), self.batch_size):
            page_urls = urls[offset : offset + self.batch_size]
            candidates = [
                url for url in page_urls if self._source_key(url) not in known_keys
            ]
            before_errors = len(self.errors)
            parsed = self._fetch_detail_batch(candidates)
            records: list[BookRecord] = []
            known_dates: list[date] = []
            for record in parsed:
                if record.release_date:
                    known_dates.append(record.release_date)
                if record.release_date and record.release_date < cutoff:
                    continue
                records.append(record)
                known_keys.add(record.source_key)
            enough_dates = len(known_dates) >= max(1, int(len(candidates) * 0.8))
            all_old = enough_dates and all(release < cutoff for release in known_dates)
            consecutive_old_batches = consecutive_old_batches + 1 if all_old else 0
            next_offset = offset + len(page_urls)
            completed = (
                next_offset >= len(urls)
                or consecutive_old_batches >= self.old_batches_to_stop
            )
            error_count = len(self.errors) - before_errors
            print(
                f"東立商品 {offset + 1}-{next_offset}/{len(urls)}："
                f"未收錄頁 {len(candidates)}、一年內候選 {len(records)} 筆、"
                f"錯誤 {error_count}",
                flush=True,
            )
            yield records, {
                "segment": self.checkpoint_segment,
                "next_page": next_offset,
                "completed": completed,
                "error_count": error_count,
            }
            if completed:
                break

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

    def _fetch_detail_batch(self, urls: list[str]) -> list[BookRecord]:
        def fetch_one(url: str) -> BookRecord:
            self._limiter.wait()
            return self._parse_detail(url, fetch_html(url, timeout=30, attempts=3))

        records: list[BookRecord] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(fetch_one, url) for url in urls]
            for url, future in zip(urls, futures):
                try:
                    records.append(future.result())
                except ValueError:
                    self.skipped_count += 1
                except Exception as exc:
                    self.errors.append(f"{url}：{exc}")
        return records

    def _parse_detail(self, url: str, markup: str) -> BookRecord:
        page = parse_page(markup)
        title_match = re.search(
            r"(?:漫畫|小說)書籍資料\s*\n([^\n]+)\n原文書名：", page.text
        )
        source_match = re.search(r"[?&][Bb][Dd]=([^&]+)", url)
        if not title_match or not source_match:
            raise ValueError("東立商品頁缺少書名或商品編號")
        title = title_match.group(1).strip()
        detail_start = page.flat_text.find(title)
        detail_end = page.flat_text.find("內容簡介", detail_start)
        detail = page.flat_text[detail_start : detail_end if detail_end >= 0 else None]
        volume = self._field(detail, r"集數：\s*第?\s*([0-9]+|全)\s*集")
        if volume and not extract_volume(title):
            title = f"{title} ({volume})"
        author = self._field(detail, r"作者：\s*(.+?)\s+(?:插畫：|系列別：)")
        isbn = self._field(detail, r"ISBN：\s*([0-9Xx-]{8,24})")
        release_text = self._field(detail, r"出版日期：\s*(20\d{2}/\d{1,2}/\d{1,2})")
        price_text = self._field(detail, r"新台幣售價：\s*([0-9,]+)\s*元")
        release = (
            date(*(int(part) for part in release_text.split("/")))
            if release_text
            else None
        )
        media_type = "novel" if "小說書籍資料" in page.text[: detail_start + 300] else "manga"
        return BookRecord(
            publisher_code=self.code,
            source_key=source_match.group(1),
            title=title,
            media_type=media_type,
            author=author,
            isbn=isbn.replace("-", "") if isbn else None,
            cover_url=page.meta.get("og:image") or None,
            list_price=int(price_text.replace(",", "")) if price_text else None,
            release_date=release,
            release_precision="day" if release else "unknown",
            edition_type=detect_edition(title),
            volume_label=volume or extract_volume(title),
            series_title=infer_series_title(title),
            source_url=url,
        )

    @staticmethod
    def _field(text: str, pattern: str) -> str | None:
        match = re.search(pattern, text)
        return match.group(1).strip() if match else None

    @staticmethod
    def _source_key(url: str) -> str:
        match = re.search(r"[?&][Bb][Dd]=([^&]+)", url)
        return match.group(1) if match else ""
