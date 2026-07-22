from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from .db import _driver


ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")

# Parent tables are copied before children. Resetting uses the reverse order.
TABLE_ORDER = (
    "publishers",
    "users",
    "books",
    "catalog_changes",
    "catalog_sync_state",
    "user_sessions",
    "oauth_login_states",
    "crawl_jobs",
    "source_sync_state",
    "source_backfill_progress",
    "followed_series",
    "wishlist_items",
    "recommendation_dismissals",
    "release_history",
    "notification_deliveries",
    "notification_preferences",
)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    user: str
    password: str
    database: str
    ssl_mode: str = "verify_identity"

    @property
    def label(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


@dataclass(frozen=True)
class TableResult:
    table: str
    source_count: int
    target_count: int
    source_digest: str | None = None
    target_digest: str | None = None

    @property
    def matches(self) -> bool:
        if self.source_count != self.target_count:
            return False
        if self.source_digest is None or self.target_digest is None:
            return True
        return self.source_digest == self.target_digest


def _identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise MigrationError(f"Invalid SQL identifier: {value!r}")
    return f"`{value}`"


def _connect(endpoint: Endpoint, include_database: bool = True):
    pymysql, dict_cursor = _driver()
    kwargs: dict[str, Any] = {
        "host": endpoint.host,
        "port": endpoint.port,
        "user": endpoint.user,
        "password": endpoint.password,
        "charset": "utf8mb4",
        "cursorclass": dict_cursor,
        "autocommit": False,
        "connect_timeout": 10,
        "read_timeout": 60,
        "write_timeout": 60,
    }
    if include_database:
        kwargs["database"] = endpoint.database
    if endpoint.ssl_mode in {"preferred", "required"}:
        kwargs["ssl"] = {"check_hostname": False}
    elif endpoint.ssl_mode in {"verify_ca", "verify_identity"}:
        kwargs["ssl_verify_cert"] = True
        kwargs["ssl_verify_identity"] = endpoint.ssl_mode == "verify_identity"
    elif endpoint.ssl_mode != "disabled":
        raise MigrationError(f"Unsupported SSL mode: {endpoint.ssl_mode}")
    return pymysql.connect(**kwargs)


def _ensure_distinct(source: Endpoint, target: Endpoint) -> None:
    source_key = (source.host.casefold(), source.port, source.database.casefold())
    target_key = (target.host.casefold(), target.port, target.database.casefold())
    if source_key == target_key:
        raise MigrationError("Source and target point to the same database")


def _available_tables(connection: Any) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_NAME AS table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
        )
        return {str(row["table_name"]) for row in cursor.fetchall()}


def _columns(connection: Any, table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME AS column_name, EXTRA AS extra "
            "FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        )
        return [
            str(row["column_name"])
            for row in cursor.fetchall()
            if "GENERATED" not in str(row.get("extra") or "").upper()
        ]


def _primary_key(connection: Any, table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME AS column_name FROM information_schema.key_column_usage "
            "WHERE table_schema = DATABASE() AND table_name = %s "
            "AND constraint_name = 'PRIMARY' ORDER BY ordinal_position",
            (table,),
        )
        return [str(row["column_name"]) for row in cursor.fetchall()]


def _count(connection: Any, table: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS row_count FROM {_identifier(table)}")
        return int(cursor.fetchone()["row_count"])


def inspect_counts(connection: Any, tables: Sequence[str]) -> dict[str, int]:
    available = _available_tables(connection)
    return {table: _count(connection, table) for table in tables if table in available}


def _canonical(value: Any) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, bytes):
        return b"B" + str(len(value)).encode() + b":" + value
    if isinstance(value, datetime):
        text = value.isoformat(sep=" ", timespec="microseconds")
    elif isinstance(value, (date, time)):
        text = value.isoformat()
    elif isinstance(value, Decimal):
        text = format(value, "f")
    else:
        text = str(value)
    encoded = text.encode("utf-8", errors="surrogatepass")
    return b"T" + str(len(encoded)).encode() + b":" + encoded


def digest_rows(rows: Iterable[dict[str, Any]], columns: Sequence[str]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        count += 1
        for column in columns:
            digest.update(_canonical(row[column]))
            digest.update(b"|")
        digest.update(b"\n")
    return count, digest.hexdigest()


def table_digest(connection: Any, table: str, chunk_size: int = 1000) -> tuple[int, str]:
    columns = _columns(connection, table)
    primary_key = _primary_key(connection, table)
    if not columns or not primary_key:
        raise MigrationError(f"{table} must have columns and a primary key")
    quoted_columns = ", ".join(_identifier(column) for column in columns)
    order_by = ", ".join(_identifier(column) for column in primary_key)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {quoted_columns} FROM {_identifier(table)} ORDER BY {order_by}"
        )

        def batches():
            while True:
                rows = cursor.fetchmany(chunk_size)
                if not rows:
                    break
                yield from rows

        return digest_rows(batches(), columns)


def _schema_statements() -> list[str]:
    schema = (ROOT / "app" / "schema.sql").read_text(encoding="utf-8")
    return [statement.strip() for statement in schema.split(";") if statement.strip()]


def ensure_target_schema(endpoint: Endpoint) -> None:
    database = _identifier(endpoint.database)
    connection = _connect(endpoint, include_database=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {database} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
        connection.commit()
    finally:
        connection.close()

    connection = _connect(endpoint)
    try:
        with connection.cursor() as cursor:
            for statement in _schema_statements():
                cursor.execute(statement)
        connection.commit()
    finally:
        connection.close()


def target_database_exists(endpoint: Endpoint) -> bool:
    connection = _connect(endpoint, include_database=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS present FROM information_schema.schemata "
                "WHERE schema_name = %s",
                (endpoint.database,),
            )
            return bool(cursor.fetchone()["present"])
    finally:
        connection.close()


def _target_has_user_data(connection: Any) -> bool:
    counts = inspect_counts(connection, TABLE_ORDER)
    return any(count for table, count in counts.items() if table != "publishers") or counts.get(
        "publishers", 0
    ) > 6


def reset_target(connection: Any, tables: Sequence[str]) -> None:
    available = _available_tables(connection)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            for table in reversed(tables):
                if table in available:
                    cursor.execute(f"DELETE FROM {_identifier(table)}")
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _upsert_sql(table: str, columns: Sequence[str]) -> str:
    quoted = [_identifier(column) for column in columns]
    updates = ", ".join(f"{column}=VALUES({column})" for column in quoted)
    placeholders = ", ".join(["%s"] * len(columns))
    return (
        f"INSERT INTO {_identifier(table)} ({', '.join(quoted)}) "
        f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}"
    )


def copy_table(
    source: Any,
    target: Any,
    table: str,
    chunk_size: int = 500,
) -> int:
    source_columns = _columns(source, table)
    target_columns = set(_columns(target, table))
    missing = [column for column in source_columns if column not in target_columns]
    if missing:
        raise MigrationError(f"Target {table} is missing columns: {', '.join(missing)}")
    primary_key = _primary_key(source, table)
    if not source_columns or not primary_key:
        raise MigrationError(f"{table} must have columns and a primary key")

    quoted_columns = ", ".join(_identifier(column) for column in source_columns)
    order_by = ", ".join(_identifier(column) for column in primary_key)
    statement = _upsert_sql(table, source_columns)
    copied = 0
    with source.cursor() as source_cursor, target.cursor() as target_cursor:
        source_cursor.execute(
            f"SELECT {quoted_columns} FROM {_identifier(table)} ORDER BY {order_by}"
        )
        while True:
            rows = source_cursor.fetchmany(chunk_size)
            if not rows:
                break
            values = [tuple(row[column] for column in source_columns) for row in rows]
            target_cursor.executemany(statement, values)
            target.commit()
            copied += len(values)
    return copied


def copy_database(
    source: Any,
    target: Any,
    tables: Sequence[str],
    *,
    reset: bool = False,
    resume: bool = False,
) -> dict[str, int]:
    source_tables = _available_tables(source)
    missing_source = [table for table in tables if table not in source_tables]
    if missing_source:
        raise MigrationError(f"Source is missing tables: {', '.join(missing_source)}")
    if reset:
        reset_target(target, tables)
    elif _target_has_user_data(target) and not resume:
        raise MigrationError(
            "Target already contains user data; use --resume or explicitly reset the target"
        )

    with source.cursor() as cursor:
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")

    copied: dict[str, int] = {}
    try:
        for table in tables:
            copied[table] = copy_table(source, target, table)
    finally:
        source.rollback()
    return copied


def verify_database(source: Any, target: Any, tables: Sequence[str]) -> list[TableResult]:
    results: list[TableResult] = []
    source_tables = _available_tables(source)
    target_tables = _available_tables(target)
    for table in tables:
        if table not in source_tables or table not in target_tables:
            results.append(
                TableResult(
                    table=table,
                    source_count=-1 if table not in source_tables else _count(source, table),
                    target_count=-1 if table not in target_tables else _count(target, table),
                )
            )
            continue
        source_count, source_digest = table_digest(source, table)
        target_count, target_digest = table_digest(target, table)
        results.append(
            TableResult(
                table=table,
                source_count=source_count,
                target_count=target_count,
                source_digest=source_digest,
                target_digest=target_digest,
            )
        )
    return results


def _endpoint_from_args(prefix: str, args: argparse.Namespace) -> Endpoint:
    password = os.getenv(f"ANISHELF_MIGRATION_{prefix.upper()}_PASSWORD")
    if password is None:
        password = getpass.getpass(f"{prefix.title()} TiDB password: ")
    return Endpoint(
        host=getattr(args, f"{prefix}_host"),
        port=getattr(args, f"{prefix}_port"),
        user=getattr(args, f"{prefix}_user"),
        password=password,
        database=getattr(args, f"{prefix}_database"),
        ssl_mode=getattr(args, f"{prefix}_ssl_mode"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely copy and verify one AniShelf TiDB database to another region."
    )
    parser.add_argument("--source-host", required=True)
    parser.add_argument("--source-port", type=int, default=4000)
    parser.add_argument("--source-user", required=True)
    parser.add_argument("--source-database", default="anishelf")
    parser.add_argument("--source-ssl-mode", default="verify_identity")
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, default=4000)
    parser.add_argument("--target-user", required=True)
    parser.add_argument("--target-database", default="anishelf")
    parser.add_argument("--target-ssl-mode", default="verify_identity")
    parser.add_argument("--execute", action="store_true", help="Copy data after inspection")
    parser.add_argument("--resume", action="store_true", help="Resume into a partially copied target")
    parser.add_argument(
        "--reset-target",
        action="store_true",
        help="Delete target rows before copying; requires --execute and --confirm-target",
    )
    parser.add_argument(
        "--confirm-target",
        help="Must exactly equal target host when --reset-target is used",
    )
    parser.add_argument("--skip-digest", action="store_true")
    return parser


def _print_counts(title: str, counts: dict[str, int]) -> None:
    print(title)
    for table in TABLE_ORDER:
        if table in counts:
            print(f"  {table:32} {counts[table]:>10,}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (args.resume or args.reset_target) and not args.execute:
        raise MigrationError("--resume and --reset-target require --execute")

    source_endpoint = _endpoint_from_args("source", args)
    target_endpoint = _endpoint_from_args("target", args)
    _ensure_distinct(source_endpoint, target_endpoint)
    if args.reset_target and args.confirm_target != target_endpoint.host:
        raise MigrationError("--confirm-target must exactly equal the target host")

    print(f"Source: {source_endpoint.label}")
    print(f"Target: {target_endpoint.label}")
    source = _connect(source_endpoint)
    try:
        source_counts = inspect_counts(source, TABLE_ORDER)
        _print_counts("Source rows:", source_counts)
    finally:
        source.close()

    if target_database_exists(target_endpoint):
        target = _connect(target_endpoint)
        try:
            target_counts = inspect_counts(target, TABLE_ORDER)
            _print_counts("Target rows:", target_counts)
        finally:
            target.close()
    else:
        print(f"Target database {target_endpoint.database!r} does not exist yet (fresh target).")

    if not args.execute:
        print("Dry run complete. No schema or data was changed.")
        return 0

    ensure_target_schema(target_endpoint)
    source = _connect(source_endpoint)
    target = _connect(target_endpoint)
    try:
        copied = copy_database(
            source,
            target,
            TABLE_ORDER,
            reset=args.reset_target,
            resume=args.resume,
        )
        _print_counts("Copied rows:", copied)
    finally:
        source.close()
        target.close()

    source = _connect(source_endpoint)
    target = _connect(target_endpoint)
    try:
        if args.skip_digest:
            source_counts = inspect_counts(source, TABLE_ORDER)
            target_counts = inspect_counts(target, TABLE_ORDER)
            results = [
                TableResult(table, source_counts.get(table, -1), target_counts.get(table, -1))
                for table in TABLE_ORDER
            ]
        else:
            results = verify_database(source, target, TABLE_ORDER)
    finally:
        source.close()
        target.close()

    failures = [result for result in results if not result.matches]
    print("Verification:")
    for result in results:
        marker = "OK" if result.matches else "MISMATCH"
        print(
            f"  {marker:8} {result.table:28} "
            f"source={result.source_count:,} target={result.target_count:,}"
        )
    if failures:
        raise MigrationError(f"Verification failed for {len(failures)} table(s)")
    print("Migration copy and verification completed successfully.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as exc:
        print(f"Migration stopped: {exc}", file=sys.stderr)
        raise SystemExit(2)
