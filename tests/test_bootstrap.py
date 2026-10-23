from __future__ import annotations

import stat
from pathlib import Path

import respx
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import db_rows, make_cfg


def test_boots_with_empty_config(client: TestClient, cfg) -> None:
    assert cfg.secret_key_path.exists()
    assert stat.S_IMODE(cfg.secret_key_path.stat().st_mode) == 0o600
    assert cfg.db_path.exists()

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["setup_completed"] is False
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["kavita"]["status"] == "not_configured"


def test_migrations_create_tables_and_defaults(client: TestClient, cfg) -> None:
    tables = {
        r[0] for r in db_rows(cfg.db_path, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"settings", "users", "sessions", "audit_log", "alembic_version"} <= tables
    [(version,)] = db_rows(cfg.db_path, "SELECT version_num FROM alembic_version")
    assert version == "0001_initial"
    keys = {r[0] for r in db_rows(cfg.db_path, "SELECT key FROM settings")}
    assert {"kavita.url", "kavita.api_key", "general.site_name", "setup.completed"} <= keys


def test_restart_is_idempotent_and_keeps_key(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    with respx.mock, TestClient(create_app(cfg, configure_logs=False)) as c:
        assert c.get("/health").status_code == 200
    key = cfg.secret_key_path.read_bytes()
    with respx.mock, TestClient(create_app(cfg, configure_logs=False)) as c:
        assert c.get("/health").status_code == 200
    assert cfg.secret_key_path.read_bytes() == key


def test_legacy_env_imported_once_and_encrypted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KAVITA_URL", "http://old-kavita:5000/")
    monkeypatch.setenv("KAPOWARR_API_KEY", "kapowarr-secret-1234")
    cfg = make_cfg(tmp_path)
    with (
        respx.mock(assert_all_called=False),
        TestClient(create_app(cfg, configure_logs=False)) as c,
    ):
        assert c.get("/api/setup/status").json()["kavita_url"] == "http://old-kavita:5000"

    [(raw, is_secret)] = db_rows(
        cfg.db_path, "SELECT value, is_secret FROM settings WHERE key='kapowarr.api_key'"
    )
    assert is_secret == 1
    assert "kapowarr-secret-1234" not in raw  # encrypted at rest

    # Changing the env afterwards has no effect: imported once, then ignored forever.
    monkeypatch.setenv("KAVITA_URL", "http://new-kavita:5000")
    with (
        respx.mock(assert_all_called=False),
        TestClient(create_app(cfg, configure_logs=False)) as c,
    ):
        assert c.get("/api/setup/status").json()["kavita_url"] == "http://old-kavita:5000"


def test_base_url_prefix(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path, BASE_URL="https://example.com/wishpanel/")
    assert cfg.base_path == "/wishpanel"
    with respx.mock, TestClient(create_app(cfg, configure_logs=False)) as c:
        assert c.get("/health").status_code == 200  # root health for Docker
        assert c.get("/wishpanel/health").status_code == 200
        assert c.get("/wishpanel/api/system/info").status_code == 200
        r = c.get("/wishpanel", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/wishpanel/"
        assert "WishPanel" in c.get("/wishpanel/").text
        assert c.get("/wishpanel/static/app.js").status_code == 200


def test_index_and_security_headers(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "alpine" in r.text
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/static/vendor/alpine-3.17.5.min.js").status_code == 200
