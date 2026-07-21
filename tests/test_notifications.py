from __future__ import annotations

import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.notifications import (
    MAX_DISCORD_CONTENT,
    EMAIL_CHANNEL,
    NotificationEvent,
    crawl_status_event,
    date_change_event,
    event_batches,
    format_email,
    format_batch,
    followed_series_event,
    milestone_event,
    deliver_notifications,
    deliver_email_notifications,
    send_test_email,
)


class NotificationTests(unittest.TestCase):
    def test_release_milestones_only_use_configured_days(self) -> None:
        row = {
            "user_id": 4,
            "book_id": 12,
            "title": "測試新刊",
            "publisher_name": "測試出版社",
            "release_date": date(2026, 7, 29),
        }
        event = milestone_event(row, date(2026, 7, 22), (7, 3, 1, 0))
        self.assertIsNotNone(event)
        self.assertEqual(event.event_key, "release:4:12:2026-07-29:7")
        self.assertIn("7 天後上市", event.line)
        self.assertIsNone(milestone_event(row, date(2026, 7, 23), (7, 3, 1, 0)))
        self.assertIsNone(
            milestone_event({**row, "release_precision": "month"}, date(2026, 7, 22), (7,))
        )

    def test_today_milestone_accepts_available_book(self) -> None:
        row = {
            "user_id": 1,
            "book_id": 2,
            "title": "今天的新書",
            "publisher_name": "出版社",
            "release_date": "2026-07-22",
            "release_status": "available",
        }
        event = milestone_event(row, date(2026, 7, 22), (0,))
        self.assertIn("今天上市", event.line)

    def test_date_change_and_followed_series_keys_are_stable(self) -> None:
        changed = date_change_event(
            {
                "user_id": 1,
                "book_id": 9,
                "history_id": 33,
                "title": "延期作品",
                "old_value": "2026-08-01",
                "new_value": "2026-08-15",
            }
        )
        followed = followed_series_event(
            {
                "user_id": 1,
                "book_id": 10,
                "title": "系列新刊",
                "publisher_name": "出版社",
                "release_date": None,
            }
        )
        self.assertEqual(changed.event_key, "date-change:1:33")
        self.assertIn("2026/08/01 → 2026/08/15", changed.line)
        self.assertEqual(followed.event_key, "followed-series:1:10")
        self.assertIn("日期未定", followed.line)

    def test_crawl_alert_only_reports_partial_or_failed_jobs(self) -> None:
        self.assertIsNone(crawl_status_event({"id": 1, "status": "completed"}))
        event = crawl_status_event({"id": 2, "status": "partial", "message": "one source failed"})
        self.assertIsNotNone(event)
        self.assertEqual(event.event_key, "crawl:2:partial")

    def test_discord_batches_stay_under_limit(self) -> None:
        events = [
            NotificationEvent(f"event:{index}", "test", "測試內容" * 50)
            for index in range(30)
        ]
        batches = event_batches(events, "https://anishelf.example.com")
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(len(batch) for batch in batches), len(events))
        for batch in batches:
            self.assertLessEqual(len(format_batch(batch, "https://anishelf.example.com")), MAX_DISCORD_CONTENT)

    def test_successful_delivery_is_recorded_after_sending(self) -> None:
        event = NotificationEvent("release:1:2:2026-07-29:7", "release_milestone", "測試通知", 1, 2)
        sent: list[tuple[str, str]] = []
        with patch("app.notifications.collect_events", return_value=[event]), patch(
            "app.notifications.undelivered_events", return_value=[event]
        ), patch("app.notifications.mark_delivered") as mark_delivered:
            result = deliver_notifications(
                webhook_url="https://discord.example/webhook",
                since=datetime(2026, 7, 22),
                lead_days=(7, 3, 1, 0),
                sender=lambda url, message: sent.append((url, message)),
            )
        self.assertEqual(result["delivered_count"], 1)
        self.assertEqual(len(sent), 1)
        mark_delivered.assert_called_once_with([event])

    def test_email_digest_is_grouped_per_user_and_recorded_separately(self) -> None:
        event = NotificationEvent(
            "release:7:12:2026-07-29:7",
            "release_milestone",
            "⏰ **7 天後上市**｜測試新刊",
            7,
            12,
            "reader@example.com",
            "讀者",
        )
        sent = []
        settings = SimpleNamespace(email_configured=True)
        with patch("app.notifications.collect_email_events", return_value=[event]), patch(
            "app.notifications.undelivered_events", return_value=[event]
        ), patch("app.notifications.mark_delivered") as mark_delivered:
            result = deliver_email_notifications(
                since=datetime(2026, 7, 22),
                public_url="https://anishelf.example.com",
                settings=settings,
                sender=lambda *args: sent.append(args),
            )
        self.assertEqual(result["recipient_count"], 1)
        self.assertEqual(result["delivered_count"], 1)
        self.assertEqual(sent[0][1], "reader@example.com")
        mark_delivered.assert_called_once_with([event], EMAIL_CHANNEL)

    def test_email_body_contains_settings_link_without_discord_markdown(self) -> None:
        event = NotificationEvent("event:1", "test", "🔄 **上市日異動**｜<測試>")
        subject, plain, html = format_email(
            [event],
            "讀者",
            "https://anishelf.example.com",
        )
        self.assertIn("1 則更新", subject)
        self.assertNotIn("**", plain)
        self.assertIn("/#notifications", plain)
        self.assertIn("&lt;測試&gt;", html)

    def test_explicit_email_smoke_test_uses_smtp_username_without_delivery_record(self) -> None:
        sent = []
        settings = SimpleNamespace(
            email_configured=True,
            smtp_username="mailer@example.com",
        )
        result = send_test_email(
            settings=settings,
            sender=lambda *args: sent.append(args),
        )
        self.assertEqual(result["delivered_count"], 1)
        self.assertEqual(result["recipient_count"], 1)
        self.assertEqual(sent[0][1], "mailer@example.com")
        self.assertIn("AniShelf", sent[0][2])


if __name__ == "__main__":
    unittest.main()
