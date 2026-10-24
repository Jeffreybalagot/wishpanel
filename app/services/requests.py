"""Requests: creation rules, the status machine, downloader hand-off and availability sync.

Status flow (SPEC §5)::

    pending ─approve─► approved ─► added (Kapowarr) ─► partial / in_library
                          │       ─► downloading (Suwayomi) ─► partial / in_library
                          ├─► waiting_for_source      (no Suwayomi match yet; retried)
                          ├─► waiting_for_downloader  (downloader down / not configured; retried)
                          └─► failed
    pending ─deny─► denied

Everything here takes an ``AppContext`` so it can run both from API handlers and
from background jobs, each with its own short-lived database session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.textmatch import best_similarity, dedupe, edition_penalty
from app.db.base import utcnow
from app.db.models import Request, User
from app.domain import MediaType, RequestStatus, SearchResult
from app.providers.base import ProviderError
from app.providers.downloaders.kapowarr import VolumeNotFound
from app.services import audit, catalog, connections
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

S = RequestStatus
CLOSED = {S.FAILED.value, S.DENIED.value}
PROCESSABLE = {
    S.APPROVED.value,
    S.WAITING_FOR_SOURCE.value,
    S.WAITING_FOR_DOWNLOADER.value,
    S.FAILED.value,
}
SYNCABLE = {S.ADDED.value, S.DOWNLOADING.value, S.PARTIAL.value, S.IN_LIBRARY.value}
USER_CANCELLABLE = {
    S.PENDING.value,
    S.FAILED.value,
    S.DENIED.value,
    S.WAITING_FOR_SOURCE.value,
    S.WAITING_FOR_DOWNLOADER.value,
}
DOWNLOADER_RETRY = timedelta(hours=1)
UNIT = {"comic": "issue", "manga": "chapter", "manhwa": "chapter"}


class RequestError(Exception):
    def __init__(self, status_code: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


@dataclass
class Rules:
    min_expected: int
    have_percent: int
    min_gap: int

    @classmethod
    async def load(cls, settings: SettingsService) -> Rules:
        return cls(
            await settings.get("requests.partial_min_expected"),
            await settings.get("requests.partial_have_percent"),
            await settings.get("requests.partial_min_gap"),
        )


def availability(have: int, expected: int, rules: Rules) -> str:
    """in_library / partial for a request with at least one item downloaded (v1 rule)."""
    if expected and have >= expected:
        return S.IN_LIBRARY.value
    if (
        expected >= rules.min_expected
        and have * 100 < expected * rules.have_percent
        and expected - have >= rules.min_gap
    ):
        return S.PARTIAL.value
    return S.IN_LIBRARY.value


def progress(req: Request) -> str | None:
    unit = UNIT.get(req.media_type, "item")
    if req.have_count is None and not req.total_count:
        return None
    have = req.have_count or 0
    if req.media_type == "comic" and req.total_count:
        total = req.total_count
    else:
        total = max(req.total_count or 0, req.expected_count or 0)
    if total:
        approx = "~" if req.media_type != "comic" and req.status != S.IN_LIBRARY.value else ""
        return f"{have} of {approx}{total} {unit}s"
    return f"{have} {unit}{'s' if have != 1 else ''}"


def to_json(req: Request, *, admin: bool, me: User | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": req.id,
        "media_type": req.media_type,
        "provider": req.provider,
        "external_id": req.external_id,
        "key": SearchResult(
            req.external_id, req.title, MediaType(req.media_type), provider=req.provider
        ).key,
        "title": req.title,
        "year": req.year,
        "publisher": req.publisher,
        "cover_url": req.cover_url,
        "status": req.status,
        "status_message": req.status_message,
        "progress": progress(req),
        "have_count": req.have_count,
        "total_count": req.total_count,
        "source_name": req.source_name,
        "created_at": req.created_at,
        "updated_at": req.updated_at,
        "completed_at": req.completed_at,
        "mine": bool(me and req.user_id == me.id),
        "username": req.user.username if req.user else None,
        "imported_from": req.imported_from,
    }
    if admin:
        d.update(
            downloader=req.downloader,
            downloader_ref=req.downloader_ref,
            source_id=req.source_id,
            attempts=req.attempts,
            next_attempt_at=req.next_attempt_at,
            approved_by=req.approved_by,
            alt_titles=json.loads(req.alt_titles) if req.alt_titles else [],
        )
    return d


# --------------------------------------------------------------------- creation


async def _limit_for(settings: SettingsService, user: User) -> int | None:
    if user.is_admin and await settings.get("requests.admins_unlimited"):
        return None
    if user.daily_limit_override is not None:
        return user.daily_limit_override
    return await settings.get("requests.daily_limit")


async def quota(db: AsyncSession, settings: SettingsService, user: User) -> dict[str, Any]:
    limit = await _limit_for(settings, user)
    since = utcnow() - timedelta(hours=24)
    rows = (
        await db.scalars(
            select(Request.created_at)
            .where(
                Request.user_id == user.id,
                Request.created_at >= since,
                Request.imported_from.is_(None),
            )
            .order_by(Request.created_at)
        )
    ).all()
    used = len(rows)
    reset = (
        (rows[0] + timedelta(hours=24)) if (limit is not None and used >= limit and rows) else None
    )
    return {
        "limit": limit,
        "used": used,
        "remaining": None if limit is None else max(0, limit - used),
        "resets_at": reset,
    }


def allowed_types(user: User) -> list[str] | None:
    """Media types the user may request (stored comma-separated); None = all."""
    if user.allowed_media_types is None:
        return None
    return [m for m in user.allowed_media_types.split(",") if m]


async def find_existing(
    db: AsyncSession, media_type: str, provider: str, external_id: str, anilist_id: int | None
) -> list[Request]:
    cond = (Request.provider == provider) & (Request.external_id == external_id)
    if anilist_id:
        cond = or_(cond, Request.anilist_id == anilist_id)
    return list(
        (await db.scalars(select(Request).where(Request.media_type == media_type, cond))).all()
    )


async def create(
    ctx: AppContext,
    db: AsyncSession,
    user: User,
    media_type: MediaType,
    provider: str,
    external_id: str,
    hints: dict[str, Any] | None = None,
    ip: str | None = None,
) -> Request:
    settings = SettingsService(db, ctx.box)
    if user.is_blocked:
        raise RequestError(403, "Your account is blocked from making requests.")
    if not await settings.get(f"media.{media_type.value}.enabled"):
        raise RequestError(403, f"{media_type.value.title()} requests are turned off.")
    allowed = allowed_types(user)
    if allowed is not None and media_type.value not in allowed:
        raise RequestError(403, f"You can't request {media_type.value} — ask an admin.")
    if provider not in catalog.PROVIDERS or (media_type == MediaType.COMIC) != (
        provider == "comicvine"
    ):
        raise RequestError(400, "That title can't be requested as this media type.")
    if not str(external_id).isdigit():
        raise RequestError(400, "Invalid title id.")

    # Server-side metadata when we can get it; the browser's copy only fills gaps.
    try:
        item = await catalog.details(db, settings, ctx.http, media_type, provider, external_id)
    except ProviderError as exc:
        hints = hints or {}
        if not hints.get("title"):
            raise RequestError(502, f"Couldn't look up that title: {exc.message}") from exc
        item = SearchResult(
            external_id=str(external_id),
            title=str(hints["title"])[:500],
            media_type=media_type,
            year=hints.get("year") if isinstance(hints.get("year"), int) else None,
            cover_url=str(hints.get("cover_url") or "")[:1000] or None,
            publisher=str(hints.get("publisher") or "")[:255] or None,
            issue_count=hints.get("issue_count")
            if isinstance(hints.get("issue_count"), int)
            else None,
            provider=provider,
            alt_titles=tuple(str(t)[:300] for t in (hints.get("alt_titles") or [])[:8]),
            anilist_id=hints.get("anilist_id")
            if isinstance(hints.get("anilist_id"), int)
            else None,
        )

    existing = await find_existing(
        db, media_type.value, provider, str(external_id), item.anilist_id
    )
    active = [r for r in existing if r.status not in CLOSED]
    if active:
        r = active[0]
        raise RequestError(409, "Already requested.", request_id=r.id, status=r.status)
    if existing and not await settings.get("requests.allow_rerequest_failed"):
        raise RequestError(
            409, "This was requested before and failed or was denied — ask an admin."
        )

    q = await quota(db, settings, user)
    if q["limit"] is not None and q["used"] >= q["limit"]:
        when = q["resets_at"].strftime("%H:%M UTC") if q["resets_at"] else "tomorrow"
        raise RequestError(
            429, f"You've used your {q['limit']} requests for today. Next one frees up at {when}."
        )

    auto = user.is_admin or (
        user.auto_approve
        if user.auto_approve is not None
        else await settings.get("requests.auto_approve")
    )
    req = Request(
        user_id=user.id,
        media_type=media_type.value,
        provider=provider,
        external_id=str(external_id),
        title=item.title,
        alt_titles=json.dumps(list(item.alt_titles)) if item.alt_titles else None,
        year=str(item.year) if item.year else None,
        publisher=item.publisher,
        cover_url=item.cover_url,
        anilist_id=item.anilist_id,
        expected_count=item.issue_count,
        downloader=await settings.get(f"media.{media_type.value}.downloader"),
        status=S.APPROVED.value if auto else S.PENDING.value,
        status_message=None if auto else "Waiting for an admin to approve.",
        approved_by="auto" if auto else None,
    )
    db.add(req)
    await db.flush()
    await audit.record(
        db,
        "request.create",
        actor=user,
        ip=ip,
        target=f"request:{req.id}",
        details={"title": req.title, "media_type": req.media_type, "status": req.status},
    )
    await db.commit()
    log.info(
        "Request created", extra={"request_id": req.id, "user": user.username, "status": req.status}
    )
    if auto:
        spawn_process(ctx, req.id)
    return req


# --------------------------------------------------------------------- processing

_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def spawn_process(ctx: AppContext, request_id: int) -> None:
    ctx.runner.spawn(process(ctx, request_id), name=f"request-{request_id}")


def _set(req: Request, status: str, message: str | None) -> bool:
    changed = req.status != status
    req.status = status
    req.status_message = message[:500] if message else None
    if status == S.IN_LIBRARY.value and req.completed_at is None:
        req.completed_at = utcnow()
    return changed


async def process(ctx: AppContext, request_id: int, *, force: bool = False) -> None:
    """Hand an approved/waiting request to its downloader. Safe to call repeatedly."""
    async with _locks[request_id], ctx.sessionmaker() as db:
        settings = SettingsService(db, ctx.box)
        req = await db.get(Request, request_id)
        if req is None or (req.status not in PROCESSABLE and not force):
            return
        before = req.status
        req.attempts += 1
        try:
            if req.media_type == MediaType.COMIC.value:
                await _send_comic(ctx, settings, req)
            else:
                await _send_manga(ctx, settings, req)
        except ProviderError as exc:
            if exc.retryable:
                _set(
                    req, S.WAITING_FOR_DOWNLOADER.value, f"{exc.message} Will retry automatically."
                )
                req.next_attempt_at = utcnow() + DOWNLOADER_RETRY
            else:
                _set(req, S.FAILED.value, exc.message)
                req.next_attempt_at = None
        except Exception:
            log.exception("Request processing crashed", extra={"request_id": request_id})
            _set(req, S.FAILED.value, "Unexpected error — see Admin → Logs.")
        if req.status != before:
            await audit.record(
                db,
                "request.status",
                target=f"request:{req.id}",
                details={"from": before, "to": req.status, "message": req.status_message},
            )
        await db.commit()
        log.info("Request processed", extra={"request_id": req.id, "status": req.status})


async def _downloader_configured(settings: SettingsService, name: str) -> bool:
    return bool(await settings.get(f"{name}.url")) and (
        name != "kapowarr" or bool(await settings.get("kapowarr.api_key"))
    )


async def _send_comic(ctx: AppContext, settings: SettingsService, req: Request) -> None:
    if not await _downloader_configured(settings, "kapowarr"):
        _set(
            req,
            S.WAITING_FOR_DOWNLOADER.value,
            "Kapowarr isn't set up yet (Admin → Downloader — Kapowarr).",
        )
        req.next_attempt_at = utcnow() + DOWNLOADER_RETRY
        return
    kapowarr = await connections.provider_for(settings, "kapowarr", ctx.http)
    folder = await settings.get("kapowarr.root_folder_id")
    if not folder:
        folders = await kapowarr.list_root_folders()
        if not folders:
            raise ProviderError("Kapowarr has no root folder — add one in Kapowarr first.")
        folder = folders[0].id
    vid, already = await kapowarr.add_volume(
        req.external_id,
        folder,
        monitor_new_issues=await settings.get("kapowarr.monitor_new_issues"),
        auto_search=await settings.get("kapowarr.auto_search"),
    )
    req.downloader = "kapowarr"
    req.downloader_ref = vid or None
    req.next_attempt_at = None
    _set(
        req,
        S.ADDED.value,
        "Already in Kapowarr." if already else "Added to Kapowarr — searching for issues.",
    )
    if vid:
        with contextlib.suppress(ProviderError):
            await _sync_comic(ctx, settings, req, kapowarr, scans=None)


async def candidates_for(
    ctx: AppContext,
    settings: SettingsService,
    titles: list[str],
    source_ids: list[str],
    names: dict[str, str],
    *,
    limit_per_source: int = 5,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Search each source for each title. Returns (candidates, error messages)."""
    suwayomi = await connections.provider_for(settings, "suwayomi", ctx.http)
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for sid in source_ids:
        found: dict[str, dict[str, Any]] = {}
        try:
            for t in titles:
                for m in await suwayomi.search_source(sid, t):
                    penalty = edition_penalty(m.get("title") or "")
                    if penalty is None:
                        continue
                    score = best_similarity(m.get("title") or "", titles) - penalty
                    mid = str(m["id"])
                    if mid not in found or found[mid]["score"] < score:
                        found[mid] = {
                            "source_id": sid,
                            "source_name": names.get(sid, sid),
                            "manga_id": int(m["id"]),
                            "title": m.get("title"),
                            "cover_url": m.get("thumbnailUrl"),
                            "score": round(score, 3),
                        }
        except ProviderError as exc:
            errors.append(f"{names.get(sid, sid)}: {exc.message}")
            continue
        out.extend(sorted(found.values(), key=lambda c: -c["score"])[:limit_per_source])
    return out, errors


async def _source_names(settings: SettingsService, ctx: AppContext) -> dict[str, str]:
    try:
        suwayomi = await connections.provider_for(settings, "suwayomi", ctx.http)
        return {s.id: s.name for s in await suwayomi.list_sources()}
    except ProviderError:
        return {}


def _titles(req: Request) -> list[str]:
    alts = json.loads(req.alt_titles) if req.alt_titles else []
    return dedupe([req.title, *alts])[:3]


async def _send_manga(ctx: AppContext, settings: SettingsService, req: Request) -> None:
    if not await _downloader_configured(settings, "suwayomi"):
        _set(
            req,
            S.WAITING_FOR_DOWNLOADER.value,
            "Suwayomi isn't set up yet (Admin → Downloader — Suwayomi).",
        )
        req.next_attempt_at = utcnow() + DOWNLOADER_RETRY
        return
    sources: list[str] = await settings.get("suwayomi.sources")
    if not sources:
        _set(
            req,
            S.WAITING_FOR_SOURCE.value,
            "No Suwayomi sources are allowed yet — an admin needs to pick some.",
        )
        req.next_attempt_at = utcnow() + timedelta(hours=await settings.get("requests.retry_hours"))
        return
    names = await _source_names(settings, ctx)
    threshold = (await settings.get("requests.source_match_percent")) / 100
    titles = _titles(req)
    best: dict[str, Any] | None = None
    errors: list[str] = []
    for sid in sources:  # priority order: the first source with a good match wins
        cands, errs = await candidates_for(ctx, settings, titles, [sid], names, limit_per_source=1)
        errors.extend(errs)
        if cands and cands[0]["score"] >= threshold:
            best = cands[0]
            break
    if best is None:
        if errors and len(errors) == len(sources):
            raise ProviderError("Couldn't search any Suwayomi source: " + errors[0], retryable=True)
        hours = await settings.get("requests.retry_hours")
        _set(
            req,
            S.WAITING_FOR_SOURCE.value,
            f"Not found on your Suwayomi sources yet — retrying every {hours} h.",
        )
        req.next_attempt_at = utcnow() + timedelta(hours=hours)
        return
    await attach_manga(ctx, settings, req, best)


async def attach_manga(
    ctx: AppContext, settings: SettingsService, req: Request, cand: dict[str, Any]
) -> None:
    suwayomi = await connections.provider_for(settings, "suwayomi", ctx.http)
    mid = int(cand["manga_id"])
    await suwayomi.add_to_library(mid)
    chapters = await suwayomi.fetch_chapters(mid)
    missing = [int(c["id"]) for c in chapters if not c.get("isDownloaded")]
    await suwayomi.enqueue(missing)
    req.downloader = "suwayomi"
    req.downloader_ref = str(mid)
    req.source_id = str(cand["source_id"])
    req.source_name = str(cand.get("source_name") or cand["source_id"])[:255]
    req.total_count = len(chapters)
    req.have_count = len(chapters) - len(missing)
    req.next_attempt_at = None
    found_as = cand.get("title") or req.title
    if not chapters:
        _set(
            req,
            S.DOWNLOADING.value,
            f"Found on {req.source_name} as “{found_as}”, but it lists no chapters yet.",
        )
    else:
        _set(
            req,
            S.DOWNLOADING.value,
            f"Found on {req.source_name} as “{found_as}” — {len(missing)} chapter(s) queued.",
        )


# --------------------------------------------------------------------- availability


async def _sync_comic(
    ctx: AppContext, settings: SettingsService, req: Request, kapowarr: Any, scans: set | None
) -> None:
    try:
        vol = await kapowarr.get_volume(req.downloader_ref)
    except VolumeNotFound:
        _set(req, S.FAILED.value, "The volume was removed from Kapowarr.")
        return
    req.have_count = vol.issues_downloaded
    req.total_count = vol.issue_count
    await _apply_counts(settings, req, scans, unit="issues")


async def _sync_manga(
    ctx: AppContext, settings: SettingsService, req: Request, suwayomi: Any, scans: set | None
) -> None:
    mid = int(req.downloader_ref)
    if await settings.get("suwayomi.auto_download_new"):
        try:
            await suwayomi.fetch_chapters(mid)  # pick up newly released chapters
        except ProviderError as exc:
            log.debug("Chapter refresh failed", extra={"request_id": req.id, "error": exc.message})
    have, total, missing = await suwayomi.chapter_status(mid)
    if missing and await settings.get("suwayomi.auto_download_new"):
        await suwayomi.enqueue(missing)
    req.have_count, req.total_count = have, total
    await _apply_counts(settings, req, scans, unit="chapters", queued=bool(missing))


async def _apply_counts(
    settings: SettingsService, req: Request, scans: set | None, *, unit: str, queued: bool = False
) -> None:
    rules = await Rules.load(settings)
    have = req.have_count or 0
    if req.downloader == "kapowarr" and req.total_count:
        expected = req.total_count  # Kapowarr's issue list is authoritative for comics
    else:  # sources can lag behind AniList's chapter count
        expected = max(req.total_count or 0, req.expected_count or 0)
    before = req.status
    if have <= 0:
        status = S.ADDED.value if req.downloader == "kapowarr" else S.DOWNLOADING.value
        msg = (
            req.status_message
            if req.status == status
            else (
                "Waiting for Kapowarr to find issues."
                if status == S.ADDED.value
                else "Waiting for chapters to download."
            )
        )
        _set(req, status, msg)
        return
    status = availability(have, expected, rules)
    recent = req.created_at and utcnow() - req.created_at < timedelta(hours=24)
    still_fetching = queued or req.downloader == "kapowarr"
    if status == S.PARTIAL.value and still_fetching and recent:
        status = S.DOWNLOADING.value
    approx = "" if req.downloader == "kapowarr" else "~"
    msg = {
        S.IN_LIBRARY.value: f"Downloaded — {have} {unit}.",
        S.PARTIAL.value: f"Only part is available so far ({have} of {approx}{expected} {unit}).",
        S.DOWNLOADING.value: f"Downloading — {have} of {expected or '?'} {unit} so far.",
    }[status]
    _set(req, status, msg)
    if status == S.IN_LIBRARY.value and before != status and scans is not None:
        scans.add(req.media_type)


async def sync_request(ctx: AppContext, request_id: int) -> None:
    """Refresh one request's progress from its downloader (admin "Refresh")."""
    async with _locks[request_id], ctx.sessionmaker() as db:
        settings = SettingsService(db, ctx.box)
        req = await db.get(Request, request_id)
        if req is None or not req.downloader_ref:
            return
        scans: set[str] = set()
        await _sync_one(ctx, settings, req, scans)
        await db.commit()
    await scan_libraries(ctx, scans)


async def _sync_one(ctx: AppContext, settings: SettingsService, req: Request, scans: set) -> None:
    before = req.status
    provider = await connections.provider_for(settings, req.downloader or "kapowarr", ctx.http)
    if req.downloader == "kapowarr":
        await _sync_comic(ctx, settings, req, provider, scans)
    elif req.downloader == "suwayomi":
        try:
            await _sync_manga(ctx, settings, req, provider, scans)
        except ProviderError as exc:
            if "no longer has" in exc.message:
                _set(req, S.FAILED.value, "The manga was removed from Suwayomi.")
            else:
                raise
    if req.status != before:
        await audit.record(
            settings.db,
            "request.status",
            target=f"request:{req.id}",
            details={"from": before, "to": req.status, "message": req.status_message},
        )


async def sync_all(ctx: AppContext) -> str:
    """Availability job: refresh every request that's with a downloader."""
    async with ctx.sessionmaker() as db:
        ids = (
            await db.scalars(
                select(Request.id).where(
                    Request.status.in_(SYNCABLE), Request.downloader_ref.is_not(None)
                )
            )
        ).all()
    scans: set[str] = set()
    counts: dict[str, int] = defaultdict(int)
    errors = 0
    down: set[str] = set()
    for rid in ids:
        async with _locks[rid], ctx.sessionmaker() as db:
            settings = SettingsService(db, ctx.box)
            req = await db.get(Request, rid)
            if req is None or req.status not in SYNCABLE:
                continue
            if req.downloader in down:
                continue
            # Finished comics don't change; finished manga only if new chapters are wanted.
            if req.status == S.IN_LIBRARY.value and (
                req.downloader == "kapowarr" or not await settings.get("suwayomi.auto_download_new")
            ):
                continue
            try:
                await _sync_one(ctx, settings, req, scans)
                counts[req.status] += 1
            except ProviderError as exc:
                errors += 1
                if exc.retryable:
                    down.add(req.downloader or "")
                log.warning(
                    "Availability check failed", extra={"request_id": rid, "error": exc.message}
                )
            await db.commit()
    await scan_libraries(ctx, scans)
    summary = (
        ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in sorted(counts.items()))
        or "nothing to check"
    )
    if down:
        summary += f"; unreachable: {', '.join(sorted(down))}"
    elif errors:
        summary += f"; {errors} error(s)"
    return summary


async def scan_libraries(ctx: AppContext, media_types: set[str]) -> None:
    if not media_types:
        return
    async with ctx.sessionmaker() as db:
        settings = SettingsService(db, ctx.box)
        if not await settings.get("kavita.scan_after_download") or not await settings.get(
            "kavita.api_key"
        ):
            return
        libs = {await settings.get(f"kavita.library.{m}") for m in media_types} - {""}
        try:
            kavita = await connections.provider_for(settings, "kavita", ctx.http)
            for lib in libs:
                await kavita.trigger_scan(lib)
            log.info("Triggered Kavita scan", extra={"libraries": ",".join(sorted(libs))})
        except ProviderError as exc:
            log.warning("Kavita scan failed", extra={"error": exc.message})


async def retry_due(ctx: AppContext) -> str:
    """Retry job: re-process waiting requests whose next attempt is due."""
    now = utcnow()
    async with ctx.sessionmaker() as db:
        ids = (
            await db.scalars(
                select(Request.id).where(
                    Request.status.in_(
                        [
                            S.APPROVED.value,
                            S.WAITING_FOR_SOURCE.value,
                            S.WAITING_FOR_DOWNLOADER.value,
                        ]
                    ),
                    or_(Request.next_attempt_at.is_(None), Request.next_attempt_at <= now),
                )
            )
        ).all()
    for rid in ids:
        await process(ctx, rid)
    async with ctx.sessionmaker() as db:
        rows = (
            (
                await db.execute(
                    select(Request.status, func.count())
                    .where(Request.id.in_(ids))
                    .group_by(Request.status)
                )
            ).all()
            if ids
            else []
        )
    if not ids:
        return "nothing due"
    return f"retried {len(ids)}: " + ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in rows)


# --------------------------------------------------------------------- annotation


async def annotate(
    db: AsyncSession, items: list[dict[str, Any]], user: User
) -> list[dict[str, Any]]:
    """Add ``request`` = {id, status, mine} to item dicts that are already requested."""
    if not items:
        return items
    pairs = {(i["provider"], str(i["external_id"])) for i in items}
    anilist_ids = {i["anilist_id"] for i in items if i.get("anilist_id")}
    ext_ids = {e for _, e in pairs}
    cond = Request.external_id.in_(ext_ids)
    if anilist_ids:
        cond = or_(cond, Request.anilist_id.in_(anilist_ids))
    rows = (await db.scalars(select(Request).where(cond).order_by(Request.created_at.desc()))).all()
    by_key: dict[tuple[str, str], Request] = {}
    by_al: dict[int, Request] = {}
    for r in rows:
        if r.status in CLOSED and (r.provider, r.external_id) in by_key:
            continue
        by_key.setdefault((r.provider, r.external_id), r)
        if r.anilist_id:
            by_al.setdefault(r.anilist_id, r)
    for i in items:
        r = by_key.get((i["provider"], str(i["external_id"]))) or (
            by_al.get(i["anilist_id"]) if i.get("anilist_id") else None
        )
        i["request"] = {"id": r.id, "status": r.status, "mine": r.user_id == user.id} if r else None
    return items


def next_attempt_text(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
