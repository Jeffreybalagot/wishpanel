from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.main import create_app

KAVITA = "http://kavita.test:5000"

USERS = {
    # username: (password, roles, kavita user id)
    "admin": ("adminpw", ["Admin", "Pleb"], 1),
    "reader": ("readerpw", ["Pleb"], 2),
}


def fake_jwt(claims: dict) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'HS256', 'typ': 'JWT'})}.{b64(claims)}.sig"


def _kavita_login(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    user = USERS.get(body["username"])
    if user is None or user[0] != body["password"]:
        return httpx.Response(401, json={"message": "Invalid credentials"})
    roles = user[1]
    token = fake_jwt({"name": body["username"], "role": roles if len(roles) > 1 else roles[0]})
    return httpx.Response(
        200,
        json={
            "id": user[2],
            "username": body["username"],
            "email": f"{body['username']}@example.com",
            "token": token,
        },
    )


@pytest.fixture
def kavita() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{KAVITA}/api/health").respond(200, text="Ok")
        router.post(f"{KAVITA}/api/Account/login").mock(side_effect=_kavita_login)
        yield router


def make_cfg(tmp_path: Path, **env: str) -> BootstrapConfig:
    return BootstrapConfig.from_env(
        {
            "CONFIG_DIR": str(tmp_path / "config"),
            "IMPORT_DIR": str(tmp_path / "import"),
            "BACKGROUND_JOBS": "0",
            **env,
        }
    )


@pytest.fixture
def cfg(tmp_path: Path) -> BootstrapConfig:
    return make_cfg(tmp_path)


@pytest.fixture
def client(cfg: BootstrapConfig, kavita: respx.MockRouter) -> Iterator[TestClient]:
    with TestClient(create_app(cfg, configure_logs=False)) as c:
        yield c


def setup_code(client: TestClient) -> str:
    """The claim code exactly as an admin would read it from /config."""
    path = client.app.state.ctx.config.config_dir / "setup-code.txt"
    return path.read_text().strip()


def do_setup(client: TestClient, username: str = "admin", password: str = "adminpw") -> dict:
    r = client.post(
        "/api/setup/complete",
        json={
            "code": setup_code(client),
            "kavita_url": KAVITA,
            "username": username,
            "password": password,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def setup_done(client: TestClient) -> dict:
    """Setup completed; the client holds the admin's session cookie."""
    return do_setup(client)


def db_rows(db_path: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(db_path)) as con:
        return con.execute(sql).fetchall()


def admin_headers(setup: dict) -> dict[str, str]:
    """CSRF header for state-changing admin calls."""
    return {"X-CSRF-Token": setup["csrf_token"]}


@pytest.fixture(autouse=True)
def _no_outbound_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outbound API pacing (ComicVine 1 req/s etc.) only slows tests down."""
    from app.core import ratelimit

    monkeypatch.setattr(ratelimit, "INTERVALS", {})
    monkeypatch.setattr(ratelimit, "OUTBOUND", ratelimit.MinInterval())
