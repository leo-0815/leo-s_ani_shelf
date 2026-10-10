from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from .db import transaction

CONTENT_MODES = frozenset({"general", "bl", "r18", "all"})
_CONTENT_MODE: ContextVar[str] = ContextVar("content_mode", default="all")


def content_mode(mode: Any = None, general_audience: bool = True) -> str:
    """Keep legacy preferences without resetting any existing account."""
    if mode is None:
        return "general" if general_audience else "all"
    if not isinstance(mode, str) or mode not in CONTENT_MODES:
        raise ValueError("無效的內容顯示模式")
    return mode


def preference_values(mode: str) -> dict[str, Any]:
    return {"content_mode": mode, "general_audience": mode == "general"}


@contextmanager
def visibility_scope(enabled: bool | str) -> Iterator[None]:
    # Thread/request-local: one account's preference must never affect another.
    mode = content_mode(None, enabled) if isinstance(enabled, bool) else content_mode(enabled)
    token = _CONTENT_MODE.set(mode)
    try:
        yield
    finally:
        _CONTENT_MODE.reset(token)


def _trusted_general_sql(prefix: str) -> str:
    from .sources.product_rating import PARSER_VERSIONS
    versions = " ".join(f"WHEN '{code}' THEN '{version}'"
                        for code, version in PARSER_VERSIONS.items())
    # Exact parser/publisher pairing prevents stale or cross-source proof.
    proof = (f"{prefix}rating_checked_at IS NOT NULL AND "
             f"{prefix}rating_parser_version = (SELECT CASE code {versions} END "
             f"FROM publishers WHERE id = {prefix}publisher_id)")
    return (f"({prefix}content_rating = 'general' AND "
            f"{prefix}rating_confidence = 100 AND "
            f"(({prefix}rating_source = 'publisher' AND {proof}) OR "
            f"({prefix}rating_source = 'manual' AND {prefix}rating_locked = TRUE)))")


def visible_publisher_sql(alias: str = "p", book_alias: str = "b") -> str:
    return visible_book_sql(book_alias)


def _general_audience_sql(prefix: str) -> str:
    # General audience means non-BL and non-R18, NOT publisher grade "general".
    # Unknown grades/categories remain visible. Positive labels alone exclude.
    return (f"(COALESCE({prefix}content_rating, 'unknown') <> 'restricted_18' AND "
            f"COALESCE({prefix}bl_category, '') = '')")


def visible_book_sql(alias: str = "b") -> str:
    mode = _CONTENT_MODE.get()
    prefix = f"{alias}." if alias else ""
    conditions = []
    if mode in {"general", "bl"}:
        conditions.append(f"COALESCE({prefix}content_rating, 'unknown') <> 'restricted_18'")
    if mode in {"general", "r18"}:
        conditions.append(f"COALESCE({prefix}bl_category, '') = ''")
    return "(" + " AND ".join(conditions) + ")" if conditions else "1 = 1"


def notification_visibility_sql(book_alias: str = "b", user_alias: str = "u") -> str:
    """Per-recipient policy; independent of the web request ContextVar."""
    mode = (f"COALESCE((SELECT COALESCE(up.content_mode, CASE WHEN up.general_audience "
            f"THEN 'general' ELSE 'all' END) FROM user_preferences up "
            f"WHERE up.user_id = {user_alias}.id), CASE WHEN {user_alias}.role = 'admin' "
            f"THEN 'all' ELSE 'general' END)")
    return (f"(({mode} IN ('r18', 'all') OR COALESCE({book_alias}.content_rating, 'unknown') <> 'restricted_18') "
            f"AND ({mode} IN ('bl', 'all') OR COALESCE({book_alias}.bl_category, '') = ''))")


def publisher_visible(code: str) -> bool:
    # Eligibility is per book now, not per publisher. Never use this to
    # authorize book details; those must apply one of the SQL predicates.
    return True


def get_preferences(user_id: int) -> dict[str, Any]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT u.role, p.general_audience, p.content_mode FROM users u "
                "LEFT JOIN user_preferences p ON p.user_id = u.id WHERE u.id = %s", (user_id,)
            )
            row = cursor.fetchone()
    default = not (row and row.get("role") == "admin")
    legacy = default if row is None or row.get("general_audience") is None else bool(row["general_audience"])
    return preference_values(content_mode(row.get("content_mode") if row else None, legacy))


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


def set_preferences(user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    if "content_mode" in payload:
        if payload["content_mode"] is None:
            raise ValueError("無效的內容顯示模式")
        mode = content_mode(payload["content_mode"])
    else:
        enabled = payload.get("general_audience")
        if not isinstance(enabled, bool):
            raise ValueError("一般向設定必須為布林值")
        mode = content_mode(None, enabled)
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO user_preferences (user_id, general_audience, content_mode) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE general_audience = VALUES(general_audience), content_mode = VALUES(content_mode)",
                (user_id, mode == "general", mode),
            )
    return preference_values(mode)

