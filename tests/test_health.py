"""Regression tests: /health is local-only; external status is separate and admin-only (M1.1)."""

from __future__ import annotations

import httpx
import respx
from fastapi.testclient import TestClient

from tests.conftest import KAVITA


def test_health_never_contacts_external_services(client: TestClient, setup_done, kavita) -> None:
    for route in kavita.routes:
        route.reset()
    for _ in range(3):
        r = client.get("/health")
        assert r.status_code == 200
        assert set(r.json()["checks"]) == {"database", "config"}
    assert not any(route.called for route in kavita.routes)


def test_health_stays_ok_when_kavita_is_down(
    client: TestClient, setup_done, kavita: respx.MockRouter
) -> None:
    kavita.get(f"{KAVITA}/api/health").mock(side_effect=httpx.ConnectError("down"))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    services = client.get("/api/admin/services").json()
    assert services["status"] == "degraded"
    assert services["services"]["kavita"]["status"] == "error"
    assert "Could not reach Kavita" in services["services"]["kavita"]["message"]


def test_health_503_when_database_unavailable(client: TestClient) -> None:
    ctx = client.app.state.ctx

    class Broken:
        def __call__(self):
            raise RuntimeError("db gone")

    original = ctx.health.sessionmaker
    ctx.health.sessionmaker = Broken()
    try:
        r = client.get("/health")
    finally:
        ctx.health.sessionmaker = original
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "error"
    assert body["checks"]["database"]["status"] == "error"


def test_health_503_when_config_not_writable(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.services.health.os.access", lambda *_a, **_k: False)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["checks"]["config"] == {"status": "error", "message": "not writable"}


def test_services_endpoint_is_admin_only(client: TestClient, setup_done) -> None:
    assert client.get("/api/admin/services").status_code == 200
    client.cookies.clear()
    assert client.get("/api/admin/services").status_code == 401
    client.post("/api/auth/login", json={"username": "reader", "password": "readerpw"})
    assert client.get("/api/admin/services").status_code == 403


def test_admin_overview_shows_health_and_services_separately(
    client: TestClient, setup_done
) -> None:
    overview = client.get("/api/admin/overview").json()
    assert overview["health"]["status"] == "ok"
    assert overview["services"]["services"]["kavita"]["status"] == "ok"
