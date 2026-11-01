from __future__ import annotations

import json
from datetime import datetime, timedelta

from .config import get_settings
from .crawler import get_job, run_incremental_sources
from .db import ensure_schema
from .rating_refresh import refresh_recent_ratings
from .recent_recheck import run_recent_rechecks
from .notifications import (
    NotificationError,
    crawl_status_event,
    deliver_email_notifications,
    deliver_notifications,
    email_status_event,
)


SCHEDULED_SOURCES = ("tohan", "chingwin", "kadokawa", "tongli", "spp")


def main() -> None:
    settings = get_settings()
    ensure_schema()
    started_at = datetime.utcnow() - timedelta(minutes=1)
    job_id = run_incremental_sources(SCHEDULED_SOURCES)
    job = get_job(job_id) or {"id": job_id, "status": "failed", "message": "找不到更新結果"}
    try:
        rating_result = refresh_recent_ratings()
    except Exception as exc:
        # Preserve notifications if the independent grading pass fails.
        rating_result = {"error": str(exc)}
    try:
        recent_result = run_recent_rechecks()
    except Exception as exc:
        recent_result = {"error": str(exc)}
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
    recent_warnings = [code for code, result in recent_result.items()
                       if isinstance(result, dict) and result.get("warning")]
    status_job = dict(job)
    if recent_result.get("error") or recent_warnings:
        status_job["status"] = "failed" if job.get("status") == "failed" else "partial"
        status_job["message"] = "近期防漏複查需注意：" + (str(recent_result.get("error")) or "、".join(recent_warnings)) + "\n" + str(job.get("message") or "")
    status_event = crawl_status_event(status_job)
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
            {"job": job, "rating_refresh": rating_result, "recent_recheck": recent_result,
             "discord": discord_result, "email": email_result},
            ensure_ascii=False,
            default=str,
            indent=2,
        )
    )
    if job.get("status") in {"partial", "failed"} or email_failure or recent_result.get("error") or recent_warnings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
