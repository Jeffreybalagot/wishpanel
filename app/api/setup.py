"""/api/setup — first-run wizard.

Until setup completes, WishPanel has no admin. The wizard asks for the Kavita
URL and an account that holds the configured admin role *on that Kavita*.
Only when that login succeeds is the URL saved and setup locked; afterwards
every endpoint here returns 409 and Kavita can only be changed by an admin
in Settings.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.auth import MeResponse, set_session_cookie, throttled_login
from app.api.deps import Ctx, Db, Settings, client_ip, require_json
from app.providers.auth.kavita import KavitaAuthProvider
from app.services import audit
from app.services.settings import SettingsService, SettingValidationError, serialise
from app.services.settings_registry import REGISTRY

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/setup", tags=["setup"])


class SetupStatus(BaseModel):
    completed: bool
    kavita_url: str
    admin_role: str


class KavitaTestBody(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class TestResponse(BaseModel):
    ok: bool
    message: str


class CompleteBody(BaseModel):
    kavita_url: str = Field(min_length=1, max_length=2048)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


async def _require_not_completed(settings: SettingsService) -> None:
    if await settings.get("setup.completed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup has already been completed.")


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
        # Pre-filled from a legacy env import, if any. Hidden after setup.
        kavita_url="" if completed else await settings.get("kavita.url"),
        admin_role=await settings.get("auth.admin_role"),
    )


@router.post("/kavita/test", response_model=TestResponse, dependencies=[Depends(require_json)])
async def test_kavita(body: KavitaTestBody, ctx: Ctx, settings: Settings) -> TestResponse:
    await _require_not_completed(settings)
    result = await KavitaAuthProvider(ctx.http, _clean_url(body.url)).test_connection()
    return TestResponse(ok=result.ok, message=result.message)


@router.post("/complete", response_model=MeResponse, dependencies=[Depends(require_json)])
async def complete_setup(
    body: CompleteBody, request: Request, response: Response, ctx: Ctx, db: Db, settings: Settings
) -> MeResponse:
    await _require_not_completed(settings)
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
    await audit.record(
        db,
        "setup.completed",
        actor=issued.user,
        ip=client_ip(request),
        details={"kavita_url": url},
    )
    await db.commit()
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
