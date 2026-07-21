from __future__ import annotations

import os
import json
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / ".vendor"
ERROR_LOG = ROOT / "anishelf.err.log"

if VENDOR.exists():
    sys.path.insert(0, str(VENDOR))


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def _browser_url() -> str:
    from app.config import get_settings

    settings = get_settings()
    host = settings.host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{settings.port}/"


def _site_is_listening(url: str) -> bool:
    health_url = f"{url.rstrip('/')}/api/health"
    try:
        with urllib.request.urlopen(health_url, timeout=0.8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # AniShelf's health endpoint can legitimately report 503 when MySQL is
        # not configured or temporarily unavailable.
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
    except (OSError, urllib.error.URLError):
        return False
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and {"ok", "configured"}.issubset(payload)


def _open_browser(url: str) -> None:
    if os.getenv("ANISHELF_NO_BROWSER") == "1":
        return
    try:
        if not webbrowser.open(url):
            print(f"瀏覽器未自動開啟，請手動前往：{url}", flush=True)
    except Exception as exc:
        print(f"瀏覽器未自動開啟（{exc}），請手動前往：{url}", flush=True)


def _wait_until_ready(
    url: str,
    ready: threading.Event,
    stop: threading.Event,
    timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout
    while not stop.is_set() and time.monotonic() < deadline:
        if _site_is_listening(url):
            ready.set()
            print(f"AniShelf 已就緒：{url}", flush=True)
            _open_browser(url)
            return
        stop.wait(0.25)
    if not stop.is_set():
        print(f"等待網站啟動逾時，請查看：{ERROR_LOG}", flush=True)


def _record_failure() -> None:
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] 啟動失敗\n")
        traceback.print_exc(file=log)


def main() -> int:
    _configure_console()
    url = _browser_url()

    if _site_is_listening(url):
        print(f"AniShelf 已在執行，正在開啟：{url}", flush=True)
        _open_browser(url)
        return 0

    print("AniShelf 正在啟動……", flush=True)
    print("請保持這個視窗開啟；關閉視窗就會停止 AniShelf。", flush=True)

    ready = threading.Event()
    stop = threading.Event()
    waiter = threading.Thread(
        target=_wait_until_ready,
        args=(url, ready, stop),
        name="anishelf-browser-opener",
        daemon=True,
    )
    waiter.start()

    try:
        from app.server import main as server_main

        server_main()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        _record_failure()
        print(f"AniShelf 啟動失敗：{exc}", file=sys.stderr, flush=True)
        print(f"詳細錯誤已寫入：{ERROR_LOG}", file=sys.stderr, flush=True)
        return 1
    finally:
        stop.set()
        waiter.join(timeout=0.5)

    if not ready.is_set():
        print(f"AniShelf 未能啟動，請查看：{ERROR_LOG}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:
        _configure_console()
        _record_failure()
        print(f"AniShelf 啟動失敗：{exc}", file=sys.stderr, flush=True)
        print(f"詳細錯誤已寫入：{ERROR_LOG}", file=sys.stderr, flush=True)
        exit_code = 1
    raise SystemExit(exit_code)
