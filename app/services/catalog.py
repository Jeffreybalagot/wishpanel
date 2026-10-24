"""Search and title details across metadata providers, with caching and ranking.

* Comics: ComicVine directly, or through Kapowarr's own ComicVine key
  (``media.comic.metadata``). Blocked publishers are dropped and results are
  ranked with the v1 rules (English publishers first, translations last).
* Manga / manhwa: AniList (country of origin decides manga vs manhwa), with
  MangaBaka as fallback when AniList is disabled or failing.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.textmatch import comic_score, norm, similarity
from app.domain import MediaType, SearchResult
from app.providers.base import ProviderError
from app.services import cache, connections
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

PROVIDERS = {"comicvine", "anilist", "mangabaka"}


def from_json(d: dict[str, Any]) -> SearchResult:
    fields = SearchResult.__dataclass_fields__
    data = {k: v for k, v in d.items() if k in fields}
    data["media_type"] = MediaType(data["media_type"])
    data["alt_titles"] = tuple(data.get("alt_titles") or ())
    return SearchResult(**data)


async def _ttl(settings: SettingsService) -> timedelta:
    return timedelta(hours=int(await settings.get("comicvine.cache_ttl_hours") or 24))


async def comic_provider(settings: SettingsService, http: httpx.AsyncClient) -> Any:
    """The configured comic metadata provider (falls back to Kapowarr without a CV key)."""
    choice = await settings.get("media.comic.metadata")
    if choice == "comicvine" and await settings.get("comicvine.api_key"):
        return await connections.provider_for(settings, "comicvine", http)
    if await settings.get("kapowarr.url") and await settings.get("kapowarr.api_key"):
        return await connections.provider_for(settings, "kapowarr", http)
    if choice == "comicvine":
        raise ProviderError(
            "Comic search needs a ComicVine API key (Admin → Metadata — ComicVine)."
        )
    raise ProviderError("Comic search needs Kapowarr to be configured.")


def rank_comics(
    results: list[SearchResult], query: str, preferred: list[str], blocked: list[str]
) -> list[SearchResult]:
    blocked_l = [b.lower() for b in blocked if b]
    kept = [r for r in results if not any(b in (r.publisher or "").lower() for b in blocked_l)]
    scored = []
    for i, r in enumerate(kept):
        _, score = comic_score(
            title=query,
            name=r.title,
            publisher=r.publisher,
            issues=r.issue_count,
            year=str(r.year or ""),
            hint=None,
            has_cover=bool(r.cover_url),
            translated=r.translated,
            preferred=preferred,
        )
        scored.append((-score, i, r))
    return [r for _, _, r in sorted(scored)]


async def search(
    db: AsyncSession,
    settings: SettingsService,
    http: httpx.AsyncClient,
    media_type: MediaType,
    query: str,
) -> list[SearchResult]:
    query = " ".join(query.split())[:120]
    if len(query) < 2:
        return []
    key = f"search:{media_type.value}:{norm(query)}"
    if (hit := await cache.get(db, key)) is not None:
        return [from_json(d) for d in hit]

    if media_type == MediaType.COMIC:
        provider = await comic_provider(settings, http)
        results = rank_comics(
            await provider.search(query, media_type),
            query,
            await settings.get("comicvine.preferred_publishers"),
            await settings.get("comicvine.blocked_publishers"),
        )
    else:
        results = await search_manga(settings, http, media_type, query)

    await cache.put(
        db, key, [r.to_json() for r in results], min(await _ttl(settings), timedelta(hours=6))
    )
    await db.commit()
    return results


async def search_manga(
    settings: SettingsService, http: httpx.AsyncClient, media_type: MediaType, query: str
) -> list[SearchResult]:
    errors: list[str] = []
    if await settings.get("anilist.enabled"):
        try:
            anilist = await connections.provider_for(settings, "anilist", http)
            results = await anilist.search(query, media_type)
            if results:
                return results
        except ProviderError as exc:
            errors.append(exc.message)
    if await settings.get("mangabaka.enabled"):
        try:
            mb = await connections.provider_for(settings, "mangabaka", http)
            return await mb.search(query, media_type)
        except ProviderError as exc:
            errors.append(exc.message)
    if errors:
        raise ProviderError(errors[0], retryable=True)
    if not (await settings.get("anilist.enabled") or await settings.get("mangabaka.enabled")):
        raise ProviderError(
            "Manga search needs AniList or MangaBaka enabled (Admin → AniList / MangaBaka)."
        )
    return []


async def details(
    db: AsyncSession,
    settings: SettingsService,
    http: httpx.AsyncClient,
    media_type: MediaType,
    provider: str,
    external_id: str,
) -> SearchResult:
    if provider not in PROVIDERS:
        raise ProviderError("Unknown metadata provider.")
    key = f"details:{provider}:{external_id}"
    if (hit := await cache.get(db, key)) is not None:
        r = from_json(hit)
    else:
        if provider == "comicvine":
            if await settings.get("comicvine.api_key"):
                cv = await connections.provider_for(settings, "comicvine", http)
                r = await cv.get_details(external_id)
            else:
                raise ProviderError("Comic details need a ComicVine API key.")
        elif provider == "anilist":
            r = await (await connections.provider_for(settings, "anilist", http)).get_details(
                external_id
            )
        else:
            mb = await connections.provider_for(settings, "mangabaka", http)
            r = await mb.get_details(external_id, media_type)
        await cache.put(db, key, r.to_json(), await _ttl(settings))
        await db.commit()
    if (
        r.media_type != media_type
        and media_type != MediaType.COMIC
        and r.media_type != MediaType.COMIC
    ):
        # The user found it under manga/manhwa; respect the tab they requested from.
        r = from_json({**r.to_json(), "media_type": media_type.value})
    return r


async def in_library_titles(
    settings: SettingsService, http: httpx.AsyncClient, media_type: MediaType, query: str
) -> list[str]:
    """Series names in the mapped Kavita library that match a search (best effort)."""
    library = await settings.get(f"kavita.library.{media_type.value}")
    if not library or not await settings.get("kavita.api_key"):
        return []
    try:
        kavita = await connections.provider_for(settings, "kavita", http)
        series = await kavita.search_series(query)
    except ProviderError as exc:
        log.debug("Kavita search failed", extra={"error": exc.message})
        return []
    names = []
    for s in series:
        if s["library_id"] == str(library):
            names.extend(n for n in (s["name"], s["original_name"], s["localized_name"]) if n)
    return names


def matches_library(result: SearchResult, names: list[str]) -> bool:
    titles = [result.title, *result.alt_titles]
    return any(similarity(t, n) >= 0.93 for t in titles for n in names)
