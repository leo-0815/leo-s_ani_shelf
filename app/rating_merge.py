"""Protect authoritative/locked grading and reject stale peer confirmations."""
from .release_dates import parse_checked_at

FIELDS = ("content_rating", "rating_raw", "rating_source", "rating_confidence",
          "rating_checked_at", "rating_parser_version")


def merge_rating_fields(existing: dict, incoming: dict) -> dict:
    result = dict(incoming)
    old_time = parse_checked_at(existing.get("rating_checked_at"))
    new_time = parse_checked_at(incoming.get("rating_checked_at"))
    preserve = bool(existing.get("rating_locked"))
    preserve |= incoming.get("content_rating") in {None, "unknown"} and existing.get("content_rating") not in {None, "unknown"}
    preserve |= bool(old_time and (not new_time or new_time < old_time))
    # Old clients without provenance cannot downgrade an explicit adult grade.
    preserve |= existing.get("content_rating") == "restricted_18" and incoming.get("content_rating") == "general" and (not new_time or not old_time or new_time <= old_time)
    if preserve:
        for field in FIELDS:
            result[field] = existing.get(field)
    return result


def peer_rating_time(payload: dict):
    from datetime import datetime, timedelta
    value = parse_checked_at(payload.get("rating_checked_at"))
    if value and value > datetime.utcnow() + timedelta(minutes=5):
        raise ValueError("rating_checked_at cannot be in the future")
    version = payload.get("rating_parser_version")
    if version is not None and (not isinstance(version, str) or len(version) > 40):
        raise ValueError("Invalid rating parser version")
    if value and not version:
        raise ValueError("Rating confirmation requires parser version")
    return value
