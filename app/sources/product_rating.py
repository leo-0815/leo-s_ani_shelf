"""Edition-specific official grading. Never infer age suitability from genre."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit

from .common import parse_page
from .chingwin_rating import parse_product_rating as parse_chingwin
from ..release_dates import product_url_valid

PARSER_VERSIONS = {"chingwin": "chingwin_rating_v1", "spp": "spp_rating_v1", "tongli": "tongli_rating_v1"}
LABELS = {"普": "general", "普遍級": "general", "限制級": "restricted_18",
          "保護級": "protected_6", "輔12級": "guidance_12", "輔15級": "guidance_15"}


def _label(text: str, pattern: str) -> tuple[str, str | None]:
    text = unicodedata.normalize("NFKC", text)
    labels = re.findall(pattern, text)
    if len(labels) != 1:
        return "unknown", None
    raw = labels[0].strip()
    return LABELS.get(raw, "unknown"), raw if raw in LABELS else None


def parse_rating(code: str, url: str, markup: str, key: str,
                 expected_isbn: str | None = None) -> tuple[str, str | None]:
    url = url.replace("http://", "https://", 1)
    if code not in PARSER_VERSIONS or not product_url_valid(code, url, key):
        return "unknown", None
    if code == "chingwin":
        return parse_chingwin(url, markup, key)
    if code == "spp":
        marker = 'SalePageIndexViewModel"] = '
        if marker not in markup:
            return "unknown", None
        try:
            data, _ = json.JSONDecoder().raw_decode(markup.split(marker, 1)[1].replace("\\'", "'").lstrip())
        except (ValueError, TypeError):
            return "unknown", None
        if str(data.get("Id")) != urlsplit(url).path.rstrip("/").split("/")[-1]:
            return "unknown", None
        text = parse_page(data.get("ShortDescription") or "").flat_text
        number = re.search(r"書\s*號[:：]\s*([A-Za-z0-9-]+)", text)
        if key != (number[1] if number else str(data.get("Id"))):
            return "unknown", None
        barcode = re.search(r"條\s*碼[:：]\s*([0-9Xx-]{8,24})", text)
        isbn = barcode[1].replace("-", "") if barcode else ""
        if expected_isbn and isbn and expected_isbn.replace("-", "") != isbn:
            return "unknown", None
        return _label(text, r"等\s*級\s*[:：]\s*([^\s<]+)")
    page = parse_page(markup)
    title = re.search(r"(?:漫畫|小說)書籍資料\s*\n([^\n]+)\n原文書名：", page.text)
    if not title:
        return "unknown", None
    start = page.flat_text.find(title[1].strip())
    end = page.flat_text.find("內容簡介", start)
    if start < 0 or end < 0:
        return "unknown", None
    text = page.flat_text[start:end]
    isbn = re.search(r"ISBN[:：]\s*([0-9Xx-]{8,24})", text)
    if expected_isbn and isbn and expected_isbn.replace("-", "") != isbn[1].replace("-", ""):
        return "unknown", None
    return _label(text, r"圖書分級\s*[:：]\s*([^\s<]+)")


def rating_fields(code: str, url: str, markup: str, key: str,
                  isbn: str | None = None) -> dict:
    rating, raw = parse_rating(code, url, markup, key, isbn)
    return dict(content_rating=rating, rating_raw=raw,
                rating_source="publisher" if rating != "unknown" else "unknown",
                rating_confidence=100 if rating != "unknown" else 0,
                rating_checked_at=datetime.utcnow().replace(microsecond=0),
                rating_parser_version=PARSER_VERSIONS[code])
