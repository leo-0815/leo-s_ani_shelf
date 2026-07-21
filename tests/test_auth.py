from __future__ import annotations

import time
import unittest

from app.auth import (
    AuthenticationError,
    _validate_google_claims,
    google_redirect_uri,
    oauth_state_cookie,
    session_cookie,
    token_hash,
)
from app.config import Settings


def settings(cloud: bool = True) -> Settings:
    return Settings(
        db_host="db.example.com",
        db_port=4000,
        db_name="anishelf",
        db_user="anishelf",
        db_password="secret",
        db_ssl_mode="verify_identity",
        host="0.0.0.0",
        port=10000,
        cloud_mode=cloud,
        auto_update=False,
        public_url="https://anishelf.example.com",
        google_client_id="client-id",
        google_client_secret="client-secret",
        admin_emails=("owner@example.com",),
    )


class AuthTests(unittest.TestCase):
    def test_redirect_uri_is_derived_from_public_url(self) -> None:
        self.assertEqual(
            google_redirect_uri(settings()),
            "https://anishelf.example.com/auth/google/callback",
        )

    def test_cloud_cookies_are_http_only_secure_and_same_site(self) -> None:
        cookie = session_cookie("session-token", settings())
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Path=/auth/google/callback", oauth_state_cookie("state", settings()))

    def test_session_tokens_are_only_stored_as_hashes(self) -> None:
        digest = token_hash("secret-token")
        self.assertEqual(len(digest), 64)
        self.assertNotIn("secret-token", digest)

    def test_google_claim_validation_accepts_expected_identity(self) -> None:
        claims = {
            "iss": "https://accounts.google.com",
            "aud": "client-id",
            "exp": int(time.time()) + 300,
            "nonce": "expected-nonce",
            "email_verified": "true",
            "sub": "google-subject",
            "email": "owner@example.com",
        }
        _validate_google_claims(claims, "client-id", "expected-nonce")

    def test_google_claim_validation_rejects_wrong_audience(self) -> None:
        claims = {
            "iss": "accounts.google.com",
            "aud": "another-client",
            "exp": int(time.time()) + 300,
            "nonce": "expected-nonce",
            "email_verified": True,
            "sub": "google-subject",
            "email": "owner@example.com",
        }
        with self.assertRaises(AuthenticationError):
            _validate_google_claims(claims, "client-id", "expected-nonce")

    def test_google_claim_validation_rejects_nonce_replay(self) -> None:
        claims = {
            "iss": "accounts.google.com",
            "aud": "client-id",
            "exp": int(time.time()) + 300,
            "nonce": "stale-nonce",
            "email_verified": True,
            "sub": "google-subject",
            "email": "owner@example.com",
        }
        with self.assertRaises(AuthenticationError):
            _validate_google_claims(claims, "client-id", "expected-nonce")


if __name__ == "__main__":
    unittest.main()
