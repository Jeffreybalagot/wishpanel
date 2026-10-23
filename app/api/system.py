"""/health, public site info, and the admin overview used by the Admin shell."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import __version__
from app.api.deps import AdminUser, Ctx, Settings

health_router = APIRouter(tags=["system"])
router = APIRouter(tags=["system"])


@health_router.get("/health", include_in_schema=True)
async def health(ctx: Ctx) -> JSONResponse:
    code, body = await ctx.health.check()
    return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})


class SiteInfo(BaseModel):
    site_name: str
    version: str
    setup_completed: bool
    default_tab: str
    theme: str
    media_types: dict[str, bool]


@router.get("/api/system/info", response_model=SiteInfo)
async def site_info(settings: Settings) -> SiteInfo:
    """Public, unauthenticated: just enough for the login page to render."""
    s = await settings.get_many(
        [
            "general.site_name",
            "setup.completed",
            "general.default_tab",
            "general.theme",
            "media.comic.enabled",
            "media.manga.enabled",
            "media.manhwa.enabled",
        ]
    )
    return SiteInfo(
        site_name=s["general.site_name"] or "WishPanel",
        version=__version__,
        setup_completed=s["setup.completed"],
        default_tab=s["general.default_tab"],
        theme=s["general.theme"],
        media_types={
            "comic": s["media.comic.enabled"],
            "manga": s["media.manga.enabled"],
            "manhwa": s["media.manhwa.enabled"],
        },
    )


_CONNECTIONS: list[tuple[str, str, str | None]] = [
    # (label, url key, secret key)
    ("Kavita", "kavita.url", "kavita.api_key"),
    ("Kapowarr", "kapowarr.url", "kapowarr.api_key"),
    ("Suwayomi", "suwayomi.url", "suwayomi.password"),
    ("ComicVine", "", "comicvine.api_key"),
]


@router.get("/api/admin/overview")
async def admin_overview(_: AdminUser, settings: Settings, ctx: Ctx) -> dict[str, Any]:
    """Read-only snapshot for the Milestone 1 Admin tab. Secrets are masked."""
    connections = []
    for label, url_key, secret_key in _CONNECTIONS:
        url = await settings.get(url_key) if url_key else ""
        connections.append(
            {
                "name": label,
                "url": url,
                "secret": await settings.masked(secret_key) if secret_key else "",
                "configured": bool(url)
                or (not url_key and await settings.is_set(secret_key or "")),
            }
        )
    _, health = await ctx.health.check()
    return {"version": __version__, "connections": connections, "health": health}
