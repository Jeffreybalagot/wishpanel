"""Search, title details and Discover shelves for signed-in users."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import Ctx, CurrentUser, Db, Settings
from app.domain import MediaType
from app.providers.base import ProviderError
from app.services import catalog, discover, requests

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["catalog"])

SEARCH_LIMIT = 30  # searches per user per minute


async def _enabled(settings: Settings, media_type: MediaType) -> None:
    if not await settings.get(f"media.{media_type.value}.enabled"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{media_type.value.title()} is turned off.")


@router.get("/search")
async def search(
    user: CurrentUser,
    ctx: Ctx,
    db: Db,
    settings: Settings,
    media_type: MediaType,
    q: str = Query(min_length=1, max_length=120),
) -> dict[str, Any]:
    await _enabled(settings, media_type)
    if not ctx.limits.allow(f"search:{user.id}", SEARCH_LIMIT, 60):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many searches — wait a minute.")
    try:
        results = await catalog.search(db, settings, ctx.http, media_type, q)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.message) from exc
    items = [r.to_json() for r in results]
    library = await catalog.in_library_titles(settings, ctx.http, media_type, q)
    for r, item in zip(results, items, strict=True):
        item["in_library"] = catalog.matches_library(r, library) if library else False
    return {"results": await requests.annotate(db, items, user)}


@router.get("/titles/{media_type}/{provider}/{external_id}")
async def title_details(
    media_type: MediaType,
    provider: str,
    external_id: str,
    user: CurrentUser,
    ctx: Ctx,
    db: Db,
    settings: Settings,
) -> dict[str, Any]:
    await _enabled(settings, media_type)
    try:
        item = await catalog.details(db, settings, ctx.http, media_type, provider, external_id)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.message) from exc
    data = item.to_json()
    library = await catalog.in_library_titles(settings, ctx.http, media_type, item.title)
    data["in_library"] = catalog.matches_library(item, library) if library else False
    return (await requests.annotate(db, [data], user))[0]


@router.get("/discover")
async def discover_rows(
    media_type: MediaType, user: CurrentUser, db: Db, settings: Settings
) -> dict[str, Any]:
    await _enabled(settings, media_type)
    rows = await discover.rows_for(db, settings, media_type)
    all_items = [i for r in rows for i in r["items"]]
    await requests.annotate(db, all_items, user)
    return {"rows": rows}
