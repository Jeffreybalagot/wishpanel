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
from app.services import connections
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
        self._cache: dict[str, _Cache] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
        """Reachability of every configured external service, checked concurrently."""
        async with self.sessionmaker() as db:
            settings = SettingsService(db, self.box)
            kavita_url = await settings.get("kavita.url")
            values = {
                svc: await connections.resolve_values(settings, svc, None)
                for svc in ("kapowarr", "suwayomi")
            }
            comicvine_set = await settings.is_set("comicvine.api_key")

        async def check(name: str) -> dict[str, Any]:
            if name == "kavita":
                if not kavita_url:
                    return {"status": "not_configured"}
                return await self._cached(
                    f"kavita|{kavita_url}",
                    lambda: KavitaAuthProvider(self.client, kavita_url).test_connection(),
                )
            v = values[name]
            if not v[f"{name}.url"]:
                return {"status": "not_configured"}
            key = name + "|" + "|".join(str(v[k]) for k in sorted(v))
            return await self._cached(
                key, lambda: connections.build(name, v, self.client).test_connection()
            )

        names = ("kavita", "kapowarr", "suwayomi")
        results = await asyncio.gather(*(check(n) for n in names))
        services = dict(zip(names, results, strict=True))
        # ComicVine is rate-limited; report configuration only (Test button does a live check).
        services["comicvine"] = {"status": "configured" if comicvine_set else "not_configured"}
        down = any(s["status"] == "error" for s in services.values())
        return {"status": "degraded" if down else "ok", "services": services}

    async def _cached(self, key: str, run) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit.at < _EXTERNAL_TTL:
            return hit.result
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit.at < _EXTERNAL_TTL:
                return hit.result
            result = await run()
            out = {"status": "ok"} if result.ok else {"status": "error", "message": result.message}
            self._cache[key] = _Cache(at=time.monotonic(), key=key, result=out)
            return out
