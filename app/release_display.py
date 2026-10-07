"""Read-only presentation metadata; dates are not proof of stock availability."""
from datetime import date, datetime, timedelta, timezone
from calendar import monthrange

TAIPEI = timezone(timedelta(hours=8))


def release_display(row: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(TAIPEI).date()
    raw_date = row.get("release_date")
    try:
        day = date.fromisoformat(str(raw_date)[:10]) if raw_date else None
    except (ValueError, TypeError):
        day = None
    try:
        checked = row.get("release_checked_at")
        checked = datetime.fromisoformat(str(checked).replace("Z", "+00:00")) if checked else None
        if checked and checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        checked = None
    status = row.get("release_status", "unknown")
    # A month estimate does not become overdue on the first of that month.
    due_day = date(day.year, day.month, monthrange(day.year, day.month)[1]) if day and row.get("release_precision") == "month" else day
    if status in {"cancelled", "delayed"}:
        display_status = status
    elif due_day and due_day <= today:
        confirmed = (row.get("release_date_source") == "product" and checked
                     and checked <= now and checked.astimezone(TAIPEI).date() >= due_day)
        display_status = "date_confirmed" if confirmed else "pending_confirmation"
    elif day and day > today:
        display_status = "scheduled"
    else:
        display_status = status
    return {"release_display_status": display_status,
            "release_date_confirmed": row.get("release_date_source") == "product" and bool(checked) and bool(day) and checked <= now,
            "release_checked_at_utc": checked.astimezone(timezone.utc).isoformat() if checked else None}
