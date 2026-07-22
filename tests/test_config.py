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
        self.assertEqual(settings.public_url, "http://127.0.0.1:8765")
        self.assertFalse(settings.auth_configured)
        self.assertEqual(settings.notification_lead_days, (7, 3, 1, 0))
        self.assertEqual(settings.discord_webhook_url, "")
        self.assertFalse(settings.email_configured)
        self.assertEqual(settings.email_test_mode, "smtp")
        self.assertFalse(settings.github_email_test_configured)
        self.assertEqual(settings.db_pool_size, 4)
        self.assertEqual(settings.health_cache_seconds, 20)
        self.assertEqual(settings.session_touch_minutes, 10)

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
        self.assertEqual(settings.public_url, "")

    def test_auth_settings_and_admin_emails_are_normalized(self) -> None:
        with patch("app.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "ANISHELF_PUBLIC_URL": "https://shelf.example.com/",
                "ANISHELF_GOOGLE_CLIENT_ID": "client-id",
                "ANISHELF_GOOGLE_CLIENT_SECRET": "client-secret",
                "ANISHELF_ADMIN_EMAILS": "Owner@Example.com, second@example.com ",
                "ANISHELF_DISCORD_WEBHOOK_URL": "https://discord.example/webhook",
                "ANISHELF_NOTIFICATION_LEAD_DAYS": "1, 7,3,1,invalid,120",
                "ANISHELF_SMTP_HOST": "smtp.example.com",
                "ANISHELF_SMTP_PORT": "587",
                "ANISHELF_SMTP_USERNAME": "mailer@example.com",
                "ANISHELF_SMTP_PASSWORD": "app-password",
                "ANISHELF_EMAIL_FROM": "AniShelf <mailer@example.com>",
            },
            clear=True,
        ):
            settings = get_settings()
        self.assertTrue(settings.auth_configured)
        self.assertEqual(settings.public_url, "https://shelf.example.com")
        self.assertEqual(settings.admin_emails, ("owner@example.com", "second@example.com"))
        self.assertEqual(settings.discord_webhook_url, "https://discord.example/webhook")
        self.assertEqual(settings.notification_lead_days, (90, 7, 3, 1))
        self.assertTrue(settings.email_configured)
        self.assertEqual(settings.smtp_host, "smtp.example.com")

    def test_explicit_values_override_cloud_defaults(self) -> None:
        with patch("app.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "RENDER": "true",
                "PORT": "10000",
                "ANISHELF_HOST": "127.0.0.2",
                "ANISHELF_AUTO_UPDATE": "yes",
                "ANISHELF_DB_POOL_SIZE": "99",
                "ANISHELF_HEALTH_CACHE_SECONDS": "0",
                "ANISHELF_SESSION_TOUCH_MINUTES": "invalid",
            },
            clear=True,
        ):
            settings = get_settings()
        self.assertEqual(settings.host, "127.0.0.2")
        self.assertTrue(settings.auto_update)
        self.assertEqual(settings.db_pool_size, 10)
        self.assertEqual(settings.health_cache_seconds, 1)
        self.assertEqual(settings.session_touch_minutes, 10)

    def test_github_actions_email_test_requires_scoped_relay_settings(self) -> None:
        with patch("app.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "ANISHELF_EMAIL_TEST_MODE": "github_actions",
                "ANISHELF_GITHUB_ACTIONS_TOKEN": "token",
                "ANISHELF_GITHUB_REPOSITORY": "owner/repository",
            },
            clear=True,
        ):
            settings = get_settings()
        self.assertEqual(settings.email_test_mode, "github_actions")
        self.assertTrue(settings.github_email_test_configured)


if __name__ == "__main__":
    unittest.main()
