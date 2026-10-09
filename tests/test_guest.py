import json
import sqlite3
import unittest
from contextlib import ExitStack, contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import guest, guest_import
from app.abuse import SlidingWindow, RequestError
from app.auth import SESSION_COOKIE
from app.server import Handler
from app.resource_guards import ResourceGuard


class GuestIdentityTests(unittest.TestCase):
    def test_signature_expiry_tamper_and_cookie_flags(self):
        with patch('app.guest.time.time', return_value=1000000000):
            owner, token = guest.issue()
            self.assertEqual(guest.identity(token), owner)
            self.assertIsNone(guest.identity(token[:-1] + ('a' if token[-1] != 'a' else 'b')))
            self.assertIsNone(guest.identity('arbitrary'))
        with patch('app.guest.time.time', return_value=1000086400):
            self.assertIsNone(guest.identity(token))
        self.assertIn('HttpOnly; SameSite=Lax; Secure', guest.cookie(token, True))
        self.assertNotIn('Secure', guest.cookie(token, False))

    def test_import_confirmation_is_owner_payload_and_expiry_bound(self):
        raw = {'version': 1, 'custom': [{'title': 'book'}]}
        with patch('app.guest.time.time', return_value=1000000000):
            token = guest.import_token(8, raw)
            self.assertTrue(guest.valid_import_token(8, raw, token))
            self.assertFalse(guest.valid_import_token(9, raw, token))
            self.assertFalse(guest.valid_import_token(8, {**raw, 'custom': []}, token))
            self.assertFalse(guest.valid_import_token(8, raw, None))
            self.assertIsNone(guest.identity(token))
        with patch('app.guest.time.time', return_value=1000000600):
            self.assertFalse(guest.valid_import_token(8, raw, token))

    @patch('app.guest.repo.list_books')
    def test_public_whitelist_never_passes_private_filters_or_user_zero(self, books):
        books.return_value = {'items': [{'id': 3, 'title': 'public', 'user_id': 99,
            'wishlist_notes': 'SECRET', 'collection': {'notes': 'SECRET'}, 'is_owned': True}], 'total': 1}
        data = guest.public_get('books', {'collection': '1', 'wishlist': '1', 'user_id': '99', 'q': 'public'})
        self.assertEqual(data['items'], [{'id': 3, 'title': 'public'}])
        self.assertEqual(books.call_args.args, ({'q': 'public'}, None, 100, 0))

    def test_resolve_and_batch_reject_unbounded_input_before_db(self):
        for raw in ({'series': []}, {'series': [{}]*21}, {'series': [{}]}, {'series': [], 'after': True}):
            with self.assertRaises(RequestError): guest.resolve_series(raw)
        for raw in ('0', '9'*30, ','.join(['1']*101), '1;SELECT'):
            with self.assertRaises(RequestError): guest.public_get('book-batch', {'ids': raw})
        with self.assertRaises(RequestError): guest.public_get('books', {'general': 'bad'})

    @patch('app.guest.transaction')
    def test_resolve_only_reads_public_rows_and_applies_policy(self, tx):
        cursor = tx.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{'id': 3, 'title': 'public', 'notes': 'private'}]
        data = guest.resolve_series({'series': [{'publisher': 'test', 'series_key': 'series', 'media_type': 'novel'}]})
        self.assertNotIn('notes', data['items'][0])
        sql, values = cursor.execute.call_args.args
        self.assertIn('LIMIT 201', sql)
        self.assertIn("restricted_18", sql)
        self.assertNotIn('wishlist_items', sql)
        self.assertEqual(values, ['test', 'series', 'novel', 0])


class QuietHandler(Handler):
    def log_message(self, *args): pass


class GuestHTTPTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.now = [0.0]
        self.stack.enter_context(patch('app.server.RESOURCES', ResourceGuard(clock=lambda:self.now[0])))
        self.stack.enter_context(patch('app.server.LIMITER', SlidingWindow(clock=lambda:self.now[0])))
        self.auth = self.stack.enter_context(patch('app.server.current_user', return_value=None))
        self.public = self.stack.enter_context(patch('app.guest.public_get', return_value={'items': []}))
        self.resolve = self.stack.enter_context(patch('app.guest.resolve_series', return_value={'items': []}))
        self.migrate = self.stack.enter_context(patch('app.guest_import.migrate', return_value={'summary': {}, 'applied': False}))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.stack.enter_context(patch('app.server.get_settings', return_value=SimpleNamespace(public_url=self.origin, cloud_mode=False)))
        self.thread = Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.cleanup)
        self.visitor, token = guest.issue(); self.cookie = guest.COOKIE+'='+token

    def cleanup(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.stack.close()

    def request(self, path='/api/guest/books', method='GET', raw=None, headers=None):
        h={'Cookie':self.cookie,'Origin':self.origin,'Content-Type':'application/json'}; h.update(headers or {})
        conn=HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        conn.request(method,path,body=json.dumps(raw) if raw is not None else None,headers=h)
        res=conn.getresponse(); status=res.status; hs=dict(res.getheaders()); data=json.loads(res.read())
        conn.close(); return status,hs,data

    def test_guests_have_independent_20_request_quota_without_account_lookup(self):
        for _ in range(20): self.assertEqual(self.request()[0],200)
        self.assertEqual(self.request()[0],429)
        _,token=guest.issue()
        self.assertEqual(self.request(headers={'Cookie':guest.COOKIE+'='+token})[0],200)
        self.auth.assert_not_called()
        self.now[0]=60; self.assertEqual(self.request()[0],200)

    def test_invalid_guest_and_query_do_not_touch_database(self):
        status,_,data=self.request(headers={'Cookie':guest.COOKIE+'=tampered'})
        self.assertEqual(status,401);self.assertTrue(data['guest_expired'])
        self.assertEqual(self.request('/api/guest/books?limit=201')[0],400)
        self.public.assert_not_called();self.auth.assert_not_called()

    def test_no_guest_cookie_can_write_private_or_admin_data(self):
        for path in ('/api/preferences','/api/wishlist/3','/api/collection/custom','/api/update','/api/guest-import/apply'):
            self.assertEqual(self.request(path,'POST',{})[0],401)
        self.assertEqual(self.request('/api/guest/wishlist/3','POST',{})[0],403)
        self.assertEqual(self.request('/api/collection/3','DELETE')[0],401)
        self.migrate.assert_not_called()

    def test_origin_and_issuance_quota_cannot_be_bypassed_with_fake_identity_or_forwarded_ip(self):
        self.assertEqual(self.request('/api/guest/start','POST',{}, {'Origin':'https://evil.invalid'})[0],403)
        for i in range(20):
            status, hs, _ = self.request('/api/guest/start','POST',{}, {'Cookie':'fake='+str(i),'X-Forwarded-For':'192.0.2.'+str(i)})
            self.assertEqual(status,200);self.assertIn('HttpOnly',hs['Set-Cookie'])
        self.assertEqual(self.request('/api/guest/start','POST',{}, {'Cookie':'fake=21'})[0],429)
        self.assertEqual(self.request()[0],200) # verified identity still independent

    def test_resolve_is_same_origin_and_read_only(self):
        self.assertEqual(self.request('/api/guest/resolve','POST',{}, {'Origin':'https://evil.invalid'})[0],403)
        self.resolve.assert_not_called()
        self.assertEqual(self.request('/api/guest/resolve','POST',{})[0],200)

    def test_oauth_peer_quota_cannot_be_reset_by_rotating_signed_guests(self):
        with patch.object(Handler,'_start_google_login',lambda handler:handler._json({'login':True})):
            for _ in range(20):
                _,token=guest.issue()
                self.assertEqual(self.request('/auth/google',headers={'Cookie':guest.COOKIE+'='+token})[0],200)
            self.assertEqual(self.request('/auth/google')[0],429)

    def test_import_uses_real_authenticated_owner_and_csrf(self):
        self.auth.return_value={'id':7,'role':'user','csrf_token':'csrf'}
        h={'Cookie':SESSION_COOKIE+'=real', 'X-CSRF-Token':'csrf'}
        self.assertEqual(self.request('/api/guest-import/preview','POST',{'data':{},'user_id':99},h)[0],200)
        self.assertEqual(self.migrate.call_args.args[0],7)
        self.assertEqual(self.request('/api/guest-import/apply','POST',{}, {**h,'X-CSRF-Token':'wrong'})[0],403)


class Cursor:
    def __init__(self, db): self.db=db;self.cursor=db.cursor()
    def __enter__(self):return self
    def __exit__(self,*args):self.cursor.close()
    def execute(self, sql, values=()):self.cursor.execute(sql.replace('%s','?').replace(' FOR UPDATE',''),values)
    def executemany(self,sql,values):self.cursor.executemany(sql.replace('%s','?').replace('INSERT IGNORE','INSERT OR IGNORE'),values)
    def fetchone(self):
        row=self.cursor.fetchone();return dict(row) if row else None
    def fetchall(self):return [dict(row) for row in self.cursor.fetchall()]


class ImportTransactionTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        CREATE TABLE users(id INTEGER PRIMARY KEY);
        CREATE TABLE publishers(id INTEGER PRIMARY KEY,code TEXT);
        CREATE TABLE books(id INTEGER PRIMARY KEY);
        CREATE TABLE wishlist_items(user_id INTEGER,book_id INTEGER,state TEXT,priority INTEGER,
          owned_format TEXT,purchased_at TEXT,paid_price INTEGER,notes TEXT,store_name TEXT,order_number TEXT,UNIQUE(user_id,book_id));
        CREATE TABLE collection_items(id INTEGER PRIMARY KEY,user_id INTEGER,book_id INTEGER,
          title TEXT CHECK(title<>'FAIL'),author TEXT,publisher_name TEXT,media_type TEXT,isbn TEXT,edition_type TEXT,release_date TEXT,
          owned_format TEXT,purchased_at TEXT,paid_price INTEGER,notes TEXT,store_name TEXT,order_number TEXT,UNIQUE(user_id,book_id));
        CREATE TABLE followed_series(user_id INTEGER,publisher_id INTEGER,normalized_series TEXT,media_type TEXT,series_title TEXT,follow_scope TEXT,
          UNIQUE(user_id,publisher_id,normalized_series,media_type));
        INSERT INTO users VALUES(7);INSERT INTO users VALUES(8);
        INSERT INTO publishers VALUES(1,'test');INSERT INTO books VALUES(1);INSERT INTO books VALUES(2);
        INSERT INTO wishlist_items(user_id,book_id,state,priority,notes) VALUES(7,1,'paused',3,'KEEP'),(8,2,'wanted',0,'OTHER');
        INSERT INTO collection_items(user_id,book_id,notes) VALUES(7,1,'OWN KEEP');
        ''')
        self.addCleanup(self.db.close)
        @contextmanager
        def transaction():
            try:
                yield SimpleNamespace(cursor=lambda:Cursor(self.db));self.db.commit()
            except Exception:
                self.db.rollback();raise
        self.patch=patch('app.guest_import.transaction',transaction);self.patch.start();self.addCleanup(self.patch.stop)

    def raw(self):
        return {'version':1,'wishlist':[{'book_id':1,'notes':'overwrite'},{'book_id':2,'notes':'new'}],
            'collection':[{'book_id':1,'notes':'overwrite'},{'book_id':2,'paid_price':123}],
            'custom':[{'title':'manual','notes':'private'}],
            'follows':[{'publisher':'test','series_key':'series','series_title':'series','media_type':'novel','scope':'all'}]}

    def test_preview_no_writes_apply_add_only_and_repeat_idempotent(self):
        raw=self.raw();preview=guest_import.migrate(7,raw)
        self.assertEqual(preview['summary']['new'],4)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wishlist_items').fetchone()[0],2)
        guest_import.migrate(7,raw,apply=True,token=preview['token'])
        self.assertEqual(self.db.execute('SELECT notes FROM wishlist_items WHERE user_id=7 AND book_id=1').fetchone()[0],'KEEP')
        self.assertEqual(self.db.execute('SELECT notes FROM collection_items WHERE user_id=7 AND book_id=1').fetchone()[0],'OWN KEEP')
        self.assertEqual(self.db.execute('SELECT notes FROM wishlist_items WHERE user_id=8 AND book_id=2').fetchone()[0],'OTHER')
        repeat=guest_import.migrate(7,raw)
        self.assertEqual(repeat['summary']['new'],0)
        guest_import.migrate(7,raw,apply=True,token=repeat['token'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM collection_items WHERE user_id=7').fetchone()[0],3)

    def test_bad_token_missing_books_and_user_scope(self):
        raw=self.raw();preview=guest_import.migrate(7,raw)
        with self.assertRaises(RequestError):guest_import.migrate(8,raw,apply=True,token=preview['token'])
        raw['collection']=[{'book_id':999}]
        self.assertEqual(guest_import.migrate(7,raw)['summary']['missing'],1)

    def test_mid_import_failure_rolls_back_all_new_rows(self):
        raw=self.raw();raw['custom']=[{'title':'manual'},{'title':'FAIL'}]
        preview=guest_import.migrate(7,raw)
        # INSERT IGNORE intentionally tolerates uniqueness, so inject an actual
        # driver failure rather than a SQLite CHECK ignored by OR IGNORE.
        original=Cursor.executemany
        def failing(cursor,sql,values):
            if 'title' in sql:raise RuntimeError('driver failed')
            return original(cursor,sql,values)
        with patch.object(Cursor,'executemany',failing),self.assertRaises(RuntimeError):
            guest_import.migrate(7,raw,apply=True,token=preview['token'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM wishlist_items WHERE user_id=7').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM collection_items WHERE user_id=7').fetchone()[0],1)

    def test_malformed_payload_is_rejected_before_transaction(self):
        for raw in ({'version':1,'wishlist':[{'book_id':1,'state':[]}]},
                    {'version':1,'wishlist':[{'book_id':True}]},
                    {'version':1,'follows':[{'publisher':'test','series_key':'x','series_title':'x','media_type':'novel','scope':{}}]},
                    {'version':1,'custom':[{'title':'x'}]*1001},
                    {'version':1,'collection':[{'book_id':1,'paid_price':{}}]}):
            with patch('app.guest_import.transaction') as tx,self.assertRaises(ValueError):guest_import.migrate(7,raw)
            tx.assert_not_called()


if __name__=='__main__':unittest.main()
