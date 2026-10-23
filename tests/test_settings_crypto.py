from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.core.crypto import SecretBox, SecretDecryptionError, load_or_create_master_key, mask_secret
from app.core.logging import RedactingFilter, redact
from app.core.security import LoginThrottle
from app.db.session import create_engine, create_sessionmaker, run_migrations
from app.services.settings import SettingsService, SettingValidationError


def test_master_key_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "secret.key"
    k1 = load_or_create_master_key(p)
    assert load_or_create_master_key(p) == k1
    box = SecretBox(k1)
    token = box.encrypt("hunter2")
    assert token != "hunter2"
    assert box.decrypt(token) == "hunter2"


def test_wrong_key_cannot_decrypt(tmp_path: Path) -> None:
    a = SecretBox(load_or_create_master_key(tmp_path / "a.key"))
    b = SecretBox(load_or_create_master_key(tmp_path / "b.key"))
    with pytest.raises(SecretDecryptionError):
        b.decrypt(a.encrypt("x"))


@pytest.mark.parametrize(
    ("value", "masked"),
    [("", ""), (None, ""), ("short", "••••••••"), ("abcdefgh12345678", "••••••••5678")],
)
def test_mask_secret(value, masked) -> None:
    assert mask_secret(value) == masked


@pytest.fixture
async def settings(tmp_path: Path):
    db_path = tmp_path / "t.db"
    run_migrations(f"sqlite:///{db_path}")
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    box = SecretBox(load_or_create_master_key(tmp_path / "secret.key"))
    async with create_sessionmaker(engine)() as db:
        svc = SettingsService(db, box)
        await svc.ensure_defaults()
        await db.commit()
        yield svc
    await engine.dispose()


async def test_defaults_and_types(settings: SettingsService) -> None:
    assert await settings.get("general.site_name") == "WishPanel"
    assert await settings.get("auth.login_max_attempts") == 5
    assert await settings.get("setup.completed") is False
    assert await settings.get("kavita.api_key") == ""


async def test_secret_is_encrypted_and_masked(settings: SettingsService) -> None:
    assert await settings.set("kavita.api_key", "my-kavita-api-key-9876") is True
    await settings.db.commit()
    from app.db.models import Setting

    row = await settings.db.get(Setting, "kavita.api_key")
    assert row.is_secret and "9876" not in row.value
    assert await settings.get("kavita.api_key") == "my-kavita-api-key-9876"
    assert await settings.masked("kavita.api_key") == "••••••••9876"
    assert await settings.set("kavita.api_key", "my-kavita-api-key-9876") is False  # unchanged


async def test_validation(settings: SettingsService) -> None:
    with pytest.raises(SettingValidationError):
        await settings.set("kavita.url", "kavita:5000")
    with pytest.raises(SettingValidationError):
        await settings.set("auth.login_max_attempts", 0)
    with pytest.raises(SettingValidationError):
        await settings.set("general.theme", "purple")
    with pytest.raises(SettingValidationError):
        await settings.set("no.such.key", "x")
    await settings.set("kavita.url", " http://10.0.0.5:5000/ ")
    assert await settings.get("kavita.url") == "http://10.0.0.5:5000"


def test_redaction() -> None:
    assert "abc123" not in redact("GET /api?apiKey=abc123&x=1")
    assert "s3cr3t" not in redact('{"password": "s3cr3t"}')
    assert "eyJ" not in redact("Authorization: Bearer eyJhbGciOi.x.y")

    rec = logging.LogRecord("t", logging.INFO, "", 0, "api_key=%s", ("zzz999",), None)
    RedactingFilter().filter(rec)
    assert "zzz999" not in rec.getMessage()


def test_throttle_window() -> None:
    t = LoginThrottle()
    keys = ["ip:1", "user:a"]
    for _ in range(3):
        t.record_failure(keys)
    assert t.retry_after(keys, max_attempts=3, window_s=60) > 0
    assert t.retry_after(["ip:2"], max_attempts=3, window_s=60) == 0
    assert t.retry_after(["user:a"], max_attempts=3, window_s=60) > 0  # other IP, same user
    t.reset(keys)
    assert t.retry_after(keys, max_attempts=3, window_s=60) == 0
