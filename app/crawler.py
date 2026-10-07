from __future__ import annotations

import threading
import traceback
from datetime import datetime, timedelta
from typing import Any, Iterable

from .db import transaction
from .repository import (
    get_sync_state,
    known_source_keys,
    max_source_release_date,
    save_sync_state,
    upsert_book,
)
from .sources import (
    ChingWinSource,
    EgMangaSource,
    KadokawaSource,
    SppSource,
    TohanSource,
    TongLiSource,
)


SOURCES = {
    "tohan": TohanSource,
    "chingwin": ChingWinSource,
    "kadokawa": KadokawaSource,
    "tongli": TongLiSource,
    "spp": SppSource,
    "egmanga": EgMangaSource,
}
_job_lock = threading.Lock()


def create_job(
    source_code: str,
    mode: str = "incremental",
    force: bool = True,
) -> int | None:
    if source_code != "all" and source_code not in SOURCES:
        raise ValueError("尚未支援此出版社")
    if mode not in {"incremental", "backfill"}:
        raise ValueError("無效的更新模式")
    if not force and mode == "incremental" and _updated_recently(source_code, hours=6):
        return None
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO crawl_jobs (source_code, status) VALUES (%s, 'queued')",
                (source_code,),
            )
            job_id = int(cursor.lastrowid)
    thread = threading.Thread(target=_run_job, args=(job_id, source_code, mode), daemon=True)
    thread.start()
    return job_id


def _updated_recently(source_code: str, hours: int) -> bool:
    threshold = datetime.now() - timedelta(hours=hours)
    with transaction() as connection:
        with connection.cursor() as cursor:
            if source_code == "all":
                cursor.execute(
                    "SELECT finished_at FROM crawl_jobs WHERE source_code = 'all' "
                    "AND status IN ('completed', 'partial') ORDER BY id DESC LIMIT 1"
                )
            else:
                cursor.execute(
                    "SELECT finished_at FROM crawl_jobs WHERE source_code = %s "
                    "AND status IN ('completed', 'partial') ORDER BY id DESC LIMIT 1",
                    (source_code,),
                )
            row = cursor.fetchone()
    return bool(row and row["finished_at"] and row["finished_at"] >= threshold)


def run_backfill(source_code: str = "all") -> int:
    """Run the one-time historical fill in the foreground so it cannot die with a CLI process."""
    if source_code != "all" and source_code not in SOURCES:
        raise ValueError("尚未支援此出版社")
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO crawl_jobs (source_code, status) VALUES (%s, 'queued')",
                (f"backfill:{source_code}",),
            )
            job_id = int(cursor.lastrowid)
    _run_job(job_id, source_code, "backfill")
    return job_id


def run_incremental(source_code: str = "all") -> int:
    """Run an incremental refresh in the foreground, mainly for diagnostics/CLI use."""
    if source_code != "all" and source_code not in SOURCES:
        raise ValueError("尚未支援此出版社")
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO crawl_jobs (source_code, status) VALUES (%s, 'queued')",
                (source_code,),
            )
            job_id = int(cursor.lastrowid)
    _run_job(job_id, source_code, "incremental")
    return job_id


def run_incremental_sources(source_codes: Iterable[str]) -> int:
    """Run one foreground job for an explicit group of scheduled sources."""
    codes = tuple(dict.fromkeys(str(code).strip() for code in source_codes if str(code).strip()))
    if not codes or any(code not in SOURCES for code in codes):
        raise ValueError("排程包含尚未支援的出版社")
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO crawl_jobs (source_code, status) VALUES (%s, 'queued')",
                ("scheduled",),
            )
            job_id = int(cursor.lastrowid)
    _run_job(job_id, "scheduled", "incremental", codes)
    return job_id


def _run_job(
    job_id: int,
    source_code: str,
    mode: str = "incremental",
    source_codes: Iterable[str] | None = None,
) -> None:
    if not _job_lock.acquire(blocking=False):
        _finish_job(job_id, "failed", message="已有另一個更新工作正在執行")
        return
    totals = {"discovered": 0, "inserted": 0, "updated": 0, "skipped": 0, "errors": 0}
    messages: list[str] = []
    try:
        _start_job(job_id)
        codes = list(source_codes) if source_codes is not None else (
            list(SOURCES) if source_code == "all" else [source_code]
        )
        for code in codes:
            source = SOURCES[code]()
            try:
                if mode == "backfill":
                    print(f"大型回填：開始 {source.name}…", flush=True)
                state = get_sync_state(code)
                known_keys = known_source_keys(code)
                source_errors = 0
                source_discovered = 0
                if hasattr(source, "collect_batches"):
                    batches = source.collect_batches(
                        known_keys=known_keys,
                        cursor_value=state.get("cursor_value"),
                        backfill=mode == "backfill",
                    )
                    for records, checkpoint in batches:
                        source_discovered += len(records)
                        totals["discovered"] += len(records)
                        batch_write_errors = _write_records(
                            source,
                            records,
                            totals,
                            messages,
                        )
                        source_errors += batch_write_errors
                        if batch_write_errors:
                            raise RuntimeError(
                                f"{source.name} 本批有 {batch_write_errors} 筆寫入失敗，"
                                "保留目前斷點供下次重試"
                            )
                        batch_fetch_errors = int((checkpoint or {}).get("error_count", 0))
                        if mode == "backfill" and batch_fetch_errors:
                            raise RuntimeError(
                                f"{source.name} 本批有 {batch_fetch_errors} 個頁面下載失敗，"
                                "保留目前斷點供下次重試"
                            )
                        source.commit_batch(checkpoint)
                        _update_job_progress(job_id, totals)
                else:
                    records = source.collect(
                        known_keys=known_keys,
                        cursor_value=state.get("cursor_value"),
                        backfill=mode == "backfill",
                    )
                    source_discovered = len(records)
                    totals["discovered"] += len(records)
                    source_errors += _write_records(source, records, totals, messages)
                fetch_errors = len(getattr(source, "errors", []))
                skipped = int(getattr(source, "skipped_count", 0))
                totals["errors"] += fetch_errors
                totals["skipped"] += skipped
                if fetch_errors:
                    messages.extend(
                        f"{source.name} 下載失敗：{message}"
                        for message in getattr(source, "errors", [])[-10:]
                    )
                if source_errors:
                    _mark_source(code, f"有 {source_errors} 筆資料寫入失敗")
                elif fetch_errors:
                    _mark_source(code, f"有 {fetch_errors} 個商品頁下載失敗")
                else:
                    save_sync_state(
                        code,
                        getattr(source, "latest_cursor", None),
                        max_source_release_date(code) or state.get("cursor_date"),
                        mode,
                    )
                    _mark_source(code, None)
                if mode == "backfill":
                    print(
                        f"大型回填：{source.name} 完成，發現 {source_discovered} 筆新資料",
                        flush=True,
                    )
            except Exception as exc:
                totals["errors"] += 1
                message = f"{source.name}：{exc}"
                messages.append(message)
                _mark_source(code, message)
                if mode == "backfill":
                    print(f"大型回填：{message}", flush=True)
            finally:
                if mode == "incremental":
                    _refresh_known_releases(source, totals, messages)
        status = "completed" if totals["errors"] == 0 else "partial"
        _finish_job(job_id, status, totals, "\n".join(messages[-10:]))
    except Exception as exc:
        _finish_job(job_id, "failed", totals, f"{exc}\n{traceback.format_exc(limit=3)}")
    finally:
        _job_lock.release()


def _refresh_known_releases(source: Any, totals: dict[str, int], messages: list[str]) -> None:
    from .release_dates import refresh_source
    try:
        result = refresh_source(source)
        totals["discovered"] += result["checked"]
        totals["updated"] += result["updated"]
        totals["errors"] += len(result["errors"])
        summary = f"{source.name} 日期回查：{result['checked']} 本、更新 {result['updated']} 本、日期未提供 {result['unconfirmed']} 本、失敗 {len(result['errors'])} 本"
        print(summary, flush=True)
        messages.append(summary)
        if result["errors"]:
            messages.extend(result["errors"][-3:])
            _mark_source(source.code, "\n".join(result["errors"][-3:]))
    except Exception as exc:
        totals["errors"] += 1
        messages.append(f"{source.name} 日期回查失敗：{exc}")
        _mark_source(source.code, str(exc))


def _write_records(
    source: Any,
    records: list[Any],
    totals: dict[str, int],
    messages: list[str],
) -> int:
    errors = 0
    for record in records:
        try:
            outcome = upsert_book(record)
            if outcome == "inserted":
                totals["inserted"] += 1
            elif outcome == "updated":
                totals["updated"] += 1
        except Exception as exc:
            totals["errors"] += 1
            errors += 1
            messages.append(
                f"{source.name} 寫入失敗 "
                f"({getattr(record, 'source_key', '?')} / "
                f"{getattr(record, 'title', '?')})：{exc}"
            )
    return errors


def _update_job_progress(job_id: int, totals: dict[str, int]) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE crawl_jobs SET discovered_count = %s, inserted_count = %s, "
                "updated_count = %s, skipped_count = %s, error_count = %s "
                "WHERE id = %s",
                (
                    totals["discovered"],
                    totals["inserted"],
                    totals["updated"],
                    totals["skipped"],
                    totals["errors"],
                    job_id,
                ),
            )


def _start_job(job_id: int) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE crawl_jobs SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = %s",
                (job_id,),
            )


def _finish_job(
    job_id: int,
    status: str,
    totals: dict[str, int] | None = None,
    message: str = "",
) -> None:
    totals = totals or {
        "discovered": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 1,
    }
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE crawl_jobs SET status = %s, discovered_count = %s, inserted_count = %s, "
                "updated_count = %s, skipped_count = %s, error_count = %s, message = %s, "
                "finished_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                (
                    status,
                    totals["discovered"],
                    totals["inserted"],
                    totals["updated"],
                    totals["skipped"],
                    totals["errors"],
                    message[:10000],
                    job_id,
                ),
            )


def _mark_source(code: str, error: str | None) -> None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            if error:
                cursor.execute(
                    "UPDATE publishers SET last_error = %s WHERE code = %s",
                    (error[:5000], code),
                )
            else:
                cursor.execute(
                    "UPDATE publishers SET last_success_at = CURRENT_TIMESTAMP, last_error = NULL WHERE code = %s",
                    (code,),
                )


def get_job(job_id: int | None = None) -> dict[str, Any] | None:
    with transaction() as connection:
        with connection.cursor() as cursor:
            if job_id is None:
                cursor.execute("SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1")
            else:
                cursor.execute("SELECT * FROM crawl_jobs WHERE id = %s", (job_id,))
            row = cursor.fetchone()
    if not row:
        return None
    for key, value in list(row.items()):
        if isinstance(value, datetime):
            row[key] = value.isoformat()
    return row
