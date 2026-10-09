"""Bounded process-local read budgets, public-only cache and load shedding.

No database counters, background polling, fingerprinting or human/bot claims.
"""
from __future__ import annotations

import json
import os
import re
from collections import OrderedDict, deque
from contextlib import contextmanager
from hashlib import sha256
from math import ceil
from threading import Condition, Lock
from time import monotonic

from .abuse import RequestError
from .db import DatabaseUnavailable


class ReadDenied(RequestError):
    def __init__(self, message, status=503, retry_after=2):
        super().__init__(message, status)
        self.retry_after = max(1, int(retry_after))


def _database_failure(exc):
    # Client disconnects / broken response pipes must not open the DB circuit.
    return isinstance(exc, DatabaseUnavailable) or type(exc).__module__.startswith('pymysql.')


def protected_read(path):
    path = path.replace('/api/guest/', '/api/', 1)
    return bool(re.fullmatch(r'/api/(?:books(?:/[0-9]+(?:/recommendations)?)?|series(?:/detail)?|'
                             r'book-batch|resolve|upcoming|recommendations|home|stats|publishers|'
                             r'quality|release-checks|export\.(?:json|csv)|calendar\.ics)', path))


def read_cost(path, query, payload=None):
    """Conservative cost estimate; not an exact SQL billing meter."""
    path = path.replace('/api/guest/', '/api/', 1)
    if path == '/api/resolve':
        rows = (payload or {}).get('series', [])
        return 5 + ceil(min(len(rows), 20) / 5) if isinstance(rows, list) else 9
    if path == '/api/book-batch':
        return 1 + ceil(min(len(query.get('ids', '').split(',')), 100) / 50)
    if path in {'/api/quality', '/api/release-checks'} or path.startswith('/api/export.') or path == '/api/calendar.ics':
        return 8 if path in {'/api/quality', '/api/release-checks'} else 20
    if path.endswith('/recommendations') or path == '/api/recommendations':
        return 5
    if path == '/api/home':
        return 5
    if path == '/api/stats':
        return 3
    if path == '/api/publishers':
        return 2
    if path == '/api/series/detail':
        return 8 + min(int(query.get('offset', 0)) // 5000, 10)
    if path in {'/api/books', '/api/series', '/api/upcoming'}:
        default = 200 if path == '/api/upcoming' else 60 if path == '/api/series' else 100
        return 1 + ceil(int(query.get('limit', default)) / 50) + min(int(query.get('offset', 0)) // 5000, 10)
    return 1


class _Window:
    """Aggregated buckets bound memory; expiry is deliberately conservative."""
    def __init__(self, seconds, width):
        self.seconds, self.width = seconds, width
        self.rows = deque()
        self.used = 0

    def purge(self, now):
        while self.rows and self.rows[0][0] + self.seconds <= now:
            self.used -= self.rows.popleft()[1]

    def retry(self, now, cost, limit):
        self.purge(now)
        remaining = self.used
        if remaining + cost <= limit:
            return 0
        for start, amount in self.rows:
            remaining -= amount
            if remaining + cost <= limit:
                return max(1, ceil(start + self.seconds - now))
        return self.seconds

    def add(self, now, cost):
        if self.rows and int(self.rows[-1][0] // self.width) == int(now // self.width):
            self.rows[-1] = (now, self.rows[-1][1] + cost)
        else:
            self.rows.append((now, cost))
        self.used += cost


class ResourceGuard:
    def __init__(self, clock=monotonic, minute_budget=60, hour_budget=1200,
                 global_budget=400, concurrency=2, cache_ttl=20,
                 cache_entries=128, cache_bytes=8 * 1024 * 1024, identity_capacity=512,
                 max_wait=2, queue_capacity=4):
        self.clock, self.minute_budget, self.hour_budget = clock, minute_budget, hour_budget
        self.global_budget, self.concurrency, self.cache_ttl = global_budget, concurrency, cache_ttl
        self.cache_entries, self.cache_max_bytes, self.identity_capacity = cache_entries, cache_bytes, identity_capacity
        self.lock = Lock()
        self.condition = Condition(self.lock)
        self.max_wait, self.queue_capacity, self.waiting = max_wait, queue_capacity, 0
        self.identities = {}
        self.global_window = _Window(60, 5)
        self.cache = OrderedDict()
        self.cache_bytes = self.active = self.generation = 0
        self.inflight = set()
        self.bad_reads = deque()
        self.cooldown_until = 0
        self.started = clock()
        self.counts = {key: 0 for key in ('cache_hits', 'cache_misses', 'budget_denied',
                      'capacity_denied', 'global_denied', 'circuit_denied', 'coalesced', 'slow_reads', 'failed_reads')}

    def _charge(self, owner, cost, now):
        # Call only while holding self.lock. Never retain cookies or query text.
        self.identities = {key: pair for key, pair in self.identities.items()
                           if pair[1].rows and pair[1].rows[-1][0] + 3600 > now}
        key = sha256(owner.encode()).hexdigest()
        if key not in self.identities:
            if len(self.identities) >= self.identity_capacity:
                self.counts['capacity_denied'] += 1
                raise ReadDenied('查詢保護容量暫時已滿，請稍後再試', retry_after=5)
            self.identities[key] = (_Window(60, 5), _Window(3600, 60))
        minute, hour = self.identities[key]
        retry = max(minute.retry(now, cost, self.minute_budget), hour.retry(now, cost, self.hour_budget))
        if retry:
            self.counts['budget_denied'] += 1
            raise ReadDenied('大量查詢已達暫時額度，請稍後繼續；藏書資料不受影響', 429, retry)
        minute.add(now, cost)
        hour.add(now, cost)

    def _reserve(self, now, cost):
        # A small bounded wait absorbs normal pages issuing 2-3 parallel reads.
        # Do not grow an unbounded queue of threads during a flood.
        if self.active >= self.concurrency and self.waiting < self.queue_capacity and self.max_wait > 0:
            self.waiting += 1
            deadline = monotonic() + self.max_wait
            try:
                while self.active >= self.concurrency and now >= self.cooldown_until:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    self.condition.wait(remaining)
                    now = self.clock()
            finally:
                self.waiting -= 1
        if now < self.cooldown_until:
            self.counts['circuit_denied'] += 1
            raise ReadDenied('查詢服務正在短暫保護中，請稍後再試', retry_after=ceil(self.cooldown_until - now))
        if self.active >= self.concurrency:
            self.counts['capacity_denied'] += 1
            raise ReadDenied('目前查詢較多，請稍後再試', retry_after=2)
        retry = self.global_window.retry(now, cost, self.global_budget)
        if retry:
            self.counts['global_denied'] += 1
            raise ReadDenied('目前公共與書架查詢量較大，請稍後再試', retry_after=retry)
        self.global_window.add(now, cost)
        self.active += 1

    def _finish(self, started, failed):
        now = self.clock()
        with self.lock:
            self.active -= 1
            self.condition.notify_all()
            slow = now - started >= 5
            self.counts['slow_reads'] += int(slow)
            self.counts['failed_reads'] += int(failed)
            while self.bad_reads and self.bad_reads[0] <= now - 30:
                self.bad_reads.popleft()
            if slow or failed:
                self.bad_reads.append(now)
                if len(self.bad_reads) >= 3:
                    self.cooldown_until = now + 10
                    self.bad_reads.clear()

    @contextmanager
    def read(self, owner, path, query, payload=None):
        cost = read_cost(path, query, payload)
        started = self.clock()
        with self.lock:
            self._charge(owner, cost, started)
            self._reserve(started, cost)
        failed = False
        try:
            yield
        except Exception as exc:
            failed = _database_failure(exc)
            raise
        finally:
            self._finish(started, failed)

    def public(self, owner, path, query, loader):
        # Only guest.public_get's whitelisted PUBLIC response may enter this cache.
        key = sha256(json.dumps([path, sorted(query.items())], ensure_ascii=False).encode()).hexdigest()
        cost, started = read_cost('/api/' + path, query), self.clock()
        with self.lock:
            self._charge(owner, cost, started)
            for expired in [k for k, row in self.cache.items() if row[0] <= started]:
                self.cache_bytes -= len(self.cache.pop(expired)[1])
            cached = self.cache.get(key)
            if cached:
                self.cache.move_to_end(key)
                self.counts['cache_hits'] += 1
                return json.loads(cached[1])
            if key in self.inflight:
                self.counts['coalesced'] += 1
                raise ReadDenied('相同查詢正在處理，請稍後再試', retry_after=2)
            self.inflight.add(key)
            try:
                self._reserve(started, cost)
            except BaseException:
                self.inflight.discard(key)
                raise
            generation = self.generation
            self.counts['cache_misses'] += 1
        failed = False
        try:
            data = loader()
            encoded = json.dumps(data, ensure_ascii=False, default=str).encode()
            with self.lock:
                if generation == self.generation and len(encoded) <= min(512 * 1024, self.cache_max_bytes):
                    while self.cache and (len(self.cache) >= self.cache_entries or self.cache_bytes + len(encoded) > self.cache_max_bytes):
                        _, old = self.cache.popitem(last=False)
                        self.cache_bytes -= len(old[1])
                    self.cache[key] = (self.clock() + self.cache_ttl, encoded)
                    self.cache_bytes += len(encoded)
            return data
        except Exception as exc:
            failed = _database_failure(exc)
            raise
        finally:
            with self.lock:
                self.inflight.discard(key)
            self._finish(started, failed)

    def invalidate(self):
        with self.lock:
            self.generation += 1
            self.cache.clear()
            self.cache_bytes = 0

    def snapshot(self):
        with self.lock:
            now = self.clock()
            self.global_window.purge(now)
            return {**self.counts, 'uptime_seconds': int(now - self.started),
                    'active_reads': self.active, 'concurrency_limit': self.concurrency,
                    'queued_reads': self.waiting, 'queue_limit': self.queue_capacity,
                    'cache_entries': len(self.cache), 'cache_bytes': self.cache_bytes,
                    'cache_ttl_seconds': self.cache_ttl, 'minute_budget': self.minute_budget,
                    'hour_budget': self.hour_budget, 'global_budget': self.global_budget,
                    'global_cost_used': self.global_window.used,
                    'cooldown_seconds': max(0, ceil(self.cooldown_until - now))}


def _setting(name, default, low, high):
    try:
        return min(high, max(low, int(os.getenv(name, default))))
    except ValueError:
        return default


RESOURCES = ResourceGuard(
    minute_budget=_setting('ANISHELF_READ_MINUTE_BUDGET', 60, 30, 600),
    hour_budget=_setting('ANISHELF_READ_HOUR_BUDGET', 1200, 300, 12000),
    global_budget=_setting('ANISHELF_READ_GLOBAL_BUDGET', 400, 100, 4000),
    concurrency=_setting('ANISHELF_READ_CONCURRENCY', 2, 1, 4),
    cache_ttl=_setting('ANISHELF_PUBLIC_CACHE_SECONDS', 20, 1, 60),
)
