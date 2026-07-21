from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
