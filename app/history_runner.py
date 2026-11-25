from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .catalog_sync import CatalogSyncClient
from .crawler import HISTORY_SOURCES, get_job, run_backfill
from .repository import get_backfill_progress


MIN_HISTORY_YEAR = 1990
REPORT_DIRECTORY = Path(__file__).resolve().parent.parent / "logs" / "history-runs"
EXPECTED_SEGMENTS = {
    "chingwin": ("comic", "novel"),
    "spp": ("catalog",),
    "tongli": ("catalog",),
}


def _next_year(source: str, progress: dict[str, Any]) -> int | None:
    return next((
        year for year in range(date.today().year - 1, MIN_HISTORY_YEAR - 1, -1)
        if not all(progress.get(f"history_v2_{year}_{segment}", {}).get("completed")
                   for segment in EXPECTED_SEGMENTS[source])
    ), None)


def _latest_year(progress: dict[str, Any]) -> int | None:
    return max((year for source in EXPECTED_SEGMENTS
                if (year := _next_year(source, progress[source])) is not None), default=None)


def current_history_year() -> int | None:
    return _latest_year({source: get_backfill_progress(source) for source in EXPECTED_SEGMENTS})


def _checkpoint_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for source in HISTORY_SOURCES:
        try:
            progress = get_backfill_progress(source)
            snapshot[source] = {"segments": {
                name: {"next_page": entry["next_page"], "completed": entry["completed"]}
                for name, entry in progress.items()
            }}
        except Exception as exc:
            snapshot[source] = {"unavailable": type(exc).__name__}
    return snapshot


def _save_report(result: dict[str, Any]) -> Path:
    REPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORT_DIRECTORY / f"history-{stamp}-{uuid4().hex[:8]}.json"
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "same": int(result["same"]),
        "local_only": len(result["local_only"]),
        "remote_only": len(result["remote_only"]),
        "different": len(result["different"]),
    }


def run_history_session(mode: str) -> dict[str, Any]:
    """Advance each source independently, then sync and report durable checkpoints."""
    if mode not in {"30m", "60m", "year", "continuous"}:
        raise ValueError("History mode must be 30m, 60m, year, or continuous")
    report: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "checkpoints_before": _checkpoint_snapshot(),
    }
    result: dict[str, Any] = {"mode": mode, "jobs": [], "attempts": [], "interrupted": False,
                              "stop_reason": "completed"}
    report["result"] = result
    previous_year = os.environ.get("ANISHELF_HISTORY_YEAR")
    stage = "initial_sync"
    try:
        client = CatalogSyncClient()
        print("[1/4] 正在同步雲端書目至本機…", flush=True)
        result["pulled"] = client.pull()
        result["before"] = _summary(client.compare())
        result["reconciled"] = client.push_manifest_differences()
        stage = "crawl"
        deadline = time.monotonic() + {"30m": 1800, "60m": 3600}[mode] if mode in {"30m", "60m"} else None
        try:
            _run_sources(mode, deadline, result)
        except KeyboardInterrupt:
            result["interrupted"] = True
            result["stop_reason"] = "manual_stop"
            print("已停止抓取；保留上次提交的斷點。", flush=True)
        stage = "upload"
        print("[3/4] 正在上傳本機已寫入的公共書目…", flush=True)
        result["pushed"] = client.push()
        stage = "verify"
        print("[4/4] 正在核對兩端書目…", flush=True)
        result["after"] = _summary(client.compare())
    except (Exception, KeyboardInterrupt) as exc:
        result["interrupted"] |= isinstance(exc, KeyboardInterrupt)
        result["stop_reason"] = f"{stage}_interrupted" if isinstance(exc, KeyboardInterrupt) else f"{stage}_failed"
        result["error_type"] = type(exc).__name__
        raise
    finally:
        if previous_year is None:
            os.environ.pop("ANISHELF_HISTORY_YEAR", None)
        else:
            os.environ["ANISHELF_HISTORY_YEAR"] = previous_year
        after = report["checkpoints_after"] = _checkpoint_snapshot()
        if all("segments" in after[source] for source in EXPECTED_SEGMENTS):
            result["next_year"] = _latest_year({source: value["segments"]
                                                for source, value in after.items() if "segments" in value})
        report["finished_at"] = datetime.now().astimezone().isoformat()
        try:
            path = _save_report(report)
            print(f"回填報告：{path}", flush=True)
        except (OSError, TypeError, ValueError) as exc:
            print(f"無法保存回填報告（{type(exc).__name__}）；已提交的資料庫斷點不受影響。", flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def _run_sources(mode: str, deadline: float | None, result: dict[str, Any]) -> None:
    blocked: set[str] = set()
    catalogs_attempted: set[str] = set()
    while True:
        attempted = False
        for source in HISTORY_SOURCES:
            if source in blocked or source in catalogs_attempted:
                continue
            if deadline is not None and time.monotonic() >= deadline:
                result["stop_reason"] = "time_limit"
                return
            year = _next_year(source, get_backfill_progress(source)) if source in EXPECTED_SEGMENTS else None
            if year is None and source in EXPECTED_SEGMENTS:
                continue
            if year is None:
                catalogs_attempted.add(source)
                os.environ.pop("ANISHELF_HISTORY_YEAR", None)
            else:
                os.environ["ANISHELF_HISTORY_YEAR"] = str(year)
            attempted = True
            attempt = {"source": source, "year": year, "status": "started"}
            result["attempts"].append(attempt)
            print(f"[2/4] {source}：{year if year is not None else '目錄回填'}", flush=True)
            try:
                job_id = run_backfill(source, deadline_monotonic=deadline)
            except KeyboardInterrupt:
                attempt["status"] = "interrupted"
                raise
            result["jobs"].append(job_id)
            job = get_job(job_id) or {}
            status = job.get("status", "unknown")
            attempt.update(job_id=job_id, status=status)
            if status == "paused":
                result["stop_reason"] = "time_limit"
                return
            if status not in {"completed", "partial"}:
                raise RuntimeError(f"回填工作 #{job_id} 狀態為 {status}，停止以免重複執行。")
            if status == "completed" and year is not None and _next_year(source, get_backfill_progress(source)) == year:
                attempt["status"] = "checkpoint_not_advanced"
            if attempt["status"] != "completed":
                blocked.add(source)
                print(f"{source}：保留斷點，本次略過，繼續下一家。", flush=True)
        if mode == "year" or not attempted:
            result["stop_reason"] = "sources_deferred" if blocked else ("round_complete" if mode == "year" else "completed")
            return
