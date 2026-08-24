from __future__ import annotations

import html
import re
from dataclasses import dataclass

from .models import normalize_text


@dataclass(frozen=True)
class SeriesAliasGroup:
    canonical: str
    aliases: tuple[str, ...]
    publisher_code: str | None = None


# Only confirmed, unambiguous aliases belong here. Structural cleanup (volumes,
# editions, punctuation and media suffixes) is handled by the functions below.
SERIES_ALIAS_GROUPS = (
    SeriesAliasGroup(
        canonical="ONE PIECE～航海王～",
        aliases=("ONE PIECE~航海王", "ONE PIECE~航海王~", "ONE PIECE 航海王", "航海王"),
        publisher_code="tongli",
    ),
)


_EDITION_LABEL = (
    r"(?:豪華限定版|首刷限定版|首刷附錄版|首刷書盒版|初回限定版|"
    r"特別版|特裝版|限定版|特典版|同捆版|電子書|愛藏版|完全版|"
    r"典藏版|珍藏版|新裝版|首刷書盒|限定盒裝版|限定盒裝|豪華版|豪華|首刷|初回)"
)
_MEDIA_SUFFIX = r"(?:漫畫小說版|輕小說版|漫畫版|小說版|comic)"
_KEY_IGNORED = frozenset(
    " \t\r\n-_—－:：,，.。!！?？'\"“”‘’、/／\\~～·・•〈〉《》【】[]()（）"
)


def clean_series_title(value: str) -> str:
    """Remove product-only decorations while preserving the work's title."""
    result = normalize_text(html.unescape(html.unescape(value or "")))
    if not result:
        return ""

    # Storefront campaign and shipment labels are not part of a series name.
    result = re.sub(
        r"^[【\[][^】\]]*(?:預購|官網限定特典|會場限定特典|漫博會場限定)[^】\]]*[】\]]\s*",
        "",
        result,
    )
    result = re.sub(r"\s*[【\[][^】\]]*(?:出貨|預購)[^】\]]*[】\]]\s*$", "", result)
    result = re.sub(r"\s+[【\[](?:漫畫|輕小說|小說)[】\]]\s*$", "", result)

    # Editions may be written as a bare suffix or inside different brackets.
    result = re.sub(rf"[（(【\[]\s*{_EDITION_LABEL}\s*[）)】\]]", "", result)
    result = re.sub(rf"\s*{_EDITION_LABEL}\s*$", "", result)
    result = re.sub(r"\s*(?:首刷|初回)\s*$", "", result)

    # Remove volume/range tokens after edition cleanup. Ranges cover box sets.
    volume = r"(?:\d{1,3}(?:\.\d{1,2})?|SS\d{1,3}|全|上|下)"
    result = re.sub(rf"[（(]\s*{volume}(?:\s*[+~～\-]\s*{volume})?\s*[）)]", "", result, flags=re.I)
    result = re.sub(rf"第\s*{volume}\s*(?:卷|集|冊)", "", result, flags=re.I)
    result = re.sub(rf"(?:Vol\.?\s*){volume}", "", result, flags=re.I)
    result = re.sub(
        rf"\s+{volume}(?:\s*[+~～\-]\s*{volume})?(?:完)?[.。]?\s*$",
        "",
        result,
        flags=re.I,
    )
    # Two known publisher separators are presentation variants, not words.
    result = re.sub(r'[@"](?=' + _MEDIA_SUFFIX + r"$)", " ", result, flags=re.I)
    result = re.sub(r"[（(]\s*[）)]", "", result)
    result = re.sub(r"[【\[]\s*[】\]]", "", result)
    return normalize_text(result).strip("-－:： .。~～")


def _series_signature(value: str) -> str:
    value = normalize_text(value).casefold()
    value = re.sub(rf"\s*{_MEDIA_SUFFIX}\s*$", "", value)
    return "".join(character for character in value if character not in _KEY_IGNORED)


def _alias_index() -> dict[tuple[str, str], str]:
    aliases: dict[tuple[str, str], str] = {}
    for group in SERIES_ALIAS_GROUPS:
        scope = group.publisher_code or "*"
        for value in (group.canonical, *group.aliases):
            aliases[(scope, _series_signature(clean_series_title(value)))] = group.canonical
    return aliases


_SERIES_ALIASES = _alias_index()


def canonical_series_title(value: str, publisher_code: str | None = None) -> str:
    cleaned = clean_series_title(value)
    if not cleaned:
        return ""
    signature = _series_signature(cleaned)
    return _SERIES_ALIASES.get(
        (publisher_code or "*", signature),
        _SERIES_ALIASES.get(("*", signature), cleaned),
    )


def series_alias_key(value: str) -> str:
    """Key stored on an alias candidate before any dictionary redirect."""
    return _series_signature(clean_series_title(value))[:190]


def series_key(value: str, publisher_code: str | None = None) -> str:
    """Stable comparison key; intentionally avoids fuzzy similarity matching."""
    canonical = canonical_series_title(value, publisher_code)
    return _series_signature(canonical)[:190]
