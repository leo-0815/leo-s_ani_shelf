"""Inspect a local history installation without opening the DB or the network."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ORDER = ("chingwin", "kadokawa", "tohan", "spp", "tongli")
REPORT_NAME = re.compile(r"history-\d{8}-\d{6}-[0-9a-f]{8}\.json")
SEGMENT_NAME = re.compile(r"[a-z0-9_]{1,80}")
MAX_REPORT_BYTES = 1024 * 1024
MAX_REPORTS_TO_TRY = 10


def _load_snapshot(report_dir: Path) -> tuple[dict | None, Path | None, int, str]:
    try:
        paths = sorted(
            (p for p in report_dir.iterdir() if REPORT_NAME.fullmatch(p.name)),
            reverse=True,
        )
    except FileNotFoundError:
        return None, None, 0, "missing"
    except OSError:
        return None, None, 0, "unreadable"
    skipped = 0
    for path in paths[:MAX_REPORTS_TO_TRY]:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("not a regular report")
            with path.open("rb") as stream:
                data = stream.read(MAX_REPORT_BYTES + 1)
            if len(data) > MAX_REPORT_BYTES:
                raise ValueError("report too large")
            report = json.loads(data.decode("utf-8-sig"))
            if not isinstance(report, dict) or not isinstance(report.get("checkpoints_after"), dict):
                raise ValueError("not a history checkpoint snapshot")
            return report, path, skipped, "found"
        except (ValueError, OSError, RecursionError):
            skipped += 1
    return None, None, skipped, "unreadable" if paths else "missing"


def inspect_history_installation(project_dir: Path) -> dict:
    project_dir = project_dir.resolve(strict=True)
    launcher = project_dir / "歷史資料更新.bat"
    cli = project_dir / "app" / "history_cli.py"
    if not project_dir.is_dir() or not launcher.is_file() or not cli.is_file():
        raise ValueError("不是 AniShelf 歷史工具資料夾：找不到啟動批次檔或選單程式")

    snapshot, path, skipped, status = _load_snapshot(project_dir / "logs" / "history-runs")
    sources = {}
    for code in SOURCE_ORDER:
        sources[code] = {"status": "no_snapshot", "complete": 0, "pending": [], "invalid": 0}
    result = {
        "project_dir": project_dir,
        "active_tool_dir": PROJECT_ROOT,
        "automated_series_module_present": (project_dir / "app" / "series_backfill_runner.py").is_file(),
        "report_path": path,
        "report_status": status,
        "skipped_reports": skipped,
        "finished_at": None,
        "next_year": None,
        "sources": sources,
    }
    if snapshot is None:
        return result
    raw_time = snapshot.get("finished_at")
    if isinstance(raw_time, str) and len(raw_time) <= 40:
        try:
            result["finished_at"] = datetime.fromisoformat(raw_time).isoformat()
        except ValueError:
            pass
    summary = snapshot.get("result")
    year = summary.get("next_year") if isinstance(summary, dict) else None
    if type(year) is int and 1900 <= year <= 2100:
        result["next_year"] = year
    for code, raw in snapshot["checkpoints_after"].items():
        if code not in sources or not isinstance(raw, dict):
            continue
        state = sources[code]
        segments = raw.get("segments")
        if not isinstance(segments, dict):
            state["status"] = "unavailable" if "unavailable" in raw else "no_snapshot"
            continue
        state["status"] = "recorded" if segments else "no_segments"
        for name, entry in sorted(segments.items()):
            if (
                not SEGMENT_NAME.fullmatch(name) or not isinstance(entry, dict)
                or type(entry.get("completed")) is not bool
                or type(entry.get("next_page")) is not int or entry["next_page"] < 1
            ):
                state["invalid"] += 1
                continue
            if entry["completed"]:
                state["complete"] += 1
            else:
                state["pending"].append((name, entry["next_page"]))
    return result


def show_history_preflight(project_dir: Path | None = None) -> None:
    result = inspect_history_installation(project_dir if project_dir is not None else PROJECT_ROOT)
    print("\nAniShelf 歷史回填預檢（唯讀；不連資料庫、不抓書、不上傳）")
    print(f"工具位置：{result['active_tool_dir']}")
    if result["project_dir"] != result["active_tool_dir"]:
        print(f"這次只檢查此資料夾的檔案與報告：{result['project_dir']}")
    print("自動找缺卷模組：" + ("有檔案" if result["automated_series_module_present"] else "未找到"))
    if result["skipped_reports"]:
        print(f"略過無法讀取的較新報告：{result['skipped_reports']} 份")
    if result["report_path"] is None:
        note = "找不到" if result["report_status"] == "missing" else "無法讀取"
        print(f"{note}已儲存的回填報告。未檢查即時斷點，不能據此判定要從頭跑。")
        return
    print(f"報告：{result['report_path'].name}（{result['finished_at'] or '無可驗證時間'}）")
    print("以下是上次報告的紀錄，不是資料庫的即時進度，也不代表全站書目收齊。")
    if result["next_year"] is not None:
        print(f"上次報告的下個年度：{result['next_year']}")
    labels = {"no_snapshot": "報告未記錄", "no_segments": "沒有分段斷點", "unavailable": "當時無法讀取斷點"}
    for code, state in result["sources"].items():
        if state["status"] != "recorded":
            print(f"{code}：{labels[state['status']]}")
            continue
        pending = state["pending"]
        print(f"{code}：{state['complete']} 段曾標完成、{len(pending)} 段未完成"
              + (f"、{state['invalid']} 筆格式不符" if state["invalid"] else ""))
        for name, position in pending[:6]:
            print(f"  {name}：下一批位置 {position}")
        if len(pending) > 6:
            print(f"  其餘 {len(pending) - 6} 段詳見原報告")
