from __future__ import annotations

import unittest

from app.scheduled import SCHEDULED_SOURCES


class ScheduledSourceTests(unittest.TestCase):
    def test_daily_entry_refreshes_grades_before_notifications(self):
        from contextlib import ExitStack
        from types import SimpleNamespace
        from unittest.mock import patch
        from app import scheduled
        order = []
        settings = SimpleNamespace(public_url="https://example.test", discord_webhook_url="",
                                   notification_lead_days=(7,3,1,0))
        with ExitStack() as stack:
            stack.enter_context(patch.object(scheduled,"get_settings",return_value=settings))
            stack.enter_context(patch.object(scheduled,"ensure_schema"))
            stack.enter_context(patch.object(scheduled,"run_incremental_sources",return_value=1))
            stack.enter_context(patch.object(scheduled,"get_job",return_value={"id":1,"status":"completed"}))
            stack.enter_context(patch.object(scheduled,"crawl_status_event",return_value=None))
            stack.enter_context(patch.object(scheduled,"refresh_recent_ratings",side_effect=lambda: order.append("rating") or {}))
            stack.enter_context(patch.object(scheduled,"deliver_email_notifications",side_effect=lambda **kw: order.append("email") or {}))
            stack.enter_context(patch.object(scheduled,"deliver_notifications",side_effect=lambda **kw: order.append("discord") or {}))
            scheduled.main()
        self.assertEqual(order, ["rating","email","discord"])

    def test_cloud_schedule_excludes_deferred_egmanga_source(self) -> None:
        self.assertEqual(
            SCHEDULED_SOURCES,
            ("tohan", "chingwin", "kadokawa", "tongli", "spp"),
        )
        self.assertNotIn("egmanga", SCHEDULED_SOURCES)


if __name__ == "__main__":
    unittest.main()
