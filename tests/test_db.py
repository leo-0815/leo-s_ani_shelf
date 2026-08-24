from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from app.config import Settings
from app.db import (
    DatabaseUnavailable,
    _ensure_followed_series_media_primary,
    _ensure_index,
    close_connection_pools,
    connect,
    transaction,
)


class _FakePyMySQL:
    def __init__(self) -> None:
        self.kwargs = None

    def connect(self, **kwargs):
        self.kwargs = kwargs
        return kwargs


class _FakeConnection:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.pings = 0
        self.closed = False
        self.fail_ping = False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def ping(self, reconnect: bool = True) -> None:
        self.pings += 1
        if self.fail_ping:
            raise OSError("stale connection")

    def close(self) -> None:
        self.closed = True


class _PoolDriver:
    def __init__(self) -> None:
        self.connections: list[_FakeConnection] = []
        self.lock = threading.Lock()

    def connect(self, **_kwargs):
        connection = _FakeConnection()
        with self.lock:
            self.connections.append(connection)
        return connection


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


class DatabaseMigrationTests(unittest.TestCase):
    def test_followed_series_primary_key_is_rebuilt_for_tidb(self) -> None:
        cursor = unittest.mock.MagicMock()
        cursor.fetchone.return_value = {
            "columns_list": "user_id,publisher_id,normalized_series"
        }

        _ensure_followed_series_media_primary(cursor)

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("CREATE TABLE followed_series_rebuild" in sql for sql in statements))
        self.assertTrue(any("INSERT IGNORE INTO followed_series_rebuild" in sql for sql in statements))
        self.assertTrue(any("RENAME TABLE followed_series" in sql for sql in statements))

    def test_followed_series_primary_key_is_left_when_current(self) -> None:
        cursor = unittest.mock.MagicMock()
        cursor.fetchone.return_value = {
            "columns_list": "user_id,publisher_id,normalized_series,media_type"
        }

        _ensure_followed_series_media_primary(cursor)

        cursor.execute.assert_called_once()

    def test_missing_index_is_added_once(self) -> None:
        cursor = unittest.mock.MagicMock()
        cursor.fetchone.return_value = {"present": 0}

        _ensure_index(cursor, "books", "idx_books_rating", "(`content_rating`)")

        self.assertEqual(cursor.execute.call_count, 2)
        self.assertIn("information_schema.statistics", cursor.execute.call_args_list[0].args[0])
        self.assertEqual(
            cursor.execute.call_args_list[1].args[0],
            "ALTER TABLE `books` ADD INDEX `idx_books_rating` (`content_rating`)",
        )

    def test_existing_index_is_not_recreated(self) -> None:
        cursor = unittest.mock.MagicMock()
        cursor.fetchone.return_value = {"present": 1}

        _ensure_index(cursor, "books", "idx_books_rating", "(`content_rating`)")

        cursor.execute.assert_called_once()


class DatabasePoolTests(unittest.TestCase):
    def setUp(self) -> None:
        close_connection_pools()

    def tearDown(self) -> None:
        close_connection_pools()

    def test_transactions_reuse_one_lazy_connection(self) -> None:
        settings = _settings("verify_identity")
        driver = _PoolDriver()
        with patch("app.db.get_settings", return_value=settings), patch(
            "app.db._driver", return_value=(driver, dict)
        ):
            with transaction() as first:
                pass
            with transaction() as second:
                pass

        self.assertIs(first, second)
        self.assertEqual(len(driver.connections), 1)
        self.assertEqual(first.commits, 2)
        self.assertEqual(first.pings, 0)

    def test_stale_idle_connection_is_replaced(self) -> None:
        settings = _settings("verify_identity")
        driver = _PoolDriver()
        with patch("app.db.get_settings", return_value=settings), patch(
            "app.db._driver", return_value=(driver, dict)
        ), patch("app.db.monotonic", side_effect=[0, 31, 31]):
            with transaction() as first:
                pass
            first.fail_ping = True
            with transaction() as second:
                pass

        self.assertIsNot(first, second)
        self.assertTrue(first.closed)
        self.assertEqual(len(driver.connections), 2)

    def test_concurrent_transactions_respect_pool_limit(self) -> None:
        settings = Settings(**{**_settings("verify_identity").__dict__, "db_pool_size": 3})
        driver = _PoolDriver()
        errors: list[Exception] = []

        def worker() -> None:
            try:
                with transaction():
                    time.sleep(0.02)
            except Exception as exc:  # pragma: no cover - assertion reports details
                errors.append(exc)

        with patch("app.db.get_settings", return_value=settings), patch(
            "app.db._driver", return_value=(driver, dict)
        ):
            threads = [threading.Thread(target=worker) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertLessEqual(len(driver.connections), 3)


if __name__ == "__main__":
    unittest.main()
