from __future__ import annotations

import hashlib
import re
from datetime import date
from urllib.parse import urljoin

from ..models import BookRecord, detect_edition, extract_volume, infer_series_title
from .common import fetch_html, parse_page, parse_tables, polite_pause, unique


class EgMangaSource:
    code = "egmanga"
    name = "長鴻出版社"
    list_url = "https://www.egmanga.com.tw/blogs/%E6%96%B0%E6%9B%B8%E4%B8%8A%E5%B8%82"
    max_pages = 3
    max_articles = 12
    latest_cursor: str | None = None

    def collect(
        self,
        known_keys: set[str] | None = None,
        cursor_value: str | None = None,
        backfill: bool = False,
    ) -> list[BookRecord]:
        known_keys = known_keys or set()
        first_listing = parse_page(fetch_html(self.list_url))
        page_urls = [self.list_url]
        page_urls.extend(
            urljoin(self.list_url, href)
            for href, text in first_listing.links
            if "page=" in href and text.strip().isdigit()
        )
        urls: list[str] = []
        selected_pages = unique(page_urls)
        if not backfill:
            selected_pages = selected_pages[:1]
        for index, page_url in enumerate(selected_pages):
            listing = first_listing if index == 0 else parse_page(fetch_html(page_url))
            urls.extend(
                urljoin(page_url, href)
                for href, text in listing.links
                if "/blogs/" in href and re.fullmatch(r"\d{1,2}月新書", text.strip())
            )
        urls = unique(urls)
        self.latest_cursor = urls[0] if urls else cursor_value
        if not backfill:
            if cursor_value in urls:
                urls = urls[: urls.index(cursor_value)]
            else:
                urls = urls[: self.max_articles]
        records: list[BookRecord] = []
        for url in urls:
            polite_pause()
            records.extend(
                record
                for record in self._parse_article(url, fetch_html(url))
                if record.source_key not in known_keys
            )
        # A new month's article can repeat late releases from the previous month.
        # Keep the first occurrence (the newest article) so source_url and hashes do
        # not oscillate between two schedule pages on every refresh.
        deduplicated: dict[str, BookRecord] = {}
        for record in records:
            deduplicated.setdefault(record.source_key, record)
        records = list(deduplicated.values())
        if not records and not known_keys and urls:
            raise RuntimeError("長鴻新書表沒有解析到有效書目")
        return records

    def _parse_article(self, url: str, markup: str) -> list[BookRecord]:
        page = parse_page(markup)
        published = re.search(r"(20\d{2})[-/]([01]?\d)[-/]([0-3]?\d)", page.flat_text)
        if not published:
            raise ValueError("長鴻新書表缺少文章年份")
        article_year, article_month = int(published.group(1)), int(published.group(2))
        results: list[BookRecord] = []
        for row in parse_tables(markup):
            if len(row) < 6 or not re.fullmatch(r"\d{1,2}/\d{1,2}", row[0]):
                continue
            month, day = (int(part) for part in row[0].split("/"))
            year = article_year
            if article_month >= 11 and month <= 2:
                year += 1
            elif article_month <= 2 and month >= 11:
                year -= 1
            category, title, author = row[1].strip(), row[2].strip(), row[3].strip()
            if category not in {"漫畫", "小說"} or not title:
                continue
            source_key = hashlib.sha1(
                f"{category}|{title}|{author}".encode("utf-8")
            ).hexdigest()
            price = int(row[5].replace(",", "")) if re.fullmatch(r"[0-9,]+", row[5]) else None
            results.append(
                BookRecord(
                    publisher_code=self.code,
                    source_key=source_key,
                    title=title,
                    media_type="novel" if category == "小說" else "manga",
                    author=author or None,
                    list_price=price,
                    release_date=date(year, month, day),
                    release_precision="day",
                    edition_type=detect_edition(title),
                    volume_label=extract_volume(title),
                    series_title=infer_series_title(title),
                    source_url=url,
                )
            )
        return results
