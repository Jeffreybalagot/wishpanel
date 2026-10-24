"""/api/admin/settings, connection tests, live options, Kavita scan, logo. Admin only."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, Ctx, Db, Settings, client_ip
from app.core.crypto import mask_secret
from app.domain import MediaType
from app.providers.auth.kavita import KavitaAuthProvider
from app.providers.base import ProviderError
from app.services import audit, branding, connections
from app.services.settings import SettingsService, SettingValidationError, serialise
from app.services.settings_pages import EDITABLE_KEYS, schema_json
from app.services.settings_registry import REGISTRY

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])
public_router = APIRouter(tags=["branding"])


class SettingsUpdate(BaseModel):
    # key -> new value. For secrets: null/absent = keep, "" = clear, text = replace.
    values: dict[str, Any] = Field(default_factory=dict)


class Overrides(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class ScanBody(BaseModel):
    media_type: MediaType | None = None


async def _value_for_client(settings: SettingsService, key: str) -> Any:
    defn = REGISTRY[key]
    value = await settings.get(key)
    if defn.secret:
        return {"set": bool(value), "masked": mask_secret(value)}
    return value


async def _all_values(settings: SettingsService) -> dict[str, Any]:
    return {key: await _value_for_client(settings, key) for key in sorted(EDITABLE_KEYS)}


def _bad_request(key: str, message: str) -> HTTPException:
    return HTTPException(422, {"key": key, "message": message})


# ---------------------------------------------------------------- settings


@router.get("/settings/schema")
async def get_schema(_: AdminUser) -> dict[str, Any]:
    return {"pages": schema_json()}


@router.get("/settings")
async def get_settings(_: AdminUser, settings: Settings) -> dict[str, Any]:
    return {"values": await _all_values(settings)}


@router.put("/settings")
async def put_settings(
    body: SettingsUpdate,
    request: Request,
    user: AdminUser,
    ctx: Ctx,
    db: Db,
    settings: Settings,
) -> dict[str, Any]:
    # 1. Validate everything before writing anything (all-or-nothing).
    pending: dict[str, Any] = {}
    for key, value in body.values.items():
        if key not in EDITABLE_KEYS:
            raise _bad_request(key, "This setting can't be changed here.")
        defn = REGISTRY[key]
        if defn.secret and value is None:
            continue  # unchanged secret
        try:
            serialise(defn, value)
        except SettingValidationError as exc:
            raise _bad_request(key, exc.message) from exc
        pending[key] = value

    # 2. Kavita is the login provider: never save a URL that doesn't answer as Kavita.
    if "kavita.url" in pending:
        new_url = serialise(REGISTRY["kavita.url"], pending["kavita.url"])
        if new_url != await settings.get("kavita.url"):
            if not new_url:
                raise _bad_request("kavita.url", "Kavita URL is required — it's used for sign-in.")
            check = await KavitaAuthProvider(ctx.http, new_url).test_connection()
            if not check.ok:
                raise _bad_request("kavita.url", f"Not saved: {check.message}")

    # 3. Write, audit (key names only — never values of secrets).
    changed = [key for key, value in pending.items() if await settings.set(key, value)]
    if changed:
        await audit.record(
            db,
            "settings.update",
            actor=user,
            ip=client_ip(request),
            details={"changed": sorted(changed)},
        )
        await db.commit()
        log.info("Settings updated", extra={"user": user.username, "keys": ",".join(changed)})
    return {"values": await _all_values(settings), "changed": sorted(changed)}


# ---------------------------------------------------------------- connections


@router.post("/connections/{service}/test")
async def test_connection(
    service: str, body: Overrides, _: AdminUser, ctx: Ctx, settings: Settings
) -> dict[str, Any]:
    try:
        provider = await connections.provider_for(settings, service, ctx.http, body.values)
    except connections.UnknownService:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown service") from None
    except SettingValidationError as exc:
        raise _bad_request(exc.key, exc.message) from exc
    result = await provider.test_connection()
    return {"ok": result.ok, "message": result.message, "details": result.details}


@router.post("/options/{option_id}")
async def options(
    option_id: str, body: Overrides, _: AdminUser, ctx: Ctx, settings: Settings
) -> dict[str, Any]:
    try:
        opts = await connections.load_options(settings, option_id, ctx.http, body.values)
    except connections.UnknownService:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown option list") from None
    except SettingValidationError as exc:
        raise _bad_request(exc.key, exc.message) from exc
    except ProviderError as exc:
        return {"ok": False, "message": exc.message, "options": []}
    return {
        "ok": True,
        "message": "",
        "options": [
            {"value": o.value, "label": o.label, "hint": o.hint, "group": o.group} for o in opts
        ],
    }


@router.post("/kavita/scan")
async def kavita_scan(
    body: ScanBody, request: Request, user: AdminUser, ctx: Ctx, db: Db, settings: Settings
) -> dict[str, Any]:
    media = [body.media_type] if body.media_type else list(MediaType)
    library_ids: list[str] = []
    for mt in media:
        lib = await settings.get(f"kavita.library.{mt.value}")
        if lib and lib not in library_ids:
            library_ids.append(lib)
    if not library_ids:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "No Kavita library is mapped yet — choose one and save first."
        )
    provider = await connections.provider_for(settings, "kavita", ctx.http)
    try:
        for lib in library_ids:
            await provider.trigger_scan(lib)
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.message) from exc
    await audit.record(
        db, "kavita.scan", actor=user, ip=client_ip(request), details={"libraries": library_ids}
    )
    await db.commit()
    n = len(library_ids)
    return {"ok": True, "message": f"Kavita is scanning {n} librar{'y' if n == 1 else 'ies'}."}


# ---------------------------------------------------------------- branding


@router.put("/branding/logo")
async def upload_logo(
    request: Request, user: AdminUser, ctx: Ctx, db: Db, settings: Settings
) -> dict[str, Any]:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > branding.MAX_BYTES:
        raise HTTPException(413, f"The logo must be under {branding.MAX_BYTES // 1024} KB.")
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > branding.MAX_BYTES:
            raise HTTPException(413, f"The logo must be under {branding.MAX_BYTES // 1024} KB.")
    try:
        ext = branding.save_logo(ctx.config.config_dir, data)
    except branding.LogoError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    version = await settings.get("general.logo_version") + 1
    await settings.set("general.logo_version", version)
    await audit.record(db, "branding.logo", actor=user, ip=client_ip(request), details={"ext": ext})
    await db.commit()
    return {"ok": True, "logo_version": version}


@router.delete("/branding/logo")
async def delete_logo(
    request: Request, user: AdminUser, ctx: Ctx, db: Db, settings: Settings
) -> dict[str, Any]:
    if branding.remove_logo(ctx.config.config_dir):
        await settings.set("general.logo_version", await settings.get("general.logo_version") + 1)
        await audit.record(db, "branding.logo_removed", actor=user, ip=client_ip(request))
        await db.commit()
    return {"ok": True}


@public_router.get("/api/branding/logo", include_in_schema=False)
async def get_logo(ctx: Ctx) -> FileResponse:
    path = branding.current_logo(ctx.config.config_dir)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No custom logo")
    return FileResponse(
        path,
        media_type=branding.MEDIA_TYPES[path.suffix.lstrip(".")],
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )
