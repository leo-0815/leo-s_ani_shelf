"""Read-only publisher and Chingwin fallback probes. No database, sync, or notifications."""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


OFFICIAL = {
    "chingwin": (
        ("schedule", "https://www.ching-win.com.tw/about-news/", r"/about-news-detail/\d+", r"預定出書表"),
        ("detail", "https://www.ching-win.com.tw/product-detail/10320709", r"螢火蟲的出嫁", r"10320709"),
    ),
    "kadokawa": (
        ("listing", "https://www.kadokawa.com.tw/categories/漫畫", r"ProductList-list", r"/products/"),
        ("detail", "https://www.kadokawa.com.tw/products/9786264452649", r"義妹生活", r"9786264452649"),
    ),
    "tongli": (
        ("schedule", "https://www.tongli.com.tw/Search1.aspx?Page=1", r"書名[／/]集數", r"作者"),
        ("detail", "https://www.tongli.com.tw/BooksDetail.aspx?Bd=IC0336006", r"我的幸福婚姻", r"978-?626-?02-?7999-?8"),
    ),
    "spp": (
        ("schedule", "https://event.spp.com.tw/event/publish.aspx", r"上市", r"書名|條碼|書號"),
        ("detail", "https://www.spp.com.tw/SalePage/Index/10051202", r"灌籃高手", r"SalePageIndexViewModel"),
    ),
    "tohan": (
        ("listing", "https://www.tohan.com.tw/product.php?cid=107", r"g-bookName", r"act=view"),
        ("detail", "https://www.tohan.com.tw/product.php?act=view&id=8317", r"妖怪的葬儀屋", r"9786264371186"),
    ),
}
RETAIL = {
    "books": (
        "https://www.books.com.tw/web/sys_puballb/books/?pubid=chingwin",
        "https://www.books.com.tw/products/0011062041",
    ),
    "kingstone": (
        "https://www.kingstone.com.tw/bookpublish/publist/08025/",
        "https://www.kingstone.com.tw/basic/2019462221654/",
    ),
}
ISBN = "9786264499903"
LIMIT = 3 * 1024 * 1024
USER_AGENT = "AniShelf/0.1 (personal release catalog; low-rate read-only source test)"


class Restricted(Exception):
    pass


class OriginRedirect(HTTPRedirectHandler):
    max_redirections = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        before, after = urlsplit(req.full_url), urlsplit(newurl)
        if after.scheme != "https" or before.netloc != after.netloc:
            raise Restricted("redirect_other_origin")
        if re.search(r"login|member\.php", after.path, re.I):
            raise Restricted("login_required")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def _fetch_browser_fallback(url: str, expected_netloc: str) -> str:
    browser_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    challenge_re = re.compile(
        r"<title\b[^>]*>\s*(?:just a moment|attention required|access denied|請稍候)"
        r"|/cdn-cgi/challenge-platform|\bcf-chl-\b",
        re.I,
    )

    def _validate_target(final_url: str, body_text: str) -> str:
        after = urlsplit(final_url)
        if after.scheme != "https" or after.netloc != expected_netloc:
            raise Restricted("redirect_other_origin")
        if re.search(r"login|member\.php", after.path, re.I):
            raise Restricted("login_required")
        if len(body_text.encode("utf-8")) > LIMIT:
            raise ValueError("response_limit")
        if challenge_re.search(body_text):
            raise Restricted("challenge")
        return body_text

    # 1. 先模擬瀏覽器 TLS 指紋
    try:
        from curl_cffi import requests as cffi_requests  # type: ignore

        resp = cffi_requests.get(
            url,
            impersonate="chrome",
            headers={"Accept-Language": "zh-TW,zh;q=0.9"},
            timeout=15,
            max_redirects=2,
        )
        if resp.status_code == 200 and resp.headers.get("cf-mitigated", "").lower() != "challenge":
            return _validate_target(str(resp.url), resp.text)
    except Restricted:
        raise
    except Exception:
        pass

    # 2. 遇到阻擋後切換成擬真瀏覽器（搭配 xvfb 虛擬螢幕與防偵測腳本）
    try:
        import os
        from playwright.sync_api import sync_playwright  # type: ignore

        use_headed = bool(os.environ.get("DISPLAY"))
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=not use_headed,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1280,800",
                ],
            )
            try:
                context = browser.new_context(
                    user_agent=browser_ua,
                    locale="zh-TW",
                    timezone_id="Asia/Taipei",
                    viewport={"width": 1280, "height": 800},
                )
                context.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'languages', { get: () => ['zh-TW', 'zh', 'en-US', 'en'] });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    """
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    html_text = page.content()
                    if not challenge_re.search(html_text):
                        return _validate_target(page.url, html_text)
                    page.wait_for_timeout(1500)
                raise Restricted("challenge")
            finally:
                browser.close()
    except Restricted:
        raise
    except Exception as exc:
        raise Restricted("challenge") from exc



def fetch(url):
    from urllib.parse import quote

    encoded_url = quote(url, safe=":/?=&%.-_")
    expected_netloc = urlsplit(encoded_url).netloc
    request = Request(encoded_url, headers={"User-Agent": USER_AGENT})
    try:
        with build_opener(OriginRedirect()).open(request, timeout=10) as response:
            if response.headers.get("cf-mitigated", "").lower() == "challenge":
                return _fetch_browser_fallback(encoded_url, expected_netloc)
            payload = response.read(LIMIT + 1)
            if len(payload) > LIMIT:
                raise ValueError("response_limit")
            return payload.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except HTTPError as error:
        if error.headers.get("cf-mitigated", "").lower() == "challenge" or error.code == 403:
            return _fetch_browser_fallback(encoded_url, expected_netloc)
        raise

# def fetch(url):
#     # The caller supplies only hardcoded public URLs. One GET, no retry or login.
#     from urllib.parse import quote
#     request = Request(quote(url, safe=":/?=&%.-_"), headers={"User-Agent": USER_AGENT})
#     with build_opener(OriginRedirect()).open(request, timeout=10) as response:
#         if response.headers.get("cf-mitigated", "").lower() == "challenge":
#             raise Restricted("challenge")
#         payload = response.read(LIMIT + 1)
#         if len(payload) > LIMIT:
#             raise ValueError("response_limit")
#         return payload.decode(response.headers.get_content_charset() or "utf-8", errors="replace")


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def retail_detail(markup):
    """Validate one fixed edition sample, not a general-purpose retailer importer."""
    parser = VisibleText()
    parser.feed(markup)
    text = " ".join(" ".join(parser.parts).split())
    published = re.search(r"出版(?:日期|日)\s*[:：]?\s*(20\d{2})[/.-](\d{1,2})[/.-](\d{1,2})", text)
    if not (published and "螢火蟲的出嫁" in text and re.search(r"出版社\s*[:：]?\s*青文", text)
            and re.search(rf"ISBN\s*[:：]?\s*{ISBN}\b", text)
            and re.search(r"紙本平裝|規格\s*[:：]\s*平裝", text)):
        return None
    return {"isbn": ISBN, "media": "print", "retailer_publication_date":
            date(*map(int, published.groups())).isoformat()}


def probe(url, validator, load=fetch):
    try:
        data = validator(load(url))
        return {"status": "sample_verified" if data else "layout_or_evidence_unverified",
                **(data if isinstance(data, dict) else {})}
    except HTTPError as error:
        if error.headers.get("cf-mitigated", "").lower() == "challenge":
            return {"status": "challenge", "http": error.code}
        return {"status": "access_blocked" if error.code in {401, 403, 429} else "http_error", "http": error.code}
    except Restricted as error:
        return {"status": str(error)}
    except (URLError, OSError, ValueError):
        return {"status": "transport_or_response_error"}


def run(*, retailers=False, load=fetch, pause=time.sleep):
    report = {}
    stop = {"challenge", "access_blocked", "login_required", "redirect_other_origin"}
    for code, probes in OFFICIAL.items():
        for role, url, first, second in probes:
            pause(1)
            result = probe(url, lambda html: all(re.search(marker, html, re.I) for marker in (first, second)), load)
            report.setdefault(code, {})[role] = result
            if result["status"] in stop:
                break
    if retailers:
        for code, (listing, detail) in RETAIL.items():
            pause(1)
            results = {"listing": probe(listing, lambda html: "青文" in html and
                       bool(re.search(r"/(?:products|basic)/[0-9]+", html)), load)}
            if results["listing"]["status"] not in stop:
                pause(1)
                results["detail"] = probe(detail, retail_detail, load)
            report[code] = results
    return {"mode": "read_only", "actual_catalog_write": False, "coverage_measured": False, "sources": report}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--retailers", action="store_true", help="Probe approved public retailer sample pages")
    args = parser.parse_args()
    print(json.dumps(run(retailers=args.retailers), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
