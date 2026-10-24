"""/health (local only), public site info, and admin-only service status."""

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
    """Local container health only — never contacts Kavita or any other service."""
    code, body = await ctx.health.local()
    return JSONResponse(body, status_code=code, headers={"Cache-Control": "no-store"})


class SiteInfo(BaseModel):
    site_name: str
    version: str
    setup_completed: bool
    default_tab: str
    theme: str
    media_types: dict[str, bool]
    logo_version: int  # 0 = default star; >0 = custom logo at api/branding/logo?v=N


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
            "general.logo_version",
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
        logo_version=s["general.logo_version"],
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
    _, health = await ctx.health.local()
    return {
        "version": __version__,
        "connections": connections,
        "health": health,
        "services": await ctx.health.services(),
    }


@router.get("/api/admin/services")
async def admin_services(_: AdminUser, ctx: Ctx) -> dict[str, Any]:
    """Reachability of external services (cached ~30s). Admin only: reveals topology."""
    return await ctx.health.services()
