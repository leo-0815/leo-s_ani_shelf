from __future__ import annotations

import unittest
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import crawler, history_runner
from app.history_preflight import _load_snapshot
from app.history_runner import current_history_year, run_history_session
from app.crawler import HISTORY_SOURCES
from app.sources.common import history_cutoff, history_segment


class HistoryRunnerTests(unittest.TestCase):
    def test_history_job_covers_all_requested_publishers(self) -> None:
        self.assertEqual(
            HISTORY_SOURCES,
            ("chingwin", "kadokawa", "tohan", "spp", "tongli"),
        )

    @patch("app.history_runner.date")
    @patch("app.history_runner.get_backfill_progress")
    def test_selects_newest_unfinished_calendar_year(self, progress, mocked_date) -> None:
        mocked_date.today.return_value.year = 2026
        completed_2025 = {
            "history_v2_2025_comic": {"completed": True},
            "history_v2_2025_novel": {"completed": True},
            "history_v2_2025_catalog": {"completed": True},
        }
        progress.return_value = completed_2025
        self.assertEqual(current_history_year(), 2024)

    @patch.dict("os.environ", {"ANISHELF_HISTORY_YEAR": "2023"}, clear=False)
    def test_history_year_changes_cutoff_and_checkpoint_namespace(self) -> None:
        self.assertEqual(history_cutoff().isoformat(), "2023-01-01")
        self.assertEqual(history_segment("catalog"), "history_v2_2023_catalog")


class HistorySessionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.reports = Path(directory.name)
        self.progress = {source: {} for source in HISTORY_SOURCES}
        self._mock(history_runner, "REPORT_DIRECTORY", self.reports)
        self._mock(history_runner, "get_backfill_progress", side_effect=lambda source: self.progress[source])
        self._mock(history_runner, "date").today.return_value.year = 2026
        self.client = self._mock(history_runner, "CatalogSyncClient").return_value
        self.client.compare.return_value = {"same": 0, "local_only": [], "remote_only": [], "different": []}
        for method in ("pull", "push", "push_manifest_differences"):
            getattr(self.client, method).return_value = {}

    def _mock(self, *args, **kwargs):
        patcher = patch.object(*args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _report(self):
        return json.loads(next(self.reports.glob("history-*.json")).read_text(encoding="utf-8"))

    def test_partial_source_does_not_block_other_sources_or_years(self):
        self.progress["chingwin"]["history_v2_2025_comic"] = {"next_page": 143, "completed": False}
        calls = []

        def crawl(source, **kwargs):
            year = os.environ.get("ANISHELF_HISTORY_YEAR")
            calls.append((source, year))
            if source in {"spp", "tongli"} and year == "2025":
                self.progress[source]["history_v2_2025_catalog"] = {"next_page": 8, "completed": True}
            return len(calls)

        self._mock(history_runner, "run_backfill", side_effect=crawl)
        self._mock(history_runner, "get_job", side_effect=[
            {"status": status} for status in
            ("partial", "completed", "completed", "completed", "completed", "partial", "partial")
        ])
        with patch.dict(os.environ, {"ANISHELF_HISTORY_YEAR": "2001"}):
            result = run_history_session("continuous")
            self.assertEqual(os.environ["ANISHELF_HISTORY_YEAR"], "2001")
        self.assertEqual(calls, [
            ("chingwin", "2025"), ("kadokawa", None), ("tohan", None),
            ("spp", "2025"), ("tongli", "2025"), ("spp", "2024"), ("tongli", "2024"),
        ])
        self.assertEqual(result["stop_reason"], "sources_deferred")
        self.client.push.assert_called_once()
        report = self._report()
        self.assertEqual(report["checkpoints_before"]["chingwin"], report["checkpoints_after"]["chingwin"])
        self.assertTrue(report["checkpoints_after"]["spp"]["segments"]["history_v2_2025_catalog"]["completed"])
        self.assertEqual(_load_snapshot(self.reports)[0], report)

    def test_single_round_does_not_start_a_second_year(self):
        def crawl(source, **kwargs):
            if source in history_runner.EXPECTED_SEGMENTS:
                for segment in history_runner.EXPECTED_SEGMENTS[source]:
                    self.progress[source][f"history_v2_2025_{segment}"] = {"next_page": 8, "completed": True}
            return 9

        run = self._mock(history_runner, "run_backfill", side_effect=crawl)
        self._mock(history_runner, "get_job", return_value={"status": "completed"})
        result = run_history_session("year")
        self.assertEqual(run.call_count, 5)
        self.assertEqual(result["stop_reason"], "round_complete")
        self.assertEqual(result["next_year"], 2024)

    def test_completed_without_advancing_does_not_spin(self):
        crawl = self._mock(history_runner, "run_backfill", return_value=3)
        self._mock(history_runner, "get_job", return_value={"status": "completed"})
        result = run_history_session("continuous")
        self.assertEqual(crawl.call_count, 5)
        self.assertEqual(result["attempts"][0]["status"], "checkpoint_not_advanced")

    def test_expired_budget_never_starts_a_new_source(self):
        self._mock(history_runner.time, "monotonic", side_effect=[100, 1900])
        crawl = self._mock(history_runner, "run_backfill")
        result = run_history_session("30m")
        crawl.assert_not_called()
        self.assertEqual(result["stop_reason"], "time_limit")
        self.client.push.assert_called_once()

    def test_interrupt_stops_other_sources_and_still_syncs(self):
        crawl = self._mock(history_runner, "run_backfill", side_effect=KeyboardInterrupt)
        result = run_history_session("continuous")
        crawl.assert_called_once()
        self.client.push.assert_called_once()
        self.assertTrue(result["interrupted"])
        self.assertEqual(self._report()["result"]["attempts"][0]["status"], "interrupted")

    def test_sync_failure_saves_report_without_starting_crawl(self):
        self.client.pull.side_effect = RuntimeError("private error text")
        crawl = self._mock(history_runner, "run_backfill")
        with self.assertRaises(RuntimeError):
            run_history_session("year")
        crawl.assert_not_called()
        self.assertEqual(self._report()["result"]["stop_reason"], "initial_sync_failed")
        self.assertNotIn("private error text", json.dumps(self._report()))

    def test_failed_fetch_does_not_commit_the_failed_batch(self):
        source = MagicMock(name="chingwin")
        source.name = "chingwin"
        source.errors = []
        good = {"page": 142}
        bad = {"page": 143, "error_count": 1}
        source.collect_batches.return_value = [([], good), ([], bad)]
        self._mock(crawler, "SOURCES", {"chingwin": lambda: source})
        for name in ("_start_job", "_update_job_progress", "_mark_source", "known_source_keys"):
            self._mock(crawler, name)
        self._mock(crawler, "get_sync_state", return_value={})
        finish = self._mock(crawler, "_finish_job")
        crawler._run_job(1, "chingwin", "backfill")
        source.commit_batch.assert_called_once_with(good)
        self.assertEqual(finish.call_args.args[:2], (1, "partial"))


if __name__ == "__main__":
    unittest.main()
