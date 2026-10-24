"""Per-key minimum-interval rate limiting for outbound API calls (SPEC §9)."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class MinInterval:
    """Ensures at least ``interval`` seconds between calls sharing a key."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def wait(self, key: str, interval: float) -> None:
        async with self._locks[key]:
            now = time.monotonic()
            delay = self._last.get(key, 0.0) + interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[key] = time.monotonic()


class SlidingWindow:
    """Simple per-key request counter for inbound limits (e.g. searches per user)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_s: float) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


# Process-wide limiter for outbound calls. ComicVine allows ~1 request/second and
# 200 per resource per hour; AniList ~90/minute; MangaBaka and CBH are polite-use.
OUTBOUND = MinInterval()
INTERVALS = {"comicvine": 1.1, "anilist": 0.8, "mangabaka": 0.5, "cbh": 1.0, "kapowarr-cv": 1.1}


async def throttle(service: str) -> None:
    await OUTBOUND.wait(service, INTERVALS.get(service, 0.5 if INTERVALS else 0))
