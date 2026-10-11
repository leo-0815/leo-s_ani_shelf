from __future__ import annotations

import re
from typing import Any


CONTENT_FILTERS = frozenset({"general", "bl", "r18", "bl_r18"})


def content_filter_sql(value: str = "", alias: str = "b") -> str:
    """Narrow the visible catalog; never replaces the account visibility scope."""
    if not isinstance(value, str):
        raise ValueError("無效的內容分類篩選")
    if value == "":
        return "1 = 1"
    if value == "none":
        return "1 = 0"
    parts = value.split(",")
    if len(parts) > 3 or len(set(parts)) != len(parts) or any(part not in CONTENT_FILTERS for part in parts):
        raise ValueError("無效的內容分類篩選")
    prefix = f"{alias}." if alias else ""
    bl = f"COALESCE({prefix}bl_category, '') <> ''"
    r18 = f"COALESCE({prefix}content_rating, 'unknown') = 'restricted_18'"
    clauses = {
        "general": f"(NOT ({bl}) AND NOT ({r18}))",
        "bl": f"({bl})",
        "r18": f"({r18})",
        "bl_r18": f"({bl} AND {r18})",
    }
    return "(" + " OR ".join(clauses[part] for part in parts) + ")"


def add_publisher_filter(filters: dict[str, str], where: list[str], values: list[Any], *, private_book_column: str | None = None) -> None:
    """Shared catalog filter; legacy publisher=code remains supported.

    No filter means all publishers; publishers=none explicitly selects none.
    Values, including unknown codes, are always passed as SQL parameters.
    """
    from .preferences import visible_publisher_sql
    visibility = visible_publisher_sql()
    where.append(f"({private_book_column} IS NULL OR {visibility})" if private_book_column else visibility)
    if filters.get("content_filter"):
        where.append(content_filter_sql(filters["content_filter"]))
    raw = filters.get("publishers", filters.get("publisher", ""))
    if not raw:
        return
    if raw == "none":
        where.append("1 = 0")
        return
    codes = list(dict.fromkeys(code.strip() for code in raw.split(",") if code.strip()))
    if len(codes) > 32 or not codes or any(
        not re.fullmatch(r"[a-z0-9_-]{1,64}", code) for code in codes
    ):
        raise ValueError("無效的出版社篩選")
    where.append("p.code IN (" + ", ".join(["%s"] * len(codes)) + ")")
    values.extend(codes)

