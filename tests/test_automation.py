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

    def test_workflow_runs_the_combined_update_and_notification_command(self) -> None:
        self.assertIn("python -m app.scheduled", self.workflow)
        self.assertIn("secrets.ANISHELF_DB_PASSWORD", self.workflow)
        self.assertIn("secrets.ANISHELF_DISCORD_WEBHOOK_URL", self.workflow)
        self.assertIn("secrets.ANISHELF_SMTP_PASSWORD", self.workflow)
        self.assertIn("secrets.ANISHELF_EMAIL_FROM", self.workflow)
        self.assertIn("python -m app.notifications --test-email", self.workflow)


if __name__ == "__main__":
    unittest.main()
