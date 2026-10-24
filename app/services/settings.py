"""Database-backed settings with transparent secret encryption.

Reads go straight to SQLite (cheap, and means a change made in the UI applies
on the very next request — no restart, no cache invalidation bugs).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import SecretBox, SecretDecryptionError, mask_secret
from app.db.models import Setting
from app.services.settings_registry import REGISTRY, SettingDef, SettingType

log = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}
LIST_MAX_ITEMS = 500
LIST_ITEM_MAX_LEN = 200
_FALSE = {"0", "false", "no", "off", ""}


class SettingValidationError(ValueError):
    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key
        self.message = message


def _definition(key: str) -> SettingDef:
    try:
        return REGISTRY[key]
    except KeyError:
        raise SettingValidationError(key, "unknown setting") from None


def serialise(defn: SettingDef, value: Any) -> str:
    """Validate a user-supplied value and convert it to its stored string form."""
    key = defn.key
    if value is None:
        value = ""
    match defn.type:
        case SettingType.BOOL:
            if isinstance(value, bool):
                return "true" if value else "false"
            text = str(value).strip().lower()
            if text in _TRUE:
                return "true"
            if text in _FALSE:
                return "false"
            raise SettingValidationError(key, "must be true or false")
        case SettingType.INT:
            try:
                num = int(value)
            except (TypeError, ValueError):
                raise SettingValidationError(key, "must be a whole number") from None
            if defn.min_value is not None and num < defn.min_value:
                raise SettingValidationError(key, f"must be at least {defn.min_value}")
            return str(num)
        case SettingType.CHOICE:
            text = str(value).strip()
            if text not in defn.choices:
                raise SettingValidationError(key, f"must be one of: {', '.join(defn.choices)}")
            return text
        case SettingType.LIST:
            if isinstance(value, str):
                value = [line for line in value.replace(",", "\n").splitlines()]
            if not isinstance(value, list | tuple):
                raise SettingValidationError(key, "must be a list")
            items: list[str] = []
            for item in value:
                text = str(item).strip()
                if not text:
                    continue
                if len(text) > LIST_ITEM_MAX_LEN:
                    raise SettingValidationError(key, "an entry is too long")
                if text not in items:  # keep first occurrence: order = priority
                    items.append(text)
            if len(items) > LIST_MAX_ITEMS:
                raise SettingValidationError(key, f"at most {LIST_MAX_ITEMS} entries")
            return json.dumps(items, ensure_ascii=False)
        case SettingType.URL:
            text = str(value).strip().rstrip("/")
            if text:
                parsed = urlparse(text)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise SettingValidationError(
                        key, "must be a full http(s) URL, e.g. http://10.0.0.5:5000"
                    )
            return text
        case _:
            return str(value).strip()


def deserialise(defn: SettingDef, raw: str | None) -> Any:
    if raw is None:
        raw = "" if defn.default is None else serialise(defn, defn.default)
    match defn.type:
        case SettingType.BOOL:
            return raw.lower() in _TRUE
        case SettingType.INT:
            try:
                return int(raw)
            except ValueError:
                return defn.default
        case SettingType.LIST:
            try:
                parsed = json.loads(raw) if raw else []
            except ValueError:
                return list(defn.default or ())
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        case _:
            return raw


class SettingsService:
    def __init__(self, db: AsyncSession, box: SecretBox) -> None:
        self.db = db
        self.box = box

    async def _row(self, key: str) -> Setting | None:
        return await self.db.get(Setting, key)

    async def get(self, key: str) -> Any:
        defn = _definition(key)
        row = await self._row(key)
        if row is None or row.value is None:
            return deserialise(defn, None)
        raw = row.value
        if row.is_secret and raw:
            try:
                raw = self.box.decrypt(raw)
            except SecretDecryptionError:
                log.error("Could not decrypt setting; re-enter it in Settings", extra={"key": key})
                return ""
        return deserialise(defn, raw)

    async def get_many(self, keys: Iterable[str]) -> dict[str, Any]:
        return {k: await self.get(k) for k in keys}

    async def set(self, key: str, value: Any) -> bool:
        """Store a value. Returns True if it actually changed. Caller commits."""
        defn = _definition(key)
        plain = serialise(defn, value)
        row = await self._row(key)
        current = await self.get(key) if row is not None else None
        if row is not None and serialise(defn, current) == plain:
            return False
        stored = self.box.encrypt(plain) if (defn.secret and plain) else plain
        if row is None:
            self.db.add(Setting(key=key, value=stored, is_secret=defn.secret))
        else:
            row.value = stored
            row.is_secret = defn.secret
        return True

    async def is_set(self, key: str) -> bool:
        return bool(await self.get(key))

    async def masked(self, key: str) -> str:
        """Value safe to return to the browser: secrets become '••••••••abcd'."""
        defn = _definition(key)
        value = await self.get(key)
        return mask_secret(value) if defn.secret else value

    async def ensure_defaults(self) -> int:
        """Create any missing setting rows with their defaults. Returns rows created."""
        existing = set((await self.db.scalars(select(Setting.key))).all())
        created = 0
        for defn in REGISTRY.values():
            if defn.key in existing:
                continue
            plain = "" if defn.default is None else serialise(defn, defn.default)
            stored = self.box.encrypt(plain) if (defn.secret and plain) else plain
            self.db.add(Setting(key=defn.key, value=stored, is_secret=defn.secret))
            created += 1
        return created
