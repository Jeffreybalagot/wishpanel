"""Login, session and user-cache logic (provider-agnostic)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_token, new_token
from app.db.base import utcnow
from app.db.models import Session, User
from app.domain import AuthenticatedUser
from app.providers.base import AuthProvider
from app.services import audit
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

SESSION_COOKIE = "wishpanel_session"
_TOUCH_INTERVAL = timedelta(minutes=5)


class LoginRefused(Exception):
    """Credentials were fine but WishPanel won't let this user in."""


@dataclass
class IssuedSession:
    token: str  # raw cookie value — only ever sent to the browser
    csrf_token: str
    max_age: int
    user: User


async def upsert_user(db: AsyncSession, authed: AuthenticatedUser, is_admin: bool) -> User:
    user = await db.scalar(
        select(User).where(User.provider == authed.provider, User.external_id == authed.external_id)
    )
    if user is None:
        user = User(provider=authed.provider, external_id=authed.external_id, username="")
        db.add(user)
    user.username = authed.username
    user.email = authed.email
    user.is_admin = is_admin
    user.last_login_at = utcnow()
    await db.flush()
    return user


async def complete_login(
    db: AsyncSession,
    settings: SettingsService,
    provider: AuthProvider,
    authed: AuthenticatedUser,
    *,
    ip: str | None,
    user_agent: str | None,
) -> IssuedSession:
    admin_role = await settings.get("auth.admin_role")
    is_admin = provider.is_admin(authed, admin_role)
    user = await upsert_user(db, authed, is_admin)
    if user.is_blocked:
        await audit.record(db, "auth.login_blocked", target=user.username, ip=ip)
        raise LoginRefused("This account has been blocked by an administrator.")

    days = await settings.get("auth.session_days")
    token, csrf = new_token(), new_token(24)
    db.add(
        Session(
            id=hash_token(token),
            user_id=user.id,
            csrf_token=csrf,
            expires_at=utcnow() + timedelta(days=days),
            ip=ip,
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    await audit.record(db, "auth.login", actor=user, ip=ip, details={"admin": is_admin})
    log.info("User logged in", extra={"user": user.username, "admin": is_admin})
    return IssuedSession(token=token, csrf_token=csrf, max_age=days * 86400, user=user)


async def resolve_session(db: AsyncSession, token: str | None) -> Session | None:
    """Return the live session for a cookie token, or None."""
    if not token:
        return None
    sess = await db.get(Session, hash_token(token))
    if sess is None:
        return None
    now = utcnow()
    if sess.expires_at <= now:
        await db.delete(sess)
        await db.commit()
        return None
    await db.refresh(sess, ["user"])
    if sess.user.is_blocked:
        return None
    if now - sess.last_seen_at > _TOUCH_INTERVAL:
        sess.last_seen_at = now
        await db.commit()
    return sess


async def end_session(db: AsyncSession, token: str | None) -> None:
    if token:
        await db.execute(delete(Session).where(Session.id == hash_token(token)))


async def purge_expired_sessions(db: AsyncSession) -> int:
    result = await db.execute(delete(Session).where(Session.expires_at <= utcnow()))
    return result.rowcount or 0
