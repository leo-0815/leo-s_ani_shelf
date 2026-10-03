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
            from .collection import migrate_collection
            migrate_collection(cursor, cloud=True)
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
            _ensure_column(cursor, "books", "series_key", "VARCHAR(190) NULL")
            _ensure_index(cursor, "books", "idx_books_series", "(`publisher_id`, `series_key`)")
            _ensure_followed_series_media_primary(cursor)
            _backfill_series_keys(cursor)
            _migrate_followed_series_keys(cursor)
            _seed_series_aliases(cursor)
            _apply_approved_series_aliases(cursor)


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


def _ensure_followed_series_media_primary(cursor: Any) -> None:
    cursor.execute(
        "SELECT GROUP_CONCAT(column_name ORDER BY seq_in_index) AS columns_list "
        "FROM information_schema.statistics WHERE table_schema = DATABASE() "
        "AND table_name = 'followed_series' AND index_name = 'PRIMARY'"
    )
    columns = str(cursor.fetchone().get("columns_list") or "")
    if columns != "user_id,publisher_id,normalized_series,media_type":
        # TiDB cannot replace an existing primary key with ALTER TABLE. Rebuild
        # this tiny per-user table and atomically swap it into place instead.
        cursor.execute("DROP TABLE IF EXISTS followed_series_rebuild")
        cursor.execute(
            "CREATE TABLE followed_series_rebuild ("
            "user_id BIGINT UNSIGNED NOT NULL, "
            "publisher_id BIGINT UNSIGNED NOT NULL, "
            "series_title VARCHAR(500) NOT NULL, "
            "normalized_series VARCHAR(190) NOT NULL, "
            "media_type VARCHAR(40) NOT NULL DEFAULT 'unknown', "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (user_id, publisher_id, normalized_series, media_type), "
            "KEY idx_followed_series_publisher (publisher_id), "
            "CONSTRAINT fk_followed_series_rebuild_user FOREIGN KEY (user_id) "
            "REFERENCES users(id) ON DELETE CASCADE, "
            "CONSTRAINT fk_followed_series_rebuild_publisher FOREIGN KEY (publisher_id) "
            "REFERENCES publishers(id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
            "COLLATE=utf8mb4_0900_ai_ci"
        )
        cursor.execute(
            "INSERT IGNORE INTO followed_series_rebuild "
            "(user_id, publisher_id, series_title, normalized_series, media_type, created_at) "
            "SELECT user_id, publisher_id, series_title, normalized_series, "
            "COALESCE(NULLIF(media_type, ''), 'unknown'), created_at FROM followed_series"
        )
        cursor.execute("DROP TABLE IF EXISTS followed_series_legacy")
        cursor.execute(
            "RENAME TABLE followed_series TO followed_series_legacy, "
            "followed_series_rebuild TO followed_series"
        )
        cursor.execute("DROP TABLE followed_series_legacy")


def _backfill_series_keys(cursor: Any, batch_size: int = 250) -> None:
    from .series import canonical_series_title, series_key

    cursor.execute(
        "SELECT b.id, b.series_title, p.code AS publisher_code FROM books b "
        "JOIN publishers p ON p.id = b.publisher_id "
        "WHERE b.series_title IS NOT NULL AND b.series_title <> '' "
        "AND (b.series_key IS NULL OR b.series_key = '')"
    )
    values = [
        (
            canonical_series_title(row["series_title"], row["publisher_code"]),
            series_key(row["series_title"], row["publisher_code"]),
            row["id"],
        )
        for row in cursor.fetchall()
    ]
    for start in range(0, len(values), max(1, batch_size)):
        batch = values[start : start + max(1, batch_size)]
        title_cases = " ".join("WHEN %s THEN %s" for _ in batch)
        key_cases = " ".join("WHEN %s THEN %s" for _ in batch)
        placeholders = ", ".join(["%s"] * len(batch))
        parameters: list[Any] = []
        for title, _key, book_id in batch:
            parameters.extend((book_id, title))
        for _title, key, book_id in batch:
            parameters.extend((book_id, key))
        parameters.extend(book_id for _title, _key, book_id in batch)
        cursor.execute(
            "UPDATE books SET "
            f"series_title = CASE id {title_cases} ELSE series_title END, "
            f"series_key = CASE id {key_cases} ELSE series_key END "
            f"WHERE id IN ({placeholders})",
            parameters,
        )


def _migrate_followed_series_keys(cursor: Any) -> None:
    from .series import canonical_series_title, series_key

    cursor.execute(
        "SELECT fs.user_id, fs.publisher_id, fs.series_title, fs.normalized_series, fs.media_type, "
        "p.code AS publisher_code FROM followed_series fs "
        "JOIN publishers p ON p.id = fs.publisher_id"
    )
    for row in cursor.fetchall():
        new_key = series_key(row["series_title"], row["publisher_code"])
        if not new_key or new_key == row["normalized_series"]:
            continue
        cursor.execute(
            "INSERT INTO followed_series "
            "(user_id, publisher_id, series_title, normalized_series, media_type) "
            "VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
            "series_title = VALUES(series_title), media_type = VALUES(media_type)",
            (
                row["user_id"], row["publisher_id"],
                canonical_series_title(row["series_title"], row["publisher_code"]),
                new_key,
                row["media_type"],
            ),
        )
        cursor.execute(
            "DELETE FROM followed_series WHERE user_id = %s AND publisher_id = %s "
            "AND normalized_series = %s AND media_type = %s",
            (
                row["user_id"], row["publisher_id"],
                row["normalized_series"], row["media_type"],
            ),
        )


def _apply_approved_series_aliases(cursor: Any) -> None:
    cursor.execute(
        "UPDATE books b JOIN series_aliases sa "
        "ON sa.publisher_id = b.publisher_id AND sa.alias_key = b.series_key "
        "SET b.series_key = sa.canonical_key WHERE sa.approved = TRUE "
        "AND b.series_key <> sa.canonical_key"
    )


def _seed_series_aliases(cursor: Any) -> None:
    from .series import SERIES_ALIAS_GROUPS, series_alias_key, series_key

    for group in SERIES_ALIAS_GROUPS:
        if not group.publisher_code:
            continue
        cursor.execute("SELECT id FROM publishers WHERE code = %s", (group.publisher_code,))
        publisher = cursor.fetchone()
        if not publisher:
            continue
        canonical_key = series_key(group.canonical, group.publisher_code)
        for alias in (group.canonical, *group.aliases):
            cursor.execute(
                "INSERT INTO series_aliases "
                "(publisher_id, alias_key, canonical_title, canonical_key, "
                "match_method, confidence, approved) VALUES (%s, %s, %s, %s, "
                "'dictionary', 100, TRUE) ON DUPLICATE KEY UPDATE "
                "canonical_title = VALUES(canonical_title), "
                "canonical_key = VALUES(canonical_key), match_method = 'dictionary', "
                "confidence = 100, approved = TRUE",
                (
                    publisher["id"],
                    series_alias_key(alias),
                    group.canonical,
                    canonical_key,
                ),
            )


def ping() -> dict[str, Any]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION() AS version, DATABASE() AS database_name")
            return cursor.fetchone()
