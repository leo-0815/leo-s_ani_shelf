import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from app.abuse import RequestError, SlidingWindow, category, csv_cell, session_identity, validate_query
from app.auth import SESSION_COOKIE
from app.server import Handler
from app.resource_guards import ResourceGuard


class LimiterTests(unittest.TestCase):
    def test_sliding_window_and_denied_requests_do_not_extend_wait(self):
        now = [0.0]
        limiter = SlidingWindow(clock=lambda: now[0])
        self.assertEqual([limiter.check("user") for _ in range(20)], [0] * 20)
        self.assertEqual(limiter.check("user"), 60)
        now[0] = 59.1
        self.assertEqual(limiter.check("user"), 1)
        now[0] = 60
        self.assertEqual(limiter.check("user"), 0)

    def test_concurrent_requests_are_atomic(self):
        limiter = SlidingWindow(clock=lambda: 0)
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: limiter.check("account"), range(100)))
        self.assertEqual(results.count(0), 20)

    def test_memory_is_bounded_without_evicting_active_quota(self):
        now = [0]
        limiter = SlidingWindow(clock=lambda: now[0], capacity=2)
        limiter.check("a"); limiter.check("b")
        self.assertEqual(limiter.check("c"), 60)
        self.assertEqual(len(limiter.entries), 2)
        now[0] = 60
        self.assertEqual(limiter.check("c"), 0)
        self.assertEqual(len(limiter.entries), 1)

    def test_cookie_identity_does_not_store_secret(self):
        self.assertNotIn("secret-cookie", session_identity("secret-cookie", "127.0.0.1"))
        self.assertEqual(session_identity("secret-cookie", "a"), session_identity("secret-cookie", "b"))

    def test_route_aliases_share_categories(self):
        self.assertEqual(category("/api/books/1", "GET"), category("/api/series", "GET"))
        self.assertEqual(category("/api/export.csv", "GET"), category("/api/export.json", "GET"))
        self.assertEqual(category("/api/wishlist/1", "POST"), category("/api/collection/1", "DELETE"))

    def test_invalid_query_bounds_and_duplicates(self):
        for query in ("limit=201", "offset=-1", "days=999999", "limit=no", "sort=SQL", "q=a&q=b", "q=" + "a" * 201):
            with self.subTest(query=query), self.assertRaises(RequestError):
                validate_query(urlparse("/api/books?" + query))
        validate_query(urlparse("/api/books?limit=100&offset=9999&sort=title_asc"))
        validate_query(urlparse("/api/catalog-sync/books?limit=500"))

    def test_csv_formula_neutralization_keeps_numeric_values(self):
        self.assertEqual(csv_cell(" =HYPERLINK('x')"), "' =HYPERLINK('x')")
        self.assertEqual(csv_cell("@SUM(1)"), "'@SUM(1)")
        self.assertEqual(csv_cell(-20), -20)
        self.assertEqual(csv_cell("普通書名"), "普通書名")


class QuietHandler(Handler):
    def log_message(self, *args):
        pass


class GuardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.now = [0.0]
        self.stack.enter_context(patch('app.server.RESOURCES', ResourceGuard(clock=lambda:self.now[0])))
        self.stack.enter_context(patch("app.server.LIMITER", SlidingWindow(clock=lambda: self.now[0])))
        self.auth = self.stack.enter_context(patch("app.server.current_user", side_effect=self.user))
        self.books = self.stack.enter_context(patch("app.server.list_books", return_value={"items": [], "total": 0}))
        self.preferences = self.stack.enter_context(patch("app.server.set_preferences", return_value={"ok": True}))
        self.stack.enter_context(patch("app.server.get_settings", return_value=MagicMock(
            catalog_sync_configured=True, catalog_sync_token="t" * 32, email_test_mode="smtp")))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.stack.close()

    @staticmethod
    def user(cookie):
        if not cookie or "anonymous" in cookie:
            return None
        return {"id": 2 if "second" in cookie else 1, "role": "user", "csrf_token": "csrf", "email": "test@example.invalid"}

    def request(self, path="/api/books", method="GET", body=None, headers=None):
        base = {"Cookie": f"{SESSION_COOKIE}=first", "X-CSRF-Token": "csrf", "Content-Type": "application/json"}
        base.update(headers or {})
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=base)
        response = connection.getresponse()
        status, response_headers = response.status, dict(response.getheaders())
        content = response.read()
        connection.close()
        return status, response_headers, json.loads(content)

    def test_21st_request_is_429_and_recovers(self):
        for _ in range(20):
            self.assertEqual(self.request()[0], 200)
        status, headers, data = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(headers["Retry-After"], "60")
        self.assertEqual(data["rate_limit_bucket"], "browse")
        self.assertEqual(self.books.call_count, 20)
        self.now[0] = 60
        self.assertEqual(self.request()[0], 200)

    def test_account_limit_shared_across_devices_not_accounts(self):
        for index in range(20):
            headers = {"Cookie": f"{SESSION_COOKIE}=device{index}", "X-Forwarded-For": f"192.0.2.{index}"}
            self.assertEqual(self.request(headers=headers)[0], 200)
        self.assertEqual(self.request(headers={"Cookie": f"{SESSION_COOKIE}=new-device"})[0], 429)
        self.assertEqual(self.request(headers={"Cookie": f"{SESSION_COOKIE}=second"})[0], 200)
        self.assertEqual(self.request("/api/auth/me")[0], 200)

    def test_anonymous_and_csrf_denied(self):
        self.assertEqual(self.request(headers={"Cookie": "anonymous"})[0], 401)
        self.assertEqual(self.request("/api/preferences", "POST", "{}", {"X-CSRF-Token": "wrong"})[0], 403)
        self.preferences.assert_not_called()

    def test_oauth_init_cannot_bypass_quota_with_fake_cookies(self):
        with patch.object(Handler, "_start_google_login", lambda handler: handler._json({"login": True})):
            for index in range(20):
                self.assertEqual(self.request("/auth/google", headers={"Cookie": f"{SESSION_COOKIE}=fake{index}"})[0], 200)
            self.assertEqual(self.request("/auth/google", headers={"Cookie": f"{SESSION_COOKIE}=fake21"})[0], 429)

    def test_rotating_invalid_cookie_has_global_auth_fuse(self):
        from app.abuse import LIMITER as unused
        import app.server as server
        for _ in range(400):
            self.assertEqual(server.LIMITER.check("auth-lookup-budget", 400), 0)
        self.assertEqual(self.request(headers={"Cookie": f"{SESSION_COOKIE}=new-invalid"})[0], 429)
        self.auth.assert_not_called()

    def test_query_is_rejected_before_database_work(self):
        for path in ("/api/books?limit=999999", "/api/books?offset=-9", "/api/series?sort=invalid"):
            self.assertEqual(self.request(path)[0], 400)
        self.auth.assert_not_called()
        self.books.assert_not_called()

    def test_body_errors_are_explicit_and_not_truncated(self):
        for body, headers, expected in (
            ("{}", {"Content-Length": "65537"}, 413),
            ("[]", {}, 400), ("{bad", {}, 400),
            ("{}", {"Content-Type": "text/plain"}, 415),
            ("{}", {"Content-Length": "-1"}, 400),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(self.request("/api/preferences", "POST", body, headers)[0], expected)
        self.preferences.assert_not_called()

    def test_machine_sync_retains_larger_batches_and_isolated_budget(self):
        with patch("app.server.ingest_catalog_books", return_value={"accepted": 1}) as ingest:
            payload = json.dumps({"items": [], "padding": "x" * 70000})
            for _ in range(21):
                self.assertEqual(self.request("/api/catalog-sync/books", "POST", payload,
                    {"Authorization": "Bearer " + "t" * 32})[0], 202)
            self.assertEqual(ingest.call_count, 21)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request("/api/catalog-sync/books", "POST", "{}",
                                     {"Authorization": "Bearer wrong"})[0], 401)

    def test_internal_errors_redacted_and_headers_present(self):
        self.books.side_effect = RuntimeError("SQL password=secret-value table private")
        status, headers, data = self.request()
        self.assertEqual(status, 500)
        self.assertNotIn("secret-value", json.dumps(data))
        for key in ("X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy", "Referrer-Policy"):
            self.assertIn(key, headers)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_email_test_cooldown_and_owner_scope(self):
        with patch("app.server.send_test_email", return_value={"ok": True}) as send:
            self.assertEqual(self.request("/api/notifications/test-email", "POST", "{}")[0], 200)
            self.assertEqual(self.request("/api/notifications/test-email", "POST", "{}")[0], 429)
            self.assertEqual(self.request("/api/notifications/test-email", "POST", "{}",
                                         {"Cookie": f"{SESSION_COOKIE}=second"})[0], 200)
            self.assertEqual(send.call_count, 2)

    def test_health_probe_exempt_and_static_headers(self):
        with patch.object(Handler, "_health", lambda handler: handler._json({"ok": True})):
            for _ in range(25):
                self.assertEqual(self.request("/api/health")[0], 200)
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request("GET", "/")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("script-src 'self'", response.getheader("Content-Security-Policy"))
        response.read(); conn.close()

    def test_oauth_query_not_logged(self):
        handler = object.__new__(Handler)
        handler.path = "/auth/google/callback?code=secret-code&state=secret-state"
        handler.command = "GET"
        handler.log_date_time_string = lambda: "now"
        output = io.StringIO()
        with redirect_stdout(output):
            handler.log_message("%s %s %s", "GET /auth/google/callback?code=secret-code HTTP/1.1", "302", "-")
        self.assertNotIn("secret-", output.getvalue())
