from __future__ import annotations

import argparse
import json
import re
import smtplib
import ssl
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .config import Settings, get_settings
from .db import ensure_schema, transaction


TAIPEI = ZoneInfo("Asia/Taipei")
DISCORD_CHANNEL = "discord"
EMAIL_CHANNEL = "email"
MAX_DISCORD_CONTENT = 1900
DEFAULT_LEAD_DAYS = (7, 3, 1, 0)


class NotificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class NotificationEvent:
    event_key: str
    event_type: str
    line: str
    user_id: int | None = None
    book_id: int | None = None
    recipient_email: str = ""
    recipient_name: str = ""


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
        recipient_email=str(row.get("email") or ""),
        recipient_name=str(row.get("display_name") or ""),
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
        recipient_email=str(row.get("email") or ""),
        recipient_name=str(row.get("display_name") or ""),
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
        recipient_email=str(row.get("email") or ""),
        recipient_name=str(row.get("display_name") or ""),
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


def email_status_event(job_id: int, message: str) -> NotificationEvent:
    return NotificationEvent(
        event_key=f"email-delivery:{job_id}:{datetime.utcnow().strftime('%Y%m%d%H')}",
        event_type="email_delivery_failed",
        line=f"⚠️ **Email 通知寄送失敗**｜{_short(message, 180)}",
    )


def _parse_lead_days(value: Any) -> tuple[int, ...]:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value or "").split(",")
    parsed = {
        min(max(int(str(item).strip()), 0), 90)
        for item in values
        if str(item).strip().isdigit()
    }
    return tuple(sorted(parsed, reverse=True)) or DEFAULT_LEAD_DAYS


def get_notification_preferences(user_id: int) -> dict[str, Any]:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT IGNORE INTO notification_preferences (user_id) VALUES (%s)",
                (user_id,),
            )
            cursor.execute(
                "SELECT u.email, np.email_enabled, np.lead_days, "
                "np.notify_release_date_changes, np.notify_followed_series, np.updated_at "
                "FROM notification_preferences np JOIN users u ON u.id = np.user_id "
                "WHERE np.user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
    if not row:
        raise ValueError("找不到通知設定")
    return {
        "email": str(row["email"]),
        "email_enabled": bool(row["email_enabled"]),
        "lead_days": list(_parse_lead_days(row["lead_days"])),
        "notify_release_date_changes": bool(row["notify_release_date_changes"]),
        "notify_followed_series": bool(row["notify_followed_series"]),
        "updated_at": row.get("updated_at"),
    }


def set_notification_preferences(user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    lead_days = _parse_lead_days(payload.get("lead_days", DEFAULT_LEAD_DAYS))
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO notification_preferences "
                "(user_id, email_enabled, lead_days, notify_release_date_changes, "
                "notify_followed_series) VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE email_enabled = VALUES(email_enabled), "
                "lead_days = VALUES(lead_days), "
                "notify_release_date_changes = VALUES(notify_release_date_changes), "
                "notify_followed_series = VALUES(notify_followed_series)",
                (
                    user_id,
                    bool(payload.get("email_enabled", False)),
                    ",".join(str(day) for day in lead_days),
                    bool(payload.get("notify_release_date_changes", True)),
                    bool(payload.get("notify_followed_series", True)),
                ),
            )
    return get_notification_preferences(user_id)


def email_subscriber_count() -> int:
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM notification_preferences np "
                "JOIN users u ON u.id = np.user_id "
                "WHERE np.email_enabled = TRUE AND u.is_active = TRUE"
            )
            return int(cursor.fetchone()["count"])


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
                "JOIN followed_series fs ON fs.user_id = u.id "
                "JOIN books b ON b.publisher_id = fs.publisher_id "
                "AND LEFT(LOWER(b.series_title), 190) = fs.normalized_series "
                "JOIN publishers p ON p.id = b.publisher_id "
                "WHERE u.role = 'admin' AND u.is_active = TRUE "
                "AND b.first_seen_at >= %s ORDER BY b.first_seen_at, b.id",
                (since,),
            )
            events.extend(followed_series_event(row) for row in cursor.fetchall())
    return _unique_events(events)


def collect_email_events(
    since: datetime,
    *,
    today: date | None = None,
) -> list[NotificationEvent]:
    """Collect opted-in events for every active account.

    Discord deliberately keeps using ``collect_events`` so it remains an
    administrator-only channel. Email preferences never affect Discord.
    """
    today = today or datetime.now(TAIPEI).date()
    events: list[NotificationEvent] = []
    with transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT u.id AS user_id, u.email, u.display_name, np.lead_days, "
                "b.id AS book_id, b.title, b.release_date, b.release_precision, "
                "p.name AS publisher_name FROM notification_preferences np "
                "JOIN users u ON u.id = np.user_id "
                "JOIN wishlist_items w ON w.user_id = u.id "
                "JOIN books b ON b.id = w.book_id "
                "JOIN publishers p ON p.id = b.publisher_id "
                "WHERE np.email_enabled = TRUE AND u.is_active = TRUE "
                "AND w.state IN ('wanted', 'preordered') "
                "AND b.release_precision = 'day' "
                "AND b.release_date BETWEEN %s AND %s "
                "ORDER BY u.id, b.release_date, b.title",
                (today, today + timedelta(days=90)),
            )
            for row in cursor.fetchall():
                event = milestone_event(row, today, _parse_lead_days(row["lead_days"]))
                if event:
                    events.append(event)

            cursor.execute(
                "SELECT u.id AS user_id, u.email, u.display_name, b.id AS book_id, "
                "b.title, rh.id AS history_id, rh.old_value, rh.new_value "
                "FROM notification_preferences np "
                "JOIN users u ON u.id = np.user_id "
                "JOIN wishlist_items w ON w.user_id = u.id "
                "JOIN books b ON b.id = w.book_id "
                "JOIN release_history rh ON rh.book_id = b.id "
                "WHERE np.email_enabled = TRUE "
                "AND np.notify_release_date_changes = TRUE AND u.is_active = TRUE "
                "AND w.state IN ('wanted', 'preordered') "
                "AND rh.field_name = 'release_date' AND rh.observed_at >= %s "
                "AND NOT (rh.old_value <=> rh.new_value) ORDER BY u.id, rh.id",
                (since,),
            )
            events.extend(date_change_event(row) for row in cursor.fetchall())

            cursor.execute(
                "SELECT u.id AS user_id, u.email, u.display_name, b.id AS book_id, "
                "b.title, b.release_date, b.release_precision, p.name AS publisher_name "
                "FROM notification_preferences np "
                "JOIN users u ON u.id = np.user_id "
                "JOIN followed_series fs ON fs.user_id = u.id "
                "JOIN books b ON b.publisher_id = fs.publisher_id "
                "AND LEFT(LOWER(b.series_title), 190) = fs.normalized_series "
                "JOIN publishers p ON p.id = b.publisher_id "
                "WHERE np.email_enabled = TRUE "
                "AND np.notify_followed_series = TRUE AND u.is_active = TRUE "
                "AND b.first_seen_at >= %s ORDER BY u.id, b.first_seen_at, b.id",
                (since,),
            )
            events.extend(followed_series_event(row) for row in cursor.fetchall())
    return _unique_events(events)


def _unique_events(events: Iterable[NotificationEvent]) -> list[NotificationEvent]:
    unique: dict[str, NotificationEvent] = {}
    for event in events:
        unique.setdefault(event.event_key, event)
    return list(unique.values())


def undelivered_events(
    events: Iterable[NotificationEvent],
    channel: str = DISCORD_CHANNEL,
) -> list[NotificationEvent]:
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
                    [channel, *keys],
                )
                delivered.update(str(row["event_key"]) for row in cursor.fetchall())
    return [event for event in candidates if event.event_key not in delivered]


def mark_delivered(
    events: Iterable[NotificationEvent],
    channel: str = DISCORD_CHANNEL,
) -> None:
    rows = [
        (event.user_id, event.book_id, channel, event.event_type, event.event_key)
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


def _plain_line(line: str) -> str:
    return re.sub(r"\*\*(.*?)\*\*", r"\1", line)


def format_email(
    events: Iterable[NotificationEvent],
    recipient_name: str = "",
    public_url: str = "",
) -> tuple[str, str, str]:
    items = list(events)
    subject = f"AniShelf：你追蹤的書有 {len(items)} 則更新"
    greeting = f"{recipient_name}，你好：" if recipient_name else "你好："
    plain_lines = [greeting, "", "以下是你在 AniShelf 追蹤書目的最新消息：", ""]
    plain_lines.extend(f"• {_plain_line(event.line)}" for event in items)
    if public_url:
        plain_lines.extend(["", f"管理通知設定：{public_url}/#notifications"])
    plain_lines.extend(["", "這封信由你在 AniShelf 啟用的通知設定自動寄出。"])

    html_items = "".join(
        f'<li style="margin:0 0 12px">{escape(_plain_line(event.line))}</li>'
        for event in items
    )
    settings_link = (
        f'<p style="margin-top:24px"><a href="{escape(public_url)}/#notifications" '
        'style="color:#c95845">管理或關閉通知</a></p>'
        if public_url
        else ""
    )
    html_body = (
        '<div style="font-family:Arial,\'Microsoft JhengHei\',sans-serif;'
        'max-width:680px;margin:auto;color:#25232a;line-height:1.65">'
        '<h1 style="font-size:24px;color:#c95845">AniShelf 書籍通知</h1>'
        f"<p>{escape(greeting)}</p>"
        "<p>以下是你在 AniShelf 追蹤書目的最新消息：</p>"
        f'<ul style="padding-left:22px">{html_items}</ul>{settings_link}'
        '<p style="margin-top:28px;color:#797681;font-size:12px">'
        "這封信由你在 AniShelf 啟用的通知設定自動寄出。</p></div>"
    )
    return subject, "\n".join(plain_lines), html_body


def post_email(
    settings: Settings,
    recipient: str,
    subject: str,
    plain_body: str,
    html_body: str,
) -> None:
    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    try:
        if settings.smtp_port == 465 and not settings.smtp_starttls:
            client: Any = smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=30,
                context=context,
            )
        else:
            client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        with client:
            client.ehlo()
            if settings.smtp_starttls:
                client.starttls(context=context)
                client.ehlo()
            client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise NotificationError(f"SMTP email delivery failed: {exc}") from exc


def send_test_email(
    *,
    settings: Settings | None = None,
    recipient: str = "",
    sender: Callable[[Settings, str, str, str, str], None] = post_email,
) -> dict[str, Any]:
    """Send one SMTP smoke-test without creating notification records."""
    settings = settings or get_settings()
    if not settings.email_configured:
        raise NotificationError("SMTP email settings are incomplete")
    target = recipient.strip() or settings.smtp_username
    if not target:
        raise NotificationError("Test email recipient is missing")
    subject = "AniShelf Email 通知測試成功"
    plain_body = (
        "這是一封 AniShelf 測試信。\n\n"
        "收到這封信代表 GitHub Actions 已成功透過 Gmail SMTP 寄信。"
    )
    html_body = (
        '<div style="font-family:Arial,\'Microsoft JhengHei\',sans-serif;'
        'max-width:680px;margin:auto;color:#25232a;line-height:1.65">'
        '<h1 style="font-size:24px;color:#c95845">AniShelf Email 通知測試成功</h1>'
        '<p>這是一封 AniShelf 測試信。</p>'
        '<p>收到這封信代表 GitHub Actions 已成功透過 Gmail SMTP 寄信。</p>'
        "</div>"
    )
    sender(settings, target, subject, plain_body, html_body)
    return {
        "enabled": True,
        "delivered_count": 1,
        "recipient_count": 1,
    }


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


def deliver_email_notifications(
    *,
    since: datetime,
    public_url: str = "",
    today: date | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
    sender: Callable[[Settings, str, str, str, str], None] = post_email,
) -> dict[str, Any]:
    settings = settings or get_settings()
    if not settings.email_configured and not dry_run:
        subscriber_count = email_subscriber_count()
        return {
            "enabled": False,
            "candidate_count": 0,
            "delivered_count": 0,
            "recipient_count": 0,
            "subscriber_count": subscriber_count,
            "configuration_error": (
                "已有使用者啟用 Email 通知，但寄件 SMTP 尚未設定"
                if subscriber_count
                else ""
            ),
        }
    events = collect_email_events(since, today=today)
    pending = events if dry_run else undelivered_events(events, EMAIL_CHANNEL)
    grouped: dict[tuple[int, str, str], list[NotificationEvent]] = defaultdict(list)
    for event in pending:
        if event.user_id is not None and event.recipient_email:
            grouped[(event.user_id, event.recipient_email, event.recipient_name)].append(event)

    previews: list[dict[str, Any]] = []
    delivered_count = 0
    for (_user_id, recipient, recipient_name), recipient_events in grouped.items():
        subject, plain_body, html_body = format_email(
            recipient_events,
            recipient_name,
            public_url,
        )
        if dry_run:
            previews.append(
                {
                    "recipient": recipient,
                    "subject": subject,
                    "body": plain_body,
                }
            )
            continue
        sender(settings, recipient, subject, plain_body, html_body)
        mark_delivered(recipient_events, EMAIL_CHANNEL)
        delivered_count += len(recipient_events)
    return {
        "enabled": settings.email_configured,
        "dry_run": dry_run,
        "candidate_count": len(events),
        "delivered_count": delivered_count,
        "recipient_count": len(grouped),
        "previews": previews if dry_run else [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or send AniShelf Discord notifications")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--since-hours", type=int, default=36)
    parser.add_argument("--test-email", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if args.test_email:
        print(json.dumps(send_test_email(settings=settings), ensure_ascii=False, indent=2))
        return
    ensure_schema()
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
