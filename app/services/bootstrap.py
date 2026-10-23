"""First-boot work: secret key, migrations, default settings, legacy env import.

Every step is idempotent, so this runs on every start.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import BootstrapConfig
from app.core.crypto import SecretBox, load_or_create_master_key
from app.db.session import run_migrations
from app.services import audit
from app.services.settings import SettingsService, SettingValidationError
from app.services.settings_registry import REGISTRY

log = logging.getLogger(__name__)


def prepare_filesystem(cfg: BootstrapConfig) -> SecretBox:
    """Synchronous part: create /config layout, load key, migrate schema."""
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    probe = cfg.config_dir / ".write-test"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"{cfg.config_dir} is not writable by uid {os.getuid()}. "
            "Check the volume mapping and PUID/PGID."
        ) from exc

    box = SecretBox(load_or_create_master_key(cfg.secret_key_path))
    run_migrations(cfg.db_url_sync)
    return box


async def seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    box: SecretBox,
    env: Mapping[str, str] | None = None,
) -> None:
    """Async part: default settings + one-time import of v1 environment variables."""
    env = os.environ if env is None else env
    async with sessionmaker() as db:
        settings = SettingsService(db, box)
        created = await settings.ensure_defaults()
        if created:
            log.info("Created default settings", extra={"count": created})

        if not await settings.get("bootstrap.env_imported"):
            imported = await _import_legacy_env(settings, env)
            await settings.set("bootstrap.env_imported", True)
            if imported:
                await audit.record(db, "settings.env_import", details={"keys": imported})
                log.warning(
                    "Imported legacy environment variables into the database. They are now "
                    "ignored — manage these values in Admin → Settings and remove them from "
                    "your container template.",
                    extra={"keys": ",".join(imported)},
                )
        await db.commit()


async def _import_legacy_env(settings: SettingsService, env: Mapping[str, str]) -> list[str]:
    imported: list[str] = []
    for defn in REGISTRY.values():
        for var in defn.legacy_env:
            value = env.get(var, "").strip()
            if not value:
                continue
            try:
                await settings.set(defn.key, value)
            except SettingValidationError as exc:
                log.warning("Skipped legacy env var", extra={"var": var, "reason": exc.message})
            else:
                imported.append(defn.key)  # log the setting key, never the value
            break
    return imported
