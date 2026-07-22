from __future__ import annotations

import unittest
from unittest.mock import patch

from app.history_runner import current_history_year
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


if __name__ == "__main__":
    unittest.main()
