"""Regression tests for the one-time setup claim code (M1.1)."""

from __future__ import annotations

import logging
import re
import stat
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import setup_claim
from tests.conftest import KAVITA, db_rows, do_setup, make_cfg, setup_code

CODE_RE = re.compile(r"^[A-Z2-9]{4}(-[A-Z2-9]{4}){3}$")


def _complete(client: TestClient, code: str, user: str = "admin", pw: str = "adminpw"):
    return client.post(
        "/api/setup/complete",
        json={"code": code, "kavita_url": KAVITA, "username": user, "password": pw},
    )


def test_code_file_created_private_and_well_formed(client: TestClient, cfg) -> None:
    path = cfg.config_dir / "setup-code.txt"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    code = path.read_text().strip()
    assert CODE_RE.match(code), code
    assert not set("01ILOU") & set(code.replace("-", ""))  # no look-alike characters


def test_only_hash_is_stored(client: TestClient, cfg) -> None:
    code = setup_code(client)
    [(stored,)] = db_rows(cfg.db_path, "SELECT value FROM settings WHERE key='setup.claim_hash'")
    assert stored == setup_claim.hash_code(code)
    assert setup_claim.normalise(code) not in stored


def test_code_is_announced_in_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = make_cfg(tmp_path)
    with (
        caplog.at_level(logging.WARNING),
        respx.mock,
        TestClient(create_app(cfg, configure_logs=False)) as c,
    ):
        code = setup_code(c)
    assert code in caplog.text
    assert "setup-code.txt" in caplog.text


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/setup/claim", {}),
        ("/api/setup/kavita/test", {"url": KAVITA}),
        (
            "/api/setup/complete",
            {"kavita_url": KAVITA, "username": "admin", "password": "adminpw"},
        ),
    ],
)
def test_every_setup_action_requires_the_code(client: TestClient, kavita, path, payload) -> None:
    # Missing code -> validation error; nothing reaches Kavita.
    assert client.post(path, json=payload).status_code == 422
    # Wrong code -> 403; nothing reaches Kavita.
    r = client.post(path, json={**payload, "code": "AAAA-BBBB-CCCC-DDDD"})
    assert r.status_code == 403
    assert "setup code" in r.json()["detail"]
    assert not any(route.called for route in kavita.routes)
    assert client.get("/api/setup/status").json()["completed"] is False


def test_code_is_case_and_dash_insensitive(client: TestClient) -> None:
    sloppy = setup_code(client).replace("-", " ").lower()
    assert client.post("/api/setup/claim", json={"code": sloppy}).status_code == 200


def test_wrong_codes_are_throttled_and_audited(client: TestClient, cfg) -> None:
    for _ in range(5):
        r = client.post("/api/setup/claim", json={"code": "AAAA-BBBB-CCCC-DDDD"})
        assert r.status_code == 403
    # Even the right code is refused while throttled.
    r = client.post("/api/setup/claim", json={"code": setup_code(client)})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    rows = db_rows(cfg.db_path, "SELECT action FROM audit_log WHERE action='setup.code_rejected'")
    assert len(rows) == 5


def test_code_survives_restart_until_claimed(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    with respx.mock, TestClient(create_app(cfg, configure_logs=False)) as c:
        first = setup_code(c)
    with respx.mock, TestClient(create_app(cfg, configure_logs=False)) as c:
        assert setup_code(c) == first
        assert c.post("/api/setup/claim", json={"code": first}).status_code == 200


def test_deleted_file_rotates_code_and_old_one_dies(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    with respx.mock, TestClient(create_app(cfg, configure_logs=False)) as c:
        old = setup_code(c)
    (cfg.config_dir / "setup-code.txt").unlink()
    with respx.mock, TestClient(create_app(cfg, configure_logs=False)) as c:
        new = setup_code(c)
        assert new != old
        assert c.post("/api/setup/claim", json={"code": old}).status_code == 403
        assert c.post("/api/setup/claim", json={"code": new}).status_code == 200


def test_tampered_file_is_not_trusted(tmp_path: Path) -> None:
    """Writing your own code into the file doesn't work without the matching hash."""
    cfg = make_cfg(tmp_path)
    with respx.mock, TestClient(create_app(cfg, configure_logs=False)):
        pass
    (cfg.config_dir / "setup-code.txt").write_text("AAAA-BBBB-CCCC-DDDD\n")
    with respx.mock, TestClient(create_app(cfg, configure_logs=False)) as c:
        assert setup_code(c) != "AAAA-BBBB-CCCC-DDDD"
        r = c.post("/api/setup/claim", json={"code": "AAAA-BBBB-CCCC-DDDD"})
        assert r.status_code == 403


def test_completion_destroys_code(client: TestClient, cfg, kavita) -> None:
    code = setup_code(client)
    assert _complete(client, code).status_code == 200
    assert not (cfg.config_dir / "setup-code.txt").exists()
    [(stored,)] = db_rows(cfg.db_path, "SELECT value FROM settings WHERE key='setup.claim_hash'")
    assert stored == ""
    assert client.get("/api/setup/status").json()["code_required"] is False
    # Re-using the old code after completion gets 409, never a second setup.
    assert _complete(client, code).status_code == 409


def test_failed_completion_keeps_code_valid(client: TestClient) -> None:
    code = setup_code(client)
    assert _complete(client, code, "reader", "readerpw").status_code == 403  # not a Kavita admin
    assert _complete(client, code, "admin", "wrong").status_code == 401
    assert _complete(client, code).status_code == 200


def test_no_code_after_restart_once_set_up(tmp_path: Path, kavita) -> None:
    cfg = make_cfg(tmp_path)
    with TestClient(create_app(cfg, configure_logs=False)) as c:
        do_setup(c)
    with TestClient(create_app(cfg, configure_logs=False)) as c:
        assert not (cfg.config_dir / "setup-code.txt").exists()
        assert c.get("/api/setup/status").json()["completed"] is True
