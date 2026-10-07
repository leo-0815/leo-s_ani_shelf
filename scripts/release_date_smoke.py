"""Read-only, one-product-per-publisher smoke test; never updates the catalog."""
from __future__ import annotations
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import transaction
from app.crawler import SOURCES
from app.release_dates import parse_product, url_patterns
from app.sources.common import fetch_html, parse_page

def main() -> None:
    today = datetime.now(timezone(timedelta(hours=8))).date()
    for code in ("kadokawa", "chingwin", "spp", "tongli", "tohan"):
        selected = [arg for arg in sys.argv[1:] if arg in SOURCES]
        if selected and code not in selected:
            continue
        with transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT b.id, b.source_key, b.title, b.source_url, b.isbn, b.media_type, "
                    "p.code AS publisher_code FROM books b JOIN publishers p ON p.id=b.publisher_id "
                    "WHERE p.code=%s AND (b.source_url LIKE %s OR b.source_url LIKE %s) "
                    "ORDER BY ABS(DATEDIFF(b.release_date, %s)), b.id DESC LIMIT 1",
                    (code, *url_patterns(code), today),
                )
                row = cursor.fetchone()
        if not row:
            print(json.dumps({"publisher": code, "result": "no_product_url"}, ensure_ascii=False), flush=True)
            continue
        markup = ""
        try:
            markup = fetch_html(row["source_url"], timeout=12, attempts=1)
            record = parse_product(SOURCES[code](), row, markup)
            result = {"publisher": code, "key": record.source_key, "url": record.source_url,
                      "date": record.release_date.isoformat(), "date_source": record.release_date_source}
        except Exception as exc:
            result = {"publisher": code, "key": row["source_key"], "url": row["source_url"], "error": str(exc)}
            if "--diagnostics" in sys.argv and markup:
                text = parse_page(markup).flat_text
                result["publication_fields"] = re.findall(r".{0,30}(?:出版日期|上市日期|上市日|產品編號|ISBN).{0,110}", text)[:12]
        print(json.dumps(result, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()

