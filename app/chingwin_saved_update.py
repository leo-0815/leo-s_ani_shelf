

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from scripts.chingwin_saved_product import PRODUCT_ID, ProductPage, parse_product

from .models import BookRecord, detect_edition
from .sources.chingwin_rating import product_identity
from .sources.product_rating import rating_fields


MEDIA_TYPES = ("manga", "novel")
MAX_FILES = 100
MAX_BYTES = 5_000_000


def parse_batch(inbox: Path, day: date) -> list[BookRecord]:
    batch = inbox / day.isoformat()
    if inbox.is_symlink() or batch.is_symlink():
        raise ValueError("不讀取符號連結的匯入目錄")
    if not batch.exists():
        return []
    if not batch.is_dir():
        raise ValueError("當日批次不是目錄")
    paths = []
    for media_dir in sorted(batch.iterdir()):
        if media_dir.is_symlink() or not media_dir.is_dir() or media_dir.name not in MEDIA_TYPES:
            raise ValueError("批次只接受 manga/、novel/ 子目錄")
        for path in sorted(media_dir.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".html" or not PRODUCT_ID.fullmatch(path.stem):
                raise ValueError("檔案須以官方商品編號命名為 .html，且不能是符號連結")
            paths.append(path)
            if len(paths) > MAX_FILES:
                raise ValueError(f"單日批次最多 {MAX_FILES} 筆")

    # The batch date is known, not the exact capture time. Use its earliest
    # possible time so a newer verified date/rating cannot be displaced.
    capture_floor = datetime.combine(day, time.min, tzinfo=timezone(timedelta(hours=8)))
    checked = capture_floor.astimezone(timezone.utc).replace(tzinfo=None)
    records: list[BookRecord] = []
    keys: set[str] = set()
    isbns: set[str] = set()
    for path in paths:
        try:
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("HTML 超過大小上限")
            markup = path.read_text(encoding="utf-8-sig")
            if not re.search(r"</html\s*>\s*\Z", markup, re.I):
                raise ValueError("HTML 尚不完整")
            product = parse_product(markup, path.stem)
            meta = ProductPage()
            meta.feed(markup)
            meta.close()
            if not meta.meta.get("og:url"):
                raise ValueError("缺少官方商品網址標記 og:url")
            isbn = product["isbn"]
            if not isbn or sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(isbn)) % 10:
                raise ValueError("缺少有效 ISBN-13，暫不自動匯入")
            if path.stem.upper() in keys or isbn in isbns:
                raise ValueError("批次內商品編號或 ISBN 重複，需確認版本")
            keys.add(path.stem.upper())
            isbns.add(isbn)
            title = re.sub(r"^\s*[（(]?預購[）)]?\s*", "", product["title"]).strip()
            if not title:
                raise ValueError("商品書名不完整")
            release = date.fromisoformat(product["published"]) if product["published"] else None
            cover = meta.meta.get("og:image", "")
            cover_url = urlsplit(cover)
            if (cover_url.scheme != "https" or cover_url.netloc not in {"www.ching-win.com.tw", "ching-win.com.tw"}
                    or not cover_url.path.startswith("/upload_files/") or cover_url.query or cover_url.fragment):
                cover = None
            grading = rating_fields("chingwin", product["source"], markup, path.stem, isbn)
            if grading["content_rating"] != "unknown" or grading["bl_category"]:
                grading["rating_checked_at"] = checked
            else:
                grading = {}  # Unknown HTML labels are not positive evidence.
            records.append(BookRecord(
                publisher_code="chingwin", source_key=path.stem, source_url=product["source"],
                title=title, author=product["author"], media_type=path.parent.name, isbn=isbn,
                cover_url=cover, edition_type=detect_edition(title), release_date=release,
                release_precision="day" if release else "unknown",
                release_date_source="product" if release else "unknown",
                release_checked_at=checked if release else None, **grading,
            ))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{path.name}：{exc}") from None
    return records


def check_catalog_conflicts(records: list[BookRecord], rows: list[dict]) -> None:
    by_key = {record.source_key.upper(): record for record in records}
    by_isbn = {record.isbn: record for record in records}
    for row in rows:
        isbn = re.sub(r"\D", "", str(row.get("isbn") or ""))
        sku = product_identity(str(row.get("source_url") or ""))
        exact = by_key.get(str(row["source_key"]).upper()) or (by_key.get(sku.upper()) if sku else None)
        if exact and isbn and isbn != exact.isbn:
            raise ValueError(f"{exact.source_key} 的 ISBN 與既有書目衝突")
        same_isbn = by_isbn.get(isbn)
        if same_isbn and sku and sku.upper() != same_isbn.source_key.upper():
            raise ValueError(f"{same_isbn.source_key} 的 ISBN 已屬於另一個正式商品")
        record = exact or same_isbn
        if record and row.get("media_type") in MEDIA_TYPES and row["media_type"] != record.media_type:
            raise ValueError(f"{record.source_key} 的漫畫／小說分類與既有書目衝突")


def apply_records(records: list[BookRecord]) -> dict[str, int]:
    from .db import transaction
    from .repository import upsert_book

    slots = ",".join(["%s"] * len(records))
    params = tuple(record.source_key for record in records) + tuple(record.isbn for record in records)
    with transaction() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT b.source_key, b.source_url, b.isbn, b.media_type FROM books b "
            "JOIN publishers p ON p.id = b.publisher_id WHERE p.code = 'chingwin' "
            f"AND (b.source_key IN ({slots}) OR b.isbn IN ({slots}))", params,
        )
        check_catalog_conflicts(records, list(cursor.fetchall()))
    # Reuse the existing edition matching, metadata merging and catalog history.
    return dict(Counter(upsert_book(record) for record in records))


def write_report(report: dict, output: Path | None) -> None:
    if output:
        output.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2) + "\n", encoding="utf-8")
    print(f"青文 HTML 匯入：{report['status']}，{len(report['books'])} 筆")


def main() -> None:
    today = datetime.now(timezone(timedelta(hours=8))).date()
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--inbox", type=Path, default=Path("inputs/chingwin"))
    cli.add_argument("--date", type=date.fromisoformat, default=today, dest="day")
    cli.add_argument("--output", type=Path)
    cli.add_argument("--apply", action="store_true")
    args = cli.parse_args()
    if args.apply and args.day != today:
        cli.error("正式匯入只接受台北時間當日批次；其他日期可預覽")
    try:
        records = parse_batch(args.inbox, args.day)
        report = {"date": args.day.isoformat(), "status": "preview" if records else "no_input",
                  "books": [record.prepared() for record in records]}
        if args.apply and records:
            settings = ("HOST", "PORT", "NAME", "USER", "PASSWORD")
            if (os.getenv("ANISHELF_CLOUD_MODE") != "true"
                    or os.getenv("ANISHELF_DB_SSL_MODE") != "verify_identity"
                    or any(not os.getenv(f"ANISHELF_DB_{key}") for key in settings)):
                raise ValueError("正式匯入需要雲端資料庫設定與 TLS 驗證")
            report["changes"] = apply_records(records)
            report["status"] = "applied"
        write_report(report, args.output)
    except (OSError, ValueError) as exc:
        cli.exit(1, f"青文 HTML 匯入停止：{exc}\n")


if __name__ == "__main__":
    main()
