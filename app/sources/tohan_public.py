"""Public listing seeds, never a substitute for member-only product evidence."""
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit
import re

from ..models import BookRecord, detect_edition, extract_volume, infer_series_title


class SourceLoginRequired(RuntimeError):
    pass


LOGIN_ERROR = "來源需登入：東販商品僅限會員讀取"


def require_public_product(markup: str) -> None:
    # The restricted response is a small script, not a product containing a login link.
    if (len(markup) < 4096 and re.search(r"member\.php\?act=login", markup, re.I)
            and re.search(r"(?:location\s*\.\s*(?:replace|href)|location\s*=)", markup, re.I)):
        raise SourceLoginRequired(LOGIN_ERROR)


class ListingParser(HTMLParser):
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.url, self.rows = url, {}
        self.card = None
        self.in_name = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            self.card = None
            self.in_name = False
            target = urlsplit(urljoin(self.url, a.get("href") or ""))
            q = parse_qs(target.query)
            key = q.get("id", [""])[0]
            if (target.scheme in {"http", "https"} and target.hostname == "www.tohan.com.tw"
                    and not target.port and not target.username and target.path == "/product.php"
                    and q.get("act") == ["view"] and re.fullmatch(r"\d+", key)):
                self.card = {"key": key, "name": [], "cover": None}
        if not self.card:
            return
        if tag == "p" and "g-bookName" in (a.get("class") or "").split():
            self.in_name = True
        if tag == "img":
            image = urlsplit(urljoin(self.url, a.get("src") or ""))
            if (image.scheme in {"http", "https"} and image.hostname == "www.tohan.com.tw"
                    and image.path.startswith("/data/goods/") and not image.username and not image.port):
                self.card["cover"] = image._replace(scheme="https", fragment="").geturl()

    def handle_data(self, value):
        if self.card and self.in_name:
            self.card["name"].append(value)

    def handle_endtag(self, tag):
        if tag == "p":
            self.in_name = False
        if tag == "a" and self.card:
            title = " ".join("".join(self.card["name"]).split())
            if title and self.card["cover"]:
                key = self.card["key"]
                self.rows[key] = BookRecord(
                    "tohan", key, title, "unknown",
                    f"https://www.tohan.com.tw/product.php?act=view&id={key}",
                    cover_url=self.card["cover"], edition_type=detect_edition(title),
                    volume_label=extract_volume(title), series_title=infer_series_title(title))
            self.card, self.in_name = None, False


def public_seeds(url: str, markup: str) -> dict[str, BookRecord]:
    parser = ListingParser(url)
    parser.feed(markup)
    return parser.rows
