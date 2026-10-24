"""/api/requests — create and list requests; admin approve/deny/retry/source/delete."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.api.deps import AdminUser, Ctx, CurrentUser, Db, Settings, client_ip
from app.db.models import Request as Req
from app.db.models import User
from app.domain import MediaType, RequestStatus
from app.providers.base import ProviderError
from app.services import audit, requests
from app.services.requests import RequestError

router = APIRouter(prefix="/api/requests", tags=["requests"])
S = RequestStatus


class CreateBody(BaseModel):
    media_type: MediaType
    provider: str = Field(max_length=16)
    external_id: str = Field(max_length=32)
    hints: dict[str, Any] | None = None


class DenyBody(BaseModel):
    reason: str = Field(default="", max_length=300)


class SourceBody(BaseModel):
    source_id: str = Field(max_length=32)
    manga_id: int
    title: str | None = Field(default=None, max_length=500)


def _err(exc: RequestError) -> HTTPException:
    detail: Any = exc.message if not exc.extra else {"message": exc.message, **exc.extra}
    return HTTPException(exc.status_code, detail)


async def _get(db: Db, request_id: int) -> Req:
    req = await db.get(Req, request_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such request")
    return req


@router.get("/quota")
async def get_quota(user: CurrentUser, db: Db, settings: Settings) -> dict[str, Any]:
    return await requests.quota(db, settings, user)


@router.get("")
async def list_requests(
    user: CurrentUser,
    db: Db,
    scope: Literal["mine", "all"] = "mine",
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    media_type: MediaType | None = None,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if scope == "all" and not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator access required")
    stmt = select(Req)
    if scope == "mine":
        stmt = stmt.where(Req.user_id == user.id)
    if status_filter == "active":
        stmt = stmt.where(Req.status.not_in([S.IN_LIBRARY.value, S.FAILED.value, S.DENIED.value]))
    elif status_filter:
        stmt = stmt.where(Req.status == status_filter)
    if media_type:
        stmt = stmt.where(Req.media_type == media_type.value)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.outerjoin(User, User.id == Req.user_id).where(
            or_(Req.title.ilike(like), User.username.ilike(like))
        )
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        (await db.scalars(stmt.order_by(Req.created_at.desc()).limit(limit).offset(offset)))
        .unique()
        .all()
    )
    counts_stmt = select(Req.status, func.count()).group_by(Req.status)
    if scope == "mine":
        counts_stmt = counts_stmt.where(Req.user_id == user.id)
    counts = {s: n for s, n in (await db.execute(counts_stmt)).all()}
    return {
        "total": total,
        "counts": counts,
        "requests": [requests.to_json(r, admin=user.is_admin, me=user) for r in rows],
    }


@router.post("", status_code=201)
async def create_request(
    body: CreateBody, request: Request, user: CurrentUser, ctx: Ctx, db: Db
) -> dict[str, Any]:
    try:
        req = await requests.create(
            ctx,
            db,
            user,
            body.media_type,
            body.provider,
            body.external_id,
            body.hints,
            client_ip(request),
        )
    except RequestError as exc:
        raise _err(exc) from exc
    await db.refresh(req)
    return requests.to_json(req, admin=user.is_admin, me=user)


@router.get("/{request_id}")
async def get_request(request_id: int, user: CurrentUser, db: Db) -> dict[str, Any]:
    req = await _get(db, request_id)
    if req.user_id != user.id and not user.is_admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such request")
    return requests.to_json(req, admin=user.is_admin, me=user)


@router.delete("/{request_id}")
async def delete_request(
    request_id: int, request: Request, user: CurrentUser, db: Db
) -> dict[str, Any]:
    req = await _get(db, request_id)
    if not user.is_admin:
        if req.user_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such request")
        if req.status not in requests.USER_CANCELLABLE:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "It's already with the downloader — ask an admin to remove it.",
            )
    await audit.record(
        db,
        "request.delete",
        actor=user,
        ip=client_ip(request),
        target=f"request:{req.id}",
        details={"title": req.title, "status": req.status},
    )
    await db.delete(req)
    await db.commit()
    return {"ok": True}


async def _admin_change(
    db: Db, req: Req, user: User, ip: str | None, action: str, **details: Any
) -> None:
    await audit.record(
        db,
        f"request.{action}",
        actor=user,
        ip=ip,
        target=f"request:{req.id}",
        details={"title": req.title, **details},
    )
    await db.commit()


@router.post("/{request_id}/approve")
async def approve(
    request_id: int, request: Request, user: AdminUser, ctx: Ctx, db: Db
) -> dict[str, Any]:
    req = await _get(db, request_id)
    if req.status not in (S.PENDING.value, S.DENIED.value):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only pending or denied requests can be approved."
        )
    req.status, req.status_message, req.approved_by = S.APPROVED.value, None, user.username
    await _admin_change(db, req, user, client_ip(request), "approve")
    requests.spawn_process(ctx, req.id)
    return requests.to_json(req, admin=True, me=user)


@router.post("/{request_id}/deny")
async def deny(
    request_id: int, body: DenyBody, request: Request, user: AdminUser, db: Db
) -> dict[str, Any]:
    req = await _get(db, request_id)
    if req.status in (S.DENIED.value,):
        raise HTTPException(status.HTTP_409_CONFLICT, "Already denied.")
    req.status = S.DENIED.value
    req.status_message = (
        f"Denied: {body.reason.strip()}" if body.reason.strip() else "Denied by an admin."
    )[:500]
    req.next_attempt_at = None
    await _admin_change(db, req, user, client_ip(request), "deny", reason=body.reason)
    return requests.to_json(req, admin=True, me=user)


@router.post("/{request_id}/retry")
async def retry(
    request_id: int, request: Request, user: AdminUser, ctx: Ctx, db: Db
) -> dict[str, Any]:
    req = await _get(db, request_id)
    if req.status in requests.SYNCABLE and req.downloader_ref:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "It's already with the downloader — use Refresh."
        )
    req.status, req.status_message, req.next_attempt_at = S.APPROVED.value, "Retrying…", None
    req.approved_by = req.approved_by or user.username
    await _admin_change(db, req, user, client_ip(request), "retry")
    requests.spawn_process(ctx, req.id)
    return requests.to_json(req, admin=True, me=user)


@router.post("/{request_id}/refresh")
async def refresh(request_id: int, user: AdminUser, ctx: Ctx, db: Db) -> dict[str, Any]:
    req = await _get(db, request_id)
    if not req.downloader_ref:
        raise HTTPException(status.HTTP_409_CONFLICT, "Not with a downloader yet.")
    try:
        await requests.sync_request(ctx, req.id)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.message) from exc
    await db.refresh(req)
    return requests.to_json(req, admin=True, me=user)


@router.get("/{request_id}/candidates")
async def candidates(
    request_id: int,
    user: AdminUser,
    ctx: Ctx,
    db: Db,
    settings: Settings,
    q: str | None = Query(default=None, max_length=200),
    source_id: str | None = Query(default=None, max_length=32),
) -> dict[str, Any]:
    req = await _get(db, request_id)
    if req.media_type == MediaType.COMIC.value:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Choosing a source applies to manga and manhwa."
        )
    names = await requests._source_names(settings, ctx)
    sources = [source_id] if source_id else (await settings.get("suwayomi.sources"))
    if not sources:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "No Suwayomi sources are allowed — pick some first."
        )
    titles = [q.strip()] if q and q.strip() else requests._titles(req)
    try:
        found, errors = await requests.candidates_for(ctx, settings, titles, sources, names)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.message) from exc
    return {
        "candidates": found,
        "errors": errors,
        "sources": [
            {"id": s, "name": names.get(s, s)} for s in await settings.get("suwayomi.sources")
        ],
        "titles": titles,
    }


@router.post("/{request_id}/source")
async def set_source(
    request_id: int,
    body: SourceBody,
    request: Request,
    user: AdminUser,
    ctx: Ctx,
    db: Db,
    settings: Settings,
) -> dict[str, Any]:
    req = await _get(db, request_id)
    if req.media_type == MediaType.COMIC.value:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Choosing a source applies to manga and manhwa."
        )
    names = await requests._source_names(settings, ctx)
    before = {"source": req.source_name, "manga": req.downloader_ref}
    try:
        await requests.attach_manga(
            ctx,
            settings,
            req,
            {
                "source_id": body.source_id,
                "source_name": names.get(body.source_id, body.source_id),
                "manga_id": body.manga_id,
                "title": body.title,
            },
        )
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.message) from exc
    req.approved_by = req.approved_by or user.username
    await _admin_change(
        db,
        req,
        user,
        client_ip(request),
        "source",
        before=json.dumps(before),
        source=req.source_name,
        manga=body.manga_id,
    )
    return requests.to_json(req, admin=True, me=user)
