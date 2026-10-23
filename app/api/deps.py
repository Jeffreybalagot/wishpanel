"""FastAPI dependencies: DB session, settings, current user, admin guard, CSRF."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.security import tokens_equal
from app.db.models import Session, User
from app.services.auth import SESSION_COOKIE, resolve_session
from app.services.settings import SettingsService

CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


async def get_db(ctx: Ctx) -> AsyncIterator[AsyncSession]:
    async with ctx.sessionmaker() as db:
        yield db


Db = Annotated[AsyncSession, Depends(get_db)]


def get_settings(ctx: Ctx, db: Db) -> SettingsService:
    return SettingsService(db, ctx.box)


Settings = Annotated[SettingsService, Depends(get_settings)]


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def require_json(request: Request) -> None:
    """For unauthenticated POSTs (login/setup): a JSON content type forces a CORS
    preflight for cross-site callers, which — with SameSite=Lax cookies — closes
    the login-CSRF hole without needing a token before the user has a session."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Expected application/json")


async def optional_session(request: Request, db: Db) -> Session | None:
    return await resolve_session(db, request.cookies.get(SESSION_COOKIE))


OptionalSession = Annotated[Session | None, Depends(optional_session)]


async def require_session(request: Request, sess: OptionalSession) -> Session:
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    if request.method not in _SAFE_METHODS and not tokens_equal(
        request.headers.get(CSRF_HEADER), sess.csrf_token
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token")
    return sess


CurrentSession = Annotated[Session, Depends(require_session)]


async def require_user(sess: CurrentSession) -> User:
    return sess.user


CurrentUser = Annotated[User, Depends(require_user)]


async def require_admin(user: CurrentUser) -> User:
    """Server-side admin check. Hiding the Admin tab in the UI is cosmetic only."""
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator access required")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
