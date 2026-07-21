from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import ROOT, Settings, get_settings


class DatabaseUnavailable(RuntimeError):
    pass


def _driver():
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:
        raise DatabaseUnavailable(
            "缺少 PyMySQL。請先執行：python -m pip install -r requirements.txt"
        ) from exc
    return pymysql, DictCursor


def connect(settings: Settings | None = None, include_database: bool = True):
    settings = settings or get_settings()
    if not settings.database_configured:
        raise DatabaseUnavailable("MySQL 尚未初始化，請執行 python -m app.setup_mysql")
    pymysql, dict_cursor = _driver()
    kwargs: dict[str, Any] = {
        "host": settings.db_host,
        "port": settings.db_port,
        "user": settings.db_user,
        "password": settings.db_password,
        "charset": "utf8mb4",
        "cursorclass": dict_cursor,
        "autocommit": False,
        "connect_timeout": 5,
        "read_timeout": 20,
        "write_timeout": 20,
    }
    ssl_mode = settings.db_ssl_mode
    if ssl_mode not in {"disabled", "preferred", "required", "verify_ca", "verify_identity"}:
        raise DatabaseUnavailable(f"不支援的 ANISHELF_DB_SSL_MODE：{ssl_mode}")
    if ssl_mode in {"preferred", "required"}:
        # Local MySQL uses encrypted transport without certificate verification.
        kwargs["ssl"] = {"check_hostname": False}
    elif ssl_mode in {"verify_ca", "verify_identity"}:
        # Let PyMySQL build a default SSLContext from the operating system CA
        # store. Passing a separate ssl dict here would bypass these flags.
        kwargs["ssl_verify_cert"] = True
        kwargs["ssl_verify_identity"] = ssl_mode == "verify_identity"
    if include_database:
        kwargs["database"] = settings.db_name
    return pymysql.connect(**kwargs)


@contextmanager
def transaction() -> Iterator[Any]:
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ensure_schema() -> None:
    schema = (ROOT / "app" / "schema.sql").read_text(encoding="utf-8")
    statements = [part.strip() for part in schema.split(";") if part.strip()]
    with transaction() as connection:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            _ensure_column(cursor, "wishlist_items", "priority", "TINYINT UNSIGNED NOT NULL DEFAULT 0")
            _ensure_column(cursor, "wishlist_items", "store_name", "VARCHAR(200) NULL")
            _ensure_column(cursor, "wishlist_items", "order_number", "VARCHAR(200) NULL")
            _ensure_column(cursor, "wishlist_items", "paid_price", "INT UNSIGNED NULL")
            _ensure_column(
                cursor,
                "wishlist_items",
                "owned_format",
                "VARCHAR(20) NOT NULL DEFAULT 'paper'",
            )
            _ensure_column(cursor, "crawl_jobs", "skipped_count", "INT UNSIGNED NOT NULL DEFAULT 0")


def _ensure_column(cursor: Any, table: str, column: str, definition: str) -> None:
    cursor.execute(
        "SELECT COUNT(*) AS present FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    if not cursor.fetchone()["present"]:
        cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")


def ping() -> dict[str, Any]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION() AS version, DATABASE() AS database_name")
            return cursor.fetchone()
