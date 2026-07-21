from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any


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
        r"[（(](\d{1,3}|全|上|下)[）)](?:\s*(?:豪華限定版|首刷限定版|初回限定版|特別版|特裝版|限定版|特典版|同捆版))?(?:\s|$)",
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

    def prepared(self) -> dict[str, Any]:
        data = asdict(self)
        data["title"] = normalize_text(self.title)
        data["normalized_title"] = normalize_text(self.title).casefold()
        data["author"] = normalize_text(self.author or "") or None
        data["edition_type"] = self.edition_type or detect_edition(self.title)
        data["volume_label"] = self.volume_label or extract_volume(self.title)
        data["series_title"] = self.series_title or infer_series_title(self.title)
        if self.release_status == "unknown":
            data["release_status"] = infer_status(self.release_date, self.release_precision)
        serializable = dict(data)
        serializable["release_date"] = self.release_date.isoformat() if self.release_date else None
        data["source_hash"] = hashlib.sha256(
            json.dumps(serializable, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return data
