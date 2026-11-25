import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app import history_cli
from app.history_preflight import inspect_history_installation


class HistoryPreflightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "app").mkdir()
        (self.root / "app" / "history_cli.py").touch()
        (self.root / "歷史資料更新.bat").touch()
        self.reports = self.root / "logs" / "history-runs"

    def test_missing_reports_do_not_create_files_or_invent_a_year(self):
        result = inspect_history_installation(self.root)
        self.assertFalse(self.reports.exists())
        self.assertEqual(result["report_status"], "missing")
        self.assertIsNone(result["next_year"])
        self.assertFalse(result["automated_series_module_present"])

    def test_uses_last_valid_report_without_reporting_it_as_live(self):
        self.reports.mkdir(parents=True)
        (self.root / "app" / "series_backfill_runner.py").touch()
        report = self.reports / "history-20261125-174658-e7c1b7f6.json"
        report.write_text(json.dumps({
            "finished_at": "2026-11-25T17:46:58+08:00",
            "result": {"next_year": 2022},
            "checkpoints_after": {
                "chingwin": {"segments": {
                    "history_v2_2022_comic": {"next_page": 143, "completed": False},
                    "history_v2_2023_comic": {"next_page": 262, "completed": True},
                    "history_v2_bad": {"next_page": -1, "completed": True},
                }},
                "tohan": {"segments": {}},
                "kadokawa": {"unavailable": "not a current result"},
            },
        }), encoding="utf-8")
        (self.reports / "history-20261126-100000-00000000.json").write_text("{", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.reports.iterdir()}
        result = inspect_history_installation(self.root)
        self.assertEqual(result["report_path"], report)
        self.assertEqual(result["skipped_reports"], 1)
        self.assertEqual(result["next_year"], 2022)
        self.assertTrue(result["automated_series_module_present"])
        self.assertEqual(result["sources"]["chingwin"], {
            "status": "recorded", "complete": 1,
            "pending": [("history_v2_2022_comic", 143)], "invalid": 1,
        })
        self.assertEqual(result["sources"]["tohan"]["status"], "no_segments")
        self.assertEqual(result["sources"]["kadokawa"]["status"], "unavailable")
        self.assertEqual(result["sources"]["spp"]["status"], "no_snapshot")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.reports.iterdir()})

    def test_cli_runs_offline_and_does_not_enter_the_write_path(self):
        stdout = io.StringIO()
        with patch("app.db.ensure_schema") as schema, patch(
            "app.history_runner.run_history_session"
        ) as run, patch("socket.create_connection", side_effect=AssertionError("network forbidden")), patch(
            "builtins.input", side_effect=AssertionError("interactive menu not expected")
        ), redirect_stdout(stdout):
            history_cli.main(("--preflight", "--project-dir", str(self.root)))
        schema.assert_not_called()
        run.assert_not_called()
        self.assertIn("不連資料庫、不抓書、不上傳", stdout.getvalue())
        self.assertIn("未檢查即時斷點", stdout.getvalue())

    def test_menu_option_three_dispatches_preflight(self):
        with patch("builtins.input", return_value="3"), patch("app.history_preflight.show_history_preflight") as show:
            history_cli.main()
        show.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main()
