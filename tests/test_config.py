from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import get_settings


class ConfigTests(unittest.TestCase):
    def test_local_defaults_keep_existing_behavior(self) -> None:
        with patch("app.config.load_dotenv"), patch.dict(os.environ, {}, clear=True):
            settings = get_settings()
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8765)
        self.assertFalse(settings.cloud_mode)
        self.assertTrue(settings.auto_update)
        self.assertEqual(settings.db_ssl_mode, "preferred")

    def test_render_uses_public_bind_port_and_disables_startup_update(self) -> None:
        with patch("app.config.load_dotenv"), patch.dict(
            os.environ,
            {"RENDER": "true", "PORT": "10000", "ANISHELF_DB_PASSWORD": "test"},
            clear=True,
        ):
            settings = get_settings()
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 10000)
        self.assertTrue(settings.cloud_mode)
        self.assertFalse(settings.auto_update)

    def test_explicit_values_override_cloud_defaults(self) -> None:
        with patch("app.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "RENDER": "true",
                "PORT": "10000",
                "ANISHELF_HOST": "127.0.0.2",
                "ANISHELF_AUTO_UPDATE": "yes",
            },
            clear=True,
        ):
            settings = get_settings()
        self.assertEqual(settings.host, "127.0.0.2")
        self.assertTrue(settings.auto_update)


if __name__ == "__main__":
    unittest.main()
