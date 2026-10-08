from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any


CONTENT_RATINGS = frozenset(
    {"unknown", "general", "protected_6", "guidance_12", "guidance_15", "restricted_18"}
)
SYNC_HASH_VERSION = 1
SYNC_HASH_FIELDS = (
    "publisher_code",
    "source_key",
    "title",
    "series_title",
    "volume_label",
    "edition_type",
    "media_type",
    "content_rating",
    "rating_raw",
    "rating_source",
    "rating_confidence",
    "author",
    "isbn",
    "cover_url",
    "list_price",
    "release_date",
    "release_precision",
    "release_status",
    "source_url",
)


def catalog_sync_hash(row: dict[str, Any]) -> str:
    """Stable, versioned peer hash independent of crawler hash migrations."""
    payload: dict[str, Any] = {}
    for field in SYNC_HASH_FIELDS:
        value = row.get(field)
        if isinstance(value, date):
            value = value.isoformat()
        payload[field] = value
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def book_content_hash(data: dict[str, Any]) -> str:
    payload = dict(data)
    payload.pop("source_hash", None)
    payload.pop("release_checked_at", None)
    payload.pop("rating_checked_at", None)
    payload.pop("rating_parser_version", None)
    payload.pop("bl_category", None)  # Derived classification travels in the change feed.
    if isinstance(payload.get("release_date"), date):
        payload["release_date"] = payload["release_date"].isoformat()
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def normalize_content_rating(value: str | None) -> str:
    """Normalize explicit publisher labels without guessing from a book title."""
    normalized = normalize_text(value or "").casefold().replace(" ", "")
    if not normalized or normalized == "unknown":
        return "unknown"
    aliases = {
        "general": "general",
        "普遍級": "general",
        "普級": "general",
        "全年齡": "general",
        "protected_6": "protected_6",
        "保護級": "protected_6",
        "6+": "protected_6",
        "guidance_12": "guidance_12",
        "輔12級": "guidance_12",
        "12+": "guidance_12",
        "guidance_15": "guidance_15",
        "輔15級": "guidance_15",
        "15+": "guidance_15",
        "restricted_18": "restricted_18",
        "限制級": "restricted_18",
        "18禁": "restricted_18",
        "r18": "restricted_18",
        "r-18": "restricted_18",
        "18+": "restricted_18",
        "未滿18歲不得購買": "restricted_18",
    }
    if normalized in aliases:
        return aliases[normalized]
    if any(label in normalized for label in ("限制級", "18禁", "未滿18歲", "成人限定")):
        return "restricted_18"
    return value if value in CONTENT_RATINGS else "unknown"


EDITION_MARKERS = (
    ("豪華限定版", "deluxe"),
    ("首刷限定版", "first_print"),
    ("首刷附錄版", "first_print"),
    ("首刷書盒版", "first_print"),
    ("初回限定版", "first_print"),
    ("特別版", "special"),
    ("特裝版", "special"),
    ("限定版", "limited"),
    ("特典版", "bonus"),
    ("同捆版", "bundle"),
    ("電子書", "digital"),
    ("愛藏版", "collector"),
    ("完全版", "collector"),
    ("典藏版", "collector"),
    ("新裝版", "collector"),
)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"[\s\u3000]+", " ", value).strip()
    return value


def detect_edition(title: str) -> str:
    for marker, edition in EDITION_MARKERS:
        if marker in title:
            return edition
    return "standard"


def extract_volume(title: str) -> str | None:
    normalized = normalize_text(title)
    for marker, _edition in EDITION_MARKERS:
        normalized = normalized.replace(marker, "")
    normalized = re.sub(r"[（(]\s*[）)]", "", normalized)
    patterns = [
        r"[（(](\d{1,3}|全|上|下)[）)](?:\s*(?:豪華限定版|首刷限定版|初回限定版|特別版|特裝版|限定版|特典版|同捆版))?(?=\s|$|[~～【〖\[])",
        r"第\s*(\d{1,3})\s*(?:卷|集|冊)",
        r"\s(\d{1,3})(?:\s*(?:特裝版|限定版|特典版|同捆版))?$",
        r"(?:Vol\.?\s*|VOL\.?\s*)(\d{1,3})(?:\D|$)",
        r"\s(\d{1,3})(?:完)?[.。]?$",
        r"(?<=[\u4e00-\u9fff~～])(\d{1,3})(?:完)?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return match.group(1)
    return None


def infer_series_title(title: str) -> str:
    result = normalize_text(title)
    result = re.sub(r"^[\[【〖](?:預購|限)[\]】〗]\s*", "", result)
    for marker, _ in EDITION_MARKERS:
        result = result.replace(marker, "")
    result = re.sub(r"[（(]\s*[）)]", "", result)
    result = re.sub(r"[【〖\[]\s*[】〗\]]", "", result)
    result = re.sub(r"\s+[【〖\[](?:漫畫|輕小說|小說)[】〗\]]\s*$", "", result)
    result = re.sub(r"[（(](?:\d{1,3}|全|上|下)[）)]", "", result)
    result = re.sub(r"第\s*\d{1,3}\s*(?:卷|集|冊)", "", result)
    result = re.sub(r"(?:Vol\.?\s*|VOL\.?\s*)\d{1,3}", "", result)
    result = re.sub(r"\s+\d{1,3}(?:完)?[.。]?$", "", result)
    result = re.sub(r"(?<=[\u4e00-\u9fff~～])\d{1,3}(?:完)?$", "", result)
    return normalize_text(result).strip("-－:： .。~～")


def infer_status(release_date: date | None, precision: str) -> str:
    if release_date is None:
        return "unknown"
    today = date.today()
    if precision == "month":
        current_month = (today.year, today.month)
        target_month = (release_date.year, release_date.month)
        return "available" if target_month < current_month else "scheduled"
    return "available" if release_date <= today else "scheduled"


@dataclass(frozen=True)
class BookRecord:
    publisher_code: str
    source_key: str
    title: str
    media_type: str
    source_url: str
    author: str | None = None
    isbn: str | None = None
    cover_url: str | None = None
    list_price: int | None = None
    release_date: date | None = None
    release_precision: str = "unknown"
    release_status: str = "unknown"
    edition_type: str = "standard"
    volume_label: str | None = None
    series_title: str | None = None
    content_rating: str = "unknown"
    rating_raw: str | None = None
    rating_source: str = "unknown"
    rating_confidence: int = 0
    rating_checked_at: datetime | None = None
    rating_parser_version: str | None = None
    bl_category: str | None = None
    release_date_source: str = "unknown"
    release_checked_at: datetime | None = None

    def prepared(self) -> dict[str, Any]:
        from .series import canonical_series_title, series_key

        data = asdict(self)
        data["title"] = normalize_text(self.title)
        data["normalized_title"] = normalize_text(self.title).casefold()
        data["author"] = normalize_text(self.author or "") or None
        from .sources.product_audience import normalize_bl_category
        data["bl_category"] = normalize_bl_category(self.bl_category)
        rating_candidate = self.content_rating
        if rating_candidate == "unknown" and self.rating_raw:
            rating_candidate = self.rating_raw
        data["content_rating"] = normalize_content_rating(rating_candidate)
        data["rating_raw"] = normalize_text(self.rating_raw or "")[:100] or None
        data["rating_confidence"] = min(max(int(self.rating_confidence or 0), 0), 100)
        if data["content_rating"] != "unknown" and data["rating_raw"]:
            if self.rating_source == "unknown":
                data["rating_source"] = "publisher"
            if data["rating_confidence"] == 0:
                data["rating_confidence"] = 100
        if data["rating_source"] not in {"unknown", "publisher", "category", "heuristic", "manual"}:
            data["rating_source"] = "unknown"
        data["edition_type"] = self.edition_type or detect_edition(self.title)
        data["volume_label"] = self.volume_label or extract_volume(self.title)
        inferred_series = self.series_title or infer_series_title(self.title)
        data["series_title"] = canonical_series_title(inferred_series, self.publisher_code) or None
        data["series_key"] = series_key(inferred_series, self.publisher_code) or None
        if self.release_status == "unknown":
            data["release_status"] = infer_status(self.release_date, self.release_precision)
        serializable = dict(data)
        # Check timestamps are operational metadata, not a content change.
        serializable.pop("release_checked_at", None)
        serializable.pop("rating_checked_at", None)
        serializable.pop("rating_parser_version", None)
        serializable.pop("bl_category", None)
        serializable["release_date"] = self.release_date.isoformat() if self.release_date else None
        data["source_hash"] = hashlib.sha256(
            json.dumps(serializable, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return data
