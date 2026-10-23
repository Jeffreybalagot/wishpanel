"""/api/setup — first-run wizard.

Until setup completes, WishPanel has no admin. Every setup action that does
anything (verifying the claim code, testing a Kavita URL, completing setup)
requires the one-time setup code from the container log / /config, so only
someone who controls the container can claim it.

Completing setup also requires signing in with an account that holds the
configured admin role *on the Kavita being configured*. Afterwards the code
is destroyed and every endpoint here returns 409.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.auth import MeResponse, set_session_cookie, throttled_login
from app.api.deps import Ctx, Db, Settings, client_ip, require_json
from app.context import AppContext
from app.providers.auth.kavita import KavitaAuthProvider
from app.services import audit, setup_claim
from app.services.settings import SettingsService, SettingValidationError, serialise
from app.services.settings_registry import REGISTRY

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/setup", tags=["setup"])

CODE_FIELD = Field(min_length=1, max_length=64)


class SetupStatus(BaseModel):
    completed: bool
    code_required: bool
    admin_role: str


class ClaimBody(BaseModel):
    code: str = CODE_FIELD


class ClaimResponse(BaseModel):
    ok: bool
    kavita_url: str  # pre-fill from a legacy env import, only revealed after a valid code


class KavitaTestBody(BaseModel):
    code: str = CODE_FIELD
    url: str = Field(min_length=1, max_length=2048)


class TestResponse(BaseModel):
    ok: bool
    message: str


class CompleteBody(BaseModel):
    code: str = CODE_FIELD
    kavita_url: str = Field(min_length=1, max_length=2048)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


async def _require_not_completed(settings: SettingsService) -> None:
    if await settings.get("setup.completed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup has already been completed.")


async def _require_code(
    request: Request, ctx: AppContext, db: Db, settings: SettingsService, code: str
) -> None:
    """Verify the setup code, throttled per IP with the login limits."""
    ip = client_ip(request)
    keys = [f"setup-ip:{ip}"]
    max_attempts = await settings.get("auth.login_max_attempts")
    window_s = (await settings.get("auth.login_window_minutes")) * 60
    wait = ctx.throttle.retry_after(keys, max_attempts, window_s)
    if wait:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many wrong setup codes. Try again in {max(1, wait // 60)} minute(s).",
            headers={"Retry-After": str(wait)},
        )
    if not await setup_claim.verify(settings, code):
        ctx.throttle.record_failure(keys)
        await audit.record(db, "setup.code_rejected", ip=ip)
        await db.commit()
        log.warning("Rejected setup code", extra={"ip": ip})
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "That setup code isn't right. Copy it from the container log or "
            "/config/setup-code.txt.",
        )
    ctx.throttle.reset(keys)


def _clean_url(raw: str) -> str:
    try:
        url = serialise(REGISTRY["kavita.url"], raw)
    except SettingValidationError as exc:
        raise HTTPException(422, exc.message) from exc
    if not url:
        raise HTTPException(422, "Kavita URL is required.")
    return url


@router.get("/status", response_model=SetupStatus)
async def setup_status(settings: Settings) -> SetupStatus:
    completed = await settings.get("setup.completed")
    return SetupStatus(
        completed=completed,
        code_required=not completed,
        admin_role=await settings.get("auth.admin_role"),
    )


@router.post("/claim", response_model=ClaimResponse, dependencies=[Depends(require_json)])
async def claim(
    body: ClaimBody, request: Request, ctx: Ctx, db: Db, settings: Settings
) -> ClaimResponse:
    await _require_not_completed(settings)
    await _require_code(request, ctx, db, settings, body.code)
    return ClaimResponse(ok=True, kavita_url=await settings.get("kavita.url"))


@router.post("/kavita/test", response_model=TestResponse, dependencies=[Depends(require_json)])
async def test_kavita(
    body: KavitaTestBody, request: Request, ctx: Ctx, db: Db, settings: Settings
) -> TestResponse:
    await _require_not_completed(settings)
    await _require_code(request, ctx, db, settings, body.code)
    result = await KavitaAuthProvider(ctx.http, _clean_url(body.url)).test_connection()
    return TestResponse(ok=result.ok, message=result.message)


@router.post("/complete", response_model=MeResponse, dependencies=[Depends(require_json)])
async def complete_setup(
    body: CompleteBody, request: Request, response: Response, ctx: Ctx, db: Db, settings: Settings
) -> MeResponse:
    await _require_not_completed(settings)
    await _require_code(request, ctx, db, settings, body.code)
    url = _clean_url(body.kavita_url)
    provider = KavitaAuthProvider(ctx.http, url)

    reach = await provider.test_connection()
    if not reach.ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, reach.message)

    authed, issued = await throttled_login(
        request, ctx, db, settings, provider, body.username, body.password
    )
    if not issued.user.is_admin:
        await db.rollback()
        role = await settings.get("auth.admin_role")
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"'{authed.username}' signed in, but isn't a Kavita '{role}'. "
            "Use a Kavita administrator account to finish setup.",
        )

    # Re-check inside this transaction so two simultaneous wizards can't both win.
    if await settings.get("setup.completed"):
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup has already been completed.")
    await settings.set("kavita.url", url)
    await settings.set("setup.completed", True)
    await setup_claim.consume(settings)
    await audit.record(
        db,
        "setup.completed",
        actor=issued.user,
        ip=client_ip(request),
        details={"kavita_url": url},
    )
    await db.commit()
    setup_claim.remove_file(ctx.config.config_dir)
    log.info("First-run setup completed", extra={"admin": issued.user.username})

    await set_session_cookie(response, request, settings, issued)
    u = issued.user
    return MeResponse(
        id=u.id,
        username=u.username,
        email=u.email,
        is_admin=u.is_admin,
        csrf_token=issued.csrf_token,
    )
