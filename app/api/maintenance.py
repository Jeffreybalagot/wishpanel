"""Admin: jobs, discover status, logs & about, backups, settings export/import, v1 import, audit."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update

from app import __version__
from app.api.deps import AdminUser, Ctx, Db, Settings, client_ip
from app.db.models import AuditLog, MatchCache, User
from app.db.models import Request as Req
from app.services import audit, discover, importer, maintenance
from app.services.settings import SettingValidationError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])
STARTED = time.time()


# ---------------------------------------------------------------- jobs


@router.get("/jobs")
async def jobs(_: AdminUser, ctx: Ctx) -> dict[str, Any]:
    m = ctx.matcher
    return {
        "jobs": await ctx.scheduler.describe(),
        "background": ctx.config.background_jobs,
        "matcher": {
            "pending": await m.pending_count(),
            "current": m.current,
            "last_match_at": m.last_match_at,
            "last_error": m.last_error,
        },
    }


@router.post("/jobs/{job_id}/run")
async def run_job(
    job_id: str, request: Request, user: AdminUser, ctx: Ctx, db: Db
) -> dict[str, Any]:
    try:
        started = ctx.scheduler.trigger(job_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown job") from None
    if not started:
        raise HTTPException(status.HTTP_409_CONFLICT, "That job is already running.")
    await audit.record(db, "job.run", actor=user, ip=client_ip(request), target=job_id)
    await db.commit()
    return {"ok": True, "message": "Started."}


# ---------------------------------------------------------------- discover


class RefreshBody(BaseModel):
    media: Literal["comic", "manga", "all"] = "all"


@router.get("/discover/status")
async def discover_status(_: AdminUser, db: Db) -> dict[str, Any]:
    return await discover.status(db)


@router.post("/discover/refresh")
async def discover_refresh(
    body: RefreshBody, request: Request, user: AdminUser, ctx: Ctx, db: Db
) -> dict[str, Any]:
    started = []
    if body.media in ("comic", "all") and ctx.scheduler.trigger("discover_comic"):
        started.append("comics")
    if body.media in ("manga", "all") and ctx.scheduler.trigger("discover_manga"):
        started.append("manga & manhwa")
    await audit.record(
        db, "discover.refresh", actor=user, ip=client_ip(request), details={"media": body.media}
    )
    await db.commit()
    if not started:
        return {"ok": True, "message": "A refresh is already running."}
    return {
        "ok": True,
        "message": f"Refreshing {' and '.join(started)} in the background — see Jobs.",
    }


@router.post("/discover/rematch")
async def discover_rematch(request: Request, user: AdminUser, ctx: Ctx, db: Db) -> dict[str, Any]:
    result = await db.execute(
        update(MatchCache).where(MatchCache.status == "miss").values(status="pending", tries=0)
    )
    await audit.record(db, "discover.rematch", actor=user, ip=client_ip(request))
    await db.commit()
    ctx.matcher.wake()
    return {"ok": True, "message": f"{result.rowcount or 0} unmatched title(s) queued again."}


# ---------------------------------------------------------------- logs & about


@router.get("/logs")
async def logs(
    _: AdminUser,
    ctx: Ctx,
    level: Literal["debug", "info", "warning", "error"] = "info",
    q: str = Query(default="", max_length=100),
    lines: int = Query(default=300, ge=10, le=2000),
) -> dict[str, Any]:
    return {"entries": maintenance.tail_logs(ctx.config.log_dir, lines=lines, level=level, q=q)}


@router.get("/about")
async def about(_: AdminUser, ctx: Ctx, db: Db) -> dict[str, Any]:
    db_path = ctx.config.db_path
    req_counts = {
        s: n
        for s, n in (await db.execute(select(Req.status, func.count()).group_by(Req.status))).all()
    }
    return {
        "version": __version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "uptime_s": int(time.time() - STARTED),
        "config_dir": str(ctx.config.config_dir),
        "db_size": db_path.stat().st_size if db_path.exists() else 0,
        "users": await db.scalar(select(func.count()).select_from(User)),
        "requests": req_counts,
        "repo": f"https://github.com/{maintenance.REPO}",
    }


@router.get("/update-check")
async def update_check(_: AdminUser, ctx: Ctx) -> dict[str, Any]:
    return await maintenance.check_update(ctx.http)


@router.get("/backup")
async def backup(
    request: Request, user: AdminUser, ctx: Ctx, db: Db, tasks: BackgroundTasks
) -> FileResponse:
    tmp = Path(tempfile.mkdtemp(prefix="wp-backup-"))
    dest = tmp / f"wishpanel-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    maintenance.backup_database(ctx.config.db_path, dest)
    await audit.record(db, "backup.download", actor=user, ip=client_ip(request))
    await db.commit()

    def cleanup() -> None:
        dest.unlink(missing_ok=True)
        tmp.rmdir()

    tasks.add_task(cleanup)
    return FileResponse(dest, media_type="application/vnd.sqlite3", filename=dest.name)


@router.get("/settings/export")
async def export_settings(
    request: Request, user: AdminUser, db: Db, settings: Settings
) -> JSONResponse:
    data = await maintenance.export_settings(settings)
    await audit.record(db, "settings.export", actor=user, ip=client_ip(request))
    await db.commit()
    name = f"wishpanel-settings-{datetime.now().strftime('%Y%m%d')}.json"
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/settings/import")
async def import_settings(
    request: Request, user: AdminUser, db: Db, settings: Settings
) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > 1024 * 1024:
        raise HTTPException(413, "That file is too large to be a settings export.")
    try:
        payload = json.loads(raw)
        changed, ignored = await maintenance.import_settings(settings, payload)
    except ValueError as exc:
        if isinstance(exc, SettingValidationError):
            await db.rollback()
            raise HTTPException(422, {"key": exc.key, "message": exc.message}) from exc
        raise HTTPException(422, {"key": "file", "message": "That file isn't valid JSON."}) from exc
    await audit.record(
        db,
        "settings.import",
        actor=user,
        ip=client_ip(request),
        details={"changed": changed, "ignored": ignored},
    )
    await db.commit()
    return {
        "ok": True,
        "changed": changed,
        "ignored": ignored,
        "message": f"Imported — {len(changed)} setting(s) changed. "
        "Exports never contain API keys or passwords.",
    }


@router.get("/audit")
async def audit_log(
    _: AdminUser,
    db: Db,
    limit: int = Query(default=200, ge=1, le=1000),
    action: str = Query(default="", max_length=64),
) -> dict[str, Any]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    rows = (await db.scalars(stmt)).all()
    return {
        "entries": [
            {
                "id": r.id,
                "ts": r.ts,
                "actor": r.actor_name,
                "action": r.action,
                "target": r.target,
                "details": json.loads(r.details) if r.details else None,
                "ip": r.ip,
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------- v1 import


def _mounted(ctx: Any) -> Path:
    return ctx.config.import_dir / "requests.db"


@router.get("/import/v1")
async def import_status(_: AdminUser, ctx: Ctx, settings: Settings, db: Db) -> dict[str, Any]:
    last = await settings.get("import.v1.last_result")
    imported = await db.scalar(
        select(func.count()).select_from(Req).where(Req.imported_from == "v1")
    )
    return {
        "mounted": _mounted(ctx).is_file(),
        "mounted_path": str(_mounted(ctx)),
        "last_result": json.loads(last) if last else None,
        "imported_requests": imported,
    }


async def _do_import(
    path: Path, request: Request, user: User, ctx: Any, db: Any, settings: Any, source: str
) -> dict[str, Any]:
    try:
        data = await asyncio.to_thread(importer.read_v1, path)
    except importer.ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    result = await importer.run_import(db, data)
    out = importer.result_json(result)
    out["source"] = source
    out["at"] = datetime.now().isoformat(timespec="seconds")
    await settings.set("import.v1.last_result", json.dumps(out, default=str)[:4000])
    await audit.record(
        db,
        "import.v1",
        actor=user,
        ip=client_ip(request),
        details={"source": source, "summary": result.summary()},
    )
    await db.commit()
    log.info("Imported v1 database", extra={"summary": result.summary()})
    ctx.matcher.wake()
    return out


@router.post("/import/v1/mounted")
async def import_mounted(
    request: Request, user: AdminUser, ctx: Ctx, db: Db, settings: Settings
) -> dict[str, Any]:
    path = _mounted(ctx)
    if not path.is_file():
        raise HTTPException(
            404,
            f"No database at {path}. Mount v1's data folder at "
            f"{ctx.config.import_dir} (read-only).",
        )
    return await _do_import(path, request, user, ctx, db, settings, str(path))


@router.put("/import/v1/upload")
async def import_upload(
    request: Request, user: AdminUser, ctx: Ctx, db: Db, settings: Settings
) -> dict[str, Any]:
    tmp = Path(tempfile.mkdtemp(prefix="wp-import-"))
    path = tmp / "requests.db"
    size = 0
    try:
        with path.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > importer.MAX_UPLOAD:
                    raise HTTPException(413, "That file is too large.")
                fh.write(chunk)
        if size < 100:
            raise HTTPException(422, "The file is empty.")
        return await _do_import(path, request, user, ctx, db, settings, "upload")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
