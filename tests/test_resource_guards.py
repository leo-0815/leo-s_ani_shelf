import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest.mock import patch

from app.abuse import RequestError
from app.auth import SESSION_COOKIE
from app.db import DatabaseUnavailable
from app.resource_guards import ResourceGuard, ReadDenied, protected_read, read_cost
from tests import test_guest as guest_tests


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.guard = ResourceGuard(clock=lambda:self.now[0], max_wait=0)

    def public(self, owner='a', path='books', query=None, loader=None):
        return self.guard.public(owner, path, query or {}, loader or (lambda:{'items':[{'id':1,'title':'book'}]}))

    def test_public_cache_is_shared_but_mutation_and_preferences_are_isolated(self):
        calls=[]
        def load(): calls.append(1); return {'items':[{'title':'general'}]}
        result=self.public(query={'general':'1'},loader=load)
        result['items'][0]['title']='mutated'
        self.assertEqual(self.public('b',query={'general':'1'},loader=load)['items'][0]['title'],'general')
        self.public('b',query={'general':'0'},loader=load)
        self.assertEqual(len(calls),2)
        self.assertEqual(self.guard.snapshot()['cache_hits'],1)

    def test_ttl_expiry_and_invalidation(self):
        calls=[]
        def load(): calls.append(1);return {'items':[]}
        self.public(loader=load);self.now[0]=19;self.public(loader=load)
        self.now[0]=20;self.public(loader=load)
        self.guard.invalidate();self.public(loader=load)
        self.assertEqual(len(calls),3)

    def test_inflight_invalidation_never_repopulates_old_data(self):
        self.public(loader=lambda:(self.guard.invalidate() or {'items':[]}))
        self.assertEqual(self.guard.snapshot()['cache_entries'],0)

    def test_cache_is_bounded_by_entries_and_bytes(self):
        self.guard=ResourceGuard(clock=lambda:0,cache_entries=2,cache_bytes=90,max_wait=0)
        for i in range(5):self.public(query={'q':str(i)},loader=lambda:{'items':['x'*20]})
        snapshot=self.guard.snapshot()
        self.assertLessEqual(snapshot['cache_entries'],2)
        self.assertLessEqual(snapshot['cache_bytes'],90)
        self.public(query={'q':'large'},loader=lambda:{'items':['x'*100]})
        self.assertLessEqual(self.guard.snapshot()['cache_bytes'],90)

    def test_cached_reads_still_consume_identity_cost_and_retry_does_not_extend(self):
        self.guard=ResourceGuard(clock=lambda:self.now[0],minute_budget=6,max_wait=0)
        self.public();self.public()
        with self.assertRaises(ReadDenied) as denied:self.public()
        self.assertEqual(denied.exception.status,429)
        self.assertEqual(denied.exception.retry_after,60)
        self.now[0]=59
        with self.assertRaises(ReadDenied) as denied:self.public()
        self.assertEqual(denied.exception.retry_after,1)
        self.now[0]=60;self.public()

    def test_long_window_cannot_be_reset_by_waiting_a_minute(self):
        self.guard=ResourceGuard(clock=lambda:self.now[0],hour_budget=6,max_wait=0)
        self.public();self.now[0]=65;self.public()
        self.now[0]=130
        with self.assertRaises(ReadDenied) as denied:self.public()
        self.assertEqual(denied.exception.retry_after,3470)
        self.now[0]=3600;self.public()

    def test_identity_capacity_does_not_evict_active_budgets(self):
        self.guard=ResourceGuard(clock=lambda:self.now[0],identity_capacity=2,max_wait=0)
        self.public('a');self.public('b')
        with self.assertRaises(ReadDenied):self.public('c')
        self.assertEqual(len(self.guard.identities),2)
        self.now[0]=3600;self.public('c')
        self.assertEqual(len(self.guard.identities),1)

    def test_global_miss_budget_covers_rotating_guests_but_cache_hits_survive(self):
        self.guard=ResourceGuard(clock=lambda:0,global_budget=3,max_wait=0)
        self.public('a')
        with self.assertRaises(ReadDenied):self.public('b',query={'q':'different'})
        self.public('b')
        self.assertEqual(self.guard.snapshot()['global_denied'],1)

    def test_singleflight_and_concurrency_bound_work_before_loader(self):
        entered,finish=Event(),Event()
        self.guard=ResourceGuard(concurrency=1,max_wait=0)
        def load():entered.set();self.assertTrue(finish.wait(3));return {'items':[]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            future=pool.submit(self.public,loader=load)
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ReadDenied):self.public('b')
            with self.assertRaises(ReadDenied):self.public('b',query={'q':'new'})
            self.assertEqual(self.guard.snapshot()['active_reads'],1)
            finish.set();future.result()
        self.assertEqual(self.guard.snapshot()['active_reads'],0)
        self.public('b',query={'q':'new'})

    def test_bounded_wait_allows_normal_parallel_page_reads(self):
        entered,waiting,finish=Event(),Event(),Event()
        self.guard=ResourceGuard(concurrency=1,max_wait=1,queue_capacity=1)
        def first():entered.set();self.assertTrue(finish.wait(2));return {'items':[]}
        def second():waiting.set();return {'items':[]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(self.public,loader=first);self.assertTrue(entered.wait(1))
            b=pool.submit(self.public,'b','books',{'q':'next'},second)
            finish.set();a.result();b.result()
        self.assertTrue(waiting.is_set());self.assertEqual(self.guard.snapshot()['queued_reads'],0)

    def test_100_simulated_callers_cannot_start_more_than_two_expensive_reads(self):
        entered, finish, lock = Event(), Event(), Lock()
        loads = [0]
        self.guard = ResourceGuard(concurrency=2, max_wait=0)
        def load():
            with lock:
                loads[0] += 1
                if loads[0] == 2:entered.set()
            self.assertTrue(finish.wait(5))
            return {'items':[]}
        def attempt(i):
            try:self.public('caller'+str(i),query={'q':str(i)},loader=load)
            except ReadDenied:return 'denied'
            return 'allowed'
        with ThreadPoolExecutor(max_workers=16) as pool:
            first=[pool.submit(attempt,i) for i in range(2)]
            self.assertTrue(entered.wait(2))
            denied=list(pool.map(attempt,range(2,102)))
            self.assertEqual(denied.count('denied'),100)
            self.assertEqual(loads[0],2)
            finish.set()
            self.assertEqual([f.result() for f in first],['allowed','allowed'])
        self.assertEqual(self.guard.snapshot()['active_reads'],0)

    def test_exceptions_release_slots_and_failures_are_not_cached(self):
        def fail():raise DatabaseUnavailable('test')
        for i in range(3):
            with self.assertRaises(DatabaseUnavailable):self.public(query={'q':str(i)},loader=fail)
        self.assertEqual(self.guard.snapshot()['active_reads'],0)
        self.assertEqual(self.guard.snapshot()['cache_entries'],0)
        with self.assertRaises(ReadDenied) as denied:self.public()
        self.assertEqual(denied.exception.retry_after,10)
        self.now[0]=10;self.public()

    def test_validation_errors_do_not_open_circuit(self):
        def fail():raise RequestError('bad',404)
        for i in range(3):
            with self.assertRaises(RequestError):self.public(query={'q':str(i)},loader=fail)
        self.assertEqual(self.guard.snapshot()['failed_reads'],0)
        self.public()

    def test_client_disconnects_do_not_open_database_circuit(self):
        for _ in range(3):
            with self.assertRaises(BrokenPipeError), self.guard.read('a','/api/books',{}):
                raise BrokenPipeError('client disconnected')
        self.assertEqual(self.guard.snapshot()['failed_reads'],0)
        self.assertEqual(self.guard.snapshot()['cooldown_seconds'],0)
        self.assertEqual(self.guard.snapshot()['active_reads'],0)

    def test_driver_errors_are_counted_without_importing_driver_or_exposing_sql(self):
        class OperationalError(Exception):__module__='pymysql.err'
        with self.assertRaises(OperationalError), self.guard.read('a','/api/books',{}):
            raise OperationalError('not returned in metrics')
        self.assertEqual(self.guard.snapshot()['failed_reads'],1)
        self.assertNotIn('not returned',str(self.guard.snapshot()))

    def test_three_slow_reads_open_short_circuit_without_background_work(self):
        def slow():self.now[0]+=5;return {'items':[]}
        for i in range(3):self.public(query={'q':str(i)},loader=slow)
        self.assertEqual(self.guard.snapshot()['cooldown_seconds'],10)
        with self.assertRaises(ReadDenied):self.public(query={'q':'miss'})
        self.public('b',query={'q':'2'}) # fresh PUBLIC cache still usable
        self.now[0]=25;self.public(query={'q':'miss'})

    def test_private_reads_never_enter_shared_cache(self):
        for owner in ('a','b'):
            with self.guard.read(owner,'/api/books',{}):pass
        self.assertEqual(self.guard.snapshot()['cache_entries'],0)

    def test_routes_and_costs_keep_writes_and_machine_sync_separate(self):
        for path in ('/api/books','/api/guest/book-batch','/api/guest/resolve','/api/home','/api/export.csv'):
            self.assertTrue(protected_read(path))
        for path in ('/api/auth/me','/api/notifications/test-email','/api/wishlist/1','/api/catalog-sync/books','/api/health','/api/resource-guards'):
            self.assertFalse(protected_read(path))
        self.assertGreater(read_cost('/api/books',{'limit':'200'}),read_cost('/api/books',{'limit':'20'}))
        self.assertGreater(read_cost('/api/books',{'offset':'5000'}),read_cost('/api/books',{}))


class ResourceHTTPTests(unittest.TestCase):
    setUp=guest_tests.GuestHTTPTests.setUp
    cleanup=guest_tests.GuestHTTPTests.cleanup
    request=guest_tests.GuestHTTPTests.request

    def test_guest_cost_denied_before_public_loader_or_auth_db(self):
        for i in range(12):self.assertEqual(self.request('/api/guest/books?limit=200&q='+str(i))[0],200)
        status,headers,data=self.request('/api/guest/books?limit=200&q=next')
        self.assertEqual(status,429);self.assertEqual(headers['Retry-After'],'60')
        self.assertEqual(data['rate_limit_bucket'],'read-budget')
        self.assertEqual(self.public.call_count,12);self.auth.assert_not_called()

    def test_cross_guest_cache_and_preference_separation(self):
        self.request('/api/guest/books?general=1')
        _,token=guest_tests.guest.issue();h={'Cookie':guest_tests.guest.COOKIE+'='+token}
        self.request('/api/guest/books?general=1',headers=h)
        self.assertEqual(self.public.call_count,1)
        self.request('/api/guest/books?general=0',headers=h)
        self.assertEqual(self.public.call_count,2)

    def test_busy_read_still_allows_private_write_and_admin_only_metrics(self):
        from app import server
        user={'id':7,'role':'user','general_audience':True,'csrf_token':'csrf'}
        self.auth.return_value=user
        h={'Cookie':SESSION_COOKIE+'=real','X-CSRF-Token':'csrf'}
        guard=ResourceGuard(clock=lambda:0,global_budget=0,max_wait=0)
        with patch('app.server.RESOURCES',guard),patch('app.server.set_preferences',return_value={'ok':True}) as save:
            self.assertEqual(self.request('/api/books',headers=h)[0],503)
            self.assertEqual(self.request('/api/preferences','POST',{'general_audience':True},h)[0],200)
            save.assert_called_once()
            self.assertEqual(self.request('/api/resource-guards',headers=h)[0],403)
            self.auth.return_value={**user,'role':'admin'}
            status,_,data=self.request('/api/resource-guards',headers=h)
            self.assertEqual(status,200);self.assertEqual(data['global_denied'],1)
            self.assertNotIn('identities',data);self.assertNotIn('cache',data)

    def test_authenticated_notes_are_never_shared_across_accounts(self):
        self.auth.return_value={'id':7,'role':'user','general_audience':True}
        h={'Cookie':SESSION_COOKIE+'=real'}
        with patch('app.server.list_books',side_effect=lambda filters,owner,*args:{'items':[{'wishlist_notes':str(owner)}],'total':1}) as load:
            self.assertEqual(self.request('/api/books',headers=h)[2]['items'][0]['wishlist_notes'],'7')
            self.auth.return_value={'id':8,'role':'user','general_audience':True}
            self.assertEqual(self.request('/api/books',headers=h)[2]['items'][0]['wishlist_notes'],'8')
            self.assertEqual(load.call_count,2)

    def test_unauthorized_admin_reads_do_not_consume_global_read_budget(self):
        self.auth.return_value={'id':7,'role':'user','general_audience':True}
        guard=ResourceGuard(clock=lambda:0,max_wait=0)
        with patch('app.server.RESOURCES',guard):
            for path in ('/api/quality','/api/release-checks','/api/export.json'):
                self.assertEqual(self.request(path,headers={'Cookie':SESSION_COOKIE+'=real'})[0],403)
            self.assertEqual(guard.snapshot()['global_cost_used'],0)


if __name__=='__main__':unittest.main()
