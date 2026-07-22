from __future__ import annotations

import json
import csv
import io
import mimetypes
import re
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auth import (
    AuthenticationError,
    OAUTH_STATE_COOKIE,
    SESSION_COOKIE,
    begin_google_login,
    clear_cookie,
    cookie_value,
    current_user,
    finish_google_login,
    logout,
    oauth_state_cookie,
    session_cookie,
)
from .config import ROOT, get_settings
from .catalog_sync import PROTOCOL_VERSION, ingest_catalog_books
from .models import SYNC_HASH_VERSION
from .crawler import create_job, get_job
from .db import DatabaseUnavailable, close_connection_pools, ensure_schema, ping
from .notifications import (
    NotificationError,
    dispatch_test_email_workflow,
    get_notification_preferences,
    send_test_discord,
    send_test_email,
    set_notification_preferences,
)
from .repository import (
    clear_recommendation_dismissals,
    delete_wishlist,
    dismiss_recommendation,
    export_catalog,
    get_book,
    get_series,
    list_book_recommendations,
    list_books,
    list_publishers,
    list_recommendations,
    list_series,
    quality_report,
    catalog_books,
    catalog_change_feed,
    catalog_manifest,
    catalog_sync_status,
    set_series_follow,
    set_wishlist,
    stats,
    upcoming_books,
)


WEB_ROOT = ROOT / "web"
_INSTANCE_LOCK: Any = None
_HEALTH_CACHE: tuple[tuple[Any, ...], float, bool, dict[str, Any]] | None = None
_HEALTH_CACHE_LOCK = Lock()


def _database_health(settings: Any) -> tuple[bool, dict[str, Any]]:
    """Cache Render's frequent readiness probe without creating repeated TLS sessions."""
    global _HEALTH_CACHE
    key = (settings.db_host, settings.db_port, settings.db_name, settings.db_user)
    now = monotonic()
    with _HEALTH_CACHE_LOCK:
        if _HEALTH_CACHE:
            cached_key, expires_at, ok, payload = _HEALTH_CACHE
            if cached_key == key and now < expires_at:
                return ok, dict(payload)
        try:
            payload = ping()
            ok = True
            ttl = settings.health_cache_seconds
        except Exception as exc:
            payload = {"message": str(exc)}
            ok = False
            ttl = min(settings.health_cache_seconds, 2)
        _HEALTH_CACHE = (key, now + ttl, ok, dict(payload))
        return ok, payload


def _clear_health_cache() -> None:
    global _HEALTH_CACHE
    with _HEALTH_CACHE_LOCK:
        _HEALTH_CACHE = None


def _instance_lock_path(port: int) -> Path:
    return ROOT / f".anishelf.{port}.lock"


def _acquire_instance_lock(port: int) -> bool:
    global _INSTANCE_LOCK
    # Local, staging and production can share a checkout during diagnostics.
    # A per-port lock prevents one environment from blocking another.
    path = _instance_lock_path(port)
    handle = path.open("a+b")
    handle.seek(0)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if __import__("os").name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _INSTANCE_LOCK = handle
    return True


def _release_instance_lock() -> None:
    global _INSTANCE_LOCK
    if not _INSTANCE_LOCK:
        return
    try:
        _INSTANCE_LOCK.seek(0)
        if __import__("os").name == "nt":
            import msvcrt

            msvcrt.locking(_INSTANCE_LOCK.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(_INSTANCE_LOCK.fileno(), fcntl.LOCK_UN)
    finally:
        _INSTANCE_LOCK.close()
        _INSTANCE_LOCK = None


class Handler(BaseHTTPRequestHandler):
    server_version = "AniShelf/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self._health()
                return
            if parsed.path == "/api/auth/config":
                self._json({"google_enabled": get_settings().auth_configured})
                return
            if parsed.path == "/api/auth/me":
                user = current_user(self.headers.get("Cookie"))
                self._json({"authenticated": bool(user), "user": user})
                return
            if parsed.path.startswith("/api/catalog-sync/"):
                if self._require_catalog_sync():
                    self._catalog_sync_get(parsed)
                return
            if parsed.path == "/auth/google":
                self._start_google_login()
                return
            if parsed.path == "/auth/google/callback":
                self._finish_google_login(parsed)
                return
            if not parsed.path.startswith("/api/"):
                self._static(parsed.path)
                return
            user = self._require_user()
            if user:
                self._authenticated_get(parsed, user)
        except AuthenticationError as exc:
            self._json({"error": str(exc)}, HTTPStatus.UNAUTHORIZED)
        except (DatabaseUnavailable, OSError) as exc:
            self._json({"error": str(exc), "setup_required": True}, HTTPStatus.SERVICE_UNAVAILABLE)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc).strip("'")}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": f"Server error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _authenticated_get(self, parsed: Any, user: dict[str, Any]) -> None:
        user_id = int(user["id"])
        if parsed.path == "/api/books":
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            self._json(list_books(query, user_id, int(query.get("limit", "100")), int(query.get("offset", "0"))))
        elif match := re.fullmatch(r"/api/books/(\d+)", parsed.path):
            book = get_book(int(match.group(1)), user_id)
            self._json(book or {"error": "Book not found"}, HTTPStatus.OK if book else HTTPStatus.NOT_FOUND)
        elif match := re.fullmatch(r"/api/books/(\d+)/recommendations", parsed.path):
            query = parse_qs(parsed.query)
            self._json({"items": list_book_recommendations(user_id, int(match.group(1)), int(query.get("limit", ["8"])[0]))})
        elif parsed.path == "/api/publishers":
            items = list_publishers()
            if not user.get("is_admin"):
                public_fields = {"code", "name", "enabled", "book_count"}
                items = [{key: value for key, value in item.items() if key in public_fields} for item in items]
            self._json({"items": items})
        elif parsed.path == "/api/series":
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            self._json(list_series(query, user_id, int(query.get("limit", "100")), int(query.get("offset", "0"))))
        elif parsed.path == "/api/series/detail":
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            series = get_series(user_id, query.get("publisher", ""), query.get("title", ""))
            self._json(series or {"error": "Series not found"}, HTTPStatus.OK if series else HTTPStatus.NOT_FOUND)
        elif parsed.path == "/api/quality":
            if self._require_admin(user):
                self._json(quality_report())
        elif parsed.path == "/api/upcoming":
            query = parse_qs(parsed.query)
            self._json({"items": upcoming_books(user_id, int(query.get("days", ["31"])[0]))})
        elif parsed.path == "/api/recommendations":
            query = parse_qs(parsed.query)
            self._json(list_recommendations(user_id, int(query.get("limit", ["60"])[0])))
        elif parsed.path in {"/api/export.json", "/api/export.csv"}:
            query = parse_qs(parsed.query)
            wishlist_only = query.get("wishlist", ["0"])[0] == "1"
            if not wishlist_only and not self._require_admin(user):
                return
            if parsed.path.endswith(".json"):
                content = json.dumps(export_catalog(user_id, wishlist_only), ensure_ascii=False, indent=2, default=str).encode("utf-8")
                self._download(content, "application/json; charset=utf-8", "anishelf-export.json")
            else:
                self._export_csv(user_id, wishlist_only)
        elif parsed.path == "/api/calendar.ics":
            query = parse_qs(parsed.query)
            self._export_calendar(user_id, int(query.get("days", ["90"])[0]))
        elif parsed.path == "/api/stats":
            self._json(stats(user_id))
        elif parsed.path == "/api/notification-preferences":
            preferences = get_notification_preferences(user_id)
            settings = get_settings()
            preferences.update(
                {
                    "email_configured": (
                        settings.github_email_test_configured
                        if settings.email_test_mode == "github_actions"
                        else settings.email_configured
                    ),
                    "email_test_mode": settings.email_test_mode,
                    "discord_configured": bool(settings.discord_webhook_url)
                    if user.get("is_admin")
                    else False,
                    "discord_lead_days": list(settings.notification_lead_days)
                    if user.get("is_admin")
                    else [],
                }
            )
            self._json(preferences)
        elif parsed.path == "/api/jobs/latest":
            if self._require_admin(user):
                self._json(get_job() or {})
        elif match := re.fullmatch(r"/api/jobs/(\d+)", parsed.path):
            if not self._require_admin(user):
                return
            job = get_job(int(match.group(1)))
            self._json(job or {"error": "Job not found"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
        else:
            self._json({"error": "API not found"}, HTTPStatus.NOT_FOUND)

    def _legacy_do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self._health()
            elif parsed.path == "/api/books":
                query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
                self._json(
                    list_books(
                        query,
                        int(query.get("limit", "100")),
                        int(query.get("offset", "0")),
                    )
                )
            elif match := re.fullmatch(r"/api/books/(\d+)", parsed.path):
                book = get_book(int(match.group(1)))
                self._json(book or {"error": "找不到書籍"}, HTTPStatus.OK if book else HTTPStatus.NOT_FOUND)
            elif match := re.fullmatch(r"/api/books/(\d+)/recommendations", parsed.path):
                query = parse_qs(parsed.query)
                self._json(
                    {
                        "items": list_book_recommendations(
                            int(match.group(1)),
                            int(query.get("limit", ["8"])[0]),
                        )
                    }
                )
            elif parsed.path == "/api/publishers":
                self._json({"items": list_publishers()})
            elif parsed.path == "/api/series":
                query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
                self._json(
                    list_series(
                        query,
                        int(query.get("limit", "100")),
                        int(query.get("offset", "0")),
                    )
                )
            elif parsed.path == "/api/series/detail":
                query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
                series = get_series(query.get("publisher", ""), query.get("title", ""))
                self._json(
                    series or {"error": "找不到系列"},
                    HTTPStatus.OK if series else HTTPStatus.NOT_FOUND,
                )
            elif parsed.path == "/api/quality":
                self._json(quality_report())
            elif parsed.path == "/api/upcoming":
                query = parse_qs(parsed.query)
                self._json({"items": upcoming_books(int(query.get("days", ["31"])[0]))})
            elif parsed.path == "/api/recommendations":
                query = parse_qs(parsed.query)
                self._json(list_recommendations(int(query.get("limit", ["60"])[0])))
            elif parsed.path == "/api/export.json":
                query = parse_qs(parsed.query)
                self._download(
                    json.dumps(
                        export_catalog(query.get("wishlist", ["0"])[0] == "1"),
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ).encode("utf-8"),
                    "application/json; charset=utf-8",
                    "anishelf-export.json",
                )
            elif parsed.path == "/api/export.csv":
                query = parse_qs(parsed.query)
                self._export_csv(query.get("wishlist", ["0"])[0] == "1")
            elif parsed.path == "/api/calendar.ics":
                query = parse_qs(parsed.query)
                self._export_calendar(int(query.get("days", ["90"])[0]))
            elif parsed.path == "/api/stats":
                self._json(stats())
            elif parsed.path == "/api/jobs/latest":
                self._json(get_job() or {})
            elif match := re.fullmatch(r"/api/jobs/(\d+)", parsed.path):
                job = get_job(int(match.group(1)))
                self._json(job or {"error": "找不到更新工作"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
            else:
                self._static(parsed.path)
        except (DatabaseUnavailable, OSError) as exc:
            self._json({"error": str(exc), "setup_required": True}, HTTPStatus.SERVICE_UNAVAILABLE)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc).strip("'")}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": f"系統錯誤：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/catalog-sync/books":
                if not self._require_catalog_sync():
                    return
                payload = self._body()
                self._json(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        **ingest_catalog_books(payload.get("items")),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return
            user = self._require_user()
            if not user or not self._require_csrf(user):
                return
            if parsed.path == "/api/auth/logout":
                logout(self.headers.get("Cookie"))
                self._json({"ok": True}, cookies=[clear_cookie(SESSION_COOKIE)])
                return
            payload = self._body()
            user_id = int(user["id"])
            if parsed.path == "/api/update":
                if not self._require_admin(user):
                    return
                job_id = create_job(str(payload.get("source", "all")), force=bool(payload.get("force", True)))
                if job_id is None:
                    self._json({"job_id": None, "status": "skipped", "reason": "cooldown"})
                else:
                    self._json({"job_id": job_id, "status": "queued"}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/series/follow":
                set_series_follow(
                    user_id,
                    str(payload.get("publisher", "")),
                    str(payload.get("series_title", "")),
                    str(payload.get("media_type", "unknown")),
                    bool(payload.get("following", True)),
                )
                self._json({"ok": True})
            elif parsed.path == "/api/recommendations/dismiss":
                dismiss_recommendation(user_id, int(payload.get("book_id", 0)))
                self._json({"ok": True})
            elif parsed.path == "/api/notification-preferences":
                self._json(set_notification_preferences(user_id, payload))
            elif parsed.path == "/api/notifications/test-email":
                settings = get_settings()
                if settings.email_test_mode == "github_actions":
                    result = dispatch_test_email_workflow(
                        settings=settings,
                        user_id=user_id,
                    )
                    message = "測試信已排入寄送，通常會在 1 分鐘內送達"
                else:
                    result = send_test_email(
                        settings=settings,
                        recipient=str(user.get("email") or ""),
                    )
                    message = "測試信已寄出，請查看收件匣"
                self._json({**result, "message": message})
            elif parsed.path == "/api/notifications/test-discord":
                if not self._require_admin(user):
                    return
                settings = get_settings()
                result = send_test_discord(
                    webhook_url=settings.discord_webhook_url,
                    public_url=settings.public_url,
                )
                self._json({**result, "message": "Discord 測試訊息已送出"})
            elif match := re.fullmatch(r"/api/wishlist/(\d+)", parsed.path):
                set_wishlist(
                    user_id,
                    int(match.group(1)),
                    str(payload.get("state", "wanted")),
                    str(payload.get("notes", "")),
                    bool(payload.get("follow_series", False)),
                    int(payload.get("priority", 0)),
                    str(payload.get("store_name", "")),
                    str(payload.get("order_number", "")),
                    int(payload["paid_price"]) if str(payload.get("paid_price", "")).isdigit() else None,
                    str(payload.get("owned_format", "paper")),
                )
                self._json({"ok": True})
            else:
                self._json({"error": "API not found"}, HTTPStatus.NOT_FOUND)
        except (DatabaseUnavailable, OSError) as exc:
            self._json({"error": str(exc), "setup_required": True}, HTTPStatus.SERVICE_UNAVAILABLE)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc).strip("'")}, HTTPStatus.BAD_REQUEST)
        except NotificationError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        except Exception as exc:
            self._json({"error": f"Server error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _legacy_do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._body()
            if parsed.path == "/api/update":
                job_id = create_job(
                    str(payload.get("source", "all")),
                    force=bool(payload.get("force", True)),
                )
                if job_id is None:
                    self._json(
                        {"job_id": None, "status": "skipped", "reason": "cooldown"},
                        HTTPStatus.OK,
                    )
                else:
                    self._json({"job_id": job_id, "status": "queued"}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/series/follow":
                set_series_follow(
                    str(payload.get("publisher", "")),
                    str(payload.get("series_title", "")),
                    str(payload.get("media_type", "unknown")),
                    bool(payload.get("following", True)),
                )
                self._json({"ok": True})
            elif parsed.path == "/api/recommendations/dismiss":
                dismiss_recommendation(int(payload.get("book_id", 0)))
                self._json({"ok": True})
            elif match := re.fullmatch(r"/api/wishlist/(\d+)", parsed.path):
                set_wishlist(
                    int(match.group(1)),
                    str(payload.get("state", "wanted")),
                    str(payload.get("notes", "")),
                    bool(payload.get("follow_series", False)),
                    int(payload.get("priority", 0)),
                    str(payload.get("store_name", "")),
                    str(payload.get("order_number", "")),
                    int(payload["paid_price"]) if str(payload.get("paid_price", "")).isdigit() else None,
                    str(payload.get("owned_format", "paper")),
                )
                self._json({"ok": True})
            else:
                self._json({"error": "找不到 API"}, HTTPStatus.NOT_FOUND)
        except (DatabaseUnavailable, OSError) as exc:
            self._json({"error": str(exc), "setup_required": True}, HTTPStatus.SERVICE_UNAVAILABLE)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc).strip("'")}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": f"系統錯誤：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            user = self._require_user()
            if not user or not self._require_csrf(user):
                return
            user_id = int(user["id"])
            if parsed.path == "/api/recommendations/dismissals":
                clear_recommendation_dismissals(user_id)
                self._json({"ok": True})
                return
            match = re.fullmatch(r"/api/wishlist/(\d+)", parsed.path)
            if not match:
                self._json({"error": "API not found"}, HTTPStatus.NOT_FOUND)
                return
            delete_wishlist(user_id, int(match.group(1)))
            self._json({"ok": True})
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _legacy_do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/recommendations/dismissals":
                clear_recommendation_dismissals()
                self._json({"ok": True})
                return
            match = re.fullmatch(r"/api/wishlist/(\d+)", parsed.path)
            if not match:
                self._json({"error": "找不到 API"}, HTTPStatus.NOT_FOUND)
                return
            delete_wishlist(int(match.group(1)))
            self._json({"ok": True})
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _require_user(self) -> dict[str, Any] | None:
        user = current_user(self.headers.get("Cookie"))
        if not user:
            self._json({"error": "Authentication required"}, HTTPStatus.UNAUTHORIZED)
        return user

    def _require_admin(self, user: dict[str, Any]) -> bool:
        if user.get("role") != "admin":
            self._json({"error": "Administrator permission required"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _require_csrf(self, user: dict[str, Any]) -> bool:
        expected = str(user.get("csrf_token") or "")
        supplied = self.headers.get("X-CSRF-Token", "")
        if not expected or not secrets.compare_digest(expected, supplied):
            self._json({"error": "Invalid CSRF token"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _require_catalog_sync(self) -> bool:
        settings = get_settings()
        if not settings.catalog_sync_configured:
            self._json(
                {"error": "Catalog sync is not configured"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return False
        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        supplied = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
        if not supplied or not secrets.compare_digest(settings.catalog_sync_token, supplied):
            self._json({"error": "Invalid catalog sync token"}, HTTPStatus.UNAUTHORIZED)
            return False
        return True

    def _catalog_sync_get(self, parsed: Any) -> None:
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        if parsed.path == "/api/catalog-sync/status":
            self._json(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "sync_hash_version": SYNC_HASH_VERSION,
                    **catalog_sync_status(),
                }
            )
        elif parsed.path == "/api/catalog-sync/manifest":
            self._json(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "sync_hash_version": SYNC_HASH_VERSION,
                    **catalog_manifest(
                        int(query.get("after_id", "0")), int(query.get("limit", "500"))
                    ),
                }
            )
        elif parsed.path == "/api/catalog-sync/books":
            status = catalog_sync_status()
            self._json(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "snapshot_change_id": status["latest_change_id"],
                    **catalog_books(
                        int(query.get("after_id", "0")), int(query.get("limit", "500"))
                    ),
                }
            )
        elif parsed.path == "/api/catalog-sync/changes":
            self._json(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    **catalog_change_feed(
                        int(query.get("after", "0")), int(query.get("limit", "500"))
                    ),
                }
            )
        else:
            self._json({"error": "Catalog sync API not found"}, HTTPStatus.NOT_FOUND)

    def _start_google_login(self) -> None:
        location, state = begin_google_login()
        self._redirect(location, [oauth_state_cookie(state)])

    def _finish_google_login(self, parsed: Any) -> None:
        query = parse_qs(parsed.query)
        if query.get("error"):
            raise AuthenticationError("Google 登入已取消")
        state = query.get("state", [""])[0]
        code = query.get("code", [""])[0]
        cookie_state = cookie_value(self.headers.get("Cookie"), OAUTH_STATE_COOKIE)
        token, _user = finish_google_login(code, state, cookie_state)
        self._redirect(
            "/",
            [
                session_cookie(token),
                clear_cookie(OAUTH_STATE_COOKIE, path="/auth/google/callback"),
            ],
        )

    def _redirect(self, location: str, cookies: list[str] | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _health(self) -> None:
        settings = get_settings()
        if not settings.database_configured:
            self._json(
                {"ok": False, "configured": False, "message": "MySQL 尚未初始化"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        ok, info = _database_health(settings)
        if ok:
            self._json({"ok": True, "configured": True, **info})
        else:
            self._json(
                {"ok": False, "configured": True, **info},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        path = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in path.parents and path != WEB_ROOT.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)

    def _body(self) -> dict[str, Any]:
        length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(
        self,
        payload: Any,
        status: HTTPStatus = HTTPStatus.OK,
        cookies: list[str] | None = None,
    ) -> None:
        content = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(content)

    def _download(self, content: bytes, mime: str, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _export_csv(self, user_id: int, wishlist_only: bool) -> None:
        data = export_catalog(user_id, wishlist_only)
        output = io.StringIO()
        fields = [
            "publisher_name",
            "title",
            "series_title",
            "volume_label",
            "edition_type",
            "media_type",
            "author",
            "isbn",
            "release_date",
            "release_status",
            "list_price",
            "wishlist_state",
            "wishlist_priority",
            "wishlist_store",
            "wishlist_order_number",
            "wishlist_paid_price",
            "wishlist_format",
            "source_url",
        ]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data["items"])
        self._download(
            ("\ufeff" + output.getvalue()).encode("utf-8"),
            "text/csv; charset=utf-8",
            "anishelf-export.csv",
        )

    def _export_calendar(self, user_id: int, days: int) -> None:
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//AniShelf//Release Calendar//ZH-TW",
            "CALSCALE:GREGORIAN",
        ]
        for book in upcoming_books(user_id, days):
            if not book["release_date"]:
                continue
            stamp = book["release_date"].replace("-", "")
            title = str(book["title"]).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:anishelf-{book['id']}@local",
                    f"DTSTART;VALUE=DATE:{stamp}",
                    f"SUMMARY:{title}",
                    f"DESCRIPTION:{book['publisher_name']} / {book.get('author') or '作者未提供'}",
                    f"URL:{book['source_url']}",
                    "END:VEVENT",
                ]
            )
        lines.append("END:VCALENDAR")
        self._download(
            ("\r\n".join(lines) + "\r\n").encode("utf-8"),
            "text/calendar; charset=utf-8",
            "anishelf-calendar.ics",
        )

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}")


def main() -> None:
    settings = get_settings()
    if not _acquire_instance_lock(settings.port):
        print(f"AniShelf 已在執行：http://{settings.host}:{settings.port}")
        return
    schema_ready = False
    if settings.database_configured:
        try:
            ensure_schema()
            schema_ready = True
        except Exception as exc:
            print(f"MySQL schema 檢查失敗：{exc}")
    try:
        server = ThreadingHTTPServer((settings.host, settings.port), Handler)
    except Exception:
        _release_instance_lock()
        raise
    print(f"AniShelf 已啟動：http://{settings.host}:{settings.port}")
    if not settings.database_configured:
        print("MySQL 尚未初始化；請先執行 python anishelf.py setup")
    elif schema_ready and settings.auto_update:
        try:
            job_id = create_job("all", force=False)
            if job_id:
                print(f"已自動開始六家出版社資料更新（工作 #{job_id}）")
            else:
                print("六小時內已執行過更新，略過本次啟動更新")
        except Exception as exc:
            print(f"啟動時自動更新失敗：{exc}")
    elif schema_ready:
        print("雲端模式已關閉網站啟動更新；資料更新交由排程工作執行")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAniShelf 已停止")
    finally:
        server.server_close()
        close_connection_pools()
        _release_instance_lock()


if __name__ == "__main__":
    main()
