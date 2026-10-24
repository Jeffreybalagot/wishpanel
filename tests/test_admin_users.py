"""Users page API and stable Kavita user ids (M2)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from tests.conftest import admin_headers, db_rows


def _reader_client(client: TestClient) -> TestClient:
    """A second browser for the same running app."""
    other = TestClient(client.app)
    r = other.post("/api/auth/login", json={"username": "reader", "password": "readerpw"})
    assert r.status_code == 200
    return other


def _users(client: TestClient) -> dict[str, dict]:
    return {u["username"]: u for u in client.get("/api/admin/users").json()}


def test_list_users(client: TestClient, setup_done) -> None:
    _reader_client(client)
    users = _users(client)
    assert set(users) == {"admin", "reader"}
    assert users["admin"]["is_admin"] and not users["reader"]["is_admin"]
    assert users["reader"]["allowed_media_types"] is None
    assert users["reader"]["active_sessions"] == 1


def test_block_ends_sessions_and_prevents_login(client: TestClient, setup_done, cfg) -> None:
    reader = _reader_client(client)
    rid = _users(client)["reader"]["id"]
    r = client.patch(
        f"/api/admin/users/{rid}", json={"is_blocked": True}, headers=admin_headers(setup_done)
    )
    assert r.status_code == 200 and r.json()["is_blocked"] and r.json()["active_sessions"] == 0
    assert reader.get("/api/auth/me").status_code == 401
    r = reader.post("/api/auth/login", json={"username": "reader", "password": "readerpw"})
    assert r.status_code == 403
    [(details,)] = db_rows(cfg.db_path, "SELECT details FROM audit_log WHERE action='user.update'")
    assert json.loads(details) == {"is_blocked": True}

    client.patch(
        f"/api/admin/users/{rid}", json={"is_blocked": False}, headers=admin_headers(setup_done)
    )
    r = reader.post("/api/auth/login", json={"username": "reader", "password": "readerpw"})
    assert r.status_code == 200


def test_cannot_block_self(client: TestClient, setup_done) -> None:
    r = client.patch(
        f"/api/admin/users/{setup_done['id']}",
        json={"is_blocked": True},
        headers=admin_headers(setup_done),
    )
    assert r.status_code == 409


def test_per_user_controls_and_reset(client: TestClient, setup_done) -> None:
    _reader_client(client)
    rid = _users(client)["reader"]["id"]
    h = admin_headers(setup_done)
    r = client.patch(
        f"/api/admin/users/{rid}",
        json={
            "daily_limit_override": 3,
            "auto_approve": False,
            "allowed_media_types": ["manhwa", "comic"],
        },
        headers=h,
    )
    u = r.json()
    assert (u["daily_limit_override"], u["auto_approve"]) == (3, False)
    assert u["allowed_media_types"] == ["comic", "manhwa"]  # canonical order
    r = client.patch(
        f"/api/admin/users/{rid}",
        json={"daily_limit_override": None, "auto_approve": None, "allowed_media_types": None},
        headers=h,
    )
    u = r.json()
    assert u["daily_limit_override"] is None and u["auto_approve"] is None
    assert u["allowed_media_types"] is None


def test_patch_validation(client: TestClient, setup_done) -> None:
    h = admin_headers(setup_done)
    uid = setup_done["id"]
    assert (
        client.patch(
            f"/api/admin/users/{uid}", json={"daily_limit_override": -1}, headers=h
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/admin/users/{uid}", json={"allowed_media_types": ["ebooks"]}, headers=h
        ).status_code
        == 422
    )
    assert client.patch("/api/admin/users/999", json={}, headers=h).status_code == 404
    assert client.patch(f"/api/admin/users/{uid}", json={}).status_code == 403  # no CSRF


def test_users_keyed_by_kavita_id(client: TestClient, setup_done, cfg) -> None:
    rows = db_rows(cfg.db_path, "SELECT username, external_id FROM users")
    assert ("admin", "id:1") in rows


def test_legacy_username_key_is_migrated(client: TestClient, setup_done, cfg) -> None:
    """Users created by 2.0.0a2 (keyed by username) keep their row and settings."""
    with closing(sqlite3.connect(cfg.db_path)) as con:
        con.execute(
            "INSERT INTO users (provider, external_id, username, is_admin, is_blocked, "
            "daily_limit_override, created_at) VALUES "
            "('kavita', 'reader', 'reader', 0, 0, 7, '2026-10-23 00:00:00')"
        )
        con.commit()
    _reader_client(client)
    rows = db_rows(
        cfg.db_path, "SELECT external_id, daily_limit_override FROM users WHERE username='reader'"
    )
    assert rows == [("id:2", 7)]
