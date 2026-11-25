"""Offline acceptance report for a prospective Chingwin daily feed."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .chingwin_coverage import (
    SPECIAL_EDITIONS, _checked_13, normalize_isbn13, normalize_title_key, normalize_volume,
)
from .models import extract_volume

TAIPEI = timezone(timedelta(hours=8))
GROUPS = {"print_standard", "print_special", "ebook"}
PUBLISHERS = {"青文", "青文出版", "青文出版社", "青文出版社股份有限公司"}
MIN_CHECK_DAYS = 7
MIN_SAMPLES = 5
MAX_LAG_HOURS = 48


def _identity(row: dict[str, Any]) -> tuple[str, ...] | None:
    publisher = row.get("publisher")
    media = row.get("media")
    edition = row.get("edition")
    raw_isbn, raw_gtin = row.get("isbn"), row.get("gtin")
    isbn, gtin = normalize_isbn13(raw_isbn), _checked_13(raw_gtin, ("471",))
    if (bool(raw_isbn) and not isbn) or (bool(raw_gtin) and not gtin):
        return None
    raw_title = row.get("title") or ""
    title = normalize_title_key(raw_title)
    vol = normalize_volume(row.get("volume"))
    title_vol = normalize_volume(extract_volume(raw_title))
    if vol and title_vol and vol != title_vol:
        return None
    known_format = (media == "ebook" and edition == "digital") or (
        media == "print" and edition in SPECIAL_EDITIONS | {"standard"}
    )
    if publisher not in PUBLISHERS or not known_format or not title or not (isbn or gtin):
        return None
    return ("isbn" if isbn else "gtin", isbn or gtin, media, edition, title, vol or title_vol or "")


def _group(identity: tuple[str, ...]) -> str:
    _, _, media, edition, *_ = identity
    if media == "ebook":
        return "ebook"
    return "print_standard" if edition == "standard" else "print_special"


def _observed(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("first_seen_at requires a timezone")
    return stamp.astimezone(TAIPEI)


def review_feed(bundle: dict[str, Any]) -> dict[str, Any]:
    """Treat all claims as operator-provided evidence, never as granted access."""
    permission = bundle.get("permission") or {}
    scopes = set(permission.get("formats") or [])
    if scopes - GROUPS:
        raise ValueError("unknown permission format")
    permission_recorded = (
        permission.get("provider_confirmed") is True
        and bool(str(permission.get("evidence_reference") or "").strip())
        and permission.get("allows_automated_daily_read") is True
        and bool(scopes)
    )
    checks: dict[date, bool] = {}
    for row in bundle.get("daily_checks", []):
        day = date.fromisoformat(row["date"])
        status = row["status"]
        if status not in {"ok", "unavailable", "blocked"}:
            raise ValueError("unknown daily check status")
        if day in checks:
            raise ValueError("duplicate daily check date")
        checks[day] = status == "ok"
    days = sorted(checks)
    stable = (
        len(days) >= MIN_CHECK_DAYS and all(checks.values())
        and (days[-1] - days[0]).days + 1 == len(days)
    )
    counts = {key: {"samples": 0, "on_time": 0, "late": 0, "unverified": 0} for key in sorted(scopes)}
    used: set[tuple[str, ...]] = set()
    invalid_baseline = 0
    for row in bundle.get("samples", []):
        official = row["official"]
        identity = _identity(official)
        if not identity:
            invalid_baseline += 1
            continue
        group = _group(identity)
        if group not in counts:
            continue
        # No repeated ID for the same delivery format and exact edition as independent evidence.
        key = identity[:4]
        if key in used:
            raise ValueError("duplicate official sample identity")
        used.add(key)
        stats = counts[group]
        stats["samples"] += 1
        feed = row.get("feed")
        conflicting_barcode = feed and official.get("gtin") and feed.get("gtin") and (
            _checked_13(official["gtin"], ("471",)) != _checked_13(feed["gtin"], ("471",))
        )
        if not feed or _identity(feed) != identity or conflicting_barcode:
            stats["unverified"] += 1
            continue
        source_seen = _observed(official["first_seen_at"])
        feed_seen = _observed(feed["first_seen_at"])
        if not all(checks.get(stamp.date()) for stamp in (source_seen, feed_seen)):
            stats["unverified"] += 1
        elif feed_seen - source_seen <= timedelta(hours=MAX_LAG_HOURS):
            stats["on_time"] += 1
        else:
            stats["late"] += 1
    # Conservative initial pilot: for each claimed format, require at least
    # five distinct observed samples and 80% same-edition discovery within 48h.
    has_coverage = bool(counts) and not invalid_baseline and all(
        x["samples"] >= MIN_SAMPLES and 5 * x["on_time"] >= 4 * x["samples"]
        for x in counts.values()
    )
    if not permission_recorded:
        status = "permission_not_recorded"
    elif not stable or not has_coverage:
        status = "insufficient_evidence"
    else:
        status = "candidate_for_manual_shadow_review"
    return {
        "status": status,
        "permission_claim_recorded": permission_recorded,
        "permission_independently_verified": False,
        "daily_checks": len(days),
        "consecutive_successful_checks": stable,
        "format_results": counts,
        "invalid_baseline": invalid_baseline,
        "network_requested": False,
        "catalog_write_allowed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="Local reviewed JSON observations")
    args = parser.parse_args()
    try:
        bundle = json.loads(args.evidence.read_text(encoding="utf-8-sig"))
        report = review_feed(bundle)
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        parser.error(f"無法讀取或驗證驗收資料（{type(exc).__name__}）")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
