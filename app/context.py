"""Process-wide objects created at startup and shared by request handlers."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import BootstrapConfig
from app.core.crypto import SecretBox
from app.core.security import LoginThrottle
from app.services.health import HealthService


@dataclass
class AppContext:
    config: BootstrapConfig
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    box: SecretBox
    http: httpx.AsyncClient
    throttle: LoginThrottle
    health: HealthService
