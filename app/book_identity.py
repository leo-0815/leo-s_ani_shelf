"""Conservative Ching Win schedule/product identity reconciliation.

This maps observations to an existing book ID; it never merges two existing IDs.
Dates are observations, not identity, and ambiguous matches stay separate.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .models import detect_edition, extract_volume, normalize_text
from .series import series_key


def chingwin_source_kind(data: dict[str, Any]) -> str:
    url = urlsplit(str(data.get("source_url") or ""))
    if url.scheme not in {"https", "http"} or url.hostname not in {
        "www.ching-win.com.tw", "ching-win.com.tw"
    }:
        return "other"
    if re.fullmatch(r"/product-detail/[^/]+/?", url.path):
        return "product"
    if re.fullmatch(r"/about-news-detail/\d+/?", url.path):
        return "schedule"
    return "other"


def chingwin_product_id(data: dict[str, Any]) -> str:
    if chingwin_source_kind(data) != "product":
        return ""
    return urlsplit(str(data["source_url"])).path.rstrip("/").rsplit("/", 1)[-1]


def _title(value: str) -> str:
    value = normalize_text(value).casefold()
    value = re.sub(r"^\(?預購\)?\s*", "", value)
    # Only known media decorations, not fuzzy series/title similarity.
    value = re.sub(r"^(?:輕小說\s*|(?:小說|漫畫)\s+)", "", value)
    value = re.sub(r"\(\s*(\d{1,3})\s*\)", lambda m: f"({int(m[1])})", value)
    return re.sub(r"\s+", "", value)


def _edition(data: dict[str, Any]) -> str:
    detected = detect_edition(str(data.get("title") or ""))
    if "電子書" in str(data.get("title") or ""):
        detected = "digital"
    return detected if detected != "standard" else str(data.get("edition_type") or "standard")


def _volume(data: dict[str, Any]) -> str:
    value = normalize_text(str(data.get("volume_label") or extract_volume(data.get("title") or "") or ""))
    return str(int(value)) if value.isdigit() else value.casefold()


def _isbn(data: dict[str, Any]) -> str:
    value = re.sub(r"[\s-]", "", str(data.get("isbn") or "")).upper()
    if re.fullmatch(r"\d{13}", value):
        if sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(value)) % 10 == 0:
            return value
    elif re.fullmatch(r"\d{9}[\dX]", value):
        if sum((10 - i) * (10 if n == "X" else int(n)) for i, n in enumerate(value)) % 11 == 0:
            return value
    return ""


def author_tokens(value: str | None) -> set[str]:
    parts = re.split(r"[、,，/／;；]+", normalize_text(value or ""))
    return {re.sub(r"\s+", "", re.sub(r"^(?:作者|原作|作畫|繪者|插畫|著)[:：]\s*", "", p)).casefold()
            for p in parts if p.strip()}


def compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("media_type") not in {"manga", "novel"} or left.get("media_type") != right.get("media_type"):
        return False
    if _edition(left) != _edition(right) or _volume(left) != _volume(right):
        return False
    # Even malformed ISBNs must not override an explicit conflicting identifier.
    a = re.sub(r"[\s-]", "", str(left.get("isbn") or "")).upper()
    b = re.sub(r"[\s-]", "", str(right.get("isbn") or "")).upper()
    return not (a and b and a != b)


def identity_candidates(cursor: Any, publisher_id: int, data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = normalize_text(data["title"])
    bare = re.sub(r"^(?:輕小說\s*|(?:小說|漫畫)\s+)", "", raw)
    keys = sorted({series_key(title, "chingwin") for title in
                   (raw, bare, "小說 " + bare, "輕小說 " + bare, "漫畫 " + bare)} - {""})
    conditions = ["normalized_title = %s"]
    args: list[Any] = [publisher_id, data["media_type"], data["normalized_title"]]
    if keys:
        conditions.append("series_key IN (" + ",".join(["%s"] * len(keys)) + ")")
        args.extend(keys)
    isbn = _isbn(data)
    if isbn:
        conditions.append("isbn = %s")
        args.append(isbn)
    cursor.execute(
        "SELECT * FROM books WHERE publisher_id = %s AND media_type = %s AND ("
        + " OR ".join(conditions) + ") LIMIT 201 FOR UPDATE", args,
    )
    return cursor.fetchall()


def find_chingwin_match(cursor: Any, publisher_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    kind = chingwin_source_kind(data)
    if kind == "other":
        return None
    candidates = identity_candidates(cursor, publisher_id, data)
    if len(candidates) > 200:
        return None  # A broad/ambiguous series is not a safe automatic match.
    matches = []
    isbn = _isbn(data)
    conflicting_product = False
    for candidate in candidates:
        old_kind = chingwin_source_kind(candidate)
        if old_kind == "other" or not compatible(candidate, data):
            continue
        # A legacy schedule hash can already point to the same official SKU.
        if kind == old_kind == "product":
            if chingwin_product_id(candidate) == chingwin_product_id(data):
                matches.append(candidate)
            elif (isbn and isbn == _isbn(candidate)) or (
                    _title(candidate["title"]) == _title(data["title"]) and
                    author_tokens(candidate.get("author")) & author_tokens(data.get("author"))):
                conflicting_product = True
            continue
        if isbn and isbn == _isbn(candidate):
            matches.append(candidate)
            continue
        if (_title(candidate["title"]) == _title(data["title"])
                and author_tokens(candidate.get("author")) & author_tokens(data.get("author"))):
            matches.append(candidate)
    if conflicting_product and any(chingwin_source_kind(row) != "product" for row in matches):
        return None
    return matches[0] if len(matches) == 1 else None


def resolve_alias(cursor: Any, publisher_id: int, key: str) -> dict[str, Any] | None:
    cursor.execute("SELECT book_id FROM book_source_aliases WHERE publisher_id = %s AND alias_key = %s FOR UPDATE",
                   (publisher_id, key))
    alias = cursor.fetchone()
    if not alias:
        return None
    cursor.execute("SELECT * FROM books WHERE publisher_id = %s AND id = %s FOR UPDATE",
                   (publisher_id, alias["book_id"]))
    row = cursor.fetchone()
    if row is None:
        raise ValueError("書目來源對應已失效")
    return row


def remember_alias(cursor: Any, publisher_id: int, book_id: int, data: dict[str, Any], method: str) -> None:
    cursor.execute("SELECT book_id FROM book_source_aliases WHERE publisher_id = %s AND alias_key = %s FOR UPDATE",
                   (publisher_id, data["source_key"]))
    alias = cursor.fetchone()
    if alias:
        if int(alias["book_id"]) != book_id:
            raise ValueError("書目來源對應衝突，需人工確認")
        return
    cursor.execute(
        "INSERT INTO book_source_aliases (publisher_id, alias_key, book_id, source_kind, match_method) "
        "VALUES (%s, %s, %s, %s, %s)",
        (publisher_id, data["source_key"], book_id, chingwin_source_kind(data), method),
    )


def merge_chingwin_metadata(existing: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Keep richer public metadata; never write or delete private references."""
    result = dict(data)
    old_kind, new_kind = chingwin_source_kind(existing), chingwin_source_kind(data)
    for field in ("author", "isbn", "cover_url", "list_price", "series_title", "series_key", "volume_label"):
        if result.get(field) in {None, ""}:
            result[field] = existing.get(field)
    old_authors, new_authors = author_tokens(existing.get("author")), author_tokens(data.get("author"))
    if new_authors and new_authors <= old_authors:
        result["author"] = existing.get("author")
    if old_kind == "product" and new_kind != "product":
        for field in ("title", "normalized_title", "series_title", "series_key", "volume_label",
                      "edition_type", "source_url", "isbn", "cover_url", "list_price"):
            if existing.get(field) not in {None, ""}:
                result[field] = existing.get(field)
        # A product URL alone is not evidence of a publication date. Keep
        # schedule fallback usable until there is an actual day/product date.
        if existing.get("release_date") and (existing.get("release_precision") == "day"
                                              or existing.get("release_date_source") == "product"):
            for field in ("release_date", "release_precision", "release_status", "release_date_source",
                          "release_checked_at"):
                result[field] = existing.get(field)
        if not (old_authors and old_authors <= new_authors):
            result["author"] = existing.get("author")
    return result
