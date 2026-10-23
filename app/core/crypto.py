"""Secret-at-rest encryption.

A random master key is generated on first boot and written to
/config/secret.key (mode 0600). Purpose-specific keys are derived from it
with HKDF so the raw master key is never used directly.

Losing secret.key means stored API keys can no longer be decrypted (they
would need to be re-entered in Settings); the rest of the database is fine.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)

_MASTER_KEY_BYTES = 32


class SecretDecryptionError(RuntimeError):
    """Raised when a stored secret cannot be decrypted with the current key."""


def load_or_create_master_key(path: Path) -> bytes:
    """Return the master key, generating and persisting it on first boot."""
    if path.exists():
        raw = path.read_text(encoding="ascii").strip()
        key = base64.urlsafe_b64decode(raw)
        if len(key) != _MASTER_KEY_BYTES:
            raise RuntimeError(f"{path} is corrupt (expected {_MASTER_KEY_BYTES} bytes)")
        return key

    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(_MASTER_KEY_BYTES)
    encoded = base64.urlsafe_b64encode(key).decode("ascii")
    # Create with 0600 atomically rather than chmod-after-write.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(encoded + "\n")
    log.info("Generated new secret key", extra={"path": str(path)})
    return key


def _derive(master: bytes, purpose: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(master)


class SecretBox:
    """Encrypts/decrypts settings secrets with a key derived from the master key."""

    def __init__(self, master_key: bytes) -> None:
        fernet_key = base64.urlsafe_b64encode(_derive(master_key, b"wishpanel/settings/v1"))
        self._fernet = Fernet(fernet_key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise SecretDecryptionError("stored secret could not be decrypted") from exc


def mask_secret(value: str | None) -> str:
    """Display form for a secret: '••••••••abcd' (last 4 only), or '' if unset."""
    if not value:
        return ""
    tail = value[-4:] if len(value) > 8 else ""
    return "••••••••" + tail
