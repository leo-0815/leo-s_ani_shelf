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
            cursor.execute(
                "SELECT u.role, p.general_audience FROM users u "
                "LEFT JOIN user_preferences p ON p.user_id = u.id WHERE u.id = %s", (user_id,)
            )
            row = cursor.fetchone()
    default = not (row and row.get("role") == "admin")
    return {"general_audience": default if row is None or row.get("general_audience") is None else bool(row["general_audience"])}


def migrate_general_audience_default(cursor: Any, *, cloud: bool) -> bool:
    """One-time reset requested by the owner; later opt-outs remain untouched."""
    cursor.execute(
        "SELECT COLUMN_DEFAULT AS column_default FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'user_preferences' "
        "AND column_name = 'general_audience'"
    )
    row = cursor.fetchone()
    if row and str(row["column_default"]).lower() not in {"1", "true"}:
        cursor.execute("ALTER TABLE user_preferences ALTER COLUMN general_audience SET DEFAULT 1")
    # Unique marker serializes concurrent startup/migration workers. It is inserted
    # after all DDL, in the same transaction as the preference update.
    cursor.execute(
        "INSERT IGNORE INTO app_migrations (migration_key) VALUES (%s)",
        ("general_audience_default_v1",),
    )
    if not cursor.rowcount:
        return False
    if cloud:
        cursor.execute(
            "UPDATE user_preferences p JOIN users u ON u.id = p.user_id "
            "SET p.general_audience = TRUE WHERE u.role = 'user'"
        )
        cursor.execute(
            "INSERT INTO user_preferences (user_id, general_audience) "
            "SELECT id, TRUE FROM users WHERE role = 'user' "
            "ON DUPLICATE KEY UPDATE general_audience = TRUE"
        )
    else:
        cursor.execute("UPDATE user_preferences SET general_audience = TRUE")
        cursor.execute(
            "INSERT INTO user_preferences (user_id, general_audience) VALUES (0, TRUE) "
            "ON DUPLICATE KEY UPDATE general_audience = TRUE"
        )
    print("一般向預設已套用至所有一般帳號；管理員設定保持不變")
    return True


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

