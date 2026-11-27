"""Browser-first release preview on synthetic loopback pages, with no storage."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread

from app.chingwin_coverage import normalize_isbn13
from app.release_dates import parse_product
from app.sources.chingwin import ChingWinSource
from app.sources.product_rating import rating_fields
from app.sources.common import parse_page

SKU = "SIM108"
BOOK = "測試作品(108)"
WRITER = "測試作家"
ISBN = "9786260303778"
SKU_URL = f"https://www.ching-win.com.tw/product-detail/{SKU}"  # Identifier only; never fetched.


def synthetic_page(visible, metadata=""):
    return ('<!doctype html><meta charset="UTF-8">' + metadata
            + '<main id="view"></main><script>document.getElementById("view").innerHTML = '
            + json.dumps(visible, ensure_ascii=True) + ';</script>')


@contextmanager
def local_fixture():
    routes = {
        "/news": (200, synthetic_page('<a href="/schedule">青文出版社 2026年12月預定出書表</a>')),
        "/schedule": (200, synthetic_page(
            f'<p>類別：漫畫 書名：{BOOK} 作者：{WRITER} 書系：少年 定價：120 首刷附錄：無 電子書：無</p>'
            f'<a href="/product">商品資料</a><p>會員服務</p>',
            '<meta property="og:title" content="2026年12月預定出書表">',
        )),
        "/product": (200, synthetic_page(
            f"<h1>{BOOK}</h1><p>產品編號：{SKU}</p><p>作家：{WRITER}</p><p>繪者：－</p>"
            f"<p>級別：普</p><p>ISBN：{ISBN}</p><p>出版日期：2026-12-25</p>"
            "<p>定價：120</p><p>預購到貨：2027-01-25</p>",
            '<meta name="Author" content="這是出版社，不是作家">',
        )),
        "/blocked": (403, "<!doctype html><title>Blocked fixture</title><h1>存取未開放</h1>"),
    }
    hits = Counter()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in routes:
                self.send_error(404)
                return
            hits[self.path] += 1
            status, markup = routes[self.path]
            data = markup.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", hits
        finally:
            server.shutdown()
            thread.join()


def chrome_path():
    for name in ("google-chrome", "chromium", "chrome"):
        if executable := shutil.which(name):
            return executable
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if root := os.environ.get(key):
            candidate = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if candidate.is_file():
                return str(candidate)
    raise RuntimeError("Chrome is unavailable on this machine")


def browser_snapshot(chrome, origin, path):
    if path not in {"/news", "/schedule", "/product", "/blocked"} or not re.fullmatch(
        r"http://127\.0\.0\.1:\d+", origin
    ):
        raise ValueError("Only the local test fixture is allowed")
    with TemporaryDirectory(prefix="anishelf-browser-first-") as profile:
        return subprocess.run(
            [chrome, "--headless", "--disable-background-networking", "--no-first-run",
             "--no-default-browser-check", f"--user-data-dir={profile}", "--dump-dom", origin + path],
            text=True, encoding="utf-8", capture_output=True, check=True, timeout=25,
        ).stdout


def verify(ok, message):
    if not ok:
        raise AssertionError(message)


def preview(fetch, entry="/news"):
    news = parse_page(fetch(entry))
    if ("/schedule", "青文出版社 2026年12月預定出書表") not in news.links:
        return None
    schedule_html = fetch("/schedule")
    if ("/product", "商品資料") not in parse_page(schedule_html).links:
        return None
    source = ChingWinSource()
    planned, = source._parse_article("https://www.ching-win.com.tw/about-news-detail/212", schedule_html)
    product_html = fetch("/product")
    text = parse_page(product_html).flat_text
    block = re.search(rf"產品編號：{SKU}\b(.+?)定價", text)
    if not block:
        return None
    writer = re.search(r"作家：\s*(.*?)\s+繪者：", block[1])
    code = re.search(r"\bISBN：\s*([0-9-]+)", block[1])
    if not writer or not code or not (isbn := normalize_isbn13(code[1])):
        return None
    seed = replace(planned, author=writer[1], isbn=isbn, source_url=SKU_URL, source_key=SKU)
    confirmed = parse_product(source, asdict(seed), product_html)
    grade = rating_fields("chingwin", SKU_URL, product_html, SKU, isbn)
    return {"author": confirmed.author, "isbn": confirmed.isbn,
            "planned_month": str(planned.release_date)[:7], "confirmed_day": str(confirmed.release_date),
            "grade": grade["content_rating"]}


def main():
    chrome = chrome_path()
    with local_fixture() as (origin, hits):
        fetch = lambda path: browser_snapshot(chrome, origin, path)
        result = preview(fetch)
        verify(result == {"author": WRITER, "isbn": ISBN, "planned_month": "2026-12",
                          "confirmed_day": "2026-12-25", "grade": "general"}, "Unexpected preview result")
        verify(hits == {"/news": 1, "/schedule": 1, "/product": 1}, "Expected only three fixture requests")
        print("## Browser-first test: localhost only")
        print("- Announcement → schedule → product: passed; read the visible writer, not the publisher meta tag.")
        print("- ISBN and explicit grade verified; publication date remained separate from preorder arrival.")
        before = hits.copy()
        verify(preview(fetch, entry="/blocked") is None, "Blocked response produced data")
        verify(hits == before + Counter({"/blocked": 1}), "Requests continued after the blocked entry")
        print("- Simulated blocked entry: stopped after one request; no candidates produced.")
        print("- No production domains, database, cookies, notifications or live update jobs used.")


if __name__ == "__main__":
    main()
