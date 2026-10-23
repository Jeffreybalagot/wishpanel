"""Health checks for /health and the Docker HEALTHCHECK.

Status semantics:
  ok        everything configured is reachable
  degraded  WishPanel works, but an external service is down (HTTP 200)
  error     WishPanel itself is broken, e.g. database unavailable (HTTP 503)

External checks are cached briefly so the 30s Docker healthcheck doesn't
hammer Kavita.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
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
    ) -> None:
        self.sessionmaker = sessionmaker
        self.box = box
        self.client = client
        self._cache = _Cache()
        self._lock = asyncio.Lock()

    async def check(self) -> tuple[int, dict[str, Any]]:
        checks: dict[str, Any] = {}
        setup_completed = False
        kavita_url = ""
        try:
            async with self.sessionmaker() as db:
                await db.execute(text("SELECT 1"))
                settings = SettingsService(db, self.box)
                setup_completed = await settings.get("setup.completed")
                kavita_url = await settings.get("kavita.url")
            checks["database"] = {"status": "ok"}
        except Exception as exc:
            checks["database"] = {"status": "error", "message": type(exc).__name__}
            return 503, self._body("error", checks, setup_completed)

        checks["kavita"] = await self._kavita(kavita_url)
        status = "ok" if checks["kavita"]["status"] in {"ok", "not_configured"} else "degraded"
        return 200, self._body(status, checks, setup_completed)

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

    @staticmethod
    def _body(status: str, checks: dict[str, Any], setup_completed: bool) -> dict[str, Any]:
        return {
            "status": status,
            "version": __version__,
            "setup_completed": setup_completed,
            "checks": checks,
        }
