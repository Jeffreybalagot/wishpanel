"""Tracks fire-and-forget background tasks so they're logged and cancelled on shutdown."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)


class TaskRunner:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str = "task") -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("Background task failed", exc_info=exc, extra={"task": task.get_name()})

    async def drain(self, wait_s: float = 30) -> None:
        """Wait for running tasks (tests, graceful shutdown)."""
        while self._tasks:
            await asyncio.wait(list(self._tasks), timeout=wait_s)

    async def cancel_all(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
