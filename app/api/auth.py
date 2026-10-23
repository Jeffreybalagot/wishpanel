"""/api/auth — login, logout, current user."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import (
    CSRF_HEADER,
    Ctx,
    CurrentSession,
    Db,
    OptionalSession,
    Settings,
    client_ip,
    require_json,
)
from app.domain import AuthenticatedUser
from app.providers.base import AuthProvider, InvalidCredentials, ProviderError
from app.providers.registry import get_auth_provider
from app.services import audit
from app.services.auth import (
    SESSION_COOKIE,
    IssuedSession,
    LoginRefused,
    complete_login,
    end_session,
)
from app.services.settings import SettingsService

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class MeResponse(BaseModel):
    id: int
    username: str
    email: str | None
    is_admin: bool
    csrf_token: str


async def _cookie_secure(settings: SettingsService, request: Request) -> bool:
    mode = await settings.get("auth.cookie_secure")
    if mode == "always":
        return True
    if mode == "never":
        return False
    return request.url.scheme == "https"


async def set_session_cookie(
    response: Response, request: Request, settings: SettingsService, issued: IssuedSession
) -> None:
    ctx = request.app.state.ctx
    response.set_cookie(
        SESSION_COOKIE,
        issued.token,
        max_age=issued.max_age,
        httponly=True,
        secure=await _cookie_secure(settings, request),
        samesite="lax",
        path=(ctx.config.base_path or "") + "/",
    )


async def throttled_login(
    request: Request,
    ctx: Ctx,
    db: Db,
    settings: SettingsService,
    provider: AuthProvider,
    username: str,
    password: str,
) -> tuple[AuthenticatedUser, IssuedSession]:
    """Authenticate against the provider with per-IP and per-username throttling."""
    ip = client_ip(request)
    keys = [f"ip:{ip}", f"user:{username.strip().lower()}"]
    max_attempts = await settings.get("auth.login_max_attempts")
    window_s = (await settings.get("auth.login_window_minutes")) * 60

    wait = ctx.throttle.retry_after(keys, max_attempts, window_s)
    if wait:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).",
            headers={"Retry-After": str(wait)},
        )

    try:
        authed = await provider.login(username, password)
    except InvalidCredentials as exc:
        ctx.throttle.record_failure(keys)
        await audit.record(db, "auth.login_failed", target=username[:255], ip=ip)
        await db.commit()
        log.info("Failed login", extra={"user": username[:64], "ip": ip})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.message) from exc
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.message) from exc

    try:
        issued = await complete_login(
            db, settings, provider, authed, ip=ip, user_agent=request.headers.get("user-agent")
        )
    except LoginRefused as exc:
        await db.commit()  # keep the audit entry
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    ctx.throttle.reset(keys)
    return authed, issued


@router.post("/login", response_model=MeResponse, dependencies=[Depends(require_json)])
async def login(
    body: LoginBody, request: Request, response: Response, ctx: Ctx, db: Db, settings: Settings
) -> MeResponse:
    if not await settings.get("setup.completed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "First-run setup has not been completed.")
    provider = await get_auth_provider(settings, ctx.http)
    _, issued = await throttled_login(
        request, ctx, db, settings, provider, body.username, body.password
    )
    await db.commit()
    await set_session_cookie(response, request, settings, issued)
    response.headers[CSRF_HEADER] = issued.csrf_token
    u = issued.user
    return MeResponse(
        id=u.id,
        username=u.username,
        email=u.email,
        is_admin=u.is_admin,
        csrf_token=issued.csrf_token,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, sess: CurrentSession, db: Db) -> None:
    await audit.record(db, "auth.logout", actor=sess.user, ip=client_ip(request))
    await end_session(db, request.cookies.get(SESSION_COOKIE))
    await db.commit()
    ctx = request.app.state.ctx
    response.delete_cookie(SESSION_COOKIE, path=(ctx.config.base_path or "") + "/")


@router.get("/me", response_model=MeResponse)
async def me(sess: OptionalSession) -> MeResponse:
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    u = sess.user
    return MeResponse(
        id=u.id,
        username=u.username,
        email=u.email,
        is_admin=u.is_admin,
        csrf_token=sess.csrf_token,
    )
