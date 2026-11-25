"""Recognize the official Shopline list, independent of the card's purchase text."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

HOST = "www.kadokawa.com.tw"


@dataclass(frozen=True)
class Card:
    url: str
    title: str


class _CatalogParser(HTMLParser):
    def __init__(self, url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.url = url
        self.category_path = unquote(urlsplit(url).path).rstrip("/")
        self.div_depth = 0
        self.grid_depth = None
        self.grid_closed = False
        self.paginator = False
        self.paginator_closed = False
        self.active = False
        self.current = set()
        self.pages = set()
        self.next_arrow = False
        self.canonical = False
        self.cards = {}

    def _same_category(self, parts) -> bool:
        return (parts.scheme == "https" and parts.netloc == HOST
                and unquote(parts.path).rstrip("/") == self.category_path)

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        cls = set((attrs.get("class") or "").split())
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical |= self._same_category(urlsplit(urljoin(self.url, attrs.get("href") or "")))
        if tag == "div":
            self.div_depth += 1
            if self.grid_depth is None and "ProductList-list" in cls:
                self.grid_depth = self.div_depth
        if tag == "ul" and "ProductList-paginator" in cls:
            self.paginator = True
        if tag == "li" and self.paginator:
            self.active = "active" in cls
        if self.paginator and "fa-angle-right" in cls:
            self.next_arrow = True
        if tag not in {"a", "product-item"} or not attrs.get("href"):
            return
        parts = urlsplit(urljoin(self.url, attrs["href"]))
        if self.paginator and self._same_category(parts):
            number = parse_qs(parts.query).get("page", [""])[0]
            if re.fullmatch(r"[0-9]{1,6}", number):
                self.pages.add(int(number))
                if self.active:
                    self.current.add(int(number))
        is_card = ("js-quick-cart-item" in cls if tag == "a" else bool(attrs.get("product-id")))
        if (self.grid_depth is None or not is_card or
                parts.scheme != "https" or parts.netloc != HOST or
                not re.fullmatch(r"/products/[^/]+/?", unquote(parts.path))):
            return
        try:
            data = json.loads(attrs.get("ga-product") or "{}")
        except ValueError:
            data = {}
        title = data.get("title", "") if isinstance(data, dict) else ""
        product_url = f"https://{HOST}{parts.path.rstrip('/')}"
        if product_url not in self.cards or title:
            self.cards[product_url] = Card(product_url, title if isinstance(title, str) else "")

    def handle_endtag(self, tag):
        if tag == "li" and self.paginator:
            self.active = False
        if tag == "ul" and self.paginator:
            self.paginator = False
            self.paginator_closed = True
        if tag == "div":
            if self.div_depth == self.grid_depth:
                self.grid_depth = None
                self.grid_closed = True
            self.div_depth = max(0, self.div_depth - 1)


def verified_listing(url: str, markup: str, expected_page: int) -> tuple[list[Card], bool]:
    """A terminal page must be a nonempty, complete list with a verified paginator."""
    parser = _CatalogParser(url)
    parser.feed(markup)
    if not parser.canonical or not parser.grid_closed or not parser.cards:
        raise ValueError(f"角川第 {expected_page} 頁：未辨識到完整官方商品清單")
    if not parser.paginator_closed or parser.current != {expected_page}:
        raise ValueError(f"角川第 {expected_page} 頁：無法驗證目前頁碼，保留舊斷點")
    next_page = expected_page + 1 in parser.pages
    if (parser.next_arrow or any(n > expected_page for n in parser.pages)) != next_page:
        raise ValueError(f"角川第 {expected_page} 頁：下一頁資訊不一致")
    return list(parser.cards.values()), next_page
