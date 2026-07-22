from __future__ import annotations

import json
import os
import time
from datetime import date
from typing import Any

from .catalog_sync import CatalogSyncClient
from .crawler import get_job, run_backfill
from .repository import get_backfill_progress


MIN_HISTORY_YEAR = 1990
EXPECTED_SEGMENTS = {
    "chingwin": ("comic", "novel"),
    "spp": ("catalog",),
    "tongli": ("catalog",),
}


def current_history_year() -> int | None:
    """Pick the newest calendar year whose three expandable catalogs are unfinished."""
    progress = {
        source: get_backfill_progress(source) for source in EXPECTED_SEGMENTS
    }
    for year in range(date.today().year - 1, MIN_HISTORY_YEAR - 1, -1):
        complete = all(
            progress[source]
            .get(f"history_v2_{year}_{segment}", {})
            .get("completed", False)
            for source, segments in EXPECTED_SEGMENTS.items()
            for segment in segments
        )
        if not complete:
            return year
    return None


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "same": int(result["same"]),
        "local_only": len(result["local_only"]),
        "remote_only": len(result["remote_only"]),
        "different": len(result["different"]),
    }


def run_history_session(mode: str) -> dict[str, Any]:
    """Reconcile, crawl with durable page checkpoints, then upload catalog-only changes."""
    if mode not in {"30m", "60m", "year", "continuous"}:
        raise ValueError("History mode must be 30m, 60m, year, or continuous")
    client = CatalogSyncClient()
    print("[1/4] Pulling newer cloud catalog rows into the local database...", flush=True)
    pulled = client.pull()
    before = _summary(client.compare())
    print(f"Pre-crawl difference: {json.dumps(before, ensure_ascii=False)}", flush=True)
    reconciled = client.push_manifest_differences()

    deadline = (
        time.monotonic() + 30 * 60
        if mode == "30m"
        else time.monotonic() + 60 * 60
        if mode == "60m"
        else None
    )
    jobs: list[int] = []
    interrupted = False
    try:
        while True:
            target_year = current_history_year()
            if target_year is None:
                print("All configured history years are complete.", flush=True)
                break
            os.environ["ANISHELF_HISTORY_YEAR"] = str(target_year)
            print(f"[2/4] Crawling calendar year {target_year}...", flush=True)
            job_id = run_backfill("history", deadline_monotonic=deadline)
            jobs.append(job_id)
            job = get_job(job_id) or {}
            print(
                f"History job #{job_id}: {job.get('status', 'unknown')} "
                f"inserted={job.get('inserted_count', 0)} "
                f"updated={job.get('updated_count', 0)} "
                f"errors={job.get('error_count', 0)}",
                flush=True,
            )
            if mode == "year":
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            if job.get("status") in {"paused", "partial", "failed"}:
                break
            if mode != "continuous" and current_history_year() == target_year:
                break
    except KeyboardInterrupt:
        interrupted = True
        print("Manual stop received; the last committed checkpoint is preserved.", flush=True)
    finally:
        os.environ.pop("ANISHELF_HISTORY_YEAR", None)

    print("[3/4] Uploading local crawler changes to the cloud catalog...", flush=True)
    pushed = client.push()
    print("[4/4] Verifying the local/cloud catalog manifest...", flush=True)
    after = _summary(client.compare())
    result = {
        "mode": mode,
        "interrupted": interrupted,
        "jobs": jobs,
        "pulled": pulled,
        "reconciled": reconciled,
        "pushed": pushed,
        "before": before,
        "after": after,
        "next_year": current_history_year(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result
