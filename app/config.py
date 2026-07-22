from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_dotenv(path: Path | None = None) -> None:
    """Load a tiny .env file without pulling in another dependency."""
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    db_ssl_mode: str
    host: str
    port: int
    cloud_mode: bool
    auto_update: bool
    public_url: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    admin_emails: tuple[str, ...] = ()
    session_days: int = 30
    discord_webhook_url: str = ""
    notification_lead_days: tuple[int, ...] = (7, 3, 1, 0)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = ""
    smtp_starttls: bool = True
    email_test_mode: str = "smtp"
    github_actions_token: str = ""
    github_repository: str = ""
    github_workflow: str = "scheduled-update.yml"
    github_ref: str = "cloud/deployment"

    @property
    def database_configured(self) -> bool:
        return bool(self.db_password and self.db_password != "replace-me")

    @property
    def auth_configured(self) -> bool:
        return bool(self.public_url and self.google_client_id and self.google_client_secret)

    @property
    def email_configured(self) -> bool:
        return bool(
            self.smtp_host
            and self.smtp_username
            and self.smtp_password
            and self.email_from
        )

    @property
    def github_email_test_configured(self) -> bool:
        return bool(
            self.email_test_mode == "github_actions"
            and self.github_actions_token
            and self.github_repository
            and self.github_workflow
            and self.github_ref
        )


def get_settings() -> Settings:
    load_dotenv()
    cloud_mode = _env_bool("ANISHELF_CLOUD_MODE", bool(os.getenv("RENDER")))
    default_host = "0.0.0.0" if cloud_mode else "127.0.0.1"
    port_value = os.getenv("PORT") or os.getenv("ANISHELF_PORT", "8765")
    public_url = os.getenv("ANISHELF_PUBLIC_URL", "").strip().rstrip("/")
    if not public_url and not cloud_mode:
        public_url = f"http://127.0.0.1:{port_value}"
    admin_emails = tuple(
        email.strip().casefold()
        for email in os.getenv("ANISHELF_ADMIN_EMAILS", "").split(",")
        if email.strip()
    )
    lead_days = tuple(
        sorted(
            {
                min(max(int(value.strip()), 0), 90)
                for value in os.getenv("ANISHELF_NOTIFICATION_LEAD_DAYS", "7,3,1,0").split(",")
                if value.strip().isdigit()
            },
            reverse=True,
        )
    ) or (7, 3, 1, 0)
    return Settings(
        db_host=os.getenv("ANISHELF_DB_HOST", "127.0.0.1"),
        db_port=int(os.getenv("ANISHELF_DB_PORT", "3306")),
        db_name=os.getenv("ANISHELF_DB_NAME", "anishelf"),
        db_user=os.getenv("ANISHELF_DB_USER", "anishelf_app"),
        db_password=os.getenv("ANISHELF_DB_PASSWORD", ""),
        db_ssl_mode=os.getenv("ANISHELF_DB_SSL_MODE", "preferred").strip().lower(),
        host=os.getenv("ANISHELF_HOST", default_host),
        port=int(port_value),
        cloud_mode=cloud_mode,
        auto_update=_env_bool("ANISHELF_AUTO_UPDATE", not cloud_mode),
        public_url=public_url,
        google_client_id=os.getenv("ANISHELF_GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.getenv("ANISHELF_GOOGLE_CLIENT_SECRET", "").strip(),
        admin_emails=admin_emails,
        session_days=max(1, min(int(os.getenv("ANISHELF_SESSION_DAYS", "30")), 90)),
        discord_webhook_url=os.getenv("ANISHELF_DISCORD_WEBHOOK_URL", "").strip(),
        notification_lead_days=lead_days,
        smtp_host=os.getenv("ANISHELF_SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("ANISHELF_SMTP_PORT") or "587"),
        smtp_username=os.getenv("ANISHELF_SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("ANISHELF_SMTP_PASSWORD", "").strip(),
        email_from=os.getenv("ANISHELF_EMAIL_FROM", "").strip(),
        smtp_starttls=_env_bool("ANISHELF_SMTP_STARTTLS", True),
        email_test_mode=(
            os.getenv("ANISHELF_EMAIL_TEST_MODE", "smtp").strip().lower()
            if os.getenv("ANISHELF_EMAIL_TEST_MODE", "smtp").strip().lower()
            in {"smtp", "github_actions"}
            else "smtp"
        ),
        github_actions_token=os.getenv("ANISHELF_GITHUB_ACTIONS_TOKEN", "").strip(),
        github_repository=os.getenv("ANISHELF_GITHUB_REPOSITORY", "").strip(),
        github_workflow=os.getenv(
            "ANISHELF_GITHUB_WORKFLOW", "scheduled-update.yml"
        ).strip(),
        github_ref=os.getenv("ANISHELF_GITHUB_REF", "cloud/deployment").strip(),
    )
