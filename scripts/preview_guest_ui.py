"""Local-only UI fixture. No database, SMTP, OAuth or cloud writes.

Run from the cloud checkout: python scripts/preview_guest_ui.py
This script does not modify AniShelf's normal startup or production handlers.
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse
from app import guest
from app.server import Handler

PORT=8872
BOOKS=[dict(id=i,title=f'訪客驗收測試書 ({i})',author='測試作者',publisher_name='測試出版社',publisher_code='test',
    series_title='訪客驗收測試書',series_key='fixture',media_type='novel',edition_type='standard',volume_label=str(i),
    release_status='scheduled' if i==3 else 'available',release_precision='day',release_date='2026-10-20',
    cover_url='',isbn='',first_seen_at='2026-10-09',content_rating='general',bl_category='') for i in (1,2,3)]

def public(path,query):
    rows=[b.copy() for b in BOOKS]
    if path=='books':
        q=query.get('q','');rows=[b for b in rows if q in b['title'] or q in b['author']]
        offset=int(query.get('offset',0));limit=int(query.get('limit',100))
        return dict(items=rows[offset:offset+limit],total=len(rows),offset=offset,limit=limit)
    if path=='book-batch':return dict(items=[b for b in rows if str(b['id']) in query['ids'].split(',')])
    if path=='publishers':return dict(items=[dict(code='test',name='測試出版社',enabled=True,book_count=3)])
    if path=='stats':return dict(total=3,scheduled=1,available=2,unknown=0,scheduled_undated=0)
    if path=='upcoming':return dict(items=rows[-1:],total=1,offset=0,limit=200)
    if path=='series':return dict(items=[dict(publisher_code='test',publisher_name='測試出版社',series_title='訪客驗收測試書',series_key='fixture',media_type='novel',book_count=3,volume_count=3,scheduled_count=1,cover_url='')],total=1,offset=0,limit=60)
    if path=='series/detail':return dict(items=rows,publisher_code='test',publisher_name='測試出版社',series_title='訪客驗收測試書',series_key='fixture',media_type='novel',missing_volumes=[],has_more=False,total=3,next_offset=3)
    if path.startswith('books/') and path.endswith('/recommendations'):
        return dict(items=[dict(b,recommendation_types=['same_series'],recommendation_reason='同系列測試') for b in rows if str(b['id'])!=path.split('/')[1]])
    if path.startswith('books/'):return next(b for b in rows if b['id']==int(path.split('/')[1]))
    raise ValueError('Fixture route unavailable')

if __name__=='__main__':
    settings=SimpleNamespace(public_url=f'http://127.0.0.1:{PORT}',cloud_mode=False,auth_configured=False)
    with ExitStack() as stack:
        stack.enter_context(patch('app.server.get_settings',return_value=settings))
        stack.enter_context(patch('app.server.current_user',return_value=None))
        stack.enter_context(patch('app.guest.public_get',side_effect=public))
        stack.enter_context(patch('app.guest.resolve_series',side_effect=lambda payload:dict(items=[b.copy() for b in BOOKS],has_more=False,next_after_id=3)))
        stack.enter_context(patch('app.db.connect',side_effect=RuntimeError('Fixture forbids DB access')))
        print(f'Guest-only, no-DB fixture: http://127.0.0.1:{PORT}/',flush=True)
        ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
