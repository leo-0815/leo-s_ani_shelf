from __future__ import annotations

import html
import re
import gzip
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from html.parser import HTMLParser
from typing import Iterable


USER_AGENT = "AniShelf/0.1 (personal release catalog; respectful low-rate fetcher)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_RESPONSE_BYTES = 6 * 1024 * 1024
RETRYABLE_HTTP_STATUS = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
)
RETRY_DELAYS = (2.0, 5.0, 10.0, 20.0)
CHALLENGE_RE = re.compile(
    r"<title\b[^>]*>\s*(?:just a moment|attention required|access denied|請稍候)"
    r"|/cdn-cgi/challenge-platform|\bcf-chl-\b",
    re.I,
)


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


def _is_cf_challenge(status: int | None, headers: dict | None, body: bytes) -> bool:
    if headers:
        for k, v in headers.items():
            if k.lower() == "cf-mitigated" and "challenge" in str(v).lower():
                return True
    if status in {403, 503} and body:
        if CHALLENGE_RE.search(body[:4096].decode("utf-8", errors="ignore")):
            return True
    return False


def _fetch_via_browser_fallback(url: str, timeout: int) -> tuple[bytes, str] | None:
    """遭遇 Cloudflare 阻擋時，先模擬瀏覽器 TLS 指紋，再切換為隱藏自動化瀏覽器；無法繞過則回傳 None"""
    # 1. 嘗試 curl_cffi 模擬 Chrome 瀏覽器 TLS 指紋
    try:
        from curl_cffi import requests as cffi_requests  # type: ignore

        resp = cffi_requests.get(
            url,
            impersonate="chrome",
            headers={"Accept-Language": "zh-TW,zh;q=0.9"},
            timeout=timeout,
        )
        raw = resp.content
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("來源頁面解壓後超過 6 MB 安全上限")
        if resp.status_code == 200 and not _is_cf_challenge(resp.status_code, dict(resp.headers), raw):
            return raw, resp.encoding or "utf-8"
    except ValueError:
        raise
    except Exception:
        pass

    # 2. 若仍被 JS Challenge 阻擋，切換為隱藏無頭瀏覽器（Playwright）執行渲染
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            try:
                context = browser.new_context(user_agent=BROWSER_UA, locale="zh-TW")
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    html_text = page.content()
                    if not CHALLENGE_RE.search(html_text):
                        raw = html_text.encode("utf-8")
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise ValueError("來源頁面解壓後超過 6 MB 安全上限")
                        return raw, "utf-8"
                    page.wait_for_timeout(1000)
            finally:
                browser.close()
    except ValueError:
        raise
    except Exception:
        pass

    return None


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
                if _is_cf_challenge(response.status, dict(response.headers), payload):
                    fallback = _fetch_via_browser_fallback(url, timeout)
                    if fallback is not None:
                        return fallback
                    raise RuntimeError(f"HTTP {response.status} (Cloudflare challenge): {url}")
                return payload, response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            last_error = exc
            err_body = exc.read(4096) if exc.fp else b""
            if _is_cf_challenge(exc.code, dict(exc.headers), err_body) or exc.code == 403:
                fallback = _fetch_via_browser_fallback(url, timeout)
                if fallback is not None:
                    return fallback
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt + 1 >= attempts:
                raise RuntimeError(f"HTTP {exc.code}: {url}") from exc
            retry_after = exc.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after and retry_after.isdigit()
                else RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            )
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                raise RuntimeError(f"無法連線到 {url}: {exc.reason}") from exc
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
        time.sleep(delay + random.uniform(0.1, 0.4))
    raise RuntimeError(f"無法連線到 {url}: {last_error}")

# USER_AGENT = "AniShelf/0.1 (personal release catalog; respectful low-rate fetcher)"
# MAX_RESPONSE_BYTES = 6 * 1024 * 1024
# RETRYABLE_HTTP_STATUS = frozenset(
#     {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
# )
# RETRY_DELAYS = (2.0, 5.0, 10.0, 20.0)


# def _normalized_url(url: str) -> str:
#     parsed = urllib.parse.urlsplit(url)
#     return urllib.parse.urlunsplit(
#         (
#             parsed.scheme,
#             parsed.netloc.encode("idna").decode("ascii"),
#             urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/%:@"),
#             urllib.parse.quote(urllib.parse.unquote(parsed.query), safe="=&%:@/?+"),
#             "",
#         )
#     )


# def _fetch_payload(
#     url: str,
#     timeout: int = 20,
#     attempts: int = 3,
#     accept: str = "*/*",
# ) -> tuple[bytes, str]:
#     url = _normalized_url(url)
#     request = urllib.request.Request(
#         url,
#         headers={
#             "User-Agent": USER_AGENT,
#             "Accept": accept,
#             "Accept-Language": "zh-TW,zh;q=0.9",
#             "Accept-Encoding": "gzip",
#         },
#     )
#     last_error: Exception | None = None
#     for attempt in range(max(1, attempts)):
#         try:
#             with urllib.request.urlopen(request, timeout=timeout) as response:
#                 payload = response.read(MAX_RESPONSE_BYTES + 1)
#                 if response.headers.get("Content-Encoding", "").lower() == "gzip":
#                     payload = gzip.decompress(payload)
#                 if len(payload) > MAX_RESPONSE_BYTES:
#                     raise ValueError("來源頁面解壓後超過 6 MB 安全上限")
#                 return payload, response.headers.get_content_charset() or "utf-8"
#         except urllib.error.HTTPError as exc:
#             last_error = exc
#             if exc.code not in RETRYABLE_HTTP_STATUS or attempt + 1 >= attempts:
#                 raise RuntimeError(f"HTTP {exc.code}: {url}") from exc
#             retry_after = exc.headers.get("Retry-After")
#             delay = (
#                 float(retry_after)
#                 if retry_after and retry_after.isdigit()
#                 else RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
#             )
#         except urllib.error.URLError as exc:
#             last_error = exc
#             if attempt + 1 >= attempts:
#                 raise RuntimeError(f"無法連線到 {url}: {exc.reason}") from exc
#             delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
#         time.sleep(delay + random.uniform(0.1, 0.4))
#     raise RuntimeError(f"無法連線到 {url}: {last_error}")


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


def product_failure(source, url: str, error: Exception, *, seed=None, hint=None) -> None:
    """Optional scheduled-run hook; historical/local parsers need no queue DB."""
    recorder = getattr(source, "record_product_failure", None)
    if recorder:
        recorder(url, str(error), seed, hint)


def product_success(source, url: str) -> None:
    successes = getattr(source, "product_fetch_successes", None)
    if successes is None:
        source.product_fetch_successes = successes = set()
    successes.add(url)


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
