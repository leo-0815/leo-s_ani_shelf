"""Bounded discovery from official public pages for the local series runner."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date
from html import unescape
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import extract_volume, infer_series_title, normalize_text
from .series import series_key
from .series_backfill import (
    OFFICIAL, UNSAFE_EDITION, VARIANT_TITLE, kadokawa_variants, valid_isbn, volume,
)
from .sources import ChingWinSource, KadokawaSource, SppSource, TohanSource, TongLiSource
from .sources.common import MAX_RESPONSE_BYTES, USER_AGENT, parse_page, polite_pause


HOSTS = {
    'kadokawa': 'www.kadokawa.com.tw', 'spp': 'www.spp.com.tw',
    'tongli': 'www.tongli.com.tw', 'chingwin': 'www.ching-win.com.tw',
    'tohan': 'www.tohan.com.tw',
}
DETAIL_LIMIT = 8
KADOKAWA_PAGES = 2


@dataclass
class Discovery:
    books: list
    errors: int = 0
    status: str = 'checked_sample'
    capped: bool = False


class SourceBlocked(RuntimeError):
    pass


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        before, after = urlsplit(req.full_url), urlsplit(newurl)
        if after.scheme != 'https' or after.netloc != before.netloc:
            raise SourceBlocked('官網轉址離開原始 HTTPS 來源')
        if re.search(r'/(?:member|login)(?:[./?]|$)', after.path, re.I):
            raise SourceBlocked('來源需登入')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_public_html(url, *, timeout, attempts):
    """The local runner never follows an off-site link or retries access denial."""
    if attempts != 1 or urlsplit(url).netloc not in HOSTS.values() or urlsplit(url).scheme != 'https':
        raise ValueError('無效的官網讀取 URL')
    request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml',
                                    'Accept-Language': 'zh-TW,zh;q=0.9'})
    try:
        with build_opener(SameOriginRedirect()).open(request, timeout=timeout) as response:
            origin = next(code for code, host in HOSTS.items() if host == urlsplit(url).netloc)
            requested = product_url(origin, url, url)
            if requested and product_url(origin, url, response.geturl()) != requested:
                raise ValueError('商品頁轉址後識別碼不一致')
            markup = response.read(MAX_RESPONSE_BYTES + 1)
            if len(markup) > MAX_RESPONSE_BYTES:
                raise ValueError('商品頁超過 6 MB 上限')
            return markup.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
    except HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code}: {url}') from exc


def _load(url, load):
    polite_pause(0.8)
    try:
        markup = load(url, timeout=12, attempts=1)
    except RuntimeError as exc:
        if getattr(exc.__cause__, 'code', None) in {401, 403, 429} or re.match(r'HTTP (401|403|429)\b', str(exc)):
            raise SourceBlocked(str(exc)) from exc
        raise
    if 'cf-chl-' in markup and ("<title>Just a moment" in markup or '/cdn-cgi/challenge-platform/' in markup):
        raise SourceBlocked('Cloudflare challenge')
    return markup


def _url(code, route, **query):
    return f'https://{HOSTS[code]}{route}' + ('?' + urlencode(query) if query else '')


def product_url(code, base, href):
    """Canonicalise only real product routes on the expected publisher origin."""
    try:
        parts = urlsplit(urljoin(base, href))
    except ValueError:
        return None
    path = unquote(parts.path).rstrip('/')
    if parts.scheme != 'https' or parts.netloc.lower() != HOSTS[code] or re.search(r'[\\\x00-\x20]', path):
        return None
    query = parse_qs(parts.query)
    if code == 'kadokawa' and re.fullmatch(r'/(?:zh-hant/)?products/[^/]+', path) and path.split('/')[-1] not in {'.', '..', 'search'}:
        return _url(code, '/products/' + parts.path.rstrip('/').split('/')[-1])
    if code == 'chingwin' and re.fullmatch(r'/product-detail/[A-Za-z0-9_-]+', path):
        return _url(code, path)
    if code == 'spp' and (match := re.fullmatch(r'/(?:v2/)?SalePage/Index/([0-9]{1,16})', path, re.I)):
        return _url(code, '/SalePage/Index/' + match[1])
    if code == 'tongli' and path.lower() in {'/booksdetail.aspx', '/noveldetail.aspx'}:
        ids = [value for key, values in query.items() if key.lower() == 'bd' for value in values]
        if len(ids) == 1 and re.fullmatch(r'[A-Za-z0-9-]{1,30}', ids[0]):
            return _url(code, '/BooksDetail.aspx' if 'books' in path.lower() else '/NovelDetail.aspx', Bd=ids[0])
    if code == 'tohan' and path == '/product.php' and query.get('act') == ['view']:
        ids = query.get('id', [])
        if len(ids) == 1 and re.fullmatch(r'[0-9]{1,16}', ids[0]):
            return _url(code, path, act='view', id=ids[0])
    return None


class Cards(HTMLParser):
    def __init__(self, scope=None):
        super().__init__(convert_charrefs=True)
        self.scope = scope
        self.depth = 0
        self.start_depth = None
        self.found_scope = False
        self.anchor = None
        self.labels = []
        self.text = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div':
            self.depth += 1
            if self.start_depth is None and (
                    (self.scope == 'tongli' and attrs.get('id') == 'owl-demo2') or
                    (self.scope == 'tohan' and 'search-result' in (attrs.get('class') or '').split())):
                self.start_depth = self.depth
                self.found_scope = True
        if self.scope and self.start_depth is None:
            return
        if tag == 'a':
            self.anchor = attrs.get('href')
            self.labels = [attrs.get('title')]
            self.text = []
        if tag == 'img' and self.anchor:
            self.labels.append(attrs.get('alt'))

    def handle_data(self, data):
        if self.anchor:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor:
            self.items.append((self.anchor, [v for v in (*self.labels, ' '.join(self.text)) if v]))
            self.anchor = None
        if tag == 'div':
            if self.depth == self.start_depth:
                self.start_depth = None
            self.depth = max(0, self.depth - 1)


def _cards(markup, scope=None):
    reader = Cards(scope)
    reader.feed(markup)
    reader.close()
    if scope == 'tohan' and not reader.found_scope:
        raise ValueError('東販搜尋沒有可驗證的結果區塊')
    return reader.items


def _listing_label(row, label):
    """Use the visible title, not storefront ranking or a substring hit."""
    label = normalize_text(label).split(' NT$', 1)[0]
    label = re.sub(r'^(?:(?:會員獨享|加入購物車|售完|預購[-－]?)\s*)+', '', label).strip()
    label = re.sub(r'^(?:[【\[]\s*(?:漫畫|輕小說|小說)\s*[】\]]|(?:輕小說|小說|漫畫)\s+)\s*', '', label)
    if UNSAFE_EDITION.search(label):
        return False
    if variant := VARIANT_TITLE.fullmatch(label):
        media = 'manga' if variant[2] == '漫畫' else 'novel'
        return media == row['media_type'] and series_key(variant[1], row['code']) == row['series_key']
    number = volume(extract_volume(label))
    return bool(number and number in row['gaps'] and
                series_key(infer_series_title(label), row['code']) == row['series_key'])


def _spp_cards(markup):
    marker = re.search(r'"serverRenderData"\s*:\s*\{\s*"searchProvider"\s*:\s*"[^"]+"\s*,\s*"searchResult"\s*:\s*', markup)
    if not marker:
        raise ValueError('尖端搜尋沒有可驗證的結果資料')
    data, _ = json.JSONDecoder().raw_decode(markup[marker.end():].lstrip())
    items = data.get('SalePageList') if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError('尖端搜尋結果格式變更')
    return [(f'/SalePage/Index/{item["Id"]}', [item['Title']]) for item in items if
            isinstance(item, dict) and item.get('ShopId') == 41817 and
            type(item.get('Id')) is int and isinstance(item.get('Title'), str)]


def _detail(row, url, markup):
    code = row['code']
    page = parse_page(markup)
    if code == 'kadokawa':
        # Parent title must agree; no inheriting release dates/ratings from variant parents.
        if VARIANT_TITLE.fullmatch(KadokawaSource._clean_title(page.meta.get('og:title', ''))):
            return kadokawa_variants(row, url, markup)
        book = KadokawaSource()._parse_detail(url, markup)
    elif code == 'spp':
        source = SppSource()
        source._catalog_cutoff = date.min
        book, _ = source._parse_detail(url, markup)
    elif code == 'tongli':
        book = TongLiSource()._parse_detail(url, markup)
    elif code == 'tohan':
        if len(markup) < 4096 and re.search(r'member\.php\?act=login', markup, re.I):
            return []  # Public search can list member-only and PURE books; do not import them.
        match = re.search(r'<title\b[^>]*>(.*?)</title>', markup, re.I | re.S)
        fields = [re.sub(r'\s+', '', v) for v in unescape(match[1]).rsplit('|', 1)[0].split('_')[1:]] if match else []
        if any('PURE' in label.upper() for label in fields):
            return []
        if not fields or 'BLOCK_PRODUCT_SPEC' not in markup:
            raise ValueError('東販單本頁沒有可確認的商品規格')
        if '分類漫畫' in fields or fields[-1] == '漫畫':
            media = 'manga'
        elif set(fields[:-1]).intersection({'翻譯輕小說', '華文輕小說'}):
            media = 'novel'
        else:
            raise ValueError('東販單本頁沒有可確認的媒體分類')
        book = replace(TohanSource()._parse_detail(url, markup), media_type=media)
    else:
        # Verify media from the product's own breadcrumb, never from the requested row.
        match = re.search(r'<(?:div|ol|ul|nav)\b[^>]*\bclass=["\'][^"\']*\bbreadcrumb\b[^"\']*["\'][^>]*>(.*?)</(?:div|ol|ul|nav)>', markup, re.I | re.S)
        labels = {label for _, label in parse_page(match[1]).links} if match else set()
        choices = labels.intersection({'漫畫', '輕小說', '小說'})
        if len(choices) != 1:
            raise ValueError('青文單本頁沒有可確認的媒體分類')
        media = 'manga' if '漫畫' in choices else 'novel'
        records, _ = ChingWinSource()._parse_catalog_page(url, media, markup, date.min)
        return [b for b in records if product_url(code, url, b.source_url) == url]
    return [book] if book else []


def _accept(row, book):
    return (book.publisher_code == row['code'] and book.media_type == row['media_type']
            and product_url(row['code'], book.source_url, book.source_url) == book.source_url
            and bool(book.source_key)
            and series_key(book.series_title or '', row['code']) == row['series_key']
            and volume(book.volume_label) in row['gaps']
            and book.edition_type == 'standard' and not UNSAFE_EDITION.search(book.title)
            and valid_isbn(book.isbn, allow_publisher_barcode=row['code'] in {'chingwin', 'spp'}))


def discover(row, *, load=fetch_public_html):
    code = row['code']
    if code not in HOSTS or not row['gaps']:
        return Discovery([], status='no_visible_gap')
    title = row['title'][:120]
    seen = {}
    found = []
    failures = 0
    capped = False
    status = 'checked_sample'
    try:
        if code == 'tongli':
            # Same-series carousel is exposed by a known volume; never calculate sibling IDs.
            listings = [seed for url in row.get('seeds', ())[:1] if (seed := product_url(code, url, url))]
            if not listings:
                return Discovery([], status='no_official_seed')
        elif code == 'kadokawa':
            listings = [OFFICIAL[title]] if title in OFFICIAL else []
            listings.append(_url(code, '/products', query=title))
        elif code == 'chingwin':
            listings = [_url(code, '/searchall-products/', keywords=title)]
        elif code == 'spp':
            listings = [_url(code, '/v2/Search', q=title, shopId=41817)]
        else:
            listings = [_url(code, '/product.php', act='search', q_type='name', keywords=title)]
        loaded = set()
        for url in listings:
            if url in loaded:
                continue
            loaded.add(url)
            try:
                markup = _load(url, load)
                cards = _spp_cards(markup) if code == 'spp' else _cards(markup, code if code in {'tongli', 'tohan'} else None)
            except SourceBlocked:
                raise
            except (RuntimeError, OSError, ValueError):
                if len(loaded) == 1 and len(listings) == 1:
                    raise
                failures += 1
                continue
            if code == 'tongli' and not cards:
                status = 'no_verified_series_links'
            for href, titles in cards:
                product = product_url(code, url, href)
                if product and any(_listing_label(row, label) for label in titles):
                    seen[product] = None
            if code == 'kadokawa' and urlsplit(url).path == '/products':
                query = parse_qs(urlsplit(url).query)
                current = int(query.get('page', ['1'])[0])
                for href, _ in parse_page(markup).links:
                    part = urlsplit(urljoin(url, href))
                    params = parse_qs(part.query)
                    if (part.scheme == 'https' and part.netloc == HOSTS[code] and part.path == '/products'
                            and params.get('query') == [title] and params.get('page') == [str(current + 1)]):
                        if current < KADOKAWA_PAGES:
                            listings.append(_url(code, '/products', query=title, page=current + 1))
                        else:
                            capped = True
                        break
        capped |= len(seen) > DETAIL_LIMIT
        for url in list(seen)[:DETAIL_LIMIT]:
            try:
                found.extend(book for book in _detail(row, url, _load(url, load)) if _accept(row, book))
            except SourceBlocked:
                raise
            except (RuntimeError, OSError, ValueError):
                failures += 1
    except SourceBlocked:
        status = 'source_blocked'
    except (RuntimeError, OSError, ValueError):
        status = 'source_error'
        failures += 1
    return Discovery(found, failures, status, capped)
