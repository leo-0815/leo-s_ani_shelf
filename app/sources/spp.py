from __future__ import annotations

import gzip
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Iterator

from ..models import BookRecord, detect_edition, extract_volume, infer_series_title
from .common import (
    RateLimiter,
    fetch_bytes,
    fetch_html,
    history_cutoff,
    history_segment,
    one_year_cutoff,
    parse_page,
    parse_tables,
)


class SppSource:
    code = "spp"
    name = "尖端出版"
    list_url = "https://event.spp.com.tw/event/publish.aspx"
    sitemap_url = "https://www.spp.com.tw/Sitemap/sitemap_ShopSalePage.xml.gz"
    batch_size = 40
    workers = 3
    requests_per_second = 2.5
    old_batches_to_stop = 3
    latest_cursor: str | None = None

    def __init__(self) -> None:
        self._limiter = RateLimiter(self.requests_per_second)
        self.errors: list[str] = []
        self.skipped_count = 0
        self._catalog_cutoff = one_year_cutoff()

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
            yield from self._collect_catalog_batches(set(known_keys))
            return

        records = self._parse_schedule(self.list_url, fetch_html(self.list_url))
        if records:
            self.latest_cursor = max(
                record.release_date.isoformat()
                for record in records
                if record.release_date
            )
        records = [record for record in records if record.source_key not in known_keys]
        if not records and not known_keys:
            raise RuntimeError("尖端上市表沒有解析到有效書目")
        yield records, None

    def _collect_catalog_batches(
        self,
        known_keys: set[str],
    ) -> Iterator[tuple[list[BookRecord], dict[str, Any]]]:
        from ..repository import get_backfill_progress

        checkpoint_segment = history_segment("catalog")
        progress = get_backfill_progress(self.code).get(checkpoint_segment, {})
        if progress.get("completed"):
            print("尖端大型回填：一年商品區段已完成", flush=True)
            return
        urls = self._sitemap_urls()
        self.latest_cursor = urls[0] if urls else None
        start = max(0, int(progress.get("next_page", 0)) - self.batch_size)
        cutoff = history_cutoff()
        self._catalog_cutoff = cutoff
        consecutive_old_batches = 0
        for offset in range(start, len(urls), self.batch_size):
            candidates = urls[offset : offset + self.batch_size]
            before_errors = len(self.errors)
            parsed = self._fetch_catalog_batch(candidates)
            records: list[BookRecord] = []
            known_dates: list[date] = []
            for record, release in parsed:
                if release:
                    known_dates.append(release)
                if record:
                    records.append(record)
                    known_keys.add(record.source_key)
            enough_dates = len(known_dates) >= max(1, int(len(candidates) * 0.8))
            all_old = enough_dates and all(release < cutoff for release in known_dates)
            consecutive_old_batches = consecutive_old_batches + 1 if all_old else 0
            next_offset = offset + len(candidates)
            completed = (
                next_offset >= len(urls)
                or consecutive_old_batches >= self.old_batches_to_stop
            )
            error_count = len(self.errors) - before_errors
            print(
                f"尖端商品 {offset + 1}-{next_offset}/{len(urls)}："
                f"一年內新增候選 {len(records)} 筆、錯誤 {error_count}",
                flush=True,
            )
            yield records, {
                "segment": checkpoint_segment,
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

    def _sitemap_urls(self) -> list[str]:
        payload = fetch_bytes(
            self.sitemap_url,
            timeout=30,
            attempts=3,
            accept="application/xml,application/gzip,*/*",
        )
        if payload[:2] == b"\x1f\x8b":
            payload = gzip.decompress(payload)
        return re.findall(r"<loc>\s*(.*?)\s*</loc>", payload.decode("utf-8"), re.I)

    def _fetch_catalog_batch(
        self,
        urls: list[str],
    ) -> list[tuple[BookRecord | None, date | None]]:
        def fetch_one(url: str) -> tuple[BookRecord | None, date | None]:
            self._limiter.wait()
            return self._parse_detail(url, fetch_html(url, timeout=30, attempts=3))

        results: list[tuple[BookRecord | None, date | None]] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(fetch_one, url) for url in urls]
            for url, future in zip(urls, futures):
                try:
                    results.append(future.result())
                except ValueError:
                    self.skipped_count += 1
                    results.append((None, None))
                except Exception as exc:
                    self.errors.append(f"{url}：{exc}")
                    results.append((None, None))
        return results

    def _parse_detail(
        self,
        url: str,
        markup: str,
    ) -> tuple[BookRecord | None, date | None]:
        marker = 'SalePageIndexViewModel"] = '
        position = markup.find(marker)
        if position < 0:
            raise ValueError("尖端商品頁缺少內嵌商品資料")
        raw = markup[position + len(marker) :].replace("\\'", "'")
        data = json.JSONDecoder().raw_decode(raw.lstrip())[0]
        title = str(data.get("Title", "")).strip()
        sale_page_id = str(data.get("Id", "")).strip()
        category = str(
            (data.get("CategoryLevelName") or {}).get("Level1_ShopCategory_Name", "")
        ).strip()
        selling_start = str(data.get("SellingStartDateTime", "")).strip()
        release = self._date(selling_start[:10])
        if category not in {"漫畫", "輕小說"}:
            return None, release
        if not title or not sale_page_id:
            raise ValueError("尖端商品頁缺少書名或商品編號")

        description = parse_page(str(data.get("ShortDescription", ""))).flat_text
        book_number = self._field(description, r"書\s*號：\s*([A-Za-z0-9-]+)")
        author = self._field(
            description,
            r"作\s*者：\s*(.+?)(?=\s+(?:譯\s*者|繪\s*者|書\s*系|規\s*格|等\s*級|上市日)：)",
        )
        isbn = self._field(description, r"條\s*碼：\s*([0-9Xx-]{8,24})")
        if not release:
            release = self._date(
                self._field(description, r"上市日：\s*(20\d{2}/\d{1,2}/\d{1,2})")
            )
        if release and release < self._catalog_cutoff:
            return None, release
        images = data.get("ImageList") or []
        cover_url = str(images[0].get("PicUrl", "")).strip() if images else ""
        if cover_url.startswith("//"):
            cover_url = "https:" + cover_url
        price = data.get("SuggestPrice")
        record = BookRecord(
            publisher_code=self.code,
            source_key=book_number or sale_page_id,
            title=title,
            media_type="novel" if category == "輕小說" else "manga",
            author=author,
            isbn=re.sub(r"[^0-9Xx]", "", isbn) if isbn else None,
            cover_url=cover_url or None,
            list_price=int(price) if isinstance(price, (int, float)) else None,
            release_date=release,
            release_precision="day" if release else "unknown",
            edition_type=detect_edition(title),
            volume_label=extract_volume(title),
            series_title=infer_series_title(title),
            source_url=url,
        )
        return record, release

    def _parse_schedule(self, url: str, markup: str) -> list[BookRecord]:
        results: list[BookRecord] = []
        for row in parse_tables(markup):
            if len(row) < 4 or not re.fullmatch(r"20\d{2}/\d{1,2}/\d{1,2}", row[0]):
                continue
            release = date(*(int(part) for part in row[0].split("/")))
            title = (row[1] or row[2]).strip()
            book_number = row[3].strip()
            if not title or not book_number:
                continue
            media_type = "novel" if re.search(r"[【〖]輕小說[】〗]", title) else "manga"
            title = re.sub(r"^[【〖]輕小說[】〗]\s*", "", title)
            results.append(
                BookRecord(
                    publisher_code=self.code,
                    source_key=book_number,
                    title=title,
                    media_type=media_type,
                    release_date=release,
                    release_precision="day",
                    edition_type=detect_edition(title),
                    volume_label=extract_volume(title),
                    series_title=infer_series_title(title),
                    source_url=url,
                )
            )
        return results

    @staticmethod
    def _field(text: str, pattern: str) -> str | None:
        match = re.search(pattern, text)
        return match.group(1).strip() if match else None

    @staticmethod
    def _date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value.replace("/", "-"))
        except ValueError:
            return None
