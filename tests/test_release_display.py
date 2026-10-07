from datetime import datetime, timezone
import unittest
from app.release_display import release_display
from app.repository import serialize_row
from app.models import catalog_sync_hash


class ReleaseDisplayTests(unittest.TestCase):
    now = datetime(2026, 10, 8, 3, tzinfo=timezone.utc)

    def row(self, **values):
        return dict(release_date="2026-10-05", release_precision="day",
                    release_status="available", **values)

    def test_overdue_schedule_is_not_claimed_available(self):
        result = release_display(self.row(), self.now)
        self.assertEqual(result["release_display_status"], "pending_confirmation")

    def test_product_checked_before_due_day_still_needs_confirmation(self):
        result = release_display(self.row(release_date_source="product",
            release_checked_at="2026-10-04T15:59:59"), self.now)
        self.assertEqual(result["release_display_status"], "pending_confirmation")

    def test_confirmation_uses_taipei_midnight(self):
        result = release_display(self.row(release_date_source="product",
            release_checked_at="2026-10-04T16:00:00"), self.now)
        self.assertEqual(result["release_display_status"], "date_confirmed")
        self.assertTrue(result["release_date_confirmed"])
        self.assertTrue(result["release_checked_at_utc"].endswith("+00:00"))

    def test_future_timestamp_never_proves_confirmation(self):
        result = release_display(self.row(release_date_source="product",
            release_checked_at="2030-10-04T16:00:00"), self.now)
        self.assertEqual(result["release_display_status"], "pending_confirmation")
        self.assertFalse(result["release_date_confirmed"])

    def test_invalid_metadata_does_not_break_api(self):
        result = release_display(self.row(release_checked_at="bad"), self.now)
        self.assertEqual(result["release_display_status"], "pending_confirmation")
        self.assertFalse(result["release_date_confirmed"])

    def test_month_estimate_waits_until_month_end(self):
        row = self.row()
        row.update(release_date="2026-10-01", release_precision="month", release_status="scheduled")
        self.assertEqual(release_display(row, self.now)["release_display_status"], "scheduled")
        later = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.assertEqual(release_display(row, later)["release_display_status"], "pending_confirmation")

    def test_cancelled_and_delayed_are_not_overridden(self):
        for status in ("cancelled", "delayed"):
            row = self.row()
            row["release_status"] = status
            self.assertEqual(release_display(row, self.now)["release_display_status"], status)

    def test_future_date_overrides_stale_display_status_not_stored_state(self):
        row = self.row()
        row["release_date"] = "2030-01-01"
        self.assertEqual(release_display(row, self.now)["release_display_status"], "scheduled")
        self.assertEqual(row["release_status"], "available")

    def test_serializer_preserves_hash_and_raw_state(self):
        row = self.row()
        result = serialize_row(row)
        self.assertEqual(catalog_sync_hash(result), catalog_sync_hash(row))
        self.assertEqual(result["release_status"], row["release_status"])
        self.assertIn("release_display_status", result)
