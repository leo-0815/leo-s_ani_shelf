from __future__ import annotations

import urllib.error
import unittest
from email.message import Message
from unittest.mock import MagicMock, patch

from app.sources.common import fetch_html


class SourceHttpTests(unittest.TestCase):
    @staticmethod
    def _http_error(status: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://example.test/catalog",
            status,
            "temporary error",
            Message(),
            None,
        )

    @staticmethod
    def _response(payload: bytes = b"ok") -> MagicMock:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = payload
        response.headers.get.return_value = ""
        response.headers.get_content_charset.return_value = "utf-8"
        return response

    @patch("app.sources.common.time.sleep")
    @patch("app.sources.common.random.uniform", return_value=0.0)
    @patch("app.sources.common.urllib.request.urlopen")
    def test_cloudflare_522_is_retried_until_success(
        self,
        urlopen: MagicMock,
        _random_uniform: MagicMock,
        sleep: MagicMock,
    ) -> None:
        urlopen.side_effect = [self._http_error(522), self._response(b"recovered")]

        self.assertEqual(fetch_html("https://example.test/catalog"), "recovered")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2.0)

    @patch("app.sources.common.time.sleep")
    @patch("app.sources.common.random.uniform", return_value=0.0)
    @patch("app.sources.common.urllib.request.urlopen")
    def test_cloudflare_522_reports_failure_after_attempt_limit(
        self,
        urlopen: MagicMock,
        _random_uniform: MagicMock,
        sleep: MagicMock,
    ) -> None:
        urlopen.side_effect = [self._http_error(522) for _ in range(3)]

        with self.assertRaisesRegex(RuntimeError, r"HTTP 522"):
            fetch_html("https://example.test/catalog", attempts=3)

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0, 5.0])

    @patch("app.sources.common.time.sleep")
    @patch("app.sources.common.urllib.request.urlopen")
    def test_non_retryable_http_error_fails_immediately(
        self,
        urlopen: MagicMock,
        sleep: MagicMock,
    ) -> None:
        urlopen.side_effect = self._http_error(403)

        with self.assertRaisesRegex(RuntimeError, r"HTTP 403"):
            fetch_html("https://example.test/catalog", attempts=5)

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
