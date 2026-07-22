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
    catalog_sync_token: str = ""
    catalog_sync_url: str = "https://anishelf-wmcu.onrender.com"

    @property
    def database_configured(self) -> bool:
        return bool(self.db_password and self.db_password != "replace-me")

    @property
    def catalog_sync_configured(self) -> bool:
        return len(self.catalog_sync_token) >= 32 and self.catalog_sync_url.startswith(
            ("http://", "https://")
        )


def get_settings() -> Settings:
    load_dotenv()
    cloud_mode = _env_bool("ANISHELF_CLOUD_MODE", bool(os.getenv("RENDER")))
    default_host = "0.0.0.0" if cloud_mode else "127.0.0.1"
    port_value = os.getenv("PORT") or os.getenv("ANISHELF_PORT", "8765")
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
        catalog_sync_token=os.getenv("ANISHELF_CATALOG_SYNC_TOKEN", "").strip(),
        catalog_sync_url=os.getenv(
            "ANISHELF_CATALOG_SYNC_URL", "https://anishelf-wmcu.onrender.com"
        ).strip().rstrip("/"),
    )
