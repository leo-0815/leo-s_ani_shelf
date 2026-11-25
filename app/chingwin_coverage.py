"""Compare a Chingwin sample with NCL open data and local retailer evidence."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .models import detect_edition, extract_volume, infer_series_title, normalize_text
from .publisher_shadow import USER_AGENT, OriginRedirect, Restricted

NCL_DATASET_URL = "https://data.gov.tw/api/v2/rest/dataset/6730"
NCL_DATASET_PAGE = "https://data.gov.tw/dataset/6730"
NCL_LICENSE_URL = "https://data.gov.tw/license"
NCL_HOSTS = {"data.gov.tw", "www.ncl.edu.tw", "isbn.ncl.edu.tw"}
NCL_PUBLISHER_HEADERS = {"出版機構", "出版機構簡稱"}
SPECIAL_EDITIONS = {"special", "limited", "first_print", "bonus", "bundle", "deluxe", "collector"}
RETAILER_HOSTS = {
    "kingstone": "www.kingstone.com.tw",
    "sanmin": "www.sanmin.com.tw",
    "udn": "reading.udn.com",
}


def validate_ncl_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in NCL_HOSTS:
        raise ValueError("NCL URLs must use HTTPS on an official open-data or NCL host")


def fetch_ncl_bytes(url: str, *, timeout: int = 20, max_bytes: int = 25 * 1024 * 1024) -> bytes:
    """Download an open-data resource from data.gov.tw / ncl.edu.tw."""
    validate_ncl_url(url)
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


def fetch_ncl_metadata(url: str) -> str:
    return fetch_ncl_bytes(url, max_bytes=3 * 1024 * 1024).decode("utf-8-sig")


def _checked_13(raw: str | None, prefixes: tuple[str, ...]) -> str | None:
    digits = re.sub(r"[-\s]", "", raw or "")
    if re.fullmatch(r"[0-9]{13}", digits) and digits.startswith(prefixes) and (
        sum(int(n) * (1 if i % 2 == 0 else 3) for i, n in enumerate(digits)) % 10 == 0
    ):
        return digits
    return None


def normalize_isbn13(raw: str | None) -> str | None:
    """Return 13-digit 978/979 ISBN only; reject 471 GTIN barcodes."""
    return _checked_13(raw, ("978", "979"))


def normalize_volume(vol: str | None) -> str | None:
    if not vol:
        return None
    v = normalize_text(vol).strip()
    if v.isdigit():
        return str(int(v))
    return v


def normalize_title_key(title: str) -> str:
    title = re.sub(r"^(?:輕小說 |[〖【]電子書[〗】]\s*)", "", normalize_text(title))
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


def read_retailer_samples(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Normalize manually checked metadata. Source URLs are evidence only, never fetched."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload or set(payload) - RETAILER_HOSTS.keys():
        raise ValueError("expected local retailer samples keyed by a supported source")
    normalized = {}
    for source, rows in payload.items():
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise ValueError(f"{source}: expected 1–100 manually checked rows")
        normalized[source] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{source}: expected an evidence row")
            title = row.get("title")
            url = urlsplit(row.get("source_url") or "")
            publishers = {"青文", "青文出版", "青文出版社", "青文出版社股份有限公司"}
            if source == "udn" and row.get("media") == "ebook":
                publishers.add("悅文社")
            if (not title or row.get("publisher") not in publishers
                    or url.scheme != "https" or url.netloc != RETAILER_HOSTS[source]
                    or row.get("media") not in {"print", "ebook"}
                    or row.get("edition_type") not in SPECIAL_EDITIONS | {"standard", "digital"}):
                raise ValueError(f"{source}: missing or conflicting title, publisher, origin, format, or edition")
            if (row["edition_type"] == "digital") != (row["media"] == "ebook"):
                raise ValueError(f"{source}: edition does not agree with media")
            isbn, gtin = normalize_isbn13(row.get("isbn")), _checked_13(row.get("gtin"), ("471",))
            if (row.get("isbn") and not isbn) or (row.get("gtin") and not gtin):
                raise ValueError(f"{source}: invalid ISBN or GTIN")
            if day := row.get("retailer_publication_date"):
                date.fromisoformat(day)
            normalized[source].append(row | {
                "isbn": isbn, "gtin": gtin, "title_key": normalize_title_key(title),
                "volume": normalize_volume(row.get("volume") or extract_volume(title)),
            })
    return normalized


def parse_ncl_csv(csv_text: str) -> list[dict[str, Any]]:
    """Parse NCL dataset 6730 CSV, accepting both publisher column names."""
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    rows: list[dict[str, Any]] = []
    for raw in reader:
        if (raw.get("出版機構簡稱") or raw.get("出版機構") or "").strip() != "青文":
            continue
        title = normalize_text(raw.get("申請書名") or "")
        if not title:
            continue
        pub_raw = (raw.get("預訂出版日") or "").strip()
        ym_match = re.fullmatch(r"(20\d{2})-(0[1-9]|1[0-2])-\d{2}", pub_raw)
        planned_ym = f"{ym_match.group(1)}-{ym_match.group(2)}" if ym_match else None
        fmt = " ".join((raw.get(k) or "") for k in ("出版形式", "裝訂方式", "資料類型"))
        electronic = re.search(r"電子書|電子出版|數位|E-?PUB|PDF", fmt, re.I) or "電子書" in title
        media = "ebook" if electronic else (
            "print" if re.search(r"紙本|平裝|精裝|線裝|經摺裝", fmt) else None
        )
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


def collect_ncl(
    *,
    ncl_months: int = 3,
    csv_urls: list[str] | None = None,
    load_meta: Callable[[str], str] = fetch_ncl_metadata,
    load_csv_bytes: Callable[[str], bytes] = fetch_ncl_bytes,
) -> dict[str, Any]:
    """Fetch latest N monthly CSVs from data.gov.tw dataset 6730 and filter Chingwin."""
    if csv_urls:
        selected = [{"resourceDownloadUrl": url} for url in dict.fromkeys(csv_urls)]
    else:
        try:
            meta = json.loads(load_meta(NCL_DATASET_URL)).get("result", {})
            dists = {}
            for dist in meta.get("distribution", []):
                if not dist.get("resourceDownloadUrl"):
                    continue
                match = re.search(r"(\d{3})年(\d{1,2})月", dist.get("resourceDescription") or "")
                if match:
                    year, month = map(int, match.groups())
                    if 1 <= month <= 12:
                        ym = f"{year + 1911:04d}-{month:02d}"
                        dists.setdefault(ym, dist | {"dataset_month": ym})
            selected = [dists[ym] for ym in sorted(dists)[-ncl_months:]]
        except (AttributeError, TypeError, ValueError, OSError) as exc:
            return {"status": "error", "error": str(exc), "distributions": [], "records": []}
    records: list[dict[str, Any]] = []
    used: list[dict[str, Any]] = []
    failed = 0
    for dist in selected:
        url = dist["resourceDownloadUrl"]
        desc = dist.get("resourceDescription") or url
        resource = {"description": desc, "url": url, "dataset_month": dist.get("dataset_month")}
        try:
            validate_ncl_url(url)
            csv_text = decode_csv_bytes(load_csv_bytes(url))
            header = next(csv.reader(io.StringIO(csv_text)), [])
            if not {"申請書名", "ISBN"}.issubset(header) or not NCL_PUBLISHER_HEADERS.intersection(header):
                raise ValueError("NCL CSV does not have the expected headers")
            parsed = parse_ncl_csv(csv_text)
        except (Restricted, ValueError, OSError, csv.Error) as exc:
            failed += 1
            used.append(resource | {"status": "error", "error": str(exc)})
            continue
        records.extend(row | {"dataset_month": resource["dataset_month"], "resource_url": url} for row in parsed)
        used.append(resource | {"status": "ok", "chingwin_rows": len(parsed)})

    return {
        "status": "error" if failed and failed == len(used) else "partial" if failed else ("ok" if records else "unverified"),
        "attribution": "國家圖書館：臺灣出版新書預告書訊",
        "latest_listed_month": max((x["dataset_month"] for x in used if x["dataset_month"]), default=None),
        "latest_read_month": max((x["dataset_month"] for x in used if x["dataset_month"] and x["status"] == "ok"), default=None),
        "distributions": used,
        "chingwin_rows_loaded": len(records),
        "records": records,
    }


def monthly_candidates(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a review queue, not a statement of availability or missing catalog books."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unresolved = 0
    for row in records:
        if not row["isbn"] or row["media"] not in {"print", "ebook"}:
            unresolved += 1
            continue
        groups.setdefault((row["isbn"], row["media"]), []).append(row)
    candidates = []
    conflicts = 0
    for (isbn, media), rows in sorted(groups.items()):
        identities = {(row["title_key"], row["volume"], row["edition_type"]) for row in rows}
        if len(identities) != 1 or not rows[0]["title_key"]:
            conflicts += 1
            continue
        row = max(rows, key=lambda r: (r["dataset_month"] or "", r["resource_url"]))
        if len({item["planned_ym"] for item in rows if item["dataset_month"] == row["dataset_month"]}) > 1:
            conflicts += 1
            continue
        candidates.append({
            "isbn": isbn, "media": media, "title": row["title"], "volume": row["volume"],
            "edition_hint": row["edition_type"], "planned_month": row["planned_ym"],
            "dataset_month": row["dataset_month"], "resource_url": row["resource_url"],
        })
    return {"candidate_count": len(candidates), "unresolved_rows": unresolved,
            "conflicting_groups": conflicts, "candidates": candidates}


def match_baseline_row(base: dict[str, Any], candidates: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """Match one baseline row against source candidates without cross-media merging."""
    b_media = media_bucket(base.get("edition_type"), base.get("media_type"))
    b_isbn = normalize_isbn13(base.get("isbn"))
    b_gtin = _checked_13(base.get("gtin"), ("471",))
    b_title_key = normalize_title_key(base.get("title") or "")
    b_vol = normalize_volume(base.get("volume_label") or extract_volume(base.get("title") or ""))
    b_edition = base.get("edition_type") or "standard"
    if b_isbn or b_gtin:
        id_field, value = ("isbn", b_isbn) if b_isbn else ("gtin", b_gtin)
        identified = [c for c in candidates if c.get(id_field) == value]
        isbn_hits = [c for c in identified if c.get("media") == b_media
                     and c.get("edition_type") == b_edition
                     and (id_field != "gtin" or c.get("title_key") == b_title_key)
                     and (c.get("volume") in {None, b_vol} or b_vol is None)]
        if len(isbn_hits) == 1:
            return "hit", isbn_hits[0]
        if len(isbn_hits) > 1:
            return "ambiguous", None
        unresolved_ids = [c for c in identified if c.get("media") in {None, b_media}
                          and (c.get("volume") in {None, b_vol} or b_vol is None)]
        if len(unresolved_ids) == 1:
            return "isbn_unverified", unresolved_ids[0]
        if len(unresolved_ids) > 1:
            return "ambiguous", None

    title_hits = [
        c for c in candidates
        if b_title_key and c.get("media") == b_media
        and c.get("title_key") == b_title_key
        and c.get("volume") == b_vol
        and c.get("edition_type") == b_edition
        and not (b_isbn and c.get("isbn") and c["isbn"] != b_isbn)
        and not (b_gtin and c.get("gtin") and c["gtin"] != b_gtin)
    ]
    if len(title_hits) == 1:
        return "title_only", title_hits[0]
    if len(title_hits) > 1:
        return "ambiguous", None
    return "miss", None


def evaluate_source(
    baseline: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    reference_date: date | None = None,
    date_field: str | None = None,
) -> dict[str, Any]:
    reference_date = reference_date or datetime.now(timezone(timedelta(hours=8))).date()
    # Multiple monthly files may repeat the same record. Do not count identical
    # copies twice, but retain contradictory format/edition/volume evidence.
    candidates = list({
        tuple(row.get(field) for field in ("isbn", "gtin", "title_key", "volume", "media", "edition_type", date_field or "")): row
        for row in candidates
    }.values())
    buckets: dict[str, dict[str, int]] = {}
    offsets: list[int] = []
    pre_listing_14d_total = 0
    pre_listing_14d_hits = 0

    for row in baseline:
        rel_str = str(row.get("release_date") or "")[:10]
        ym = row.get("announcement_month") or (rel_str[:7] if len(rel_str) >= 7 else "unknown")
        eb = edition_bucket(row.get("edition_type"), row.get("media_type"))
        key = f"{ym}|{eb}"
        stats = buckets.setdefault(key, {"baseline": 0, "hit": 0, "isbn_unverified": 0, "title_only": 0,
                                         "miss": 0, "ambiguous": 0})
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
        *(f"{src}: {details['status']}" for src, details in report["sources"].items()),
        "",
        "| Source | Announcement month | Edition | Baseline | ID hit | ID details unverified | Title only | Not seen | Ambiguous | ID hit rate |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    if "monthly_candidates" in report:
        ncl = report["sources"]["ncl"]
        lines[3:3] = [
            f"Latest NCL file listed/read: {ncl.get('latest_listed_month')} / {ncl.get('latest_read_month')}",
            f"Monthly review candidates: {report['monthly_candidates']['candidate_count']} (not checked against AniShelf)",
            "Planned month is not an official release date. No catalog writes.",
        ]
    for src_name, details in report["sources"].items():
        cov = details.get("coverage", {})
        for key, st in cov.items():
            ym, eb = key.split("|", 1)
            lines.append(
                f"| {src_name} | {ym} | {eb} | {st['baseline']} | {st['hit']} | {st['isbn_unverified']} | "
                f"{st['title_only']} | {st['miss']} | {st['ambiguous']} | {st['hit_rate']:.1%} |"
            )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Chingwin coverage benchmark.")
    parser.add_argument("--baseline", required=True, help="Path to baseline JSON export.")
    parser.add_argument("--ncl-months", type=int, default=3, help="Number of latest NCL CSVs to load.")
    parser.add_argument("--ncl-csv", action="append", help="Use a published NCL CSV URL; repeat for multiple months.")
    parser.add_argument("--monthly-candidates", action="store_true", help="Include an NCL-only review queue; no catalog writes.")
    parser.add_argument("--skip-ncl", action="store_true", help="Make the comparison completely offline.")
    parser.add_argument("--retailer-samples", type=Path, help="Local JSON of manually reviewed retailer samples; never fetched.")
    parser.add_argument("--out", required=True, help="Output JSON report path.")
    args = parser.parse_args()

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    if not isinstance(baseline, list) or not baseline or any(not isinstance(row, dict) or not row.get("title") for row in baseline):
        parser.error("--baseline must contain a nonempty JSON array of book rows")
    if not 1 <= args.ncl_months <= 12 or len(args.ncl_csv or []) > 12:
        parser.error("select between 1 and 12 NCL distributions")
    if args.monthly_candidates and (args.retailer_samples or args.skip_ncl):
        parser.error("--monthly-candidates requires NCL data only")
    retailer_samples = read_retailer_samples(args.retailer_samples) if args.retailer_samples else {}
    ncl = ({"status": "not_run", "records": []} if args.skip_ncl
           else collect_ncl(ncl_months=args.ncl_months, csv_urls=args.ncl_csv))
    ncl_eval = evaluate_source(baseline, ncl["records"]) if ncl["records"] else {}

    report = {
        "mode": "read_only",
        "actual_catalog_write": False,
        "attribution": {"ncl": "國家圖書館：臺灣出版新書預告書訊（政府資料開放授權條款－第1版）",
                        "dataset": NCL_DATASET_PAGE, "license": NCL_LICENSE_URL},
        "note": "Not seen means missing from the reviewed sample, not absent from a catalog. Order, coverage, and listing lead/lag are not measured.",
        "baseline": {
            "total": len(baseline),
            "day_precision": sum(1 for r in baseline if r.get("release_precision") == "day"),
            "month_precision": sum(1 for r in baseline if r.get("release_precision") == "month"),
        },
        "sources": {
            "ncl": {
                k: v for k, v in ncl.items() if k != "records"
            } | ncl_eval,
        },
    }
    if args.monthly_candidates:
        report["monthly_candidates"] = monthly_candidates(ncl["records"])
    for source, records in retailer_samples.items():
        report["sources"][source] = {
            "status": "manual_evidence_only",
            "automatic_access_verified": False,
            "evidence_records": len(records),
        } | evaluate_source(baseline, records, date_field="retailer_publication_date")
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(format_markdown_summary(report))


if __name__ == "__main__":
    main()
