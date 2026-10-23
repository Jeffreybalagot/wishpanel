"""Health and service status — deliberately two different things.

Local health (``/health``, Docker HEALTHCHECK, CI smoke test)
    Is *this container* working? Database answers, /config is writable.
    Never calls another service, so an outage elsewhere (Kavita restarting,
    a network blip) can't mark WishPanel unhealthy and get it restarted.
    ok -> HTTP 200, error -> HTTP 503.

Service status (``/api/admin/services``, admin only)
    Can WishPanel reach the things it orchestrates? Results are cached
    briefly so an open admin page doesn't hammer them.
    ok / degraded / not_configured per service.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import __version__
from app.core.crypto import SecretBox
from app.providers.auth.kavita import KavitaAuthProvider
from app.services.settings import SettingsService

_EXTERNAL_TTL = 30.0


@dataclass
class _Cache:
    at: float = 0.0
    key: str = ""
    result: dict[str, Any] = field(default_factory=dict)


class HealthService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        box: SecretBox,
        client: httpx.AsyncClient,
        config_dir: Path,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.box = box
        self.client = client
        self.config_dir = config_dir
        self._cache = _Cache()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ local
    async def local(self) -> tuple[int, dict[str, Any]]:
        checks: dict[str, Any] = {}
        setup_completed: bool | None = None
        try:
            async with self.sessionmaker() as db:
                await db.execute(text("SELECT 1"))
                setup_completed = await SettingsService(db, self.box).get("setup.completed")
            checks["database"] = {"status": "ok"}
        except Exception as exc:
            checks["database"] = {"status": "error", "message": type(exc).__name__}

        writable = os.access(self.config_dir, os.W_OK)
        checks["config"] = (
            {"status": "ok"} if writable else {"status": "error", "message": "not writable"}
        )

        ok = all(c["status"] == "ok" for c in checks.values())
        body = {
            "status": "ok" if ok else "error",
            "version": __version__,
            "setup_completed": setup_completed,
            "checks": checks,
        }
        return (200 if ok else 503), body

    # ------------------------------------------------------------ external
    async def services(self) -> dict[str, Any]:
        async with self.sessionmaker() as db:
            kavita_url = await SettingsService(db, self.box).get("kavita.url")
        services = {"kavita": await self._kavita(kavita_url)}
        down = any(s["status"] == "error" for s in services.values())
        return {"status": "degraded" if down else "ok", "services": services}

    async def _kavita(self, url: str) -> dict[str, Any]:
        if not url:
            return {"status": "not_configured"}
        async with self._lock:
            now = time.monotonic()
            if self._cache.key == url and now - self._cache.at < _EXTERNAL_TTL:
                return self._cache.result
            result = await KavitaAuthProvider(self.client, url).test_connection()
            out = {"status": "ok"} if result.ok else {"status": "error", "message": result.message}
            self._cache = _Cache(at=now, key=url, result=out)
            return out
