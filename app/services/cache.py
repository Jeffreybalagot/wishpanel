"""Small JSON cache in SQLite for metadata lookups (spares external rate limits)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import MetadataCache


async def get(db: AsyncSession, key: str) -> Any | None:
    row = await db.get(MetadataCache, key)
    if row is None or row.expires_at <= utcnow():
        return None
    try:
        return json.loads(row.payload)
    except ValueError:
        return None


async def put(db: AsyncSession, key: str, value: Any, ttl: timedelta) -> None:
    payload = json.dumps(value, default=str)
    row = await db.get(MetadataCache, key)
    if row is None:
        db.add(MetadataCache(key=key[:300], payload=payload, expires_at=utcnow() + ttl))
    else:
        row.payload = payload
        row.expires_at = utcnow() + ttl


async def purge_expired(db: AsyncSession) -> int:
    result = await db.execute(delete(MetadataCache).where(MetadataCache.expires_at <= utcnow()))
    return result.rowcount or 0
