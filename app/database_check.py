from __future__ import annotations

import json
from typing import Any

from .db import connect


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
    finally:
        connection.close()

    return {
        "database": server["database_name"],
        "version": server["version"],
        "publishers": publisher_count,
        "books": book_count,
    }


def main() -> None:
    print(json.dumps(run_database_smoke_test(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
