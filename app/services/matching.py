"""Background worker that resolves list titles (Comic Book Herald) to ComicVine volumes.

One lookup every ``discover.match_interval_seconds`` (default 18 s) so a big
refresh never trips ComicVine's limits. Uses ComicVine directly when a key is
set, otherwise Kapowarr's search. Scoring follows v1: title similarity, English
publishers preferred, translations/foreign reprints pushed down, issue count and
year hint as tie-breakers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.textmatch import comic_score
from app.db.base import utcnow
from app.db.models import MatchCache
from app.domain import SearchResult
from app.providers.base import ProviderError
from app.services import catalog
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

MAX_TRIES = 3
MIN_SIM = 0.8
GOOD_SIM = 0.9


def queries_for(title: str) -> list[str]:
    out = [title]
    if ":" in title:
        head = title.split(":", 1)[0].strip()
        if len(head) >= 3:
            out.append(head)
    return out


def pick(
    title: str, hint: int | None, results: list[SearchResult], preferred: list[str]
) -> tuple[SearchResult, float] | None:
    best: tuple[float, SearchResult] | None = None
    scored = []
    for r in results:
        sim, score = comic_score(
            title=title,
            name=r.title,
            publisher=r.publisher,
            issues=r.issue_count,
            year=str(r.year or ""),
            hint=hint,
            has_cover=bool(r.cover_url),
            translated=r.translated,
            preferred=preferred,
        )
        scored.append((sim, score, r))
    top = sorted(scored, key=lambda x: -x[0])[:8]
    for sim, score, r in top:
        if sim >= MIN_SIM and (best is None or score > best[0]):
            best = (score, r)
    return (best[1], best[0]) if best else None


class MatchWorker:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_match_at: datetime | None = None
        self.last_error: str | None = None
        self.paused_until: datetime | None = None
        self.current: str | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="match-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                interval = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Match worker error")
                self.last_error = str(exc)[:200]
                interval = 300
            await self._sleep(interval)

    async def step(self) -> float:
        """Match one title. Returns seconds to wait before the next step."""
        async with self.ctx.sessionmaker() as db:
            settings = SettingsService(db, self.ctx.box)
            interval = float(await settings.get("discover.match_interval_seconds"))
            row = await db.scalar(
                select(MatchCache)
                .where(MatchCache.status == "pending")
                .order_by(MatchCache.priority, MatchCache.updated_at)
                .limit(1)
            )
            if row is None:
                self.current = None
                return 600
            try:
                provider = await catalog.comic_provider(settings, self.ctx.http)
            except ProviderError as exc:
                self.last_error = exc.message
                return 600
            self.current = row.title
            preferred = await settings.get("comicvine.preferred_publishers")
            blocked = [b.lower() for b in await settings.get("comicvine.blocked_publishers")]
            found: tuple[SearchResult, float] | None = None
            try:
                for i, q in enumerate(queries_for(row.title)):
                    if i:
                        await asyncio.sleep(interval)
                    results = [
                        r
                        for r in await provider.search(q)
                        if not any(b in (r.publisher or "").lower() for b in blocked)
                    ]
                    cand = pick(row.title, row.hint_year, results, preferred)
                    if cand and (found is None or cand[1] > found[1]):
                        found = cand
                    if found and found[1] >= GOOD_SIM * 100:
                        break
            except ProviderError as exc:
                row.tries += 1
                row.updated_at = utcnow()
                if row.tries >= MAX_TRIES:
                    row.status = "miss"
                await db.commit()
                self.last_error = exc.message
                return 900 if exc.retryable else interval
            row.tries += 1
            row.updated_at = utcnow()
            if found:
                r, score = found
                row.status = "ok"
                row.comicvine_id = int(r.external_id)
                row.name = r.title
                row.year = str(r.year) if r.year else None
                row.publisher = r.publisher
                row.issues = r.issue_count
                row.cover_url = r.cover_url
                row.score = round(score, 2)
            else:
                row.status = "miss"
            await db.commit()
            self.last_match_at = utcnow()
            self.last_error = None
            return interval

    async def pending_count(self) -> int:
        async with self.ctx.sessionmaker() as db:
            return len(
                (
                    await db.scalars(select(MatchCache.key).where(MatchCache.status == "pending"))
                ).all()
            )
