from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date
from typing import Any, Iterable
from urllib.parse import urlparse

from .config import Settings, get_settings
from .models import CONTENT_RATINGS, BookRecord, catalog_sync_hash, normalize_text
from .repository import (
    get_catalog_peer_state,
    catalog_rows_by_keys,
    local_catalog_manifest,
    pending_catalog_changes,
    prune_catalog_changes,
    save_catalog_peer_state,
    upsert_book,
)


PROTOCOL_VERSION = 1
UPLOAD_BATCH_SIZE = 100
UPLOAD_FIELDS = (
    "publisher_code",
    "source_key",
    "title",
    "media_type",
    "source_url",
    "author",
    "isbn",
    "cover_url",
    "list_price",
    "release_date",
    "release_precision",
    "release_status",
    "edition_type",
    "volume_label",
    "series_title",
    "content_rating",
    "rating_raw",
    "rating_source",
    "rating_confidence",
)


def _optional_text(value: Any, maximum: int) -> str | None:
    text = normalize_text(str(value or ""))[:maximum]
    return text or None


def _choice(value: Any, allowed: set[str], default: str = "unknown") -> str:
    result = str(value or default).strip().lower()
    return result if result in allowed else default


def _date(value: Any) -> date | None:
    if value in {None, ""}:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def record_from_peer(payload: dict[str, Any]) -> BookRecord:
    publisher_code = _optional_text(payload.get("publisher_code"), 100)
    source_key = _optional_text(payload.get("source_key"), 190)
    title = _optional_text(payload.get("title"), 500)
    source_url = _optional_text(payload.get("source_url"), 1000)
    if not publisher_code or not source_key or not title or not source_url:
        raise ValueError("Peer record is missing publisher_code, source_key, title, or source_url")
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Peer source_url must be an http(s) URL")
    price = payload.get("list_price")
    return BookRecord(
        publisher_code=publisher_code,
        source_key=source_key,
        title=title,
        media_type=_choice(payload.get("media_type"), {"manga", "novel", "unknown"}),
        source_url=source_url,
        author=_optional_text(payload.get("author"), 500),
        isbn=_optional_text(payload.get("isbn"), 32),
        cover_url=_optional_text(payload.get("cover_url"), 1000),
        list_price=int(price) if price not in {None, ""} else None,
        release_date=_date(payload.get("release_date")),
        release_precision=_choice(
            payload.get("release_precision"), {"unknown", "day", "month", "year"}
        ),
        release_status=_choice(
            payload.get("release_status"), {"unknown", "scheduled", "available"}
        ),
        edition_type=_optional_text(payload.get("edition_type"), 40) or "standard",
        volume_label=_optional_text(payload.get("volume_label"), 80),
        series_title=_optional_text(payload.get("series_title"), 500),
        content_rating=_choice(payload.get("content_rating"), set(CONTENT_RATINGS)),
        rating_raw=_optional_text(payload.get("rating_raw"), 100),
        rating_source=_choice(
            payload.get("rating_source"),
            {"unknown", "publisher", "category", "heuristic", "manual"},
        ),
        rating_confidence=min(max(int(payload.get("rating_confidence") or 0), 0), 100),
    )


class CatalogSyncClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.catalog_sync_configured:
            raise ValueError(
                "Catalog sync is not configured. Check ANISHELF_CATALOG_SYNC_TOKEN and URL."
            )

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None
        headers = {
            "Authorization": f"Bearer {self.settings.catalog_sync_token}",
            "Accept": "application/json",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.settings.catalog_sync_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "")
            except Exception:
                detail = ""
            raise RuntimeError(f"Catalog sync HTTP {exc.code}: {detail or exc.reason}") from exc
        if int(result.get("protocol_version", 0)) != PROTOCOL_VERSION:
            raise RuntimeError("Unsupported catalog sync protocol")
        return result

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/api/catalog-sync/status")

    def remote_manifest(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = self._request(
                "GET", f"/api/catalog-sync/manifest?after_id={cursor}&limit=1000"
            )
            items.extend(page.get("items", []))
            cursor = int(page.get("next_after_id", cursor))
            if not page.get("has_more"):
                return items

    def local_manifest(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = 0
        while True:
            page = local_catalog_manifest(cursor, 1000)
            items.extend(page["items"])
            cursor = int(page["next_after_id"])
            if not page["has_more"]:
                return items

    def compare(self) -> dict[str, Any]:
        return compare_manifests(self.local_manifest(), self.remote_manifest())

    def pull(self, *, force_snapshot: bool = False) -> dict[str, int]:
        state = get_catalog_peer_state("cloud")
        first_sync = state.get("last_verified_at") is None
        counts = {"inserted": 0, "updated": 0, "unchanged": 0, "received": 0}
        cursor = int(state.get("last_pulled_change_id") or 0)
        try:
            if force_snapshot or first_sync:
                cursor = self._pull_snapshot(counts)
            cursor = self._pull_changes(cursor, counts)
            save_catalog_peer_state(
                "cloud", last_pulled_change_id=cursor, verified=True, error=None
            )
            return counts
        except Exception as exc:
            save_catalog_peer_state("cloud", error=str(exc))
            raise

    def _pull_snapshot(self, counts: dict[str, int]) -> int:
        after_id = 0
        snapshot_change_id = 0
        while True:
            page = self._request(
                "GET", f"/api/catalog-sync/books?after_id={after_id}&limit=500"
            )
            if after_id == 0:
                snapshot_change_id = int(page.get("snapshot_change_id", 0))
            self._apply_peer_items(page.get("items", []), counts)
            after_id = int(page.get("next_after_id", after_id))
            if not page.get("has_more"):
                return snapshot_change_id

    def _pull_changes(self, cursor: int, counts: dict[str, int]) -> int:
        while True:
            page = self._request(
                "GET", f"/api/catalog-sync/changes?after={cursor}&limit=500"
            )
            self._apply_peer_items(page.get("items", []), counts)
            cursor = int(page.get("next_after_change_id", cursor))
            if not page.get("has_more"):
                return cursor

    @staticmethod
    def _apply_peer_items(items: Iterable[dict[str, Any]], counts: dict[str, int]) -> None:
        for item in items:
            outcome = upsert_book(
                record_from_peer(item),
                change_origin="cloud_pull",
                source_hash_override=str(item.get("source_hash") or ""),
            )
            counts[outcome] = counts.get(outcome, 0) + 1
            counts["received"] += 1

    def push(self) -> dict[str, int]:
        state = get_catalog_peer_state("cloud")
        cursor = int(state.get("last_pushed_change_id") or 0)
        remote_hashes = {
            (str(item["publisher_code"]), str(item["source_key"])): str(
                item.get("sync_hash") or item.get("source_hash") or ""
            )
            for item in self.remote_manifest()
        }
        totals = {
            "scanned": 0,
            "content_unchanged": 0,
            "uploaded": 0,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
        }
        try:
            while True:
                page = pending_catalog_changes(cursor, 500)
                rows = page["items"]
                totals["scanned"] += len(rows)
                outgoing_by_key: dict[tuple[str, str], dict[str, Any]] = {}
                for row in rows:
                    if row.get("change_origin") in {"cloud_pull", "sync_upload"}:
                        continue
                    if row.get("media_type") not in {"manga", "novel"}:
                        continue
                    clean = self._clean_outgoing(row)
                    identity = (str(row["publisher_code"]), str(row["source_key"]))
                    if remote_hashes.get(identity) == catalog_sync_hash(clean):
                        totals["content_unchanged"] += 1
                        continue
                    outgoing_by_key[identity] = clean
                outgoing = list(outgoing_by_key.values())
                for offset in range(0, len(outgoing), UPLOAD_BATCH_SIZE):
                    result = self._request(
                        "POST",
                        "/api/catalog-sync/books",
                        {"items": outgoing[offset : offset + UPLOAD_BATCH_SIZE]},
                    )
                    totals["uploaded"] += int(result.get("accepted", 0))
                    for key in ("inserted", "updated", "unchanged"):
                        totals[key] += int(result.get(key, 0))
                    for item in outgoing[offset : offset + UPLOAD_BATCH_SIZE]:
                        remote_hashes[(item["publisher_code"], item["source_key"])] = (
                            catalog_sync_hash(item)
                        )
                cursor = int(page["last_scanned_change_id"])
                save_catalog_peer_state("cloud", last_pushed_change_id=cursor, error=None)
                if not page["has_more"]:
                    totals["pruned"] = prune_catalog_changes(cursor)
                    return totals
        except Exception as exc:
            save_catalog_peer_state("cloud", error=str(exc))
            raise

    @staticmethod
    def _clean_outgoing(row: dict[str, Any]) -> dict[str, Any]:
        clean = {field: row.get(field) for field in UPLOAD_FIELDS}
        if clean.get("rating_source") not in {"unknown", "publisher", "category"}:
            clean["rating_raw"] = None
            clean["rating_source"] = "unknown"
            clean["rating_confidence"] = 0
        return clean

    def push_manifest_differences(self) -> dict[str, int]:
        """Upload local-only/richer rows after cloud-first reconciliation."""
        difference = self.compare()
        keys = [tuple(key) for key in difference["local_only"] + difference["different"]]
        rows = [self._clean_outgoing(row) for row in catalog_rows_by_keys(keys)]
        totals = {"uploaded": 0, "inserted": 0, "updated": 0, "unchanged": 0}
        for offset in range(0, len(rows), UPLOAD_BATCH_SIZE):
            result = self._request(
                "POST",
                "/api/catalog-sync/books",
                {"items": rows[offset : offset + UPLOAD_BATCH_SIZE]},
            )
            totals["uploaded"] += int(result.get("accepted", 0))
            for key in ("inserted", "updated", "unchanged"):
                totals[key] += int(result.get(key, 0))
        return totals


def compare_manifests(
    local_items: Iterable[dict[str, Any]], remote_items: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    def keyed(items: Iterable[dict[str, Any]]) -> dict[tuple[str, str], str]:
        return {
            (str(item.get("publisher_code") or ""), str(item.get("source_key") or "")): str(
                item.get("sync_hash") or item.get("source_hash") or ""
            )
            for item in items
        }

    local = keyed(local_items)
    remote = keyed(remote_items)
    both = set(local) & set(remote)
    different = sorted(key for key in both if local[key] != remote[key])
    return {
        "same": len(both) - len(different),
        "local_only": [list(key) for key in sorted(set(local) - set(remote))],
        "remote_only": [list(key) for key in sorted(set(remote) - set(local))],
        "different": [list(key) for key in different],
    }
