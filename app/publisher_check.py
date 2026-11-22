"""Manually verify one repaired publisher against the normal cloud catalog path.

No email/Discord delivery, no other publishers, no full historical backfill.
"""
import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=("tohan", "spp"))
    args = parser.parse_args()
    from .db import ensure_schema
    from .crawler import run_incremental, get_job
    from .recent_recheck import backlog
    ensure_schema()
    job = get_job(run_incremental(args.source))
    print(json.dumps({"publisher": args.source, "job": job,
                      "pending_details": backlog(args.source)}, ensure_ascii=False, indent=2))
    if not job or job.get("status") != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
