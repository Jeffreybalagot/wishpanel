"""Discover shelves: definitions, refresh jobs and read-out for the UI.

Manga/manhwa rows come straight from AniList / MangaBaka. Comic rows come from
Comic Book Herald lists, whose titles are resolved to ComicVine volumes by the
background match worker (``services/matching.py``); a comic row shows the titles
matched so far. Each refresh keeps the previous items if a source fails.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.textmatch import norm
from app.db.base import utcnow
from app.db.models import DiscoverRow, MatchCache
from app.domain import MediaType, SearchResult
from app.providers.base import ProviderError
from app.providers.discover import cbh
from app.services import connections
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

KIND_LABEL = {MediaType.MANGA: "Manga", MediaType.MANHWA: "Manhwa"}


@dataclass(frozen=True)
class MangaRowDef:
    id: str
    title: str  # "{K}" -> Manga / Manhwa
    source: str  # anilist | mangabaka
    sort: str
    status: str | None = None


MANGA_ROWS: tuple[MangaRowDef, ...] = (
    MangaRowDef("al-trending", "Trending {K}", "anilist", "TRENDING_DESC"),
    MangaRowDef("al-ongoing", "Popular Ongoing {K}", "anilist", "POPULARITY_DESC", "RELEASING"),
    MangaRowDef("al-popular", "All-Time Popular {K}", "anilist", "POPULARITY_DESC"),
    MangaRowDef(
        "mb-completed", "Completed {K} to Binge", "mangabaka", "popularity_asc", "completed"
    ),
    MangaRowDef("mb-top-rated", "Highest Rated {K}", "mangabaka", "score_desc"),
)


def row_options(media_type: MediaType) -> list[tuple[str, str]]:
    """(id, label) of every built-in row for the Discover settings page."""
    if media_type == MediaType.COMIC:
        return [(d.id, d.title) for d in cbh.LISTS]
    return [(d.id, d.title.replace("{K}", KIND_LABEL[media_type])) for d in MANGA_ROWS]


async def enabled_rows(settings: SettingsService, media_type: MediaType) -> list[str]:
    chosen = await settings.get(f"discover.{media_type.value}.rows")
    valid = [i for i, _ in row_options(media_type)]
    return [r for r in chosen if r in valid] if chosen else valid


def match_key(title: str, hint: int | None) -> str:
    return f"{norm(title)}|{hint or ''}"[:300]


async def _upsert_row(
    db: AsyncSession,
    row_id: str,
    media_type: MediaType,
    title: str,
    source: str,
    position: float,
    *,
    items: list[dict[str, Any]] | None,
    url: str | None = None,
    error: str | None = None,
) -> None:
    row = await db.get(DiscoverRow, row_id)
    if row is None:
        row = DiscoverRow(id=row_id, media_type=media_type.value, title=title, source=source)
        db.add(row)
    row.title, row.source, row.position = title, source, position
    if items is not None:
        row.items = json.dumps(items)
        row.item_count = len(items)
        row.refreshed_at = utcnow()
        row.last_error = None
        row.source_url = url or row.source_url
    if error:
        row.last_error = error[:500]


async def _prune(db: AsyncSession, media_type: MediaType, keep: set[str]) -> None:
    await db.execute(
        delete(DiscoverRow).where(
            DiscoverRow.media_type == media_type.value, DiscoverRow.id.not_in(keep)
        )
    )


# --------------------------------------------------------------------- manga


async def refresh_manga(ctx: AppContext) -> str:
    done, failed = 0, []
    async with ctx.sessionmaker() as db:
        settings = SettingsService(db, ctx.box)
        per = await settings.get("discover.items_per_row")
        anilist = await connections.provider_for(settings, "anilist", ctx.http)
        mangabaka = await connections.provider_for(settings, "mangabaka", ctx.http)
        use = {
            "anilist": await settings.get("anilist.enabled"),
            "mangabaka": await settings.get("mangabaka.enabled"),
        }
        for mt in (MediaType.MANGA, MediaType.MANHWA):
            if not await settings.get(f"media.{mt.value}.enabled"):
                await _prune(db, mt, set())
                continue
            defs = {d.id: d for d in MANGA_ROWS}
            keep: set[str] = set()
            for pos, rid in enumerate(await enabled_rows(settings, mt)):
                d = defs[rid]
                if not use[d.source]:
                    continue
                row_id = f"{mt.value}:{rid}"
                keep.add(row_id)
                title = d.title.replace("{K}", KIND_LABEL[mt])
                try:
                    if d.source == "anilist":
                        items = await anilist.row(mt, d.sort, d.status, per=per)
                    else:
                        items = await mangabaka.row(mt, d.sort, d.status, limit=per)
                    await _upsert_row(
                        db, row_id, mt, title, d.source, pos, items=[i.to_json() for i in items]
                    )
                    done += 1
                except ProviderError as exc:
                    failed.append(f"{title}: {exc.message}")
                    await _upsert_row(
                        db, row_id, mt, title, d.source, pos, items=None, error=exc.message
                    )
                await db.commit()
            await _prune(db, mt, keep)
            await db.commit()
    msg = f"{done} row(s) refreshed"
    if failed:
        msg += f"; {len(failed)} failed ({failed[0]})"
        if not done:
            raise ProviderError(msg)
    return msg


# --------------------------------------------------------------------- comics


async def refresh_comics(ctx: AppContext) -> str:
    done, failed, new_titles = 0, [], 0
    async with ctx.sessionmaker() as db:
        settings = SettingsService(db, ctx.box)
        if not await settings.get("media.comic.enabled"):
            await _prune(db, MediaType.COMIC, set())
            await db.commit()
            return "comics are disabled"
        source = cbh.ComicBookHeraldSource(ctx.http)
        defs: list[cbh.ListDef] = []
        by_id = {d.id: d for d in cbh.LISTS}
        defs += [by_id[r] for r in await enabled_rows(settings, MediaType.COMIC)]
        defs += [cbh.character_def(name) for name in await settings.get("discover.characters")]
        keep: set[str] = set()
        for pos, d in enumerate(defs):
            row_id = f"comic:{d.id}"
            keep.add(row_id)
            kind = "character" if d.character else "list"
            try:
                items, url = await _comic_items(ctx, settings, source, d)
                for idx, it in enumerate(items):
                    if "title" in it:
                        new_titles += await _queue_match(
                            db, it["title"], it.get("hint"), pos * 100 + idx
                        )
                await _upsert_row(
                    db, row_id, MediaType.COMIC, d.title, kind, pos, items=items, url=url
                )
                done += 1
            except ProviderError as exc:
                failed.append(f"{d.title}: {exc.message}")
                await _upsert_row(
                    db, row_id, MediaType.COMIC, d.title, kind, pos, items=None, error=exc.message
                )
            await db.commit()
        await _prune(db, MediaType.COMIC, keep)
        await db.commit()
    ctx.matcher.wake()
    msg = f"{done} list(s) refreshed, {new_titles} new title(s) queued for matching"
    if failed:
        msg += f"; {len(failed)} failed ({failed[0]})"
        if not done:
            raise ProviderError(msg)
    return msg


async def _comic_items(
    ctx: AppContext, settings: SettingsService, source: cbh.ComicBookHeraldSource, d: cbh.ListDef
) -> tuple[list[dict[str, Any]], str | None]:
    url: str | None = None
    items: list[dict[str, Any]] = []
    if d.slug or d.search:
        try:
            url, picks = await source.get_list(d)
            items = [{"title": p.title, "hint": p.hint} for p in picks[:80]]
        except ProviderError:
            if not d.character:
                raise
    if d.character and len(items) < 6:
        # Fall back to the character's/team's ComicVine volumes.
        if not await settings.get("comicvine.api_key"):
            if items:
                return items, url
            raise ProviderError(
                "No reading order on Comic Book Herald and no ComicVine key for a fallback."
            )
        cv = await connections.provider_for(settings, "comicvine", ctx.http)
        try:
            ref = d.cv or await cv.search_character(d.character)
        except ProviderError:
            if items:
                return items, url
            raise
        if not ref:
            raise ProviderError(f"Couldn't find “{d.character}” on Comic Book Herald or ComicVine.")
        vols = await cv.credited_volumes(
            ref[0], ref[1], limit=await settings.get("discover.items_per_row")
        )
        blocked = [b.lower() for b in await settings.get("comicvine.blocked_publishers")]
        fallback = [
            {"item": v.to_json()}
            for v in vols
            if not any(b in (v.publisher or "").lower() for b in blocked)
        ]
        if len(fallback) > len(items):
            items, url = fallback, None
    return items, url


async def _queue_match(db: AsyncSession, title: str, hint: int | None, priority: float) -> int:
    key = match_key(title, hint)
    row = await db.get(MatchCache, key)
    if row is None:
        db.add(
            MatchCache(key=key, title=title, hint_year=hint, status="pending", priority=priority)
        )
        return 1
    if row.status == "pending":
        row.priority = min(row.priority, priority)
    return 0


# --------------------------------------------------------------------- read


def _cv_item(m: MatchCache) -> dict[str, Any]:
    return SearchResult(
        external_id=str(m.comicvine_id),
        title=m.name or m.title,
        media_type=MediaType.COMIC,
        year=int(m.year) if (m.year or "").isdigit() else None,
        cover_url=m.cover_url,
        publisher=m.publisher,
        issue_count=m.issues,
        provider="comicvine",
    ).to_json()


async def rows_for(
    db: AsyncSession, settings: SettingsService, media_type: MediaType
) -> list[dict[str, Any]]:
    per = await settings.get("discover.items_per_row")
    rows = (
        await db.scalars(
            select(DiscoverRow)
            .where(DiscoverRow.media_type == media_type.value)
            .order_by(DiscoverRow.position)
        )
    ).all()
    out = []
    if media_type != MediaType.COMIC:
        for r in rows:
            items = json.loads(r.items or "[]")[:per]
            out.append(_row_json(r, items, 0))
        return out

    raw = {r.id: json.loads(r.items or "[]") for r in rows}
    keys = {
        match_key(i["title"], i.get("hint"))
        for items in raw.values()
        for i in items
        if "title" in i
    }
    cache = {}
    if keys:
        for m in (await db.scalars(select(MatchCache).where(MatchCache.key.in_(keys)))).all():
            cache[m.key] = m
    blocked = [b.lower() for b in await settings.get("comicvine.blocked_publishers")]
    for r in rows:
        items, seen, pending = [], set(), 0
        for i in raw[r.id]:
            if "item" in i:
                item = i["item"]
            else:
                m = cache.get(match_key(i["title"], i.get("hint")))
                if m is None or m.status == "pending":
                    pending += 1
                    continue
                if m.status != "ok" or not m.comicvine_id:
                    continue
                item = _cv_item(m)
            if item["external_id"] in seen or any(
                b in (item.get("publisher") or "").lower() for b in blocked
            ):
                continue
            seen.add(item["external_id"])
            items.append(item)
            if len(items) >= per:
                break
        out.append(_row_json(r, items, pending))
    return out


def _row_json(r: DiscoverRow, items: list[dict[str, Any]], pending: int) -> dict[str, Any]:
    return {
        "id": r.id,
        "title": r.title,
        "source": r.source,
        "source_url": r.source_url,
        "items": items,
        "pending": pending,
        "refreshed_at": r.refreshed_at,
        "error": r.last_error,
    }


async def status(db: AsyncSession) -> dict[str, Any]:
    """Counts for the Discover settings page."""
    counts: dict[str, int] = {}
    for st in ("pending", "ok", "miss"):
        counts[st] = len(
            (await db.scalars(select(MatchCache.key).where(MatchCache.status == st))).all()
        )
    rows = (
        await db.scalars(select(DiscoverRow).order_by(DiscoverRow.media_type, DiscoverRow.position))
    ).all()
    return {
        "match": counts,
        "rows": [
            {
                "id": r.id,
                "media_type": r.media_type,
                "title": r.title,
                "items": r.item_count,
                "refreshed_at": r.refreshed_at,
                "error": r.last_error,
                "source_url": r.source_url,
            }
            for r in rows
        ],
    }
