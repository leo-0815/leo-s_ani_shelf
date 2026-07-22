from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.server import Handler, _instance_lock_path


class ServerInstanceTests(unittest.TestCase):
    def test_instance_lock_is_scoped_to_port(self) -> None:
        self.assertEqual(_instance_lock_path(8765).name, ".anishelf.8765.lock")
        self.assertNotEqual(_instance_lock_path(8765), _instance_lock_path(8877))


class ServerAuthorizationTests(unittest.TestCase):
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
