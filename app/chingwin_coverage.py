"""Read-only Chingwin coverage benchmark against Kingstone and NCL open data.

No database writes, no catalog mutations, no notifications.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import statistics
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener

from .models import detect_edition, extract_volume, infer_series_title, normalize_text
from .publisher_shadow import LIMIT, USER_AGENT, OriginRedirect, Restricted, VisibleText

KINGSTONE_BASE = "https://www.kingstone.com.tw"
KINGSTONE_LIST_TEMPLATE = (
    "https://www.kingstone.com.tw/bookpublish/publist/08025/"
    "?subkind=all&classname=new&dis=pic&format=0&sort=sa_desc&page={page}"
)
KINGSTONE_DISALLOWED_PREFIXES = (
    "/api/",
    "/bms/",
    "/dks/",
    "/cart/",
    "/refund/",
    "/einvoice/",
    "/wp-admin/",
)
NCL_DATASET_URL = "https://data.gov.tw/api/v2/rest/dataset/6730"
STOP_STATUSES = {"challenge", "access_blocked", "login_required", "redirect_other_origin"}
SPECIAL_EDITIONS = {"special", "limited", "first_print", "bonus", "bundle", "deluxe", "collector"}


def assert_robots_allowed(url: str) -> None:
    """Refuse Kingstone paths disallowed by robots.txt."""
    parsed = urlsplit(url)
    if parsed.hostname and parsed.hostname.endswith("kingstone.com.tw"):
        path = parsed.path.lower()
        for prefix in KINGSTONE_DISALLOWED_PREFIXES:
            if path.startswith(prefix):
                raise Restricted(f"robots_disallowed:{prefix}")


def fetch_text(url: str, *, timeout: int = 15, max_bytes: int = LIMIT) -> str:
    """Single GET with origin-redirect guard and robots check. Never retries on 403/429."""
    assert_robots_allowed(url)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with build_opener(OriginRedirect()).open(request, timeout=timeout) as response:
        if response.headers.get("cf-mitigated", "").lower() == "challenge":
            raise Restricted("challenge")
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("response_limit")
        charset = response.headers.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")


def fetch_ncl_bytes(url: str, *, timeout: int = 20, max_bytes: int = 25 * 1024 * 1024) -> bytes:
    """Download an open-data resource from data.gov.tw / ncl.edu.tw."""
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with build_opener(OriginRedirect()).open(request, timeout=timeout) as response:
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("response_limit")
        return payload


def decode_csv_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp950", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def normalize_isbn13(raw: str | None) -> str | None:
    """Return 13-digit 978/979 ISBN only; reject 471 GTIN barcodes."""
    digits = re.sub(r"[^0-9]", "", raw or "")
    if len(digits) == 13 and digits.startswith(("978", "979")):
        return digits
    return None


def extract_gtin_471(text: str) -> str | None:
    compact = re.sub(r"(?<=\d)[-\s]+(?=\d)", "", text or "")
    m = re.search(r"(?<!\d)(471\d{10})(?!\d)", compact)
    return m.group(1) if m else None


def normalize_volume(vol: str | None) -> str | None:
    if not vol:
        return None
    v = normalize_text(vol).strip()
    if v.isdigit():
        return str(int(v))
    return v


def normalize_title_key(title: str) -> str:
    base = infer_series_title(title) or title
    base = normalize_text(base).casefold()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", base)


def edition_bucket(edition_type: str | None, media_type: str | None) -> str:
    if edition_type == "digital" or media_type == "ebook":
        return "digital"
    if edition_type in SPECIAL_EDITIONS:
        return "special"
    return "standard"


def media_bucket(edition_type: str | None, media_type: str | None) -> str:
    return "ebook" if (edition_type == "digital" or media_type == "ebook") else "print"


def parse_kingstone_listing(markup: str) -> list[dict[str, Any]]:
    """Parse product cards from a Kingstone publisher listing page."""
    items: list[dict[str, Any]] = []
    cards = re.split(r'(?=<div class="mod_prod_card\b)', markup)[1:]
    for card in cards:
        prod_match = re.search(r'data-prodno="(\d+)"', card)
        title_match = re.search(r'<h3 class="pdnamebox[^"]*">\s*<a[^>]*>(.*?)</a>', card, re.S)
        if not prod_match or not title_match:
            continue
        prodno = prod_match.group(1)
        title = normalize_text(html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))))
        date_match = re.search(r'<div class="pubdate">\s*(20\d{2})/(\d{1,2})/(\d{1,2})', card)
        pub_date = (
            date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
            if date_match
            else None
        )
        items.append(
            {
                "prodno": prodno,
                "url": urljoin(KINGSTONE_BASE, f"/basic/{prodno}/"),
                "title": title,
                "listing_pub_date": pub_date.isoformat() if pub_date else None,
            }
        )
    return items


def parse_kingstone_detail(markup: str, fallback_title: str = "") -> dict[str, Any]:
    """Parse one Kingstone detail page, separating publication date from shipping date."""
    parser = VisibleText()
    parser.feed(markup)
    text = " ".join(" ".join(parser.parts).split())

    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", markup, re.S | re.I)
    title = normalize_text(html.unescape(re.sub(r"<[^>]+>", "", h1_match.group(1)))) if h1_match else ""
    if not title:
        title = fallback_title

    # Publication date: explicitly exclude 預計出貨 / 出貨日
    cleaned_for_date = re.sub(r"(?:預計)?出貨(?:日期|日)?\s*[:：]?\s*20\d{2}[/.-]\d{1,2}[/.-]\d{1,2}", "", text)
    pub_match = re.search(
        r"出版(?:日期|日)\s*[:：]?\s*(20\d{2})[/.-](\d{1,2})[/.-](\d{1,2})", cleaned_for_date
    )
    pub_date = (
        date(int(pub_match.group(1)), int(pub_match.group(2)), int(pub_match.group(3))).isoformat()
        if pub_match
        else None
    )

    # ISBN (978/979 only); 471 is GTIN
    isbn_match = re.search(r"ISBN\s*[:：]?\s*([0-9-]{10,17})", text, re.I)
    isbn = normalize_isbn13(isbn_match.group(1)) if isbn_match else None
    gtin = extract_gtin_471(text)

    # Media: print vs ebook
    is_print = bool(re.search(r"裝訂\s*[:：]?\s*(?:紙本平裝|紙本精裝|平裝|精裝)|規格\s*[:：]\s*(?:平裝|精裝)", text))
    is_ebook = bool(re.search(r"(?:裝訂|規格|出版形式)\s*[:：]?\s*電子書", text)) or (
        not is_print and "電子書" in title
    )
    media = "ebook" if is_ebook else ("print" if is_print else "print")

    edition = "digital" if media == "ebook" else detect_edition(title)
    rating_match = re.search(r"分級\s*[:：]?\s*(限制級|輔15級|輔12級|保護級|普遍級)", text)
    bl_marker = bool(re.search(r"級別\s*[^。]*\bBL\b", text))

    return {
        "title": title,
        "isbn": isbn,
        "gtin": gtin,
        "retailer_publication_date": pub_date,
        "media": media,
        "edition_type": edition,
        "volume": normalize_volume(extract_volume(title)),
        "title_key": normalize_title_key(title),
        "rating_raw": rating_match.group(1) if rating_match else None,
        "bl_marker": bl_marker,
    }


def parse_ncl_csv(csv_text: str) -> list[dict[str, Any]]:
    """Parse NCL dataset 6730 CSV, keeping only 出版機構 == '青文' rows."""
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    rows: list[dict[str, Any]] = []
    for raw in reader:
        if (raw.get("出版機構") or "").strip() != "青文":
            continue
        title = normalize_text(raw.get("申請書名") or "")
        if not title:
            continue
        pub_raw = (raw.get("預訂出版日") or "").strip()
        ym_match = re.match(r"^(20\d{2})-(\d{2})-\d{2}$", pub_raw)
        planned_ym = f"{ym_match.group(1)}-{ym_match.group(2)}" if ym_match else None
        fmt = f"{raw.get('出版形式') or ''} {raw.get('裝訂方式') or ''}"
        media = "ebook" if "電子書" in fmt or "電子書" in title else "print"
        edition = "digital" if media == "ebook" else detect_edition(title)
        rows.append(
            {
                "title": title,
                "isbn": normalize_isbn13(raw.get("ISBN")),
                "planned_ym": planned_ym,
                "release_precision": "month",
                "media": media,
                "edition_type": edition,
                "volume": normalize_volume(extract_volume(title)),
                "title_key": normalize_title_key(title),
                "rating_raw": (raw.get("分級註記") or "").strip() or None,
            }
        )
    return rows


def collect_kingstone(
    *,
    max_listing_pages: int = 15,
    max_details: int = 120,
    min_listing_date: str = "2026-09-01",
    detail_window_start: str = "2026-09-15",
    load: Callable[[str], str] = fetch_text,
    pause: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Collect recent Chingwin records from Kingstone within strict page/detail caps."""
    listing_items: list[dict[str, Any]] = []
    seen_prodnos: set[str] = set()
    status = "ok"
    error_detail: str | None = None
    pages_fetched = 0
    listing_cap_hit = False

    for page in range(1, max_listing_pages + 1):
        url = KINGSTONE_LIST_TEMPLATE.format(page=page)
        try:
            markup = load(url)
        except HTTPError as err:
            status = "challenge" if err.headers.get("cf-mitigated", "").lower() == "challenge" else (
                "access_blocked" if err.code in {401, 403, 429} else "http_error"
            )
            error_detail = f"HTTP {err.code}"
            break
        except Restricted as err:
            status = str(err)
            break
        except (URLError, OSError, ValueError) as err:
            status = "transport_or_response_error"
            error_detail = str(err)
            break

        pages_fetched += 1
        page_items = parse_kingstone_listing(markup)
        if not page_items:
            break
        for item in page_items:
            if item["prodno"] not in seen_prodnos:
                seen_prodnos.add(item["prodno"])
                listing_items.append(item)

        dates = [it["listing_pub_date"] for it in page_items if it["listing_pub_date"]]
        if dates and max(dates) < min_listing_date:
            break
        if page == max_listing_pages:
            listing_cap_hit = True
        pause(1.0)

    candidates = [
        it for it in listing_items
        if not it["listing_pub_date"] or it["listing_pub_date"] >= detail_window_start
    ]
    detail_cap_hit = len(candidates) > max_details
    records: list[dict[str, Any]] = []
    details_fetched = 0

    if status not in STOP_STATUSES:
        for it in candidates[:max_details]:
            pause(1.0)
            try:
                d_markup = load(it["url"])
            except HTTPError as err:
                status = "challenge" if err.headers.get("cf-mitigated", "").lower() == "challenge" else (
                    "access_blocked" if err.code in {401, 403, 429} else "http_error"
                )
                error_detail = f"detail HTTP {err.code}"
                break
            except Restricted as err:
                status = str(err)
                break
            except (URLError, OSError, ValueError):
                continue
            details_fetched += 1
            parsed = parse_kingstone_detail(d_markup, fallback_title=it["title"])
            if not parsed["retailer_publication_date"] and it["listing_pub_date"]:
                parsed["retailer_publication_date"] = it["listing_pub_date"]
            parsed["url"] = it["url"]
            parsed["prodno"] = it["prodno"]
            records.append(parsed)

    return {
        "status": status,
        "error": error_detail,
        "listing_pages_fetched": pages_fetched,
        "listing_items_seen": len(listing_items),
        "details_fetched": details_fetched,
        "listing_cap_hit": listing_cap_hit,
        "detail_cap_hit": detail_cap_hit,
        "records": records,
    }


def collect_ncl(
    *,
    ncl_months: int = 3,
    load_meta: Callable[[str], str] = fetch_text,
    load_csv_bytes: Callable[[str], bytes] = fetch_ncl_bytes,
) -> dict[str, Any]:
    """Fetch latest N monthly CSVs from data.gov.tw dataset 6730 and filter Chingwin."""
    try:
        meta = json.loads(load_meta(NCL_DATASET_URL)).get("result", {})
    except Exception as exc:
        return {"status": "error", "error": str(exc), "distributions": [], "records": []}

    dists = [d for d in meta.get("distribution", []) if d.get("resourceDownloadUrl")]
    selected = dists[-ncl_months:] if ncl_months > 0 else []
    records: list[dict[str, Any]] = []
    used: list[dict[str, Any]] = []
    for dist in selected:
        url = dist["resourceDownloadUrl"]
        desc = dist.get("resourceDescription") or url
        raw_bytes = load_csv_bytes(url)
        parsed = parse_ncl_csv(decode_csv_bytes(raw_bytes))
        records.extend(parsed)
        used.append({"description": desc, "url": url, "chingwin_rows": len(parsed)})

    return {
        "status": "ok",
        "attribution": "國家圖書館 (data.gov.tw/dataset/6730)",
        "distributions": used,
        "chingwin_rows_loaded": len(records),
        "records": records,
    }


def match_baseline_row(base: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """Match one baseline row against source candidates without cross-media merging."""
    b_media = media_bucket(base.get("edition_type"), base.get("media_type"))
    b_isbn = normalize_isbn13(base.get("isbn"))
    if b_isbn:
        isbn_hits = [c for c in candidates if c.get("isbn") == b_isbn and c.get("media") == b_media]
        if len(isbn_hits) == 1:
            return "hit", isbn_hits[0]
        if len(isbn_hits) > 1:
            return "ambiguous", None

    b_title_key = normalize_title_key(base.get("title") or "")
    b_vol = normalize_volume(base.get("volume_label") or extract_volume(base.get("title") or ""))
    b_bucket = edition_bucket(base.get("edition_type"), base.get("media_type"))
    title_hits = [
        c for c in candidates
        if c.get("media") == b_media
        and c.get("title_key") == b_title_key
        and c.get("volume") == b_vol
        and edition_bucket(c.get("edition_type"), c.get("media")) == b_bucket
    ]
    if len(title_hits) == 1:
        return "hit", title_hits[0]
    if len(title_hits) > 1:
        return "ambiguous", None
    return "miss", None


def evaluate_source(
    baseline: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    reference_date: date = date(2026, 11, 25),
    date_field: str | None = None,
) -> dict[str, Any]:
    buckets: dict[str, dict[str, int]] = {}
    offsets: list[int] = []
    pre_listing_14d_total = 0
    pre_listing_14d_hits = 0

    for row in baseline:
        rel_str = str(row.get("release_date") or "")[:10]
        ym = rel_str[:7] if len(rel_str) >= 7 else "unknown"
        eb = edition_bucket(row.get("edition_type"), row.get("media_type"))
        key = f"{ym}|{eb}"
        stats = buckets.setdefault(key, {"baseline": 0, "hit": 0, "miss": 0, "ambiguous": 0})
        stats["baseline"] += 1

        outcome, matched = match_baseline_row(row, candidates)
        stats[outcome] += 1

        if row.get("release_precision") == "day" and len(rel_str) == 10:
            off_date = date.fromisoformat(rel_str)
            is_future_14d = reference_date < off_date <= reference_date + timedelta(days=14)
            if is_future_14d:
                pre_listing_14d_total += 1
                if outcome == "hit":
                    pre_listing_14d_hits += 1
            if outcome == "hit" and date_field and matched and matched.get(date_field):
                ret_date = date.fromisoformat(matched[date_field])
                offsets.append((ret_date - off_date).days)

    for stats in buckets.values():
        stats["hit_rate"] = round(stats["hit"] / stats["baseline"], 4) if stats["baseline"] else 0.0

    offset_summary = (
        {
            "count": len(offsets),
            "min_days": min(offsets),
            "median_days": statistics.median(offsets),
            "max_days": max(offsets),
        }
        if offsets
        else {"count": 0}
    )
    return {
        "coverage": dict(sorted(buckets.items())),
        "date_offset_days": offset_summary,
        "pre_listing_next_14d": {"baseline": pre_listing_14d_total, "hits": pre_listing_14d_hits},
    }


def format_markdown_summary(report: dict[str, Any]) -> str:
    lines = [
        f"Baseline rows: {report['baseline']['total']}",
        f"NCL attribution: {report['attribution']['ncl']}",
        "",
        "| Source | Month | Edition | Baseline | Hit | Miss | Ambiguous | Hit rate |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for src_name in ("kingstone", "ncl"):
        cov = report["sources"][src_name].get("coverage", {})
        for key, st in cov.items():
            ym, eb = key.split("|", 1)
            lines.append(
                f"| {src_name} | {ym} | {eb} | {st['baseline']} | {st['hit']} | {st['miss']} | {st['ambiguous']} | {st['hit_rate']:.1%} |"
            )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Chingwin coverage benchmark.")
    parser.add_argument("--baseline", required=True, help="Path to baseline JSON export.")
    parser.add_argument("--ncl-months", type=int, default=3, help="Number of latest NCL CSVs to load.")
    parser.add_argument("--max-listing-pages", type=int, default=15, help="Max Kingstone listing pages.")
    parser.add_argument("--max-details", type=int, default=120, help="Max Kingstone detail fetches.")
    parser.add_argument("--out", required=True, help="Output JSON report path.")
    args = parser.parse_args()

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    ks = collect_kingstone(max_listing_pages=args.max_listing_pages, max_details=args.max_details)
    ncl = collect_ncl(ncl_months=args.ncl_months)

    ks_eval = evaluate_source(baseline, ks["records"], date_field="retailer_publication_date")
    ncl_eval = evaluate_source(baseline, ncl["records"], date_field=None)

    report = {
        "mode": "read_only",
        "actual_catalog_write": False,
        "attribution": {"ncl": "國家圖書館 (data.gov.tw/dataset/6730)"},
        "note": "Single snapshot measures presence and retailer-vs-official date offset only; true listing lead/lag requires repeated daily snapshots.",
        "baseline": {
            "total": len(baseline),
            "day_precision": sum(1 for r in baseline if r.get("release_precision") == "day"),
            "month_precision": sum(1 for r in baseline if r.get("release_precision") == "month"),
        },
        "sources": {
            "kingstone": {
                k: v for k, v in ks.items() if k != "records"
            } | ks_eval,
            "ncl": {
                k: v for k, v in ncl.items() if k != "records"
            } | ncl_eval,
        },
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(format_markdown_summary(report))


if __name__ == "__main__":
    main()
