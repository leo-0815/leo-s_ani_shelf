from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from .db import transaction

_GENERAL_AUDIENCE: ContextVar[bool] = ContextVar("general_audience", default=False)


@contextmanager
def visibility_scope(enabled: bool) -> Iterator[None]:
    # Thread/request-local: one account's preference must never affect another.
    token = _GENERAL_AUDIENCE.set(bool(enabled))
    try:
        yield
    finally:
        _GENERAL_AUDIENCE.reset(token)


def visible_publisher_sql(alias: str = "p") -> str:
    return f"({alias}.code IS NULL OR {alias}.code <> 'chingwin')" if _GENERAL_AUDIENCE.get() else "1 = 1"


def visible_book_sql(alias: str = "b") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}publisher_id IS NULL OR {prefix}publisher_id NOT IN "
        "(SELECT id FROM publishers WHERE code = 'chingwin'))"
    ) if _GENERAL_AUDIENCE.get() else "1 = 1"


def publisher_visible(code: str) -> bool:
    return not (_GENERAL_AUDIENCE.get() and code == "chingwin")


def get_preferences(user_id: int) -> dict[str, bool]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT general_audience FROM user_preferences WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
    return {"general_audience": bool(row and row["general_audience"])}


def set_preferences(user_id: int, payload: dict[str, Any]) -> dict[str, bool]:
    enabled = payload.get("general_audience")
    if not isinstance(enabled, bool):
        raise ValueError("一般向設定必須為布林值")
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO user_preferences (user_id, general_audience) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE general_audience = VALUES(general_audience)",
                (user_id, enabled),
            )
    return {"general_audience": enabled}

