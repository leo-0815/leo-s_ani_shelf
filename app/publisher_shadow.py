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


def fetch(url):
    # The caller supplies only hardcoded public URLs. One GET, no retry or login.
    from urllib.parse import quote
    request = Request(quote(url, safe=":/?=&%.-_"), headers={"User-Agent": USER_AGENT})
    with build_opener(OriginRedirect()).open(request, timeout=10) as response:
        if response.headers.get("cf-mitigated", "").lower() == "challenge":
            raise Restricted("challenge")
        payload = response.read(LIMIT + 1)
        if len(payload) > LIMIT:
            raise ValueError("response_limit")
        return payload.decode(response.headers.get_content_charset() or "utf-8", errors="replace")


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
