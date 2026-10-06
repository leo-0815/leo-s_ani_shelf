from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.server import Handler, _clear_health_cache, _database_health, _instance_lock_path


class ServerInstanceTests(unittest.TestCase):
    def test_instance_lock_is_scoped_to_port(self) -> None:
        self.assertEqual(_instance_lock_path(8765).name, ".anishelf.8765.lock")
        self.assertNotEqual(_instance_lock_path(8765), _instance_lock_path(8877))

    @patch("app.server.ping", return_value={"version": "TiDB", "database_name": "anishelf"})
    def test_database_health_reuses_short_cache(self, ping: MagicMock) -> None:
        settings = MagicMock(
            db_host="db.example.com",
            db_port=4000,
            db_name="anishelf",
            db_user="anishelf",
            health_cache_seconds=20,
        )
        _clear_health_cache()

        first = _database_health(settings)
        second = _database_health(settings)

        self.assertEqual(first, second)
        self.assertTrue(first[0])
        ping.assert_called_once_with()

    def test_database_health_retries_failure_after_short_ttl(self) -> None:
        settings = MagicMock(
            db_host="db.example.com",
            db_port=4000,
            db_name="anishelf",
            db_user="anishelf",
            health_cache_seconds=20,
        )
        _clear_health_cache()
        with patch("app.server.monotonic", side_effect=[0, 1, 3]), patch(
            "app.server.ping",
            side_effect=[OSError("temporary outage"), {"version": "TiDB"}],
        ) as ping:
            first = _database_health(settings)
            cached = _database_health(settings)
            recovered = _database_health(settings)

        self.assertFalse(first[0])
        self.assertEqual(first, cached)
        self.assertTrue(recovered[0])
        self.assertEqual(ping.call_count, 2)


class ServerAuthorizationTests(unittest.TestCase):
    def test_preferences_save_targets_current_user_not_payload_user(self) -> None:
        handler = self.handler()
        handler.path = "/api/preferences"
        handler._require_user = lambda: {"id": 7, "role": "user"}
        handler._require_csrf = lambda user: True
        handler._body = lambda: {"general_audience": True, "user_id": 99}
        with patch("app.server.set_preferences", return_value={"general_audience": True}) as save:
            handler.do_POST()
        save.assert_called_once_with(7, {"general_audience": True, "user_id": 99})
        self.assertEqual(handler.responses[-1][1], 200)

    def test_preferences_write_is_csrf_protected(self) -> None:
        handler = self.handler()
        handler.path = "/api/preferences"
        handler._require_user = lambda: {"id": 7, "role": "user"}
        handler._require_csrf = lambda user: False
        with patch("app.server.set_preferences") as save:
            handler.do_POST()
        save.assert_not_called()

    def test_authenticated_browsing_scopes_do_not_leak_between_accounts(self) -> None:
        from urllib.parse import urlparse
        from app.preferences import visible_book_sql
        handler = self.handler()
        seen = []
        handler._visible_authenticated_get = lambda parsed, user: seen.append(visible_book_sql() == "1 = 1")
        handler._authenticated_get(urlparse("/api/books"), {"id": 7, "general_audience": True})
        handler._authenticated_get(urlparse("/api/books"), {"id": 8, "general_audience": False})
        handler._authenticated_get(urlparse("/api/export.json"), {"id": 7, "general_audience": True})
        self.assertEqual(seen, [False, True, True])
        self.assertEqual(visible_book_sql(), "1 = 1")

    def handler(self):
        handler = object.__new__(Handler)
        handler.headers = {}
        handler.responses = []
        handler._json = lambda payload, status=200, cookies=None: handler.responses.append(
            (payload, int(status))
        )
        return handler

    def test_regular_user_cannot_use_admin_route(self) -> None:
        handler = self.handler()
        self.assertFalse(handler._require_admin({"role": "user"}))
        self.assertEqual(handler.responses[-1][1], 403)

    def test_admin_can_use_admin_route(self) -> None:
        handler = self.handler()
        self.assertTrue(handler._require_admin({"role": "admin"}))
        self.assertEqual(handler.responses, [])

    def test_write_requires_matching_csrf_token(self) -> None:
        handler = self.handler()
        handler.headers = {"X-CSRF-Token": "correct"}
        self.assertTrue(handler._require_csrf({"csrf_token": "correct"}))
        handler.headers = {"X-CSRF-Token": "wrong"}
        self.assertFalse(handler._require_csrf({"csrf_token": "correct"}))
        self.assertEqual(handler.responses[-1][1], 403)

    @patch("app.server.get_settings")
    def test_catalog_sync_requires_bearer_token(self, get_settings: MagicMock) -> None:
        handler = self.handler()
        get_settings.return_value.catalog_sync_configured = True
        get_settings.return_value.catalog_sync_token = "s" * 40

        self.assertFalse(handler._require_catalog_sync())
        self.assertEqual(handler.responses[-1][1], 401)

        handler.headers = {"Authorization": f"Bearer {'s' * 40}"}
        self.assertTrue(handler._require_catalog_sync())

    @patch("app.server.get_settings")
    def test_catalog_sync_stays_closed_when_not_configured(
        self, get_settings: MagicMock
    ) -> None:
        handler = self.handler()
        get_settings.return_value.catalog_sync_configured = False
        self.assertFalse(handler._require_catalog_sync())
        self.assertEqual(handler.responses[-1][1], 503)

    @patch("app.server.send_test_email")
    @patch("app.server.get_settings")
    def test_email_smoke_test_targets_current_account(
        self,
        get_settings: MagicMock,
        send_test_email: MagicMock,
    ) -> None:
        handler = self.handler()
        handler.path = "/api/notifications/test-email"
        handler._require_user = lambda: {
            "id": 7,
            "email": "reader@example.com",
            "role": "user",
        }
        handler._require_csrf = lambda user: True
        handler._body = lambda: {}
        send_test_email.return_value = {"delivered_count": 1}

        handler.do_POST()

        send_test_email.assert_called_once_with(
            settings=get_settings.return_value,
            recipient="reader@example.com",
        )
        self.assertEqual(handler.responses[-1][1], 200)

    @patch("app.server.send_test_discord")
    def test_regular_user_cannot_send_discord_smoke_test(
        self,
        send_test_discord: MagicMock,
    ) -> None:
        handler = self.handler()
        handler.path = "/api/notifications/test-discord"
        handler._require_user = lambda: {
            "id": 7,
            "email": "reader@example.com",
            "role": "user",
            "is_admin": False,
        }
        handler._require_csrf = lambda user: True
        handler._body = lambda: {}

        handler.do_POST()

        send_test_discord.assert_not_called()
        self.assertEqual(handler.responses[-1][1], 403)

    @patch("app.server.dispatch_test_email_workflow")
    @patch("app.server.get_settings")
    def test_render_can_queue_email_test_through_github_actions(
        self,
        get_settings: MagicMock,
        dispatch_test_email_workflow: MagicMock,
    ) -> None:
        handler = self.handler()
        handler.path = "/api/notifications/test-email"
        handler._require_user = lambda: {
            "id": 7,
            "email": "reader@example.com",
            "role": "user",
        }
        handler._require_csrf = lambda user: True
        handler._body = lambda: {}
        get_settings.return_value.email_test_mode = "github_actions"
        dispatch_test_email_workflow.return_value = {"queued": True}

        handler.do_POST()

        dispatch_test_email_workflow.assert_called_once_with(
            settings=get_settings.return_value,
            user_id=7,
        )
        self.assertIn("1 分鐘內", handler.responses[-1][0]["message"])


if __name__ == "__main__":
    unittest.main()
