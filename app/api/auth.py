"""/api/auth — login, logout, current user."""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
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
from app.context import AppContext
from app.core.crypto import SecretDecryptionError
from app.core.security import tokens_equal
from app.domain import AuthenticatedUser
from app.providers.auth.oidc import OIDCClient
from app.providers.base import AuthProvider, InvalidCredentials, ProviderError
from app.providers.registry import get_auth_provider
from app.services import audit
from app.services.auth import (
    SESSION_COOKIE,
    IssuedSession,
    LoginRefused,
    complete_login,
    end_session,
    issue_session,
    resolve_oidc_user,
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


# ---------------- single sign-on (OpenID Connect) ----------------

OIDC_STATE_COOKIE = "wishpanel_oidc"
_OIDC_STATE_TTL = 600  # seconds the user has to finish signing in at the provider


async def _base_url(request: Request, settings: SettingsService) -> str:
    """Absolute URL of WishPanel's root (no trailing slash), as users reach it."""
    prefix = request.app.state.ctx.config.base_path or ""
    public = (await settings.get("general.public_url") or "").rstrip("/")
    if public:
        return public if not prefix or public.endswith(prefix) else public + prefix
    return f"{request.url.scheme}://{request.url.netloc}{prefix}"


async def _oidc_client(settings: SettingsService, ctx: AppContext) -> OIDCClient:
    return OIDCClient(
        ctx.http,
        await settings.get("auth.oidc.issuer_url"),
        await settings.get("auth.oidc.client_id"),
        await settings.get("auth.oidc.client_secret"),
    )


def _home(request: Request, error: str | None = None) -> RedirectResponse:
    prefix = request.app.state.ctx.config.base_path or ""
    url = f"{prefix}/"
    if error:
        url += "?" + urlencode({"sso_error": error})
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _clear_state(response: Response, request: Request) -> None:
    prefix = request.app.state.ctx.config.base_path or ""
    response.delete_cookie(OIDC_STATE_COOKIE, path=f"{prefix}/api/auth/oidc")


@router.get("/oidc/login", include_in_schema=False)
async def oidc_login(request: Request, ctx: Ctx, settings: Settings) -> RedirectResponse:
    """Start SSO: remember state/nonce/PKCE in an encrypted cookie, go to the provider."""
    if not await settings.get("setup.completed"):
        return _home(request, "First-run setup has not been completed.")
    if not await settings.get("auth.oidc.enabled"):
        return _home(request, "Single sign-on is turned off.")
    redirect_uri = await _base_url(request, settings) + "/api/auth/oidc/callback"
    try:
        auth = await (await _oidc_client(settings, ctx)).authorization_request(redirect_uri)
    except ProviderError as exc:
        log.warning("SSO start failed", extra={"error": exc.message})
        return _home(request, exc.message)
    pending = json.dumps(
        {
            "state": auth.state,
            "nonce": auth.nonce,
            "verifier": auth.code_verifier,
            "redirect_uri": redirect_uri,
            "exp": int(time.time()) + _OIDC_STATE_TTL,
        }
    )
    response = RedirectResponse(auth.url, status_code=status.HTTP_303_SEE_OTHER)
    prefix = ctx.config.base_path or ""
    response.set_cookie(
        OIDC_STATE_COOKIE,
        ctx.box.encrypt(pending),
        max_age=_OIDC_STATE_TTL,
        httponly=True,
        secure=await _cookie_secure(settings, request),
        samesite="lax",  # sent on the top-level redirect back from the provider
        path=f"{prefix}/api/auth/oidc",
    )
    return response


@router.get("/oidc/callback", include_in_schema=False)
async def oidc_callback(request: Request, ctx: Ctx, db: Db, settings: Settings) -> RedirectResponse:
    """Finish SSO: verify state, exchange the code, sign the user in, go home."""
    ip = client_ip(request)

    def fail(message: str) -> RedirectResponse:
        resp = _home(request, message)
        _clear_state(resp, request)
        return resp

    if not await settings.get("auth.oidc.enabled") or not await settings.get("setup.completed"):
        return fail("Single sign-on is turned off.")
    params = request.query_params
    if params.get("error"):
        reason = params.get("error_description") or params.get("error") or ""
        log.info("SSO cancelled or refused by provider", extra={"error": reason[:200], "ip": ip})
        return fail("Sign-in was cancelled or refused by the SSO provider.")

    raw = request.cookies.get(OIDC_STATE_COOKIE)
    try:
        pending = json.loads(ctx.box.decrypt(raw)) if raw else None
    except (SecretDecryptionError, ValueError):
        pending = None
    if (
        not isinstance(pending, dict)
        or int(pending.get("exp") or 0) < time.time()
        or not tokens_equal(params.get("state"), pending.get("state"))
        or not params.get("code")
    ):
        return fail("Your sign-in expired or came from another browser. Please try again.")

    try:
        claims = await (await _oidc_client(settings, ctx)).finish(
            params["code"],
            str(pending["redirect_uri"]),
            nonce=str(pending["nonce"]),
            code_verifier=str(pending["verifier"]),
        )
        user = await resolve_oidc_user(
            db,
            claims,
            admin_group=await settings.get("auth.oidc.admin_group"),
            link_by_email=await settings.get("auth.oidc.link_by_email"),
            auto_create=await settings.get("auth.oidc.auto_create"),
        )
        issued = await issue_session(
            db, settings, user, ip=ip, user_agent=request.headers.get("user-agent"), via="sso"
        )
    except ProviderError as exc:
        await audit.record(db, "auth.login_failed", target="sso", ip=ip)
        await db.commit()
        return fail(exc.message)
    except LoginRefused as exc:
        await db.commit()  # keep the audit entry
        return fail(str(exc))

    await db.commit()
    response = _home(request)
    _clear_state(response, request)
    await set_session_cookie(response, request, settings, issued)
    return response


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
