from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Settings, get_settings
from .db import transaction


AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
TOKEN_INFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"
SESSION_COOKIE = "anishelf_session"
OAUTH_STATE_COOKIE = "anishelf_oauth_state"


class AuthenticationError(ValueError):
    pass


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def google_redirect_uri(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return f"{settings.public_url}/auth/google/callback"


def begin_google_login(settings: Settings | None = None) -> tuple[str, str]:
    settings = settings or get_settings()
    if not settings.auth_configured:
        raise AuthenticationError("Google 登入尚未設定完成")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM oauth_login_states WHERE expires_at < CURRENT_TIMESTAMP")
            cursor.execute(
                "INSERT INTO oauth_login_states (state_hash, nonce, expires_at) "
                "VALUES (%s, %s, DATE_ADD(CURRENT_TIMESTAMP, INTERVAL 10 MINUTE))",
                (token_hash(state), nonce),
            )
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": google_redirect_uri(settings),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}", state


def finish_google_login(
    code: str,
    state: str,
    cookie_state: str,
    settings: Settings | None = None,
) -> tuple[str, dict[str, Any]]:
    settings = settings or get_settings()
    if not settings.auth_configured:
        raise AuthenticationError("Google 登入尚未設定完成")
    if not code or not state or not secrets.compare_digest(state, cookie_state or ""):
        raise AuthenticationError("登入驗證狀態不一致，請重新登入")
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT nonce FROM oauth_login_states WHERE state_hash = %s "
                "AND expires_at >= CURRENT_TIMESTAMP FOR UPDATE",
                (token_hash(state),),
            )
            row = cursor.fetchone()
            cursor.execute("DELETE FROM oauth_login_states WHERE state_hash = %s", (token_hash(state),))
    if not row:
        raise AuthenticationError("登入連結已失效，請重新登入")
    token_data = _post_form(
        TOKEN_ENDPOINT,
        {
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": google_redirect_uri(settings),
            "grant_type": "authorization_code",
        },
    )
    id_token = str(token_data.get("id_token") or "")
    if not id_token:
        raise AuthenticationError("Google 未回傳身分憑證")
    claims = _get_json(f"{TOKEN_INFO_ENDPOINT}?{urlencode({'id_token': id_token})}")
    _validate_google_claims(claims, settings.google_client_id, str(row["nonce"]))
    user = _upsert_user(claims, settings)
    session_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_sessions WHERE expires_at < CURRENT_TIMESTAMP")
            cursor.execute(
                "INSERT INTO user_sessions (token_hash, user_id, csrf_token, expires_at) "
                "VALUES (%s, %s, %s, %s)",
                (
                    token_hash(session_token),
                    user["id"],
                    csrf_token,
                    datetime.now() + timedelta(days=settings.session_days),
                ),
            )
    return session_token, user


def current_user(cookie_header: str | None) -> dict[str, Any] | None:
    token = cookie_value(cookie_header, SESSION_COOKIE)
    if not token:
        return None
    settings = get_settings()
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT u.id, u.email, u.display_name, u.avatar_url, u.role, "
                "s.csrf_token, s.expires_at, "
                "TIMESTAMPDIFF(MINUTE, s.last_seen_at, CURRENT_TIMESTAMP) >= %s AS should_touch "
                "FROM user_sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = %s AND s.expires_at >= CURRENT_TIMESTAMP "
                "AND u.is_active = TRUE",
                (settings.session_touch_minutes, token_hash(token)),
            )
            user = cursor.fetchone()
            if user and user.get("should_touch"):
                cursor.execute(
                    "UPDATE user_sessions SET last_seen_at = CURRENT_TIMESTAMP WHERE token_hash = %s",
                    (token_hash(token),),
                )
    if not user:
        return None
    user["id"] = int(user["id"])
    user["is_admin"] = user["role"] == "admin"
    user.pop("expires_at", None)
    user.pop("should_touch", None)
    return user


def logout(cookie_header: str | None) -> None:
    token = cookie_value(cookie_header, SESSION_COOKIE)
    if not token:
        return
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_sessions WHERE token_hash = %s", (token_hash(token),))


def cookie_value(cookie_header: str | None, name: str) -> str:
    if not cookie_header:
        return ""
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel else ""


def session_cookie(token: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    secure = "; Secure" if settings.cloud_mode else ""
    max_age = settings.session_days * 86400
    return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"


def oauth_state_cookie(state: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    secure = "; Secure" if settings.cloud_mode else ""
    return f"{OAUTH_STATE_COOKIE}={state}; Path=/auth/google/callback; HttpOnly; SameSite=Lax; Max-Age=600{secure}"


def clear_cookie(name: str, settings: Settings | None = None, path: str = "/") -> str:
    settings = settings or get_settings()
    secure = "; Secure" if settings.cloud_mode else ""
    return f"{name}=; Path={path}; HttpOnly; SameSite=Lax; Max-Age=0{secure}"


def _upsert_user(claims: dict[str, Any], settings: Settings) -> dict[str, Any]:
    email = str(claims["email"]).strip().casefold()
    configured_admin = email in settings.admin_emails
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (provider, provider_subject, email, display_name, avatar_url, role) "
                "VALUES ('google', %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE email = VALUES(email), display_name = VALUES(display_name), "
                "avatar_url = VALUES(avatar_url), last_login_at = CURRENT_TIMESTAMP, "
                "role = IF(role = 'admin' OR VALUES(role) = 'admin', 'admin', 'user')",
                (
                    str(claims["sub"]),
                    email,
                    str(claims.get("name") or email)[:200],
                    str(claims.get("picture") or "")[:1000] or None,
                    "admin" if configured_admin else "user",
                ),
            )
            cursor.execute("SELECT * FROM users WHERE provider = 'google' AND provider_subject = %s", (str(claims["sub"]),))
            user = cursor.fetchone()
    user["id"] = int(user["id"])
    user["is_admin"] = user["role"] == "admin"
    return user


def _validate_google_claims(claims: dict[str, Any], client_id: str, nonce: str) -> None:
    issuer = str(claims.get("iss") or "")
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        raise AuthenticationError("Google 身分憑證簽發者不正確")
    if str(claims.get("aud") or "") != client_id:
        raise AuthenticationError("Google 身分憑證不屬於此網站")
    if int(claims.get("exp") or 0) <= int(time.time()):
        raise AuthenticationError("Google 身分憑證已過期")
    if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise AuthenticationError("Google 身分憑證 nonce 不一致")
    if str(claims.get("email_verified") or "").lower() not in {"true", "1"}:
        raise AuthenticationError("Google 電子郵件尚未驗證")
    if not claims.get("sub") or not claims.get("email"):
        raise AuthenticationError("Google 身分資料不完整")


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    request = Request(
        url,
        data=urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    return _request_json(request)


def _get_json(url: str) -> dict[str, Any]:
    return _request_json(Request(url, headers={"Accept": "application/json"}))


def _request_json(request: Request) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AuthenticationError(f"Google 登入服務拒絕請求：{detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AuthenticationError("暫時無法連線到 Google 登入服務") from exc
