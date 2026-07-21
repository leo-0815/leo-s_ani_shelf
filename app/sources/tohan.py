from __future__ import annotations

import re
from datetime import date
from urllib.parse import parse_qs, urljoin, urlparse

from ..models import BookRecord, detect_edition, extract_volume, infer_series_title
from .common import fetch_html, parse_page, polite_pause, unique


class TohanSource:
    code = "tohan"
    name = "台灣東販"
    list_url = "https://www.tohan.com.tw/product.php?cid=107"
    catalog_url = "https://www.tohan.com.tw/product.php?cid=1"
    max_items = 30
    latest_cursor: str | None = None

    def collect(
        self,
        known_keys: set[str] | None = None,
        cursor_value: str | None = None,
        backfill: bool = False,
    ) -> list[BookRecord]:
        known_keys = known_keys or set()
        start_url = self.catalog_url if backfill else self.list_url
        first_listing = parse_page(fetch_html(start_url))
        page_urls = [start_url]
        if backfill:
            page_urls.extend(
                urljoin(start_url, href)
                for href, text in first_listing.links
                if "page=" in href and text.strip().isdigit()
            )
        urls: list[str] = []
        for index, page_url in enumerate(unique(page_urls)):
            listing = first_listing if index == 0 else parse_page(fetch_html(page_url))
            for href, _ in listing.links:
                absolute = urljoin(page_url, href)
                parsed = urlparse(absolute)
                query = parse_qs(parsed.query)
                source_id = query.get("id", [""])[0]
                if parsed.path.endswith("/product.php") and query.get("act") == ["view"] and source_id:
                    if source_id not in known_keys:
                        urls.append(absolute)
            if index + 1 < len(page_urls):
                polite_pause(0.35)
        self.latest_cursor = self.list_url
        records: list[BookRecord] = []
        candidates = unique(urls)
        if not backfill:
            candidates = candidates[: self.max_items]
        for url in candidates:
            polite_pause()
            try:
                records.append(self._parse_detail(url, fetch_html(url)))
            except (ValueError, RuntimeError):
                continue
        if not records and not known_keys:
            raise RuntimeError("台灣東販商品清單沒有解析到有效書目")
        return records

    def _parse_detail(self, url: str, markup: str) -> BookRecord:
        page = parse_page(markup)
        first_line = page.text.splitlines()[0] if page.text.splitlines() else ""
        title = page.meta.get("og:title", "").split("_")[0].strip()
        if not title or title in {"書名", "台灣東販"}:
            title = re.split(r"[_|]", first_line, maxsplit=1)[0].strip()
        if not title or title in {"書名", "台灣東販"}:
            title_match = re.search(r"(?:^|\n)([^\n]{2,200})(?:\n+作者\n)", page.text)
            title = title_match.group(1).strip() if title_match else ""
        # The global search bar also contains the labels 書名/作者/ISBN. Restrict
        # field extraction to the final standalone 作者 label in the product detail.
        detail_start = page.text.rfind("\n作者\n")
        detail_text = page.text[detail_start + 1 :] if detail_start >= 0 else page.text
        detail_flat = " ".join(detail_text.split())
        author = self._field(detail_flat, r"^作者\s+(.+?)\s+(?:譯者|ISBN)")
        isbn = self._field(detail_flat, r"ISBN\s+([0-9Xx-]{8,20})")
        release_text = self._field(detail_flat, r"出版日期\s+(\d{4}-\d{2}-\d{2})")
        price_text = self._field(detail_flat, r"定價\s+NT\$\s*([0-9,]+)")
        source_id = parse_qs(urlparse(url).query).get("id", [""])[0]
        if not title or not source_id:
            raise ValueError("商品頁缺少標題或來源 ID")
        release = date.fromisoformat(release_text) if release_text else None
        product_heading = f"{first_line} {title}"
        media_type = "novel" if re.search(r"(?:華文|翻譯)?輕小說|小說", product_heading) else "manga"
        return BookRecord(
            publisher_code=self.code,
            source_key=source_id,
            title=title,
            media_type=media_type,
            author=author,
            isbn=isbn.replace("-", "") if isbn else None,
            cover_url=page.meta.get("og:image") or None,
            list_price=int(price_text.replace(",", "")) if price_text else None,
            release_date=release,
            release_precision="day" if release else "unknown",
            edition_type=detect_edition(title),
            volume_label=extract_volume(title),
            series_title=infer_series_title(title),
            source_url=url,
        )

    @staticmethod
    def _field(text: str, pattern: str) -> str | None:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        return match.group(1).strip() if match else None
