"""Small daily grading budget, independent from publication-date revalidation."""
from __future__ import annotations
import time
from datetime import datetime, timedelta
from .db import transaction
from .rating_enrichment import CHECK_TABLE_SQL, candidates, check_book
from .sources.product_rating import PARSER_VERSIONS

DAILY_LIMIT = 20
BUDGET_SQL = """
CREATE TABLE IF NOT EXISTS rating_refresh_budget (
 budget_day DATE NOT NULL PRIMARY KEY,
 attempted INT UNSIGNED NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def claim_daily_budget(now: datetime, limit: int = DAILY_LIMIT) -> bool:
    day = (now + timedelta(hours=8)).date()
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT IGNORE INTO rating_refresh_budget (budget_day) VALUES (%s)", (day,))
            cursor.execute("UPDATE rating_refresh_budget SET attempted=attempted+1 "
                           "WHERE budget_day=%s AND attempted < %s", (day, min(max(limit,0),DAILY_LIMIT)))
            return cursor.rowcount == 1


def refresh_recent_ratings(*, limit: int = DAILY_LIMIT, minutes: float = 10) -> dict:
    limit = min(max(int(limit), 0), DAILY_LIMIT)
    counts = dict(checked=0, updated=0, confirmed=0, unknown=0, errors=[], daily_limit=DAILY_LIMIT)
    if not limit or minutes <= 0:
        return counts
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(CHECK_TABLE_SQL)
            cursor.execute(BUDGET_SQL)
    deadline = time.monotonic() + min(minutes,10) * 60
    while counts["checked"] < limit:
        progressed = False
        for code in PARSER_VERSIONS:
            if time.monotonic() >= deadline:
                return counts
            # 90-day recent arrivals / releases, not a full historical crawl.
            rows = candidates(min(4,limit-counts["checked"]), code=code, recent_days=90)
            for row in rows:
                if time.monotonic() >= deadline or counts["checked"] >= limit:
                    return counts
                if not claim_daily_budget(datetime.utcnow()):
                    counts["budget_exhausted"] = True
                    return counts
                time.sleep(1.5)
                grade, raw, error, changed = check_book(row)
                progressed = True
                counts["checked"] += 1
                counts["updated"] += bool(changed)
                counts["confirmed" if grade != "unknown" else "unknown"] += 1
                if error:
                    counts["errors"].append(dict(publisher=code, source_key=row["source_key"], error=error))
        if not progressed:
            break
    return counts


def main() -> None:
    import argparse
    import json
    from .db import ensure_schema
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DAILY_LIMIT)
    args = parser.parse_args()
    ensure_schema()
    print(json.dumps(refresh_recent_ratings(limit=args.limit), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

