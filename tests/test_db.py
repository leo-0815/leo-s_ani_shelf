from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config import Settings
from app.db import DatabaseUnavailable, connect


class _FakePyMySQL:
    def __init__(self) -> None:
        self.kwargs = None

    def connect(self, **kwargs):
        self.kwargs = kwargs
        return kwargs


def _settings(ssl_mode: str) -> Settings:
    return Settings(
        db_host="db.example.com",
        db_port=4000,
        db_name="anishelf",
        db_user="anishelf_app",
        db_password="test-password",
        db_ssl_mode=ssl_mode,
        host="0.0.0.0",
        port=10000,
        cloud_mode=True,
        auto_update=False,
    )


class DatabaseTlsTests(unittest.TestCase):
    def test_verify_identity_enables_certificate_and_hostname_checks(self) -> None:
        driver = _FakePyMySQL()
        with patch("app.db._driver", return_value=(driver, dict)):
            connect(_settings("verify_identity"))
        self.assertTrue(driver.kwargs["ssl_verify_cert"])
        self.assertTrue(driver.kwargs["ssl_verify_identity"])
        self.assertNotIn("ssl", driver.kwargs)

    def test_disabled_omits_tls_arguments(self) -> None:
        driver = _FakePyMySQL()
        with patch("app.db._driver", return_value=(driver, dict)):
            connect(_settings("disabled"))
        self.assertNotIn("ssl", driver.kwargs)
        self.assertNotIn("ssl_verify_cert", driver.kwargs)

    def test_unknown_ssl_mode_is_rejected(self) -> None:
        driver = _FakePyMySQL()
        with patch("app.db._driver", return_value=(driver, dict)):
            with self.assertRaises(DatabaseUnavailable):
                connect(_settings("mystery"))


if __name__ == "__main__":
    unittest.main()
