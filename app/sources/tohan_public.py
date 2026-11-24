"""Public listing seeds, never a substitute for member-only product evidence."""
from html.parser import HTMLParser
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit
import re

from ..models import BookRecord, detect_edition, extract_volume, infer_series_title


class SourceLoginRequired(RuntimeError):
    pass


class UnsupportedTohanProduct(ValueError):
    """An explicit official category outside manga/light novels, not a parse error."""


class ExcludedTohanPure(UnsupportedTohanProduct):
    pass


PURE_EXCLUSION = "東販官方 PURE 書系不收錄"
PURE_CATEGORIES = {"PURE", "PURE系列", "限定版/特裝版(PURE)"}


@dataclass(frozen=True)
class ListingRecord(BookRecord):
    """In-process provenance: listing metadata must never erase product fields."""


MANGA_CATEGORIES = frozenset({
    "分類漫畫", "限定版/特裝版(一般)", "科幻/戰爭", "推理懸疑/驚悚恐怖", "靈異/神怪",
    "奇幻/魔法", "戀愛/校園", "輕鬆/詼諧", "社會寫實/職場職人", "溫馨療育", "人文歷史",
    "其他類型", "動作/冒險", "運動/競賽", "漫畫書",
})


def product_categories(markup: str) -> tuple[str, str] | None:
    match = re.search(r"<title\b[^>]*>(.*?)</title>", markup, re.S | re.I)
    text = unescape(re.sub(r"<[^>]+>", "", match[1])) if match else ""
    taxonomy = text.rsplit("|", 1)[0].rsplit("_", 2)
    if len(taxonomy) == 3:
        return tuple(re.sub(r"\s+", "", x) for x in taxonomy[-2:])
    return None


def reject_pure_product(markup: str) -> None:
    if PURE_CATEGORIES.intersection(product_categories(markup) or ()):
        raise ExcludedTohanPure(PURE_EXCLUSION)


def detail_media_type(markup: str, hint: str | None = None, *, strict_hint: bool = True) -> str:
    """Read this product's document-title taxonomy, never global navigation/title keywords."""
    reject_pure_product(markup)
    own = "unknown"
    categories = product_categories(markup)
    if categories:
        child, parent = categories
        if parent not in {"漫畫", "漫畫/輕小說"}:
            raise UnsupportedTohanProduct(f"非漫畫／輕小說商品：{parent[:60]}")
        if child in {"華文輕小說", "翻譯輕小說"}:
            own = "novel"
        elif parent == "漫畫" or child in MANGA_CATEGORIES:
            own = "manga"
    if hint in {"manga", "novel"}:
        if own != "unknown" and own != hint:
            if strict_hint:
                raise ValueError("東販商品分類與清單類型衝突，保留原資料")
            return own
        return hint
    return own


LOGIN_ERROR = "來源需登入：東販商品僅限會員讀取"


def require_public_product(markup: str) -> None:
    # The restricted response is a small script, not a product containing a login link.
    if (len(markup) < 4096 and re.search(r"member\.php\?act=login", markup, re.I)
            and re.search(r"(?:location\s*\.\s*(?:replace|href)|location\s*=)", markup, re.I)):
        raise SourceLoginRequired(LOGIN_ERROR)


class ListingParser(HTMLParser):
    VOID = frozenset({'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'})

    def __init__(self, url: str, media_hint: str | None = None):
        super().__init__(convert_charrefs=True)
        self.url, self.rows = url, {}
        self.card = None
        self.in_name = False
        self.media_hint = media_hint
        self.stack = []
        self.grid_depth = None
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in self.VOID:
            self.stack.append(tag)
        if tag == 'div' and {'tab-pane','active','gobal-warp'} <= set((a.get('class') or '').split()):
            self.grid_depth = len(self.stack)
        if self.grid_depth is None:
            return
        if tag in {'script','style'}:
            self.skip_depth += 1
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
        if self.card and self.in_name and not self.skip_depth:
            self.card["name"].append(value)

    def handle_endtag(self, tag):
        if tag == "p":
            self.in_name = False
        if tag == "a" and self.card:
            title = " ".join("".join(self.card["name"]).split())
            if title and self.card["cover"]:
                key = self.card["key"]
                self.rows[key] = ListingRecord(
                    "tohan", key, title, self.media_hint or "unknown",
                    f"https://www.tohan.com.tw/product.php?act=view&id={key}",
                    cover_url=self.card["cover"], edition_type=detect_edition(title),
                    volume_label=extract_volume(title), series_title=infer_series_title(title))
            self.card, self.in_name = None, False
        if tag in {'script','style'} and self.skip_depth:
            self.skip_depth -= 1
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]
        if self.grid_depth is not None and len(self.stack) < self.grid_depth:
            self.grid_depth = None
            self.card, self.in_name = None, False


def public_seeds(url: str, markup: str, media_hint: str | None = None) -> dict[str, BookRecord]:
    if media_hint not in {None, 'manga', 'novel'}:
        raise ValueError('Invalid media hint')
    parser = ListingParser(url, media_hint)
    parser.feed(markup)
    return parser.rows
