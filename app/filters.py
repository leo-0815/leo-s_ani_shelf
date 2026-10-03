from __future__ import annotations

import re
from typing import Any


def add_publisher_filter(filters: dict[str, str], where: list[str], values: list[Any]) -> None:
    """Shared catalog filter; legacy publisher=code remains supported.

    No filter means all publishers; publishers=none explicitly selects none.
    Values, including unknown codes, are always passed as SQL parameters.
    """
    from .preferences import visible_publisher_sql
    where.append(visible_publisher_sql())
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

