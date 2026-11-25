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
        for job_status, recent_result in (
                ("partial", {}),
                ("completed", {"spp": {"errors": ["HTTP 404"], "scan": "completed"}}),
                ("completed", {"chingwin": {"errors": [], "scan": "partial"}})):
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

    def test_backlog_age_and_old_failures_do_not_trigger_failure_notification(self):
        from contextlib import ExitStack
        from types import SimpleNamespace
        from unittest.mock import patch
        from app import scheduled
        from app.recent_recheck import _finish_recheck_result

        settings = SimpleNamespace(public_url="", discord_webhook_url="", notification_lead_days=(7,))
        recent = {"scan": "completed", "checked": 0, "errors": []}
        with patch("app.recent_recheck.backlog",
                   return_value={"pending": 3, "failed": 3, "oldest_days": 23}):
            _finish_recheck_result("tongli", recent)
        self.assertTrue(recent["backlog_warning"])
        self.assertFalse(recent["warning"])
        with ExitStack() as stack:
            for name, value in (("get_settings", settings), ("ensure_schema", None),
                ("run_incremental_sources", 1), ("get_job", {"id": 1, "status": "completed"}),
                ("refresh_recent_ratings", {}), ("run_recent_rechecks", {"tongli": recent}),
                ("deliver_email_notifications", {})):
                stack.enter_context(patch.object(scheduled, name, return_value=value))
            discord = stack.enter_context(patch.object(scheduled, "deliver_notifications", return_value={}))
            scheduled.main()
        self.assertEqual(discord.call_args.kwargs["extra_events"], [])

    def test_chingwin_block_is_used_by_both_later_stages_and_reset_next_run(self):
        from contextlib import ExitStack
        from types import SimpleNamespace
        from unittest.mock import patch
        from app import scheduled

        settings = SimpleNamespace(public_url="", discord_webhook_url="", notification_lead_days=(7,))
        def launch(sources, *, blocked_sources):
            blocked_sources.add("chingwin")
            return 1
        with ExitStack() as stack:
            for name, value in (("get_settings", settings), ("ensure_schema", None),
                ("deliver_email_notifications", {}), ("deliver_notifications", {})):
                stack.enter_context(patch.object(scheduled, name, return_value=value))
            run = stack.enter_context(patch.object(scheduled, "run_incremental_sources"))
            jobs = stack.enter_context(patch.object(scheduled, "get_job"))
            jobs.side_effect = [{"id": 1, "status": "partial"}, {"id": 2, "status": "completed"}]
            status = stack.enter_context(patch.object(scheduled, "crawl_status_event", return_value=None))
            rating = stack.enter_context(patch.object(scheduled, "refresh_recent_ratings", return_value={}))
            recent = stack.enter_context(patch.object(scheduled, "run_recent_rechecks", return_value={}))
            run.side_effect = launch
            with self.assertRaises(SystemExit):
                scheduled.main()
            rating.assert_called_once_with(skip_sources={"chingwin"})
            recent.assert_called_once_with(skip_sources={"chingwin"})
            self.assertTrue(status.call_args.args[0]["message"].startswith("青文官網 HTTP 403"))
            run.side_effect = None
            run.return_value = 2
            scheduled.main()
            self.assertEqual(run.call_args.kwargs["blocked_sources"], set())
            self.assertEqual(rating.call_args.kwargs, {})
            self.assertEqual(recent.call_args.kwargs, {})

    def test_landing_403_blocks_only_chingwin_and_preserves_partial_job(self):
        from contextlib import ExitStack
        from io import BytesIO
        from types import SimpleNamespace
        from unittest.mock import patch
        from urllib.error import HTTPError
        from app import crawler

        def blocked_collect(**kwargs):
            url = "https://www.ching-win.com.tw/about-news/"
            raise RuntimeError("HTTP 403: " + url) from HTTPError(url, 403, "Forbidden", None, BytesIO())

        cw = SimpleNamespace(code="chingwin", name="青文", collect=blocked_collect)
        other = SimpleNamespace(code="kadokawa", name="角川", collect=lambda **kwargs: [])
        blocked = set()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(crawler.SOURCES, {"chingwin": lambda: cw, "kadokawa": lambda: other}))
            for name, value in (("_start_job", None), ("get_sync_state", {}), ("known_source_keys", set()),
                                ("_write_records", 0), ("_mark_source", None), ("max_source_release_date", None),
                                ("save_sync_state", None)):
                stack.enter_context(patch.object(crawler, name, return_value=value))
            finish = stack.enter_context(patch.object(crawler, "_finish_job"))
            dates = stack.enter_context(patch.object(crawler, "_refresh_known_releases"))
            crawler._run_job(1, "scheduled", "incremental", ["chingwin", "kadokawa"],
                             blocked_sources=blocked)
        self.assertEqual(blocked, {"chingwin"})
        dates.assert_called_once()
        self.assertIs(dates.call_args.args[0], other)
        self.assertEqual(finish.call_args.args[1], "partial")
        self.assertEqual(finish.call_args.args[2]["errors"], 1)
        self.assertFalse(crawler._chingwin_landing_forbidden(RuntimeError("HTTP 403")))
        self.assertFalse(crawler._chingwin_landing_forbidden(
            HTTPError("https://www.ching-win.com.tw/product-detail/book", 403, "Forbidden", None, BytesIO())))

    def test_blocked_source_does_not_claim_later_stage_budgets(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        from app import rating_refresh, recent_recheck

        with ExitStack() as stack:
            stack.enter_context(patch.object(rating_refresh, "PARSER_VERSIONS", {"chingwin": 1}))
            stack.enter_context(patch.object(rating_refresh, "transaction"))
            candidates = stack.enter_context(patch.object(rating_refresh, "candidates"))
            budget = stack.enter_context(patch.object(rating_refresh, "claim_daily_budget"))
            self.assertEqual(rating_refresh.refresh_recent_ratings(skip_sources={"chingwin"})["checked"], 0)
            candidates.assert_not_called()
            budget.assert_not_called()

        with ExitStack() as stack:
            stack.enter_context(patch.object(recent_recheck, "SOURCES", ("chingwin",)))
            stack.enter_context(patch.object(recent_recheck, "backlog",
                return_value={"pending": 5, "failed": 2, "oldest_days": 23}))
            product = stack.enter_context(patch.object(recent_recheck, "claim_product"))
            scan = stack.enter_context(patch.object(recent_recheck, "claim_scan"))
            result = recent_recheck.run_recent_rechecks(skip_sources={"chingwin"})["chingwin"]
            self.assertEqual(result["scan"], "skipped_source_blocked")
            self.assertTrue(result["backlog_warning"])
            self.assertFalse(result["warning"])
            product.assert_not_called()
            scan.assert_not_called()

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
