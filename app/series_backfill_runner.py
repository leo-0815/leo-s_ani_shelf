"""Local-only queue and durable review list for automatic historical series lookup."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
import time
from inspect import signature

from .config import ROOT
from .db import transaction
from .models import BookRecord
from .series_backfill import volume
from .series_discovery import HOSTS, _accept, discover, product_url


STATE = ROOT / 'logs' / 'series-backfill.sqlite3'
BATCH_SIZE = 6
REVIEW_LIMIT = 50
RETRY = {'source_error': 3600, 'partial': 86400, 'source_blocked': 86400}
RECHECK = 7 * 86400
SCHEMA = '''
CREATE TABLE IF NOT EXISTS series_checks (
    code TEXT, series_key TEXT, media TEXT, checked_at INTEGER NOT NULL,
    status TEXT NOT NULL, details TEXT NOT NULL,
    PRIMARY KEY (code, series_key, media)
);
CREATE TABLE IF NOT EXISTS source_holds (code TEXT PRIMARY KEY, retry_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS review (
    id INTEGER PRIMARY KEY, code TEXT, source_key TEXT, isbn TEXT,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(code, source_key), UNIQUE(isbn)
);
'''


def _identity(row):
    return row['code'], row['series_key'], row['media_type']


def catalogue_gaps():
    """One read of local book metadata, independent of personal follows and preferences."""
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT p.code,b.series_key,b.series_title,b.media_type,b.volume_label,b.source_url "
            "FROM books b JOIN publishers p ON p.id=b.publisher_id "
            "WHERE p.enabled AND p.code IN (%s,%s,%s,%s,%s) AND b.series_key<>'' "
            "AND b.series_title<>'' AND b.media_type IN ('manga','novel') "
            "AND b.edition_type='standard' AND b.volume_label REGEXP '^[0-9]{1,3}$' "
            "ORDER BY b.id DESC",
            tuple(HOSTS),
        )
        entries = cur.fetchall()
    groups = {}
    for item in entries:
        number = volume(item['volume_label'])
        if not number:
            continue
        row = groups.setdefault(_identity(item), dict(
            code=item['code'], series_key=item['series_key'], title=item['series_title'],
            media_type=item['media_type'], volumes=set(), seeds=[],
        ))
        row['volumes'].add(number)
        if not row['seeds'] and (seed := product_url(row['code'], item['source_url'], item['source_url'])):
            row['seeds'].append(seed)
    result = []
    for row in groups.values():
        if len(row['volumes']) < 2:  # Avoid claiming gaps from a single isolated or misparsed volume.
            continue
        row['gaps'] = sorted(set(range(1, max(row['volumes']) + 1)) - row['volumes'])
        if row['gaps']:
            result.append(row)
    return result


@contextmanager
def open_state(path=STATE):
    """OS lock is released on exit or crash; SQLite commits once per completed series."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a+b') as lock:
        try:
            import msvcrt
        except ImportError:
            import fcntl
            acquire = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            lock.seek(0)
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            acquire()
        except OSError as exc:
            raise RuntimeError('另一個系列補查視窗正在執行。') from exc
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(SCHEMA)
            yield conn


def _due(conn, rows, now):
    checks = {(code, key, media): (checked_at, status) for code, key, media, checked_at, status in
              conn.execute('SELECT code,series_key,media,checked_at,status FROM series_checks')}
    holds = dict(conn.execute('SELECT code,retry_at FROM source_holds WHERE retry_at>?', (now,)))
    queues = defaultdict(deque)
    for row in sorted(rows, key=lambda item: (
            checks.get(_identity(item), (0, ''))[0], len(item['gaps']), _identity(item))):
        checked_at, status = checks.get(_identity(row), (None, ''))
        if row['code'] not in holds and (checked_at is None or checked_at + RETRY.get(status, RECHECK) <= now):
            queues[row['code']].append(row)
    return queues, holds, checks


def _record(conn, row, result, now):
    status = result.status
    if status == 'checked_sample' and (result.errors or result.capped):
        status = 'partial'
    added = 0
    with conn:
        for book in result.books:
            if not _accept(row, book):
                continue
            # A shared ISBN may not resolve contradictory publisher/edition observations.
            matches = conn.execute(
                'SELECT id,code,source_key,isbn FROM review WHERE isbn=? OR (code=? AND source_key=?)',
                (book.isbn, book.publisher_code, book.source_key),
            ).fetchall()
            if matches:
                if len(matches) != 1 or tuple(matches[0][1:]) != (book.publisher_code, book.source_key, book.isbn):
                    conn.executemany("UPDATE review SET status='conflict' WHERE id=? AND status='pending'",
                                     [(match[0],) for match in matches])
                continue
            cursor = conn.execute('INSERT INTO review (code,source_key,isbn,payload) VALUES (?,?,?,?)',
                                  (book.publisher_code, book.source_key, book.isbn,
                                   json.dumps(asdict(book), default=_serialize_date, ensure_ascii=False)))
            added += cursor.rowcount
        volumes_found = {volume(book.volume_label) for book in result.books if _accept(row, book)}
        conn.execute('INSERT OR REPLACE INTO series_checks VALUES (?,?,?,?,?,?)', (*_identity(row), now, status,
                     json.dumps({'title': row['title'], 'found': sorted(volumes_found), 'new_review': added,
                                 'unresolved': sorted(set(row['gaps']) - volumes_found),
                                 'errors': result.errors, 'capped': result.capped}, ensure_ascii=False)))
        if status == 'source_blocked':
            conn.execute('INSERT OR REPLACE INTO source_holds VALUES (?,?)', (row['code'], now + RETRY[status]))
    return status, added


def scan(conn, rows, *, limit=BATCH_SIZE, lookup=discover, now=None, emit=print):
    now = int(time.time()) if now is None else now
    queues, holds, checks = _due(conn, rows, now)
    # Start at the least-recently checked publisher, even when batches are cut short.
    latest = {code: max((stamp for key, (stamp, _) in checks.items() if key[0] == code), default=0)
              for code in queues}
    order = sorted(queues, key=lambda code: (latest[code], code))
    tried = Counter()
    reviewed = 0
    suspended = set(holds)
    while sum(tried.values()) < limit and any(queues[code] for code in order if code not in suspended):
        for code in order:
            if sum(tried.values()) >= limit:
                break
            if code in suspended or not queues[code]:
                continue
            row = queues[code].popleft()
            result = lookup(row)
            status, added = _record(conn, row, result, now)
            tried[status] += 1
            reviewed += added
            emit(f"{code}｜{row['title']}：{LABELS[status]}，待確認 +{added}")
            if status in {'source_blocked', 'source_error'}:
                suspended.add(code)
    return {'attempted': sum(tried.values()), 'new_review': reviewed,
            'due_after': sum(map(len, queues.values())), 'source_hold': sorted(suspended)}


LABELS = {
    'checked_sample': '已抽查', 'partial': '僅完成部分查詢', 'source_blocked': '來源拒絕存取（暫停）',
    'source_error': '來源或解析異常（本輪暫停）', 'no_verified_series_links': '找不到官方同系列連結',
    'no_official_seed': '缺可用的官方商品連結', 'no_visible_gap': '無可見缺卷',
}


def _serialize_date(value):
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f'Unsupported review field: {type(value).__name__}')


def pending(conn):
    for key, payload in conn.execute("SELECT id,payload FROM review WHERE status='pending' ORDER BY id LIMIT ?",
                                     (REVIEW_LIMIT,)):
        fields = json.loads(payload)
        if fields['release_date']:
            fields['release_date'] = date.fromisoformat(fields['release_date'])
        for key_name in ('rating_checked_at', 'release_checked_at'):
            if fields.get(key_name):
                raw = fields[key_name]
                # Older review files may have lost the time; do not invent one.
                fields[key_name] = datetime.fromisoformat(raw) if len(raw) > 10 else None
        yield key, BookRecord(**fields)


def apply_review(conn, reviewed):
    from .repository import upsert_book

    inserted = skipped = 0
    for key, book in reviewed:
        # Existing rows and edition identities are checked inside the insert transaction.
        state = upsert_book(book, insert_only=True)
        with conn:
            conn.execute('UPDATE review SET status=? WHERE id=?', (state, key))
        inserted += state == 'inserted'
        skipped += state != 'inserted'
    return inserted, skipped


def supports_safe_insert():
    from .repository import upsert_book

    return 'insert_only' in signature(upsert_book).parameters


def run_preview(limit: int):
    """Read local public catalog metadata and official pages; keep results in memory."""
    if type(limit) is not int or not 1 <= limit <= BATCH_SIZE:
        raise ValueError(f'試查系列數須為 1–{BATCH_SIZE}')
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.executescript(SCHEMA)
        result = scan(conn, catalogue_gaps(), limit=limit)
        print(f"試查 {result['attempted']} 個系列，找到 {result['new_review']} 筆待確認候選。")
        for code, details in conn.execute('SELECT code,details FROM series_checks ORDER BY rowid'):
            row = json.loads(details)
            found = '、'.join(map(str, row['found'])) or '無'
            missing = row['unresolved']
            unresolved = '、'.join(map(str, missing[:10])) or '無'
            if len(missing) > 10:
                unresolved += f'…共 {len(missing)} 卷'
            print(f"{code}｜{row['title']}：核對到 {found}；未確認 {unresolved}")
        if result['source_hold']:
            print('本輪已停止的來源：' + '、'.join(result['source_hold']))
        print('試查未寫入書目、未保存斷點、未上傳。')
        return result


def run_interactive():
    print('自動找缺卷：每輪最多 6 個系列。Ctrl+C 可停止；已查的系列和待確認書目會保留。')
    try:
        with open_state() as conn:
            result = scan(conn, catalogue_gaps())
            print(f"本輪查了 {result['attempted']} 個系列，新增 {result['new_review']} 筆待確認。")
            if result['source_hold']:
                print('暫停來源：' + '、'.join(result['source_hold']))
            print('抽查結果不代表系列已完整。')
            reviewed = list(pending(conn))
            if not reviewed:
                return
            for _, book in reviewed:
                print(f'  {book.publisher_code}｜{book.title} [{book.isbn}]')
            if not supports_safe_insert():
                print('此版本缺少安全新增功能，只保留候選，不修改書庫。')
                return
            if input(f'新增以上 {len(reviewed)} 筆到本機？輸入 YES 確認（不會上傳）：').strip() == 'YES':
                inserted, skipped = apply_review(conn, reviewed)
                print(f'本機新增 {inserted} 筆，略過已存在或衝突 {skipped} 筆。')
    except KeyboardInterrupt:
        print('\n已停止；已完成的系列與待確認清單已保存。')
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        print(f'系列補查中止：{exc}')
