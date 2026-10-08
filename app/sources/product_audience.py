"""Positive official BL labels, independent of age rating. Missing is not BL."""
from __future__ import annotations

import json
import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .common import parse_page
from ..release_dates import product_url_valid

BL_CATEGORIES = frozenset({"BL", "BL漫畫", "BL小說", "耽美漫畫", "耽美小說", "耽夢文庫"})


def normalize_bl_category(value: str | None) -> str | None:
    text = unicodedata.normalize("NFKC", value or "").strip()
    text = re.sub(r"\s+", "", text)
    for label in BL_CATEGORIES:
        if text.casefold() == label.casefold():
            return label
    return None


class BreadcrumbParser(HTMLParser):
    """Read only named product breadcrumb containers, never global menus."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.active = None
        self.labels = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        if tag in {"br", "img", "input", "meta", "link", "hr", "source"}:
            return
        self.depth += 1
        if self.active is None and dict(attrs).get("id", "").lower() in {
            "breadcrumb", "breadcrumbs", "productlist-breadcrumb", "productdetail-breadcrumb"
        }:
            self.active = self.depth

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1
        if tag in {"br", "img", "input", "meta", "link", "hr", "source"}:
            return
        if self.active == self.depth:
            self.active = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.active is not None and not self.skip:
            label = normalize_bl_category(data)
            if label:
                self.labels.append(label)


def parse_bl_category(code: str, url: str, markup: str, key: str) -> str | None:
    url = url.replace("http://", "https://", 1)
    if not product_url_valid(code, url, key):
        return None
    page = parse_page(markup)
    if code == "chingwin":
        # The requested SKU must actually be present, not an age gate/list page.
        identity = re.search(r"產品編號\s*[:：]\s*([A-Za-z0-9_-]+)", page.flat_text)
        if not identity or identity[1] != key:
            return None
    if code == "spp":
        marker = 'SalePageIndexViewModel"] = '
        if marker not in markup:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(markup.split(marker, 1)[1].replace("\\'", "'").lstrip())
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict) or str(data.get("Id")) != urlsplit(url).path.rstrip("/").split("/")[-1]:
            return None
        specs = parse_page(data.get("ShortDescription") or "").flat_text
        number = re.search(r"書\s*號[:：]\s*([A-Za-z0-9-]+)", specs)
        if key != (number[1] if number else str(data.get("Id"))):
            return None
        levels = data.get("CategoryLevelName") or {}
        if not isinstance(levels, dict):
            levels = {}
        labels = [data.get("CategoryName"), *(v for k, v in levels.items() if k.endswith("_Name"))]
        for label in labels:
            if isinstance(label, str) and normalize_bl_category(label):
                return normalize_bl_category(label)
        seo = data.get("SEOTag") or {}
        if isinstance(seo, dict):
            for token in re.split(r"[,，;；|]", str(seo.get("Keywords") or "")):
                if normalize_bl_category(token):
                    return normalize_bl_category(token)
    breadcrumb = BreadcrumbParser()
    breadcrumb.feed(markup)
    breadcrumb.close()
    if breadcrumb.labels:
        return breadcrumb.labels[-1]
    # Generic head keywords may describe the entire shop, not this product.
    if code == "tohan":
        description = page.meta.get("og:description", "")
        if not re.search(r"(?:不是|並非|非)\s*BL", description, re.I):
            match = re.search(r"(?<![A-Za-z])BL\s*(漫畫|小說)", description, re.I)
            if match:
                return "BL" + match[1]
    return None
