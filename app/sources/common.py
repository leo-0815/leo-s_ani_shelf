from __future__ import annotations

import html
import gzip
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import os
from datetime import date
from html.parser import HTMLParser
from typing import Iterable


USER_AGENT = "AniShelf/0.1 (personal release catalog; respectful low-rate fetcher)"
MAX_RESPONSE_BYTES = 6 * 1024 * 1024


def _normalized_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc.encode("idna").decode("ascii"),
            urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/%:@"),
            urllib.parse.quote(urllib.parse.unquote(parsed.query), safe="=&%:@/?+"),
            "",
        )
    )


def _fetch_payload(
    url: str,
    timeout: int = 20,
    attempts: int = 3,
    accept: str = "*/*",
) -> tuple[bytes, str]:
    url = _normalized_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "zh-TW,zh;q=0.9",
            "Accept-Encoding": "gzip",
        },
    )
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    payload = gzip.decompress(payload)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("來源頁面解壓後超過 6 MB 安全上限")
                return payload, response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {408, 425, 429, 500, 502, 503, 504} or attempt + 1 >= attempts:
                raise RuntimeError(f"HTTP {exc.code}: {url}") from exc
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                raise RuntimeError(f"無法連線到 {url}: {exc.reason}") from exc
            delay = 2 ** attempt
        time.sleep(delay + random.uniform(0.1, 0.4))
    raise RuntimeError(f"無法連線到 {url}: {last_error}")


def fetch_bytes(
    url: str,
    timeout: int = 20,
    attempts: int = 3,
    accept: str = "*/*",
) -> bytes:
    payload, _charset = _fetch_payload(url, timeout, attempts, accept)
    return payload


def fetch_html(url: str, timeout: int = 20, attempts: int = 3) -> str:
    payload, charset = _fetch_payload(
        url,
        timeout=timeout,
        attempts=attempts,
        accept="text/html,application/xhtml+xml",
    )
    return payload.decode(charset, errors="replace")


def polite_pause(seconds: float = 0.6) -> None:
    time.sleep(seconds)


class RateLimiter:
    """Share a conservative request start-rate across a small worker pool."""

    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1.0 / max(requests_per_second, 0.1)
        self.lock = threading.Lock()
        self.next_start = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_start - now)
            self.next_start = max(now, self.next_start) + self.interval
        if delay:
            time.sleep(delay)


class PageParser(HTMLParser):
    BLOCK_TAGS = {"p", "div", "li", "tr", "td", "th", "br", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.meta: dict[str, str] = {}
        self._skip_depth = 0
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style", "svg"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            self._anchor_href = attributes.get("href")
            self._anchor_parts = []
        if tag == "meta":
            key = attributes.get("property") or attributes.get("name")
            content = attributes.get("content")
            if key and content:
                self.meta[key.lower()] = content

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a" and self._anchor_href:
            text = " ".join("".join(self._anchor_parts).split())
            self.links.append((self._anchor_href, text))
            self._anchor_href = None
            self._anchor_parts = []
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        value = html.unescape(data)
        self.parts.append(value)
        if self._anchor_href is not None:
            self._anchor_parts.append(value)

    @property
    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)

    @property
    def flat_text(self) -> str:
        return " ".join(self.text.split())


def parse_page(markup: str) -> PageParser:
    parser = PageParser()
    parser.feed(markup)
    parser.close()
    return parser


class TableParser(HTMLParser):
    """Small dependency-free HTML table reader used by publisher schedules."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None
            self._cell = None

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and self._cell is not None:
            self._cell.append(html.unescape(data))


def parse_tables(markup: str) -> list[list[str]]:
    parser = TableParser()
    parser.feed(markup)
    parser.close()
    return parser.rows


def unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def one_year_cutoff(today: date | None = None) -> date:
    """Return the same calendar day one year earlier, including leap-day handling."""
    today = today or date.today()
    try:
        return today.replace(year=today.year - 1)
    except ValueError:
        return today.replace(year=today.year - 1, day=28)


def history_cutoff(today: date | None = None) -> date:
    """Use a calendar-year window when the local history runner selects one."""
    raw = os.getenv("ANISHELF_HISTORY_YEAR", "").strip()
    if raw.isdigit() and 1990 <= int(raw) <= 2100:
        return date(int(raw), 1, 1)
    return one_year_cutoff(today)


def history_segment(prefix: str) -> str:
    raw = os.getenv("ANISHELF_HISTORY_YEAR", "").strip()
    return f"history_v2_{raw}_{prefix}" if raw.isdigit() else f"one_year_v1_{prefix}"
