"""A small persistent scheduler.

SPEC §10 suggests APScheduler with a SQLite job store. Our jobs are a handful of
"every N hours" tasks whose schedules live in Settings (so they change without a
restart), so a 30-second asyncio tick against the ``jobs`` table does the same
with less machinery: a job is due when it's enabled and its last start is older
than its interval. Last start / finish / success / error survive restarts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.db.base import utcnow
from app.db.models import Job
from app.providers.base import ProviderError
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

TICK_SECONDS = 30
STARTUP_DELAY = timedelta(seconds=45)


@dataclass(frozen=True)
class JobDef:
    id: str
    label: str
    description: str
    run: Callable[[Any], Awaitable[str]]


class Scheduler:
    def __init__(self, ctx: Any, jobs: list[JobDef]) -> None:
        self.ctx = ctx
        self.jobs = {j.id: j for j in jobs}
        self.running: dict[str, datetime] = {}
        self._task: asyncio.Task[None] | None = None
        self._started = utcnow()

    def start(self) -> None:
        if self._task is None:
            self._started = utcnow()
            self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def _schedule(self, settings: SettingsService, job_id: str) -> tuple[bool, int]:
        return await settings.get(f"jobs.{job_id}.enabled"), await settings.get(
            f"jobs.{job_id}.hours"
        )

    async def tick(self) -> None:
        if utcnow() - self._started < STARTUP_DELAY:
            return
        async with self.ctx.sessionmaker() as db:
            settings = SettingsService(db, self.ctx.box)
            if not await settings.get("setup.completed"):
                return
            due = []
            for job_id in self.jobs:
                enabled, hours = await self._schedule(settings, job_id)
                if not enabled or job_id in self.running:
                    continue
                row = await db.get(Job, job_id)
                last = row.last_started_at if row else None
                if last is None or utcnow() - last >= timedelta(hours=hours):
                    due.append(job_id)
        for job_id in due:
            await self.run(job_id)

    def trigger(self, job_id: str) -> bool:
        """Start a job now in the background. False if it's already running."""
        if job_id not in self.jobs:
            raise KeyError(job_id)
        if job_id in self.running:
            return False
        self.running[job_id] = utcnow()
        self.ctx.runner.spawn(self.run(job_id, claimed=True), name=f"job-{job_id}")
        return True

    async def run(self, job_id: str, *, claimed: bool = False) -> None:
        if not claimed:
            if job_id in self.running:
                return
            self.running[job_id] = utcnow()
        started = time.perf_counter()
        status, message, error = "ok", "", None
        try:
            async with self.ctx.sessionmaker() as db:
                row = await db.get(Job, job_id) or Job(id=job_id)
                row.last_started_at = utcnow()
                db.add(row)
                await db.commit()
            log.info("Job started", extra={"job": job_id})
            message = await self.jobs[job_id].run(self.ctx) or ""
        except ProviderError as exc:
            status, error = "error", exc.message
        except Exception as exc:
            log.exception("Job crashed", extra={"job": job_id})
            status, error = "error", f"{type(exc).__name__}: {exc}"[:500]
        finally:
            self.running.pop(job_id, None)
        ms = int((time.perf_counter() - started) * 1000)
        async with self.ctx.sessionmaker() as db:
            row = await db.get(Job, job_id) or Job(id=job_id)
            row.last_finished_at = utcnow()
            row.last_status = status
            row.last_duration_ms = ms
            row.last_message = message[:500] or None
            if status == "ok":
                row.last_success_at = row.last_finished_at
                row.last_error = None
            else:
                row.last_error = (error or "")[:500]
            db.add(row)
            await db.commit()
        log.info(
            "Job finished",
            extra={"job": job_id, "status": status, "ms": ms, "result": message or error},
        )

    async def describe(self) -> list[dict[str, Any]]:
        out = []
        async with self.ctx.sessionmaker() as db:
            settings = SettingsService(db, self.ctx.box)
            for job in self.jobs.values():
                enabled, hours = await self._schedule(settings, job.id)
                row = await db.get(Job, job.id)
                last = row.last_started_at if row else None
                nxt = None
                if enabled:
                    nxt = (last + timedelta(hours=hours)) if last else self._started + STARTUP_DELAY
                    nxt = max(nxt, utcnow())
                out.append(
                    {
                        "id": job.id,
                        "label": job.label,
                        "description": job.description,
                        "enabled": enabled,
                        "hours": hours,
                        "running": job.id in self.running,
                        "last_started_at": last,
                        "last_finished_at": row.last_finished_at if row else None,
                        "last_success_at": row.last_success_at if row else None,
                        "last_status": row.last_status if row else None,
                        "last_message": row.last_message if row else None,
                        "last_error": row.last_error if row else None,
                        "last_duration_ms": row.last_duration_ms if row else None,
                        "next_run_at": nxt,
                    }
                )
        return out
