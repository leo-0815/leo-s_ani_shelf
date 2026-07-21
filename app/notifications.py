from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .config import get_settings
from .db import ensure_schema, transaction


TAIPEI = ZoneInfo("Asia/Taipei")
DISCORD_CHANNEL = "discord"
MAX_DISCORD_CONTENT = 1900


class NotificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class NotificationEvent:
    event_key: str
    event_type: str
    line: str
    user_id: int | None = None
    book_id: int | None = None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None


def _short(value: Any, limit: int = 90) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _date_label(value: Any) -> str:
    parsed = _as_date(value)
    return parsed.strftime("%Y/%m/%d") if parsed else "日期未定"


def _release_label(row: dict[str, Any]) -> str:
    parsed = _as_date(row.get("release_date"))
    if not parsed:
        return "日期未定"
    if row.get("release_precision") == "month":
        return parsed.strftime("%Y/%m")
    return parsed.strftime("%Y/%m/%d")


def milestone_event(row: dict[str, Any], today: date, lead_days: Iterable[int]) -> NotificationEvent | None:
    if row.get("release_precision") == "month":
        return None
    release_date = _as_date(row.get("release_date"))
    if not release_date:
        return None
    days_before = (release_date - today).days
    if days_before not in set(lead_days):
        return None
    timing = "今天上市" if days_before == 0 else f"{days_before} 天後上市"
    title = _short(row.get("title"))
    publisher = _short(row.get("publisher_name"), 30)
    return NotificationEvent(
        event_key=(
            f"release:{int(row['user_id'])}:{int(row['book_id'])}:"
            f"{release_date.isoformat()}:{days_before}"
        ),
        event_type="release_milestone",
        user_id=int(row["user_id"]),
        book_id=int(row["book_id"]),
        line=f"⏰ **{timing}**｜{_date_label(release_date)}｜{publisher}｜{title}",
    )


def date_change_event(row: dict[str, Any]) -> NotificationEvent:
    title = _short(row.get("title"))
    old_date = _date_label(row.get("old_value"))
    new_date = _date_label(row.get("new_value"))
    return NotificationEvent(
        event_key=f"date-change:{int(row['user_id'])}:{int(row['history_id'])}",
        event_type="release_date_changed",
        user_id=int(row["user_id"]),
        book_id=int(row["book_id"]),
        line=f"🔄 **上市日異動**｜{title}｜{old_date} → {new_date}",
    )


def followed_series_event(row: dict[str, Any]) -> NotificationEvent:
    title = _short(row.get("title"))
    publisher = _short(row.get("publisher_name"), 30)
    return NotificationEvent(
        event_key=f"followed-series:{int(row['user_id'])}:{int(row['book_id'])}",
        event_type="followed_series_new_book",
        user_id=int(row["user_id"]),
        book_id=int(row["book_id"]),
        line=f"✨ **追蹤系列新刊**｜{_release_label(row)}｜{publisher}｜{title}",
    )


def crawl_status_event(job: dict[str, Any]) -> NotificationEvent | None:
    status = str(job.get("status") or "")
    if status not in {"partial", "failed"}:
        return None
    label = "部分來源更新失敗" if status == "partial" else "資料更新失敗"
    details = _short(job.get("message") or "請查看 GitHub Actions 執行紀錄", 180)
    return NotificationEvent(
        event_key=f"crawl:{int(job['id'])}:{status}",
        event_type="crawl_status",
        line=f"⚠️ **{label}**｜工作 #{int(job['id'])}｜{details}",
    )


def collect_events(
    since: datetime,
    *,
    today: date | None = None,
    lead_days: Iterable[int] = (7, 3, 1, 0),
) -> list[NotificationEvent]:
    today = today or datetime.now(TAIPEI).date()
    lead_days = tuple(sorted({max(0, int(value)) for value in lead_days}, reverse=True))
    furthest = max(lead_days, default=0)
    events: list[NotificationEvent] = []
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS present FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = 'wishlist_items' "
                "AND column_name = 'user_id'"
            )
            if not cursor.fetchone()["present"]:
                return []
            cursor.execute(
                "SELECT u.id AS user_id, b.id AS book_id, b.title, b.release_date, "
                "p.name AS publisher_name FROM users u "
                "JOIN wishlist_items w ON w.user_id = u.id "
                "JOIN books b ON b.id = w.book_id "
                "JOIN publishers p ON p.id = b.publisher_id "
                "WHERE u.role = 'admin' AND u.is_active = TRUE "
                "AND w.state IN ('wanted', 'preordered') "
                "AND b.release_precision = 'day' "
                "AND b.release_date BETWEEN %s AND %s "
                "ORDER BY b.release_date, b.title",
                (today, today + timedelta(days=furthest)),
            )
            for row in cursor.fetchall():
                event = milestone_event(row, today, lead_days)
                if event:
                    events.append(event)

            cursor.execute(
                "SELECT u.id AS user_id, b.id AS book_id, b.title, rh.id AS history_id, "
                "rh.old_value, rh.new_value FROM release_history rh "
                "JOIN books b ON b.id = rh.book_id "
                "JOIN wishlist_items w ON w.book_id = b.id "
                "JOIN users u ON u.id = w.user_id "
                "WHERE u.role = 'admin' AND u.is_active = TRUE "
                "AND w.state IN ('wanted', 'preordered') "
                "AND rh.field_name = 'release_date' AND rh.observed_at >= %s "
                "AND NOT (rh.old_value <=> rh.new_value) ORDER BY rh.id",
                (since,),
            )
            events.extend(date_change_event(row) for row in cursor.fetchall())

            cursor.execute(
                "SELECT u.id AS user_id, b.id AS book_id, b.title, b.release_date, "
                "b.release_precision, "
                "p.name AS publisher_name FROM users u "
                "JOIN wishlist_items w ON w.user_id = u.id "
                "JOIN books b ON b.id = w.book_id "
                "JOIN publishers p ON p.id = b.publisher_id "
                "WHERE u.role = 'admin' AND u.is_active = TRUE "
                "AND w.state IN ('wanted', 'preordered') AND w.follow_series = TRUE "
                "AND b.first_seen_at >= %s ORDER BY b.first_seen_at, b.id",
                (since,),
            )
            events.extend(followed_series_event(row) for row in cursor.fetchall())
    return _unique_events(events)


def _unique_events(events: Iterable[NotificationEvent]) -> list[NotificationEvent]:
    unique: dict[str, NotificationEvent] = {}
    for event in events:
        unique.setdefault(event.event_key, event)
    return list(unique.values())


def undelivered_events(events: Iterable[NotificationEvent]) -> list[NotificationEvent]:
    candidates = _unique_events(events)
    if not candidates:
        return []
    delivered: set[str] = set()
    with transaction() as connection:
        with connection.cursor() as cursor:
            for start in range(0, len(candidates), 200):
                keys = [event.event_key for event in candidates[start : start + 200]]
                placeholders = ", ".join(["%s"] * len(keys))
                cursor.execute(
                    "SELECT event_key FROM notification_deliveries "
                    f"WHERE channel = %s AND event_key IN ({placeholders})",
                    [DISCORD_CHANNEL, *keys],
                )
                delivered.update(str(row["event_key"]) for row in cursor.fetchall())
    return [event for event in candidates if event.event_key not in delivered]


def mark_delivered(events: Iterable[NotificationEvent]) -> None:
    rows = [
        (event.user_id, event.book_id, DISCORD_CHANNEL, event.event_type, event.event_key)
        for event in events
    ]
    if not rows:
        return
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT IGNORE INTO notification_deliveries "
                "(user_id, book_id, channel, event_type, event_key) VALUES (%s, %s, %s, %s, %s)",
                rows,
            )


def event_batches(events: Iterable[NotificationEvent], public_url: str = "") -> list[list[NotificationEvent]]:
    batches: list[list[NotificationEvent]] = []
    current: list[NotificationEvent] = []
    header = "📚 **AniShelf 通知**\n"
    footer = f"\n<{public_url}>" if public_url else ""
    current_length = len(header) + len(footer)
    for event in events:
        addition = len(event.line) + 3
        if current and (len(current) >= 10 or current_length + addition > MAX_DISCORD_CONTENT):
            batches.append(current)
            current = []
            current_length = len(header) + len(footer)
        current.append(event)
        current_length += addition
    if current:
        batches.append(current)
    return batches


def format_batch(events: Iterable[NotificationEvent], public_url: str = "") -> str:
    lines = ["📚 **AniShelf 通知**", *(f"• {event.line}" for event in events)]
    if public_url:
        lines.extend(["", f"<{public_url}>"])
    return "\n".join(lines)


def post_discord(webhook_url: str, content: str) -> None:
    payload = json.dumps(
        {"content": content, "allowed_mentions": {"parse": []}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "AniShelf/1.0"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            if response.status not in {200, 204}:
                raise NotificationError(f"Discord webhook returned HTTP {response.status}")
    except HTTPError as exc:
        raise NotificationError(f"Discord webhook returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise NotificationError(f"Discord webhook connection failed: {exc.reason}") from exc


def deliver_notifications(
    *,
    webhook_url: str,
    since: datetime,
    lead_days: Iterable[int],
    public_url: str = "",
    today: date | None = None,
    extra_events: Iterable[NotificationEvent] = (),
    dry_run: bool = False,
    sender: Callable[[str, str], None] = post_discord,
) -> dict[str, Any]:
    if not webhook_url and not dry_run:
        return {"enabled": False, "candidate_count": 0, "delivered_count": 0, "messages": []}
    events = collect_events(since, today=today, lead_days=lead_days)
    events = _unique_events([*extra_events, *events])
    pending = events if dry_run else undelivered_events(events)
    batches = event_batches(pending, public_url)
    messages = [format_batch(batch, public_url) for batch in batches]
    if not dry_run:
        for batch, message in zip(batches, messages):
            sender(webhook_url, message)
            mark_delivered(batch)
    return {
        "enabled": bool(webhook_url),
        "dry_run": dry_run,
        "candidate_count": len(events),
        "delivered_count": 0 if dry_run else len(pending),
        "messages": messages if dry_run else [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or send AniShelf Discord notifications")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--since-hours", type=int, default=36)
    args = parser.parse_args()
    ensure_schema()
    settings = get_settings()
    result = deliver_notifications(
        webhook_url=settings.discord_webhook_url,
        since=datetime.utcnow() - timedelta(hours=max(1, args.since_hours)),
        lead_days=settings.notification_lead_days,
        public_url=settings.public_url,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
