"""Offline two-pass local importer for manually saved Chingwin HTML pages.

Pass 1: Drop saved comic/novel listing HTML into inputs/chingwin/YYYY/MM/DD/ (or list/)
        to compare against local MySQL and emit pending_chingwin_urls.txt for
        new or unrated SKUs that need individual product pages.
Pass 2: Drop saved product detail HTML into the same dated folder (or detail/)
        to enrich exact publication date, list price, content rating, and BL
        category, upsert to local MySQL on explicit confirmation, and optionally
        push to cloud Render via CatalogSyncClient.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import time
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from .models import BookRecord, detect_edition, extract_volume, infer_series_title
from .sources.chingwin import ChingWinSource
from .sources.product_rating import rating_fields

ROOT_URL = "https://www.ching-win.com.tw"
DEFAULT_INBOX = Path("inputs/chingwin")
MAX_BYTES = 5_000_000
TAIPEI = timezone(timedelta(hours=8))

CHALLENGE = re.compile(
    r"<title\b[^>]*>\s*(?:just a moment|attention required|access denied|請稍候)"
    r"|/cdn-cgi/challenge-platform|\bcf-chl-\b",
    re.I,
)
PRODUCT_LABEL = re.compile(r"產品編號\s*[:：]\s*([A-Za-z0-9_-]+)")
SECTION_END = re.compile(r"(?:定價|優惠價|內容簡介|商品介紹|推薦商品|相關商品)")
SPEC_LABELS = "產品編號|作家|作者|繪者|譯者|集數|包裝|級別|開本|尺寸|出版日期|頁數|ISBN"


class _DetailPage(HTMLParser):
    BLOCKS = {"br", "div", "p", "li", "tr", "td", "th", "h1", "h2", "h3", "dt", "dd"}
    SKIP = {"script", "style", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.meta: dict[str, str] = {}
        self._ignored: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._ignored.append(tag)
        if self._ignored:
            return
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "meta":
            fields = dict(attrs)
            name = fields.get("property") or fields.get("name")
            content = fields.get("content")
            if name and content:
                self.meta.setdefault(name.lower(), content)

    def handle_endtag(self, tag: str) -> None:
        if self._ignored:
            if tag == self._ignored[-1]:
                self._ignored.pop()
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)

    @property
    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def _valid_isbn13(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) != 13 or not digits.startswith(("978", "979")):
        return None
    if sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(digits)) % 10 != 0:
        return None
    return digits


def _safe_cover(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"www.ching-win.com.tw", "ching-win.com.tw"}
        or not parsed.path.startswith("/upload_files/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    return url.strip()


def _infer_media_type(path: Path, markup: str, title: str = "") -> str:
    lower_path = str(path).lower()
    if any(tok in lower_path for tok in ("novel", "小說")):
        return "novel"
    if any(tok in lower_path for tok in ("comic", "manga", "漫畫")):
        return "manga"
    if "/books/novel" in markup or "輕小說" in title or "輕小說" in markup[:2500]:
        return "novel"
    return "manga"


def _official_detail_sku(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in {"www.ching-win.com.tw", "ching-win.com.tw"}:
        return None
    match = re.fullmatch(r"/product-detail/([A-Za-z0-9_-]+)(?:/[^/]+)?/?", parsed.path)
    return match[1] if match else None


def detect_html_kind(markup: str) -> str:
    if CHALLENGE.search(markup):
        raise ValueError("這是 Cloudflare 驗證或拒絕頁，不是有效青文頁面")
    page = _DetailPage()
    page.feed(markup)
    page.close()
    if _official_detail_sku(page.meta.get("og:url", "")):
        return "detail"
    if re.search(r"application/ld\+json", markup, re.I) and re.search(r'"@type"\s*:\s*"Book"', markup, re.I):
        return "listing"
    if PRODUCT_LABEL.search(page.text):
        return "detail"
    raise ValueError("無法辨識為青文列表頁或單本商品頁 HTML")


def parse_listing_html(path: Path, markup: str) -> list[BookRecord]:
    media_type = _infer_media_type(path, markup)
    page_url = f"{ROOT_URL}/products/chingwin/books/{'novel' if media_type == 'novel' else 'comic'}/"
    source = ChingWinSource()
    records, _total_pages = source._parse_catalog_page(page_url, markup, media_type, date(2000, 1, 1))
    return records


def parse_detail_html(path: Path, markup: str, *, checked_at: datetime | None = None) -> BookRecord:
    if CHALLENGE.search(markup):
        raise ValueError("這是 Cloudflare 驗證或拒絕頁，不是商品頁")
    page = _DetailPage()
    page.feed(markup)
    page.close()
    text = page.text

    sku_match = PRODUCT_LABEL.search(text)
    canonical = page.meta.get("og:url", "")
    url_sku = _official_detail_sku(canonical)
    if canonical and not url_sku:
        raise ValueError("商品頁的 og:url 不是青文官方單本商品網址")
    sku = (sku_match.group(1) if sku_match else None) or url_sku
    if not sku:
        raise ValueError("找不到商品編號（產品編號 / og:url）")
    if sku_match and url_sku and sku_match.group(1) != url_sku:
        raise ValueError(f"頁面產品編號 {sku_match.group(1)} 與 og:url {url_sku} 不符")

    source_url = f"{ROOT_URL}/product-detail/{sku}"
    anchor = sku_match.end() if sku_match else 0
    specs = SECTION_END.split(text[anchor : anchor + 1600], maxsplit=1)[0]

    author_field = re.search(r"作家\s*[:：][ \t]*", specs)
    author = None
    if author_field:
        remainder = specs[author_field.end() :].lstrip()
        author_line = re.split(rf"\n|(?:{SPEC_LABELS})\s*[:：]", remainder, maxsplit=1)[0].strip()
        if author_line not in {"", "-", "－", "—"}:
            author = author_line

    html_title = re.search(r"<title\b[^>]*>(.*?)</title\s*>", markup, re.I | re.S)
    raw_title = page.meta.get("og:title") or (unescape(html_title[1]) if html_title else "")
    title = re.sub(r"\s*[-–—|｜]\s*青文出版.*$", "", " ".join(raw_title.split()))
    title = re.sub(r"^\s*[（(]?預購[）)]?\s*", "", title).strip()
    if not title:
        raise ValueError("讀不到商品書名")

    isbn_match = re.search(r"ISBN\s*[:：]\s*(97[89](?:[ -]?\d){10})(?![ -]?\d)", specs, re.I)
    isbn = _valid_isbn13(isbn_match.group(1)) if isbn_match else None

    date_match = re.search(r"出版日期\s*[:：]\s*(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", specs)
    release: date | None = None
    if date_match:
        try:
            release = date(*map(int, date_match.groups()))
        except ValueError:
            release = None

    price_scope = re.split(r"(?:內容簡介|商品介紹|推薦商品|相關商品)", text[anchor : anchor + 2000], maxsplit=1)[0]
    price_match = re.search(r"定價\s*[:：]?\s*(?:NT\$|新台幣)?\s*([0-9,]+)", price_scope)
    list_price = int(price_match.group(1).replace(",", "")) if price_match else None
    if list_price is not None and list_price <= 0:
        list_price = None

    observed = checked_at or datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    grading = rating_fields("chingwin", source_url, markup, sku, isbn)
    if grading["content_rating"] != "unknown" or grading.get("bl_category"):
        grading["rating_checked_at"] = observed
    else:
        grading = {}

    media_type = _infer_media_type(path, markup, title=title)
    return BookRecord(
        publisher_code="chingwin",
        source_key=sku,
        source_url=source_url,
        title=title,
        author=author,
        media_type=media_type,
        isbn=isbn,
        cover_url=_safe_cover(page.meta.get("og:image")),
        list_price=list_price,
        edition_type=detect_edition(title),
        volume_label=extract_volume(title),
        series_title=infer_series_title(title),
        release_date=release,
        release_precision="day" if release else "unknown",
        release_date_source="product" if release else "unknown",
        release_checked_at=observed if release else None,
        **grading,
    )


def merge_listing_and_details(
    listing_records: list[BookRecord],
    detail_records: list[BookRecord],
) -> list[BookRecord]:
    by_key: dict[str, BookRecord] = {}
    for rec in listing_records:
        by_key[rec.source_key] = rec
    for det in detail_records:
        base = by_key.get(det.source_key)
        if base is None:
            by_key[det.source_key] = det
            continue
        by_key[det.source_key] = replace(
            base,
            title=det.title or base.title,
            author=det.author or base.author,
            isbn=det.isbn or base.isbn,
            cover_url=det.cover_url or base.cover_url,
            list_price=det.list_price if det.list_price is not None else base.list_price,
            release_date=det.release_date or base.release_date,
            release_precision=det.release_precision if det.release_date else base.release_precision,
            release_date_source=det.release_date_source if det.release_date else base.release_date_source,
            release_checked_at=det.release_checked_at if det.release_date else base.release_checked_at,
            content_rating=det.content_rating if det.content_rating != "unknown" else base.content_rating,
            rating_raw=det.rating_raw if det.content_rating != "unknown" else base.rating_raw,
            rating_source=det.rating_source if det.content_rating != "unknown" else base.rating_source,
            rating_confidence=det.rating_confidence if det.content_rating != "unknown" else base.rating_confidence,
            rating_checked_at=det.rating_checked_at or base.rating_checked_at,
            rating_parser_version=det.rating_parser_version or base.rating_parser_version,
            bl_category=det.bl_category or base.bl_category,
        )
    return list(by_key.values())


def find_pending_details(
    listing_records: list[BookRecord],
    existing_by_key: dict[str, dict[str, Any]],
    supplied_detail_keys: set[str] | None = None,
) -> list[BookRecord]:
    supplied = supplied_detail_keys or set()
    pending: list[BookRecord] = []
    seen: set[str] = set()
    for rec in listing_records:
        if rec.source_key in seen or rec.source_key in supplied:
            continue
        seen.add(rec.source_key)
        existing = existing_by_key.get(rec.source_key)
        if existing is None or existing.get("content_rating") in {None, "", "unknown"}:
            pending.append(rec)
    return pending


def write_pending_list(pending: list[BookRecord], output_path: Path) -> None:
    lines = [f"{rec.source_key}\t{rec.title}\t{rec.source_url}" for rec in pending]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def dated_inbox(inbox: Path, day: date) -> Path:
    """Select exactly one Taipei calendar day; do not scan older or undated input."""
    location = inbox
    for part in (f"{day.year:04d}", f"{day.month:02d}", f"{day.day:02d}"):
        location = location / part
        if location.is_symlink():
            raise ValueError(f"青文日期資料夾不得使用符號連結：{location}")
    return location


def collect_inbox_files(inbox: Path) -> list[Path]:
    files: list[Path] = []
    for folder in (inbox, inbox / "list", inbox / "detail"):
        if folder.is_symlink() or not folder.is_dir():
            continue
        for item in sorted(folder.iterdir()):
            if item.is_file() and not item.is_symlink() and item.suffix.lower() in {".html", ".htm"}:
                files.append(item)
    return files


def saved_dates(inbox: Path, *, today: date | None = None) -> Iterator[date]:
    """Only descend into valid YYYY/MM/DD paths, not done/ or undated files."""
    today = today or datetime.now(TAIPEI).date()
    if not inbox.is_dir() or inbox.is_symlink():
        return
    for year in sorted(inbox.iterdir()):
        if not re.fullmatch(r"[0-9]{4}", year.name) or year.is_symlink() or not year.is_dir():
            continue
        for month in sorted(year.iterdir()):
            if not re.fullmatch(r"[0-9]{2}", month.name) or month.is_symlink() or not month.is_dir():
                continue
            for day in sorted(month.iterdir()):
                if not re.fullmatch(r"[0-9]{2}", day.name) or day.is_symlink() or not day.is_dir():
                    continue
                try:
                    day_value = date(int(year.name), int(month.name), int(day.name))
                except ValueError:
                    continue
                if day_value <= today:
                    yield day_value


def _day_start_utc(day: date) -> datetime:
    local = datetime.combine(day, datetime.min.time(), tzinfo=TAIPEI)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def read_saved_batch(
    files: list[Path], *, checked_at: datetime | None = None,
) -> tuple[list[BookRecord], list[BookRecord], list[Path], list[str]]:
    listings: list[BookRecord] = []
    details: list[BookRecord] = []
    parsed: list[Path] = []
    errors: list[str] = []
    for path in files:
        try:
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("檔案超過 5 MB 上限")
            markup = path.read_text(encoding="utf-8-sig")
            if detect_html_kind(markup) == "listing":
                rows = parse_listing_html(path, markup)
                if not rows:
                    raise ValueError("列表未讀到書目")
                listings.extend(rows)
            else:
                details.append(parse_detail_html(path, markup, checked_at=checked_at))
            parsed.append(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}：{exc}")
    return listings, details, parsed, errors


def read_saved_history(inbox: Path = DEFAULT_INBOX) -> Iterator[tuple[date, list[BookRecord], list[Path], list[str]]]:
    """Read local input only; callers decide whether to write or archive."""
    for day in saved_dates(inbox):
        batch = dated_inbox(inbox, day)
        files = collect_inbox_files(batch)
        if not files:
            continue
        listings, details, parsed, errors = read_saved_batch(files, checked_at=_day_start_utc(day))
        yield day, merge_listing_and_details(listings, details), parsed, errors


def _require_local_database() -> None:
    from .config import get_settings

    settings = get_settings()
    if settings.cloud_mode or settings.db_host.strip().casefold() not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("此匯入工具只支援本機 MySQL；請用本機書目設定執行。")


def import_saved_history(
    write: Callable[[list[BookRecord]], int], messages: list[str], *,
    inbox: Path = DEFAULT_INBOX, deadline: float | None = None,
) -> dict[str, Any]:
    """Archive successful batches; leave unreadable files and failed writes for retry."""
    outcome = dict(batches=0, discovered=0, archived=0, file_errors=0, write_errors=0, paused=False)
    _require_local_database()
    batches = iter(read_saved_history(inbox))
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            outcome["paused"] = True
            return outcome
        try:
            day, records, files, errors = next(batches)
        except StopIteration:
            return outcome
        outcome["batches"] += 1
        outcome["file_errors"] += len(errors)
        outcome["discovered"] += len(records)
        messages.extend(f"青文 {day}：{error}" for error in errors[-10:])
        if deadline is not None and time.monotonic() >= deadline:
            outcome["paused"] = True
            return outcome
        if not records:
            continue
        outcome["write_errors"] = write(records)
        if outcome["write_errors"]:
            return outcome
        outcome["archived"] += len(archive_files(files, inbox / "done", day=day))


def archive_files(files: list[Path], done_root: Path, *, day: date | None = None) -> list[Path]:
    target_dir = done_root / (day or datetime.now(TAIPEI).date()).isoformat()
    target_dir.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for src in files:
        dst = target_dir / src.name
        counter = 1
        while dst.exists():
            dst = target_dir / f"{src.stem}_{counter}{src.suffix}"
            counter += 1
        shutil.move(str(src), str(dst))
        moved.append(dst)
    return moved


def load_existing_chingwin_status() -> dict[str, dict[str, Any]]:
    from .db import connect

    conn = connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT b.source_key, b.content_rating, b.release_date_source "
                "FROM books b JOIN publishers p ON p.id = b.publisher_id "
                "WHERE p.code = 'chingwin'"
            )
            rows = cursor.fetchall()
        conn.rollback()
    finally:
        conn.close()
    return {str(row["source_key"]): dict(row) for row in rows}


def run_interactive(inbox: Path = DEFAULT_INBOX, *, day: date | None = None) -> None:
    from .db import ensure_schema
    from .repository import upsert_book

    today = datetime.now(TAIPEI).date()
    batch_day = day or today
    if batch_day > today:
        raise ValueError("匯入日期不能晚於台北時間今天")
    batch = dated_inbox(inbox, batch_day)
    batch.mkdir(parents=True, exist_ok=True)
    files = collect_inbox_files(batch)
    if not files:
        print("\n找不到這一天的青文 HTML 檔案。")
        print(f"請放入：{batch.resolve()}")
        print("  - 漫畫／輕小說列表頁可放 list/（或直接放該日資料夾）")
        print("  - 單本商品明細頁可放 detail/（或直接放該日資料夾）")
        return

    _require_local_database()
    ensure_schema()
    # An older batch must not be recorded as a fresh observation today.
    checked_at = None if batch_day == today else _day_start_utc(batch_day)
    listing_records, detail_records, ok_files, errors = read_saved_batch(files, checked_at=checked_at)

    existing = load_existing_chingwin_status()
    detail_keys = {rec.source_key for rec in detail_records}
    pending = find_pending_details(listing_records, existing, supplied_detail_keys=detail_keys)
    merged = merge_listing_and_details(listing_records, detail_records)

    pending_path = batch / "pending_chingwin_urls.txt"
    write_pending_list(pending, pending_path)

    print(f"\n=== 青文離線 HTML 匯入預覽：{batch_day.isoformat()} ===")
    print(f"讀取檔案：{len(ok_files)} 個成功，{len(errors)} 個失敗")
    print(f"列表頁解析：{len(listing_records)} 筆；單本商品頁解析：{len(detail_records)} 筆；合併後共 {len(merged)} 筆")
    if errors:
        print("\n失敗檔案：")
        for err in errors:
            print(f"  - {err}")

    if pending:
        print(f"\n尚需補存單本商品頁的新書／未分級書目共 {len(pending)} 筆（已寫入 {pending_path}）：")
        for rec in pending[:15]:
            print(f"  - [{rec.source_key}] {rec.title} -> {rec.source_url}")
        if len(pending) > 15:
            print(f"  ... 其餘 {len(pending) - 15} 筆請見 {pending_path}")
    else:
        print("\n沒有缺商品頁的新書或未分級項目。")

    if not merged:
        return

    print("\n待寫入書目預覽（前 10 筆）：")
    for rec in merged[:10]:
        print(
            f"  - [{rec.source_key}] {rec.title} | {rec.media_type} | "
            f"出版日={rec.release_date} | 分級={rec.content_rating} | BL={rec.bl_category or '-'}"
        )

    confirm = input("\n輸入 YES 將上述書目寫入本機 MySQL（其他鍵取消，不改資料庫）：").strip()
    if confirm != "YES":
        print("已取消寫入本機資料庫。")
        return

    counts = Counter(upsert_book(rec) for rec in merged)
    moved = archive_files(ok_files, inbox / "done", day=batch_day)
    print(f"本機寫入完成：{dict(counts)}；已歸檔 {len(moved)} 個 HTML 至 done/。")

    from . import catalog_sync

    sync_client = getattr(catalog_sync, "CatalogSyncClient", None)
    if sync_client is None:
        print("已保留本機書目；本版沒有雲端同步用戶端。")
        return
    sync_confirm = input("輸入 YES 立即同步至雲端 Render（若要等補完商品頁再同步請按 Enter 略過）：").strip()
    if sync_confirm == "YES":
        sync_result = sync_client().push()
        print(f"雲端同步完成：{sync_result}")
    else:
        print("已略過本次雲端同步（下次同步會自動帶上）。")


if __name__ == "__main__":
    from argparse import ArgumentParser

    cli = ArgumentParser(description="匯入青文 YYYY/MM/DD 資料夾的 HTML")
    cli.add_argument("--date", type=date.fromisoformat, help="批次日期 YYYY-MM-DD，預設台北時間今天")
    cli.add_argument("--inbox", type=Path, default=DEFAULT_INBOX, help="日期資料夾的根目錄")
    args = cli.parse_args()
    try:
        run_interactive(args.inbox, day=args.date)
    except (OSError, ValueError) as exc:
        cli.exit(1, f"青文匯入停止：{exc}\n")
