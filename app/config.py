"""Bootstrap configuration.

This is the *only* place WishPanel reads environment variables for its own
behaviour. Everything else (URLs, API keys, mappings, limits) lives in the
database and is edited in Admin -> Settings.

Supported variables (all optional):

    CONFIG_DIR           where the database, secret key and logs live (default /config)
    PORT                 port uvicorn listens on inside the container (default 8080)
    LOG_LEVEL            debug | info | warning | error (default info)
    BASE_URL             public URL or path prefix, e.g. https://example.com/wishpanel or /wishpanel
    FORWARDED_ALLOW_IPS  proxies trusted to set X-Forwarded-* (default 127.0.0.1)

PUID / PGID / TZ are consumed by the container entrypoint, not by Python.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_LOG_LEVELS = {"debug", "info", "warning", "error", "critical"}


def _normalise_prefix(raw: str) -> str:
    """Turn BASE_URL (full URL or path) into a path prefix like '/wishpanel' or ''."""
    raw = raw.strip()
    if not raw:
        return ""
    path = urlparse(raw).path if "://" in raw else raw
    path = "/" + path.strip("/")
    return "" if path == "/" else path


@dataclass(frozen=True)
class BootstrapConfig:
    config_dir: Path
    port: int
    log_level: str
    base_path: str
    forwarded_allow_ips: str

    @property
    def db_path(self) -> Path:
        return self.config_dir / "wishpanel.db"

    @property
    def db_url_async(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def db_url_sync(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def secret_key_path(self) -> Path:
        return self.config_dir / "secret.key"

    @property
    def log_dir(self) -> Path:
        return self.config_dir / "logs"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BootstrapConfig:
        env = dict(os.environ if env is None else env)
        level = env.get("LOG_LEVEL", "info").strip().lower() or "info"
        if level not in _LOG_LEVELS:
            level = "info"
        try:
            port = int(env.get("PORT", "8080") or 8080)
        except ValueError:
            port = 8080
        return cls(
            config_dir=Path(env.get("CONFIG_DIR", "/config")).expanduser(),
            port=port,
            log_level=level,
            base_path=_normalise_prefix(env.get("BASE_URL", "")),
            forwarded_allow_ips=env.get("FORWARDED_ALLOW_IPS", "127.0.0.1").strip() or "127.0.0.1",
        )
