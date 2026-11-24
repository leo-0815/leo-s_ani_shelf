from __future__ import annotations
from .product_rating import rating_fields

import re
from datetime import date
from urllib.parse import parse_qs, urljoin, urlparse

from ..models import BookRecord, detect_edition, extract_volume, infer_series_title
from .common import fetch_html, parse_page, polite_pause, unique
from .tohan_public import (public_seeds, require_public_product, detail_media_type,
                           UnsupportedTohanProduct, ExcludedTohanPure, SourceLoginRequired,
                           PURE_EXCLUSION, reject_pure_product)


class TohanSource:
    code = "tohan"
    name = "台灣東販"
    list_url = "https://www.tohan.com.tw/product.php?cid=107"
    catalog_url = "https://www.tohan.com.tw/product.php?cid=1"
    max_items = 30
    pure_index_url = "https://www.tohan.com.tw/product.php?cid=2"
    category_urls = (
        (None, "https://www.tohan.com.tw/product.php?cid=107"),
        ("manga", "https://www.tohan.com.tw/product.php?cid=3"),
        ("novel", "https://www.tohan.com.tw/product.php?cid=139"),
        ("novel", "https://www.tohan.com.tw/product.php?cid=17"),
        (None, "https://www.tohan.com.tw/product.php?cid=172"),
    )
    latest_cursor: str | None = None

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.skipped_count = 0

    def excluded_keys(self, load) -> set[str]:
        """Read the current PURE index only as a denylist, never as a feed."""
        if self.pure_index_url:
            cards = public_seeds(self.pure_index_url, load(self.pure_index_url))
            if not cards:
                raise ValueError("東販 PURE 排除清單無法辨識")
            recorder = getattr(self, "record_pure_exclusions", None)
            if recorder:
                recorder([card.source_url for card in cards.values()])
        else:
            cards = {}
        known_exclusions = getattr(self, "known_excluded_keys", lambda: set())
        return set(cards) | known_exclusions()

    def collect(
        self,
        known_keys: set[str] | None = None,
        cursor_value: str | None = None,
        backfill: bool = False,
    ) -> list[BookRecord]:
        known_keys = known_keys or set()
        excluded = self.excluded_keys(fetch_html)
        seeds = {}
        conflicting = set()
        for hint, base in self.category_urls:
            pending, seen = [base], set()
            while pending:
                page_url = pending.pop(0)
                if page_url in seen:
                    continue
                seen.add(page_url)
                polite_pause(0.6)
                try:
                    markup = fetch_html(page_url)
                    cards = public_seeds(page_url, markup, hint)
                    if not cards:
                        raise ValueError("東販主要商品區沒有可辨識的商品卡片")
                    for key, seed in cards.items():
                        if key in excluded:
                            self.skipped_count += 1
                            continue
                        old = seeds.get(key)
                        if old and old.media_type != 'unknown' and seed.media_type != 'unknown' and old.media_type != seed.media_type:
                            conflicting.add(key)
                        if old is None or old.media_type == 'unknown':
                            seeds[key] = seed
                    if backfill:
                        pending.extend(self.page_links(page_url, markup))
                except Exception as exc:
                    self.errors.append(f"清單 {page_url}：{exc}")
                    break
        from dataclasses import replace
        for key in conflicting:
            seeds[key] = replace(seeds[key], media_type='unknown')
            self.errors.append(f"商品 {key} 的官方清單類型衝突，保留未知")
        self.latest_cursor = self.list_url
        records: list[BookRecord] = []
        upgrades = getattr(self, 'listing_upgrade_keys', set())
        if callable(upgrades):
            upgrades = upgrades()
        candidates = [seed for key, seed in seeds.items() if key not in known_keys]
        records.extend(seed for key, seed in seeds.items()
                       if key in known_keys and key in upgrades and seed.media_type != 'unknown')
        if not backfill:
            candidates = candidates[: self.max_items]
        for seed in candidates:
            url = seed.source_url
            allowed = getattr(self, "should_fetch_product", None)
            if allowed and not allowed(url):
                if seed:
                    records.append(seed)
                continue
            polite_pause()
            try:
                records.append(self._parse_detail(url, fetch_html(url), media_hint=seed.media_type))
                from .common import product_success
                product_success(self, url)
            except (SourceLoginRequired, ExcludedTohanPure) as exc:
                from .common import product_failure
                product_failure(self, url, exc)
                self.skipped_count += 1
            except UnsupportedTohanProduct:
                self.skipped_count += 1
            except Exception as exc:
                self.errors.append(f"{url}：{exc}")
                from .common import product_failure
                product_failure(self, url, exc, seed=seed)
                if seed:
                    records.append(seed)
        if not records and not known_keys and not self.skipped_count:
            raise RuntimeError("台灣東販商品清單沒有解析到有效書目")
        return records

    @staticmethod
    def page_links(base: str, markup: str) -> list[str]:
        category = parse_qs(urlparse(base).query).get('cid')
        links = []
        for href, _ in parse_page(markup).links:
            target = urlparse(urljoin(base, href))
            query = parse_qs(target.query)
            page = query.get('page', [''])[0]
            if (target.scheme in {'http','https'} and target.hostname == 'www.tohan.com.tw'
                    and not target.port and not target.username and target.path == '/product.php'
                    and query.get('cid') == category and query.get('act', ['list']) == ['list']
                    and page.isdigit() and 1 < int(page) <= 1000):
                links.append(f'https://www.tohan.com.tw/product.php?act=list&cid={category[0]}&page={int(page)}')
        return unique(links)

    def _parse_detail(self, url: str, markup: str, media_hint: str | None = None, *, strict_hint: bool = True) -> BookRecord:
        require_public_product(markup)
        reject_pure_product(markup)
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
        if detail_start < 0 or not (isbn or author):
            raise ValueError("東販商品頁缺少單本商品欄位，保留清單資料")
        release = date.fromisoformat(release_text) if release_text else None
        media_type = detail_media_type(markup, media_hint, strict_hint=strict_hint)
        from ..release_dates import product_record
        return product_record(
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
            **rating_fields(self.code, url, markup, source_id, isbn),
        )

    @staticmethod
    def _field(text: str, pattern: str) -> str | None:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        return match.group(1).strip() if match else None
