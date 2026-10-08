"""Explicit Chingwin product specifications only; no age/content inference."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .common import parse_page

RATING_PARSER_VERSION = "chingwin_rating_v2"
LABELS = {"普": "general", "普遍級": "general", "18限": "restricted_18",
          "限": "restricted_18", "限制級": "restricted_18"}


def product_identity(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "www.ching-win.com.tw" or parsed.port:
        return None
    match = re.fullmatch(r"/product-detail/([A-Za-z0-9_-]+)(?:/[^/]*)?/?", parsed.path)
    return match.group(1) if match else None


def parse_product_rating(url: str, markup: str, expected_key: str | None = None) -> tuple[str, str | None]:
    sku = product_identity(url)
    if not sku or (expected_key is not None and sku != expected_key):
        return "unknown", None
    text = parse_page(markup).flat_text
    block = re.search(
        rf"產品編號\s*[:：]\s*{re.escape(sku)}\b(.{{0,1500}}?)(?:定價|優惠價)", text,
    )
    if not block:
        return "unknown", None
    labels = re.findall(r"級別\s*[:：]\s*(\S+)", block.group(1))
    if len(labels) != 1:
        return "unknown", None
    raw = labels[0]
    return LABELS.get(raw, "unknown"), raw

