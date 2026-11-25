import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.chingwin_feed_readiness import main, review_feed


def evidence():
    samples = []
    for index in range(5):
        prefix = f"97862600000{index}"
        isbn = prefix + str(-sum(int(d) * (1 if i % 2 == 0 else 3)
                                 for i, d in enumerate(prefix)) % 10)
        book = {"isbn": isbn, "title": f"測試作品({index + 1:02d})", "volume": str(index + 1),
                "publisher": "青文", "media": "print", "edition": "standard",
                "first_seen_at": "2026-11-03T08:00:00+08:00"}
        samples.append({"official": book, "feed": book | {"first_seen_at": "2026-11-04T08:00:00+08:00"}})
    return {
        "permission": {"provider_confirmed": True, "evidence_reference": "synthetic-test",
                       "allows_automated_daily_read": True, "formats": ["print_standard"]},
        "daily_checks": [{"date": f"2026-11-{day:02d}", "status": "ok"} for day in range(1, 8)],
        "samples": samples,
    }


class FeedReadinessTests(unittest.TestCase):
    def test_missing_permission_never_becomes_ready(self):
        bundle = evidence()
        bundle["permission"]["evidence_reference"] = ""
        result = review_feed(bundle)
        self.assertEqual(result["status"], "permission_not_recorded")
        self.assertFalse(result["catalog_write_allowed"])

    def test_good_evidence_is_only_a_manual_review_candidate(self):
        result = review_feed(evidence())
        self.assertEqual(result["status"], "candidate_for_manual_shadow_review")
        self.assertFalse(result["permission_independently_verified"])
        self.assertFalse(result["catalog_write_allowed"])
        self.assertFalse(result["network_requested"])

    def test_format_conflict_and_delayed_observation_do_not_count(self):
        bundle = evidence()
        bundle["samples"][0]["feed"].update(media="ebook", edition="digital")
        bundle["samples"][1]["feed"]["first_seen_at"] = "2026-11-06T08:00:00+08:00"
        result = review_feed(bundle)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["format_results"]["print_standard"], {
            "samples": 5, "on_time": 3, "late": 1, "unverified": 1,
        })

    def test_outage_and_missing_day_block_review(self):
        for issue in ("gap", "blocked"):
            with self.subTest(issue=issue):
                bundle = evidence()
                if issue == "gap":
                    bundle["daily_checks"].pop(2)
                    bundle["daily_checks"].append({"date": "2026-11-08", "status": "ok"})
                else:
                    bundle["daily_checks"][0]["status"] = issue
                self.assertEqual(review_feed(bundle)["status"], "insufficient_evidence")

    def test_duplicate_sample_cannot_inflate_coverage(self):
        bundle = evidence()
        bundle["samples"].append(copy.deepcopy(bundle["samples"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            review_feed(bundle)

    def test_permission_for_another_format_does_not_prove_coverage(self):
        bundle = evidence()
        bundle["permission"]["formats"].append("ebook")
        self.assertEqual(review_feed(bundle)["status"], "insufficient_evidence")

    def test_cli_never_fetches_or_discloses_permission_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.json"
            path.write_text(json.dumps(evidence()), encoding="utf-8")
            with patch("sys.argv", [__name__, str(path)]), patch("socket.create_connection") as network, patch(
                "builtins.print"
            ) as output:
                main()
        network.assert_not_called()
        self.assertNotIn("synthetic-test", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
