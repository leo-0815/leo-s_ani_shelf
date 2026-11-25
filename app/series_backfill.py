"""Bounded, operator-reviewed historical lookup; never part of cloud cron."""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import quote, unquote, urljoin, urlsplit

from .db import transaction
from .models import BookRecord, extract_volume
from .series import series_key
from .sources.common import fetch_html, parse_page, polite_pause
from .sources.kadokawa import KadokawaSource
from .sources.spp import SppSource

HOST = "www.kadokawa.com.tw"
PAGES, DETAILS = 3, 25
SPP_HOST = "www.spp.com.tw"
OFFICIAL = {"義妹生活": "https://" + HOST + "/categories/" + quote("義妹生活-全系列")}
UNSAFE_EDITION = re.compile(r"套[書组組]|全套|合輯|畫集|設定集|全集|[0-9][)）]?\s*[~～〜至–—-]\s*[(（]?[0-9]", re.I)
VARIANT_TITLE = re.compile(r"^(.+?)\s*系列\s*[【〖［（(]\s*(輕小說|小說|漫畫)\s*[】〗］）)]")
SHOPLINE_PRODUCT = re.compile(r"""app\.value\('product',\s*JSON\.parse\('((?:[^'\\]|\\.)*)'\)""")


def volume(value):
    label = str(value or "")
    return int(label) if re.fullmatch(r"[0-9]{1,3}", label) and int(label) > 0 else None


def listed_series(query=""):
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT p.code,b.series_key,MIN(b.series_title) AS title,b.media_type,"
            "GROUP_CONCAT(DISTINCT CAST(b.volume_label AS UNSIGNED)) AS vols "
            "FROM books b JOIN publishers p ON p.id=b.publisher_id "
            "WHERE b.series_key<>'' AND b.series_title LIKE %s AND b.media_type IN ('manga','novel') "
            "AND b.volume_label REGEXP '^[0-9]{1,3}$' "
            "GROUP BY p.code,b.series_key,b.media_type HAVING COUNT(DISTINCT CAST(b.volume_label AS UNSIGNED))>=2 "
            "ORDER BY (MAX(CAST(b.volume_label AS UNSIGNED))-COUNT(DISTINCT CAST(b.volume_label AS UNSIGNED))) DESC,"
            " p.code,b.series_key,b.media_type LIMIT 30",
            (f"%{query[:120]}%",),
        )
        rows = cur.fetchall()
    for row in rows:
        row["volumes"] = {int(v) for v in row.pop("vols").split(",") if int(v) > 0}
        row["gaps"] = sorted(set(range(1, max(row["volumes"], default=0) + 1)) - row["volumes"])
    return rows


def official_url(url, kind):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != HOST
            or not re.fullmatch(rf"/{kind}/[^/]+/?", unquote(parts.path))
            or re.search(r"[\\\x00-\x20]", unquote(parts.path))):
        raise ValueError("只接受台灣角川官網的系列分類頁或單本商品頁")
    return "https://" + HOST + parts.path.rstrip("/")


def valid_isbn(isbn, *, allow_publisher_barcode=False):
    isbn = str(isbn or "").replace("-", "").strip()
    prefix = r"(?:97[89]|471)" if allow_publisher_barcode else r"97[89]"
    return (bool(re.fullmatch(prefix + r"[0-9]{10}", isbn))
            and sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(isbn)) % 10 == 0)


def kadokawa_variants(row, link, markup):
    """Read the publisher's own variant GTIN/volume pair, not recommendations or bundle metadata."""
    match = SHOPLINE_PRODUCT.search(markup)
    if not match:
        raise ValueError("找不到角川商品規格")
    data = json.loads(json.loads('"' + match[1].replace("\\'", "'") + '"'))
    title = (data.get("title_translations") or {}).get("zh-hant", "")
    match = VARIANT_TITLE.fullmatch(title.strip()) if isinstance(title, str) else None
    if not match or UNSAFE_EDITION.search(title) or data.get("bundle_set") or data.get("child_products"):
        return []
    media = "manga" if match[2] == "漫畫" else "novel"
    if media != row["media_type"] or series_key(match[1], "kadokawa") != row["series_key"]:
        return []
    rows, identities = [], {}
    variations = data.get("variations") or []
    if not isinstance(variations, list) or len(variations) > 100:
        raise ValueError("角川商品規格數量異常")
    for item in variations:
        labels = (item.get("fields_translations") or {}).get("zh-hant") if isinstance(item, dict) else None
        number = volume(labels[0]) if isinstance(labels, list) and len(labels) == 1 else None
        isbn = str(item.get("gtin") or "").strip() if isinstance(item, dict) else ""
        if not number or not valid_isbn(isbn):
            continue
        if isbn in identities:
            if identities[isbn] != number:
                raise ValueError("相同 ISBN 指向不同卷號")
            continue
        identities[isbn] = number
        # ISBN is also Kadokawa's normal single-book URL key. Do not reuse parent
        # publication date, rating, cover, or price for a variant.
        rows.append(BookRecord(
            "kadokawa", isbn, f"{match[1].strip()} ({number})", media, link,
            series_title=match[1].strip(), volume_label=str(number), isbn=isbn,
        ))
    return rows


def lookup(row, url, *, load=fetch_html):
    """Return evidence-backed candidates only; unresolved variants are never fabricated."""
    if row["code"] != "kadokawa":
        raise ValueError("官網定向補查目前只支援台灣角川")
    url = official_url(url, "categories")
    source = KadokawaSource()
    links = {}
    rejected = []
    for index in range(1, PAGES + 1):
        page_url = source._page_url(url, index)
        polite_pause()
        page = parse_page(load(page_url, timeout=15, attempts=1))
        for href, title in page.links:
            if "加入購物車" not in title or row["title"] not in title:
                continue
            try:
                product = official_url(urljoin(page_url, href), "products")
            except ValueError:
                continue
            label = title.split(" NT$")[0].removeprefix("會員獨享 ").strip()
            multi = VARIANT_TITLE.fullmatch(label) is not None
            if UNSAFE_EDITION.search(title) or (not multi and not volume(extract_volume(label))):
                continue
            links[product] = multi
        if index + 1 not in source._page_numbers(page.links):
            break
    candidates = []
    for link in list(links)[:DETAILS]:
        try:
            polite_pause()
            markup = load(link, timeout=15, attempts=1)
            if links[link]:
                candidates.extend(kadokawa_variants(row, link, markup))
                continue
            book = source._parse_detail(link, markup)
        except (RuntimeError, ValueError, OSError):
            rejected.append(link)
            continue
        if (book.media_type == row["media_type"] and book.publisher_code == row["code"]
                and series_key(book.series_title or "", row["code"]) == row["series_key"]
                and volume(book.volume_label) and valid_isbn(book.isbn)
                and book.author and not UNSAFE_EDITION.search(book.title)):
            candidates.append(book)
    return candidates, rejected, len(links) > DETAILS


def existing(book):
    with transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT p.code,b.series_key,b.media_type FROM books b JOIN publishers p ON p.id=b.publisher_id "
            "WHERE (p.code=%s AND b.source_key=%s) OR b.isbn=%s",
            (book.publisher_code, book.source_key, book.isbn),
        )
        return cur.fetchall()


def spp_lookup(row, urls, *, load=fetch_html):
    """Allow a known official category or specific old item pages, never general site crawling."""
    products = {}
    rejected = []
    if not urls or len(urls) > DETAILS:
        raise ValueError(f"請輸入 1～{DETAILS} 個尖端官網連結")
    for url in urls:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != SPP_HOST:
            raise ValueError("只接受尖端網路書店連結")
        product = re.fullmatch(r"/(?:v2/)?SalePage/Index/([0-9]{1,16})/?", parts.path, re.I)
        category = re.fullmatch(r"/v2/official/SalePageCategory/[0-9]{1,16}/?", parts.path, re.I)
        if product:
            products["https://" + SPP_HOST + "/SalePage/Index/" + product[1]] = None
        elif category and len(urls) == 1:
            polite_pause()
            for href, title in parse_page(load("https://" + SPP_HOST + parts.path, timeout=15, attempts=1)).links:
                link = urlsplit(urljoin(url, href))
                matched = re.fullmatch(r"/(?:v2/)?SalePage/Index/([0-9]{1,16})/?", link.path, re.I)
                if matched and link.scheme == "https" and link.netloc == SPP_HOST and row["title"] in title:
                    products["https://" + SPP_HOST + "/SalePage/Index/" + matched[1]] = None
        else:
            raise ValueError("請輸入一個尖端官方分類，或以空格分開單本商品頁")
    source = SppSource()
    source._catalog_cutoff = date.min  # Only these explicitly selected editions bypass the rolling date window.
    candidates = []
    for link in list(products)[:DETAILS]:
        try:
            polite_pause()
            book, _ = source._parse_detail(link, load(link, timeout=15, attempts=1))
            if (book and book.publisher_code == "spp" and book.media_type == row["media_type"]
                    and series_key(book.series_title or "", "spp") == row["series_key"]
                    and not UNSAFE_EDITION.search(book.title) and volume(book.volume_label)
                    and valid_isbn(book.isbn, allow_publisher_barcode=True)):
                candidates.append(book)
        except (RuntimeError, ValueError, OSError):
            rejected.append(link)
    return candidates, rejected, len(products) > DETAILS


def upload_selected(books, client=None):
    """Send just the explicitly confirmed local inserts; do not advance the ordinary sync cursor."""
    from .catalog_sync import CatalogSyncClient
    from .repository import catalog_rows_by_keys

    keys = list(dict.fromkeys((b.publisher_code, b.source_key) for b in books))
    if not keys:
        return {"accepted": 0, "skipped": 0}
    if len(keys) > 100:
        raise ValueError("定向同步每次最多 100 筆")
    client = client or CatalogSyncClient()
    requested = {(b.publisher_code, b.source_key): b.isbn for b in books}
    wanted = set(keys)
    isbns = {}
    for key, isbn in requested.items():
        isbns.setdefault((key[0], isbn), set()).add(key)
    after = 0
    revision = None
    # Check ISBN as well as publisher/source key: old editions can have URL aliases.
    for _ in range(60):
        page = client._request("GET", f"/api/catalog-sync/books?after_id={after}&limit=500")
        observed_revision = page.get("snapshot_change_id")
        if revision is not None and observed_revision != revision:
            raise RuntimeError("雲端書目正在更新，未同步")
        revision = observed_revision
        for item in page["items"]:
            wanted.discard((item["publisher_code"], item["source_key"]))
            wanted.difference_update(isbns.get((item["publisher_code"], item.get("isbn")), ()))
        if not page.get("has_more") or not wanted:
            break
        next_id = int(page["next_after_id"])
        if next_id <= after:
            raise RuntimeError("雲端書目比對未完成")
        after = next_id
    else:
        raise RuntimeError("超過書目比對上限，未同步")
    rows = catalog_rows_by_keys(sorted(wanted)) if wanted else []
    items = [CatalogSyncClient._clean_outgoing(row) for row in rows
             if (row["publisher_code"], row["source_key"]) in wanted and
             row.get("isbn") == requested[(row["publisher_code"], row["source_key"])]]
    if not items:
        return {"accepted": 0, "skipped": len(keys)}
    result = client._request("POST", "/api/catalog-sync/books", {"items": items})
    return {"accepted": int(result.get("accepted", 0)), "skipped": len(keys) - len(items)}


def interactive_series():
    query = input("系列搜尋（留空列出有缺卷候選的系列）：").strip()
    rows = listed_series(query)
    for index, row in enumerate(rows, 1):
        gaps = "、".join(map(str, row["gaps"][:12])) or "無可見卷號缺口"
        print(f"{index}. {row['title']} [{row['code']} / {row['media_type']}]：{gaps}")
    if not rows:
        print("找不到已收錄且有足夠卷號的系列。")
        return
    selection = input("輸入系列編號查官網；Enter 返回：").strip()
    if not selection.isascii() or not selection.isdigit() or len(selection) > 2 or not 1 <= int(selection) <= len(rows):
        return
    row = rows[int(selection) - 1]
    if row["code"] == "kadokawa":
        default = OFFICIAL.get(row["title"], "")
        url = input(f"角川官方系列分類網址{'（Enter 用已知網址）' if default else ''}：").strip() or default
        if not url:
            return
        candidates, errors, capped = lookup(row, url)
    elif row["code"] == "spp":
        urls = input("尖端官方分類，或單本商品網址（多本用空格分開）：").split()
        if not urls:
            return
        candidates, errors, capped = spp_lookup(row, urls)
    else:
        print("此出版社目前僅提供本機卷號盤點。")
        return
    new = []
    for book in candidates:
        matches = existing(book)
        state = ("已在此系列" if any(m["series_key"] == row["series_key"] and
                 m["media_type"] == row["media_type"] and m["code"] == row["code"] for m in matches)
                 else "已在庫，系列或版本需人工確認" if matches else "可新增")
        print(f"{state}：{book.title} [{book.isbn}]")
        if not matches:
            new.append(book)
    verified = {volume(b.volume_label) for b in candidates}
    unresolved = sorted(set(row["gaps"]) - verified)
    print(f"此次未確認的候選缺卷：{unresolved}；讀取失敗 {len(errors)} 筆。"
          + ("已達商品查詢上限。" if capped else "這不是完整出版清單。"))
    if new and input(f"新增 {len(new)} 筆到本機？輸入 YES 確認：").strip() == "YES":
        from .repository import upsert_book
        inserted = []
        for book in new:
            if not existing(book) and upsert_book(book) == "inserted":
                inserted.append(book)
        print(f"本機新增 {len(inserted)} 筆。")
        if inserted and input("只同步本次新增的書目？輸入 SYNC 確認：").strip() == "SYNC":
            try:
                result = upload_selected(inserted)
                print(f"雲端接受 {result['accepted']} 筆、略過 {result['skipped']} 筆。")
            except RuntimeError:
                print("雲端同步未確認；本機書目已保存，可用既有書目同步重試。")
