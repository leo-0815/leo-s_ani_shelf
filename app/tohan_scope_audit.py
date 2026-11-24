"""Read-only review of legacy Tohan media types. Never rewrites or deletes books."""
import argparse
import json
from collections import Counter
from .sources.tohan_public import detail_media_type, require_public_product, SourceLoginRequired, UnsupportedTohanProduct
from .release_dates import product_url_valid


def inspect(row: dict, markup: str) -> dict:
    result = {key: row.get(key) for key in ('id','source_key','title','media_type')}
    try:
        require_public_product(markup)
        kind = detail_media_type(markup)
        result.update(status='needs_review' if kind == 'unknown' else 'same' if kind == row['media_type'] else 'type_mismatch',
                      observed_media_type=kind)
    except SourceLoginRequired:
        result['status'] = 'requires_login'
    except UnsupportedTohanProduct as exc:
        result.update(status='out_of_scope', reason=str(exc))
    return result


def main() -> None:
    from .db import transaction
    from .recent_recheck import canonical_product
    from .sources.common import fetch_html, polite_pause
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('limit must be 1..100')
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT b.id,b.source_key,b.title,b.media_type,b.source_url FROM books b "
                           "JOIN publishers p ON p.id=b.publisher_id WHERE p.code='tohan' ORDER BY b.id LIMIT %s",
                           (args.limit,))
            rows = list(cursor.fetchall())
    counts = Counter()
    for row in rows:
        try:
            url = canonical_product('tohan', row['source_url'])
            if not url or not product_url_valid('tohan', url, row['source_key']):
                raise ValueError('Not an exact official product URL')
            polite_pause(1)
            result = inspect(row, fetch_html(url, timeout=15, attempts=1))
        except Exception as exc:
            result = dict(id=row['id'], source_key=row['source_key'], status='fetch_error', error=str(exc))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        counts[result['status']] += 1
    print(json.dumps({'checked': len(rows), 'summary': dict(counts), 'read_only': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
