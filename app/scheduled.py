from __future__ import annotations

import json
from datetime import datetime, timedelta

from .config import get_settings
from .crawler import get_job, run_incremental
from .db import ensure_schema
from .notifications import crawl_status_event, deliver_notifications


def main() -> None:
    settings = get_settings()
    ensure_schema()
    started_at = datetime.utcnow() - timedelta(minutes=1)
    job_id = run_incremental("all")
    job = get_job(job_id) or {"id": job_id, "status": "failed", "message": "找不到更新結果"}
    status_event = crawl_status_event(job)
    notification_result = deliver_notifications(
        webhook_url=settings.discord_webhook_url,
        since=started_at,
        lead_days=settings.notification_lead_days,
        public_url=settings.public_url,
        extra_events=[status_event] if status_event else [],
    )
    print(
        json.dumps(
            {"job": job, "notifications": notification_result},
            ensure_ascii=False,
            default=str,
            indent=2,
        )
    )
    if job.get("status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
