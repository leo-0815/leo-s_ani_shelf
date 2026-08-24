from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Full, LifoQueue
from threading import Lock
from time import monotonic
from typing import Any, Iterator

from .config import ROOT, Settings, get_settings


class DatabaseUnavailable(RuntimeError):
    pass


class _ConnectionPool:
    """A small, lazy pool suited to one Render process and a remote TiDB endpoint."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.max_size = settings.db_pool_size
        self.available: LifoQueue[tuple[Any, float]] = LifoQueue(maxsize=self.max_size)
        self.lock = Lock()
        self.created = 0

    def acquire(self) -> Any:
        while True:
            try:
                connection, returned_at = self.available.get_nowait()
            except Empty:
                connection = None
            if connection is not None:
                try:
                    if monotonic() - returned_at >= 30:
                        connection.ping(reconnect=True)
                    return connection
                except Exception:
                    self._discard(connection)
                    continue

            with self.lock:
                if self.created < self.max_size:
                    self.created += 1
                    create = True
                else:
                    create = False
            if create:
                try:
                    return connect(self.settings)
                except Exception:
                    with self.lock:
                        self.created -= 1
                    raise
            try:
                connection, returned_at = self.available.get(timeout=5)
            except Empty as exc:
                raise DatabaseUnavailable("Database connection pool is busy") from exc
            try:
                if monotonic() - returned_at >= 30:
                    connection.ping(reconnect=True)
                return connection
            except Exception:
                self._discard(connection)

    def release(self, connection: Any, *, broken: bool = False) -> None:
        if broken:
            self._discard(connection)
            return
        try:
            self.available.put_nowait((connection, monotonic()))
        except Full:
            self._discard(connection)

    def _discard(self, connection: Any) -> None:
        try:
            connection.close()
        except Exception:
            pass
        with self.lock:
            self.created = max(0, self.created - 1)

    def close(self) -> None:
        while True:
            try:
                connection, _returned_at = self.available.get_nowait()
            except Empty:
                break
            self._discard(connection)


_POOLS: dict[tuple[Any, ...], _ConnectionPool] = {}
_POOLS_LOCK = Lock()


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


def _pool_key(settings: Settings) -> tuple[Any, ...]:
    return (
        settings.db_host,
        settings.db_port,
        settings.db_name,
        settings.db_user,
        settings.db_password,
        settings.db_ssl_mode,
        settings.db_pool_size,
    )


def _pool(settings: Settings) -> _ConnectionPool:
    key = _pool_key(settings)
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = _ConnectionPool(settings)
            _POOLS[key] = pool
        return pool


def close_connection_pools() -> None:
    """Close idle connections, primarily for clean shutdowns and tests."""
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        pool.close()


@contextmanager
def transaction() -> Iterator[Any]:
    settings = get_settings()
    pool = _pool(settings)
    connection = pool.acquire()
    broken = False
    try:
        yield connection
    except Exception:
        try:
            connection.rollback()
        except Exception:
            broken = True
        raise
    else:
        try:
            connection.commit()
        except Exception:
            broken = True
            try:
                connection.rollback()
            except Exception:
                pass
            raise
    finally:
        pool.release(connection, broken=broken)


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
            _ensure_column(cursor, "wishlist_items", "purchased_at", "DATE NULL")
            _ensure_column(cursor, "crawl_jobs", "skipped_count", "INT UNSIGNED NOT NULL DEFAULT 0")
            _ensure_column(
                cursor,
                "books",
                "content_rating",
                "VARCHAR(30) NOT NULL DEFAULT 'unknown' AFTER media_type",
            )
            _ensure_column(cursor, "books", "rating_raw", "VARCHAR(100) NULL AFTER content_rating")
            _ensure_column(
                cursor,
                "books",
                "rating_source",
                "VARCHAR(30) NOT NULL DEFAULT 'unknown' AFTER rating_raw",
            )
            _ensure_column(
                cursor,
                "books",
                "rating_confidence",
                "TINYINT UNSIGNED NOT NULL DEFAULT 0 AFTER rating_source",
            )
            _ensure_column(
                cursor,
                "books",
                "rating_locked",
                "BOOLEAN NOT NULL DEFAULT FALSE AFTER rating_confidence",
            )
            _ensure_index(
                cursor,
                "books",
                "idx_books_rating",
                "(`content_rating`, `rating_locked`)",
            )


def _ensure_column(cursor: Any, table: str, column: str, definition: str) -> None:
    cursor.execute(
        "SELECT COUNT(*) AS present FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    if not cursor.fetchone()["present"]:
        cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")


def _ensure_index(cursor: Any, table: str, index: str, columns: str) -> None:
    cursor.execute(
        "SELECT COUNT(*) AS present FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
        (table, index),
    )
    if not cursor.fetchone()["present"]:
        cursor.execute(f"ALTER TABLE `{table}` ADD INDEX `{index}` {columns}")


def ping() -> dict[str, Any]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION() AS version, DATABASE() AS database_name")
            return cursor.fetchone()
