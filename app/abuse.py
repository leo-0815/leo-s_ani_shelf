"""Process-local, bounded guards. No database or external cache per request."""
from __future__ import annotations

from collections import deque
from hashlib import sha256
from math import ceil
from threading import Lock
from time import monotonic
from urllib.parse import parse_qs


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class SlidingWindow:
    def __init__(self, clock=monotonic, capacity: int = 4096):
        self.clock = clock
        self.capacity = capacity
        self.entries: dict[str, deque[float]] = {}
        self.lock = Lock()
        self.last_cleanup = float("-inf")

    def check(self, key: str, limit: int = 20, seconds: int = 60) -> int:
        """Return zero when allowed, otherwise seconds until retry; atomic."""
        now = self.clock()
        with self.lock:
            if now - self.last_cleanup >= 1:
                # All windows are <= 60 seconds. Never evict active keys to admit
                # attacker-controlled identities (that would reset their quota).
                self.entries = {k: v for k, v in self.entries.items() if v and v[-1] > now - 60}
                self.last_cleanup = now
            queue = self.entries.get(key)
            if queue is None:
                if len(self.entries) >= self.capacity:
                    return 60
                queue = self.entries[key] = deque()
            while queue and queue[0] <= now - seconds:
                queue.popleft()
            if len(queue) >= limit:
                return max(1, ceil(queue[0] + seconds - now))
            queue.append(now)
            return 0


LIMITER = SlidingWindow()


def category(path: str, method: str) -> str:
    if path.startswith("/auth/") or path.startswith("/api/auth/"):
        return "auth"
    if path.startswith("/api/notifications/test-"):
        return "notification-test"
    if path == "/api/update":
        return "update"
    if path.startswith("/api/export.") or path == "/api/calendar.ics":
        return "export"
    if method != "GET":
        return "write"
    if "recommendations" in path:
        return "recommendations"
    if path.startswith("/api/books") or path.startswith("/api/series") or path == "/api/upcoming":
        return "browse"
    return "metadata"


def session_identity(token: str, peer: str) -> str:
    # No raw credentials or cookies retained in limiter state; forwarding headers
    # are deliberately ignored (they must not provide a spoofable quota bypass).
    return "session:" + sha256(token.encode()).hexdigest() if token else "peer:" + peer


def validate_query(parsed) -> None:
    if len(parsed.geturl()) > 8192:
        raise RequestError("網址過長", 414)
    for part in parsed.path.split("/"):
        if part.isdecimal() and (not part.isascii() or len(part) > 18):
            raise RequestError("無效的書目識別碼")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=32)
    except ValueError:
        raise RequestError("查詢參數過多") from None
    if any(len(values) != 1 for values in query.values()):
        raise RequestError("查詢參數不可重複")
    for key, values in query.items():
        maximum = 200 if key == "q" else 300 if key == "title" else 1000
        if len(key) > 64 or len(values[0]) > maximum:
            raise RequestError("查詢文字過長")
    if parsed.path.startswith("/auth/") or parsed.path.startswith("/api/catalog-sync/"):
        return  # OAuth token lengths and existing machine pagination are distinct.
    for key, maximum in (("limit", 200), ("offset", 1_000_000), ("days", 366)):
        if key in query:
            value = query[key][0]
            minimum = 0 if key == "offset" else 1
            if not value.isascii() or not value.isdigit() or not minimum <= int(value) <= maximum:
                raise RequestError(f"{key} 超出允許範圍")
    sorts = {"", "release_asc", "title_asc", "added_desc", "updated_desc", "purchased_desc"}
    if "sort" in query and query["sort"][0] not in sorts:
        raise RequestError("不支援的排序")


def csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
