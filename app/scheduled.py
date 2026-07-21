from __future__ import annotations

import json
from datetime import datetime, timedelta

from .config import get_settings
from .crawler import get_job, run_incremental
from .db import ensure_schema
from .notifications import (
    NotificationError,
    crawl_status_event,
    deliver_email_notifications,
    deliver_notifications,
    email_status_event,
)


def main() -> None:
    settings = get_settings()
    ensure_schema()
    started_at = datetime.utcnow() - timedelta(minutes=1)
    job_id = run_incremental("all")
    job = get_job(job_id) or {"id": job_id, "status": "failed", "message": "找不到更新結果"}
    status_event = crawl_status_event(job)
    email_failure = None
    try:
        email_result = deliver_email_notifications(
            since=started_at,
            public_url=settings.public_url,
            settings=settings,
        )
        if email_result.get("configuration_error"):
            email_failure = str(email_result["configuration_error"])
    except NotificationError as exc:
        email_failure = str(exc)
        email_result = {"enabled": True, "error": email_failure}
    extra_events = [event for event in (status_event,) if event]
    if email_failure:
        extra_events.append(email_status_event(job_id, email_failure))
    discord_result = deliver_notifications(
        webhook_url=settings.discord_webhook_url,
        since=started_at,
        lead_days=settings.notification_lead_days,
        public_url=settings.public_url,
        extra_events=extra_events,
    )
    print(
        json.dumps(
            {"job": job, "discord": discord_result, "email": email_result},
            ensure_ascii=False,
            default=str,
            indent=2,
        )
    )
    if job.get("status") == "failed" or email_failure:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
