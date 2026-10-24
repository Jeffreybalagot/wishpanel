"""/api/admin/users — list known users and adjust per-user controls. Admin only.

Admin status itself comes from Kavita roles at every sign-in, so it's shown
here but not editable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.api.deps import AdminUser, Db, client_ip
from app.db.base import utcnow
from app.db.models import Session, User
from app.domain import MediaType
from app.services import audit

router = APIRouter(prefix="/api/admin/users", tags=["admin"])

MEDIA_VALUES = [m.value for m in MediaType]


class UserOut(BaseModel):
    id: int
    username: str
    email: str | None
    is_admin: bool
    is_blocked: bool
    daily_limit_override: int | None
    auto_approve: bool | None
    allowed_media_types: list[str] | None  # None = all enabled types
    created_at: datetime
    last_login_at: datetime | None
    active_sessions: int


class UserPatch(BaseModel):
    """Every field optional; sending ``null`` resets to the global default."""

    is_blocked: bool | None = None
    daily_limit_override: int | None = Field(default=None, ge=0, le=10_000)
    auto_approve: bool | None = None
    allowed_media_types: list[MediaType] | None = None


def _media(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [m for m in raw.split(",") if m in MEDIA_VALUES]


async def _out(db: Db, user: User) -> UserOut:
    sessions = await db.scalar(
        select(func.count())
        .select_from(Session)
        .where(Session.user_id == user.id, Session.expires_at > utcnow())
    )
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        is_admin=user.is_admin,
        is_blocked=user.is_blocked,
        daily_limit_override=user.daily_limit_override,
        auto_approve=user.auto_approve,
        allowed_media_types=_media(user.allowed_media_types),
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        active_sessions=sessions or 0,
    )


@router.get("", response_model=list[UserOut])
async def list_users(_: AdminUser, db: Db) -> list[UserOut]:
    users = (await db.scalars(select(User).order_by(func.lower(User.username)))).all()
    return [await _out(db, u) for u in users]


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int, body: UserPatch, request: Request, admin: AdminUser, db: Db
) -> UserOut:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    sent = body.model_fields_set
    changes: dict[str, Any] = {}

    if "is_blocked" in sent:
        blocked = bool(body.is_blocked)
        if blocked and user.id == admin.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "You can't block yourself.")
        if blocked != user.is_blocked:
            user.is_blocked = blocked
            changes["is_blocked"] = blocked
            if blocked:  # end every session immediately
                await db.execute(delete(Session).where(Session.user_id == user.id))
    if "daily_limit_override" in sent and body.daily_limit_override != user.daily_limit_override:
        user.daily_limit_override = body.daily_limit_override
        changes["daily_limit_override"] = body.daily_limit_override
    if "auto_approve" in sent and body.auto_approve != user.auto_approve:
        user.auto_approve = body.auto_approve
        changes["auto_approve"] = body.auto_approve
    if "allowed_media_types" in sent:
        new = (
            None
            if body.allowed_media_types is None
            else ",".join(m.value for m in MediaType if m in body.allowed_media_types)
        )
        if new != user.allowed_media_types:
            user.allowed_media_types = new
            changes["allowed_media_types"] = new

    if changes:
        await audit.record(
            db,
            "user.update",
            actor=admin,
            target=user.username,
            ip=client_ip(request),
            details=changes,
        )
        await db.commit()
    return await _out(db, user)
