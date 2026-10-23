from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, User


async def record(
    db: AsyncSession,
    action: str,
    *,
    actor: User | None = None,
    target: str | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Append an audit entry. Caller commits. Never pass secret values in details."""
    db.add(
        AuditLog(
            action=action,
            actor_user_id=actor.id if actor else None,
            actor_name=actor.username if actor else None,
            target=target,
            details=json.dumps(details, default=str) if details else None,
            ip=ip,
        )
    )
