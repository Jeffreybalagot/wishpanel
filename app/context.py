"""Process-wide objects created at startup and shared by request handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import BootstrapConfig
from app.core.crypto import SecretBox
from app.core.ratelimit import SlidingWindow
from app.core.security import LoginThrottle
from app.jobs.runner import TaskRunner
from app.services.health import HealthService

if TYPE_CHECKING:
    from app.jobs.scheduler import Scheduler
    from app.services.matching import MatchWorker


@dataclass
class AppContext:
    config: BootstrapConfig
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    box: SecretBox
    http: httpx.AsyncClient
    throttle: LoginThrottle
    health: HealthService
    runner: TaskRunner = field(default_factory=TaskRunner)
    limits: SlidingWindow = field(default_factory=SlidingWindow)
    scheduler: Scheduler = None  # type: ignore[assignment]  # set in lifespan
    matcher: MatchWorker = None  # type: ignore[assignment]
