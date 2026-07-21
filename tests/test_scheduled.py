from __future__ import annotations

import unittest

from app.scheduled import SCHEDULED_SOURCES


class ScheduledSourceTests(unittest.TestCase):
    def test_cloud_schedule_excludes_deferred_egmanga_source(self) -> None:
        self.assertEqual(
            SCHEDULED_SOURCES,
            ("tohan", "chingwin", "kadokawa", "tongli", "spp"),
        )
        self.assertNotIn("egmanga", SCHEDULED_SOURCES)


if __name__ == "__main__":
    unittest.main()
