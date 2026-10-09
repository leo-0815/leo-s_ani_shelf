from __future__ import annotations

import unittest

from app.config import ROOT


class ScheduledWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (ROOT / ".github" / "workflows" / "scheduled-update.yml").read_text(
            encoding="utf-8"
        )

    def test_daily_schedule_and_manual_dispatch_are_available(self) -> None:
        self.assertIn('cron: "15 0 * * *"', self.workflow)
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("test_email_only:", self.workflow)
        self.assertIn("test_database_only:", self.workflow)
        self.assertIn("migrate_database_only:", self.workflow)
        self.assertIn("test_email_user_id:", self.workflow)

    def test_workflow_runs_the_combined_update_and_notification_command(self) -> None:
        self.assertIn("python -m app.scheduled", self.workflow)
        self.assertIn("secrets.ANISHELF_DB_PASSWORD", self.workflow)
        self.assertIn("secrets.ANISHELF_DISCORD_WEBHOOK_URL", self.workflow)
        self.assertIn("secrets.ANISHELF_SMTP_PASSWORD", self.workflow)
        self.assertIn("secrets.ANISHELF_EMAIL_FROM", self.workflow)
        self.assertIn("python -m app.notifications --test-email", self.workflow)
        self.assertIn("python -m app.database_check", self.workflow)
        self.assertIn("python anishelf.py migrate", self.workflow)
        self.assertIn('python -m app.notifications --test-user-id "$ANISHELF_TEST_USER_ID"', self.workflow)


class FrontendBootstrapTests(unittest.TestCase):
    def test_home_bootstrap_does_not_prefetch_library_or_redundant_health(self) -> None:
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        load_all = script.split("async function loadAll()", 1)[1].split(
            "async function loadStats()", 1
        )[0]

        self.assertNotIn("/api/health", load_all)
        self.assertNotIn("loadBooks()", load_all)
        self.assertIn('switchView(location.hash.slice(1) || "home", true)', load_all)


if __name__ == "__main__":
    unittest.main()
