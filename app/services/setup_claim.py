"""One-time setup claim code.

Until first-run setup completes, WishPanel has no admin, so anyone who can
reach the port could otherwise run the wizard. To prove they control the
container, the person setting it up must enter a code that is only visible
to whoever can see the container's log or its /config folder.

Lifecycle:
  * On every boot while unclaimed, the code is (re)loaded from
    /config/setup-code.txt, or freshly generated if that file is missing or
    doesn't match the stored hash. It is printed to the container log.
  * Only a SHA-256 of the code is stored in the database.
  * On successful setup the hash is cleared and the file is deleted, so the
    code is worthless afterwards.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

from app.services.settings import SettingsService

log = logging.getLogger(__name__)

FILE_NAME = "setup-code.txt"
# Crockford-style alphabet without look-alikes (0/O, 1/I/L, U).
ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
GROUPS, GROUP_LEN = 4, 4  # 16 chars ≈ 78 bits — far beyond online guessing


def generate_code() -> str:
    chars = [secrets.choice(ALPHABET) for _ in range(GROUPS * GROUP_LEN)]
    return "-".join("".join(chars[i : i + GROUP_LEN]) for i in range(0, len(chars), GROUP_LEN))


def normalise(code: str) -> str:
    """Users may type lower case, spaces, or omit the dashes."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def hash_code(code: str) -> str:
    return hashlib.sha256(normalise(code).encode("ascii", "ignore")).hexdigest()


def code_path(config_dir: Path) -> Path:
    return config_dir / FILE_NAME


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(text + "\n")
    os.chmod(path, 0o600)  # O_CREAT mode is ignored if the file already existed


async def ensure_claim_code(settings: SettingsService, config_dir: Path) -> str | None:
    """Return the active claim code (creating one if needed), or None once set up.

    Caller commits.
    """
    path = code_path(config_dir)
    if await settings.get("setup.completed"):
        await settings.set("setup.claim_hash", "")
        path.unlink(missing_ok=True)
        return None

    stored = await settings.get("setup.claim_hash")
    if stored and path.exists():
        existing = path.read_text(encoding="ascii", errors="ignore").strip()
        if existing and hmac.compare_digest(hash_code(existing), stored):
            return existing

    code = generate_code()
    _write_private(path, code)
    await settings.set("setup.claim_hash", hash_code(code))
    return code


async def verify(settings: SettingsService, code: str) -> bool:
    stored = await settings.get("setup.claim_hash")
    if not stored or not normalise(code):
        return False
    return hmac.compare_digest(hash_code(code), stored)


async def consume(settings: SettingsService) -> None:
    """Invalidate the code. Caller commits, then calls remove_file()."""
    await settings.set("setup.claim_hash", "")


def remove_file(config_dir: Path) -> None:
    code_path(config_dir).unlink(missing_ok=True)


def announce(code: str, config_dir: Path) -> None:
    bar = "=" * 66
    log.warning(
        "\n%s\n  WishPanel is not set up yet. To claim it, open the web UI and enter\n"
        "  this setup code:\n\n      %s\n\n"
        "  It is also saved in %s and is deleted\n"
        "  automatically once setup is complete.\n%s",
        bar,
        code,
        code_path(config_dir),
        bar,
    )
