from __future__ import annotations

import argparse
import json
import os
from typing import Any
from urllib.request import Request, urlopen

from .catalog_sync import compare_catalog_manifests
from .repository import catalog_manifest


def _local_manifest() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = 0
    while True:
        page = catalog_manifest(cursor, 1000)
        items.extend(page["items"])
        cursor = int(page["next_after_id"])
        if not page["has_more"]:
            return items


def _remote_manifest(base_url: str, token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = 0
    while True:
        url = f"{base_url.rstrip('/')}/api/catalog-sync/manifest?after_id={cursor}&limit=1000"
        request = Request(url, headers={"Authorization": f"Bearer {token}"})
        with urlopen(request, timeout=60) as response:  # noqa: S310 - operator-supplied URL
            page = json.loads(response.read().decode("utf-8"))
        if int(page.get("protocol_version", 0)) != 1:
            raise RuntimeError("Unsupported catalog sync protocol")
        items.extend(page.get("items", []))
        cursor = int(page.get("next_after_id", cursor))
        if not page.get("has_more"):
            return items


def compare_remote_catalog(base_url: str, token: str) -> dict[str, Any]:
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("Catalog sync URL must start with http:// or https://")
    if len(token) < 32:
        raise ValueError("ANISHELF_CATALOG_SYNC_TOKEN must contain at least 32 characters")
    return compare_catalog_manifests(_local_manifest(), _remote_manifest(base_url, token))


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare the local and cloud AniShelf catalogs")
    parser.add_argument(
        "--base-url",
        default=os.getenv("ANISHELF_CATALOG_SYNC_URL", "https://anishelf-wmcu.onrender.com"),
    )
    args = parser.parse_args()
    token = os.getenv("ANISHELF_CATALOG_SYNC_TOKEN", "").strip()
    result = compare_remote_catalog(args.base_url, token)
    summary = {
        "same": result["same"],
        "local_only": len(result["local_only"]),
        "remote_only": len(result["remote_only"]),
        "different": len(result["different"]),
        "examples": {
            "local_only": result["local_only"][:20],
            "remote_only": result["remote_only"][:20],
            "different": result["different"][:20],
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
