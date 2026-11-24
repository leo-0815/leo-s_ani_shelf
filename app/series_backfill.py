"""Bounded, operator-reviewed historical lookup; never part of cloud cron."""
from __future__ import annotations

import re
from urllib.parse import quote, unquote, urljoin, urlsplit

from .db import transaction
from .models import extract_volume
from .series import series_key
from .sources.common import fetch_html, parse_page, polite_pause
from .sources.kadokawa import KadokawaSource

HOST = "www.kadokawa.com.tw"
PAGES, DETAILS = 3, 25
OFFICIAL = {"義妹生活": "https://" + HOST + "/categories/" + quote("義妹生活-全系列")}
UNSAFE_EDITION = re.compile(r"套[書组組]|全套|合輯|畫集|設定集|全集|[0-9][)）]?\s*[~～〜至–—-]\s*[(（]?[0-9]", re.I)


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


def valid_isbn(isbn):
    isbn = str(isbn or "").replace("-", "").strip()
    return (bool(re.fullmatch(r"97[89][0-9]{10}", isbn))
            and sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(isbn)) % 10 == 0)


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
            if UNSAFE_EDITION.search(title) or not volume(extract_volume(title.split(" NT$")[0])):
                continue
            links[product] = None
        if index + 1 not in source._page_numbers(page.links):
            break
    candidates = []
    for link in list(links)[:DETAILS]:
        try:
            polite_pause()
            book = source._parse_detail(link, load(link, timeout=15, attempts=1))
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
    if row["code"] != "kadokawa":
        print("此出版社目前僅提供本機卷號盤點。")
        return
    default = OFFICIAL.get(row["title"], "")
    url = input(f"角川官方系列分類網址{'（Enter 用已知網址）' if default else ''}：").strip() or default
    if not url:
        return
    candidates, errors, capped = lookup(row, url)
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
        count = 0
        for book in new:
            if not existing(book):
                count += upsert_book(book) == "inserted"
        print(f"本機新增 {count} 筆。本次不自動上傳；確認後可走既有書目同步。")
