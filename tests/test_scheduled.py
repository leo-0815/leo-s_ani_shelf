from __future__ import annotations

import unittest

from app.scheduled import SCHEDULED_SOURCES


class ScheduledSourceTests(unittest.TestCase):
    def test_partial_or_recheck_warning_exits_red_after_notifications(self):
        from contextlib import ExitStack
        from types import SimpleNamespace
        from unittest.mock import patch
        from app import scheduled
        settings = SimpleNamespace(public_url="https://example.test", discord_webhook_url="", notification_lead_days=(7,3,1,0))
        for job_status, recent_result in (("partial", {}), ("completed", {"spp": {"warning": True}})):
            with self.subTest(status=job_status), ExitStack() as stack:
                for name, value in (("get_settings", settings), ("ensure_schema", None),
                    ("run_incremental_sources", 1), ("get_job", {"id": 1, "status": job_status}),
                    ("refresh_recent_ratings", {}), ("run_recent_rechecks", recent_result)):
                    stack.enter_context(patch.object(scheduled, name, return_value=value))
                email = stack.enter_context(patch.object(scheduled, "deliver_email_notifications", return_value={}))
                discord = stack.enter_context(patch.object(scheduled, "deliver_notifications", return_value={}))
                status = stack.enter_context(patch.object(scheduled, "crawl_status_event", return_value=None))
                with self.assertRaises(SystemExit) as exc:
                    scheduled.main()
                self.assertEqual(exc.exception.code, 1)
                email.assert_called_once(); discord.assert_called_once()
                self.assertEqual(status.call_args.args[0]["status"], "partial")

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
            stack.enter_context(patch.object(scheduled,"run_recent_rechecks",side_effect=lambda: order.append("recent") or {}))
            stack.enter_context(patch.object(scheduled,"deliver_email_notifications",side_effect=lambda **kw: order.append("email") or {}))
            stack.enter_context(patch.object(scheduled,"deliver_notifications",side_effect=lambda **kw: order.append("discord") or {}))
            scheduled.main()
        self.assertEqual(order, ["rating","recent","email","discord"])

    def test_cloud_schedule_excludes_deferred_egmanga_source(self) -> None:
        self.assertEqual(
            SCHEDULED_SOURCES,
            ("tohan", "chingwin", "kadokawa", "tongli", "spp"),
        )
        self.assertNotIn("egmanga", SCHEDULED_SOURCES)


if __name__ == "__main__":
    unittest.main()
