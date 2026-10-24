"""The jobs WishPanel runs (SPEC §10). Schedules live in Settings → Jobs."""

from __future__ import annotations

from typing import Any

from app.domain import MediaType
from app.jobs.scheduler import JobDef
from app.providers.base import ProviderError
from app.services import cache, connections, discover, requests
from app.services.settings import SettingsService


async def library_scan(ctx: Any) -> str:
    async with ctx.sessionmaker() as db:
        settings = SettingsService(db, ctx.box)
        libs = {await settings.get(f"kavita.library.{m.value}") for m in MediaType} - {""}
        if not libs:
            raise ProviderError("No Kavita libraries are mapped (Admin → Library — Kavita).")
        kavita = await connections.provider_for(settings, "kavita", ctx.http)
        for lib in sorted(libs):
            await kavita.trigger_scan(lib)
        purged = await cache.purge_expired(db)
        await db.commit()
    return f"scan requested for {len(libs)} librar{'y' if len(libs) == 1 else 'ies'}" + (
        f"; {purged} expired cache entries removed" if purged else ""
    )


JOBS = [
    JobDef(
        "discover_comic",
        "Discover refresh — comics",
        "Fetches Comic Book Herald lists and queues new titles for ComicVine matching.",
        discover.refresh_comics,
    ),
    JobDef(
        "discover_manga",
        "Discover refresh — manga & manhwa",
        "Refreshes AniList and MangaBaka shelves.",
        discover.refresh_manga,
    ),
    JobDef(
        "retry",
        "Not-found retry",
        "Searches Suwayomi again for titles that weren't found, and retries requests "
        "that were waiting for a downloader.",
        requests.retry_due,
    ),
    JobDef(
        "availability",
        "Availability sync",
        "Updates progress, partial and in-library status from Kapowarr and Suwayomi "
        "(and queues new chapters if enabled).",
        requests.sync_all,
    ),
    JobDef(
        "library_scan",
        "Kavita library scan",
        "Asks Kavita to rescan every mapped library. Scans after a finished download "
        "happen automatically when enabled on the Kavita page.",
        library_scan,
    ),
]
