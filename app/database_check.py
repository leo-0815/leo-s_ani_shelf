from __future__ import annotations

import json
from typing import Any

from .db import connect
from .repository import list_series


def run_database_smoke_test() -> dict[str, Any]:
    """Run a small read-only query set used by deployment checks."""
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION() AS version, DATABASE() AS database_name")
            server = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) AS count FROM publishers")
            publisher_count = int(cursor.fetchone()["count"])
            cursor.execute("SELECT COUNT(*) AS count FROM books")
            book_count = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT "
                "SUM(series_title IS NOT NULL AND series_title <> '') AS with_series_title, "
                "SUM(series_key IS NOT NULL AND series_key <> '') AS with_series_key "
                "FROM books"
            )
            coverage = cursor.fetchone()
            cursor.execute("SELECT MIN(id) AS id FROM users WHERE is_active = TRUE")
            user = cursor.fetchone()
    finally:
        connection.close()

    result = {
        "database": server["database_name"],
        "version": server["version"],
        "publishers": publisher_count,
        "books": book_count,
        "books_with_series_title": int(coverage["with_series_title"] or 0),
        "books_with_series_key": int(coverage["with_series_key"] or 0),
    }
    if user and user.get("id"):
        try:
            series = list_series({}, int(user["id"]), limit=1)
            result["series_query_total"] = int(series["total"])
            result["series_query_items"] = len(series["items"])
        except Exception as exc:  # Diagnostic command must print the database error.
            result["series_query_error"] = f"{type(exc).__name__}: {exc}"
    return result


def main() -> None:
    print(json.dumps(run_database_smoke_test(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
