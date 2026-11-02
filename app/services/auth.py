"""Login, session and user-cache logic (provider-agnostic)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_token, new_token
from app.db.base import utcnow
from app.db.models import Session, User
from app.domain import AuthenticatedUser
from app.providers.auth.oidc import OIDCClaims
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
    if user is None and authed.legacy_ids:
        user = await db.scalar(
            select(User).where(
                User.provider == authed.provider, User.external_id.in_(authed.legacy_ids)
            )
        )
        if user is not None:
            log.info("Re-keyed user to stable provider id", extra={"user": authed.username})
            user.external_id = authed.external_id
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
    return await issue_session(db, settings, user, ip=ip, user_agent=user_agent)


async def issue_session(
    db: AsyncSession,
    settings: SettingsService,
    user: User,
    *,
    ip: str | None,
    user_agent: str | None,
    via: str | None = None,
) -> IssuedSession:
    """Refuse blocked users, otherwise create a session row for ``user``."""
    if user.is_blocked:
        await audit.record(db, "auth.login_blocked", target=user.username, ip=ip)
        raise LoginRefused("This account has been blocked by an administrator.")
    is_admin = user.is_admin

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
    details: dict[str, object] = {"admin": is_admin}
    if via:
        details["via"] = via
    await audit.record(db, "auth.login", actor=user, ip=ip, details=details)
    log.info("User logged in", extra={"user": user.username, "admin": is_admin, "via": via})
    return IssuedSession(token=token, csrf_token=csrf, max_age=days * 86400, user=user)


OIDC_PROVIDER = "oidc"


class SSOAccountMissing(LoginRefused):
    """SSO sign-in worked, but there is no WishPanel account and auto-create is off."""


async def resolve_oidc_user(
    db: AsyncSession,
    claims: OIDCClaims,
    *,
    admin_group: str,
    link_by_email: bool,
    auto_create: bool,
) -> User:
    """Find (or create) the WishPanel user for an SSO sign-in.

    1. A user who signed in with SSO before (same ``sub``).
    2. Otherwise, if allowed, an existing user with the same email address —
       typically the account created by a Kavita sign-in, so request history
       and per-user limits carry over. That account keeps its Kavita identity,
       so Kavita password sign-in keeps working for it.
    3. Otherwise a new user (if auto-create is on).

    Admin rights: for SSO-only accounts, membership of the configured admin
    group decides (checked at every SSO sign-in; no group = not admin). A linked
    Kavita account stays admin if Kavita says so, and the group can add admin.
    """
    user = await db.scalar(
        select(User).where(User.provider == OIDC_PROVIDER, User.external_id == claims.subject)
    )
    if user is None and link_by_email and claims.email and claims.email_verified is not False:
        candidates = (
            await db.scalars(
                select(User)
                .where(func.lower(User.email) == claims.email.lower())
                .order_by(User.last_login_at.desc().nulls_last(), User.id)
            )
        ).all()
        if candidates:
            user = candidates[0]
            log.info(
                "SSO sign-in linked to existing user by email",
                extra={"user": user.username, "provider": user.provider},
            )
    if user is None:
        if not auto_create:
            raise SSOAccountMissing(
                "You signed in, but there is no WishPanel account for you yet. "
                "Ask an administrator."
            )
        user = User(
            provider=OIDC_PROVIDER,
            external_id=claims.subject,
            username=claims.username,
            email=claims.email,
            is_admin=False,
        )
        db.add(user)
    elif user.provider == OIDC_PROVIDER:
        user.username = claims.username
        user.email = claims.email

    wanted = admin_group.strip().lower()
    in_group = bool(wanted) and any(g.lower() == wanted for g in claims.groups)
    if user.provider == OIDC_PROVIDER:
        if wanted:
            user.is_admin = in_group
    else:
        # A linked Kavita account: Kavita's admin role (refreshed at each Kavita
        # sign-in) still counts; the group can add admin rights, never remove them.
        user.is_admin = user.is_admin or in_group
    user.last_login_at = utcnow()
    await db.flush()
    return user


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
