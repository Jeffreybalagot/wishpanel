from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import KAVITA, db_rows, do_setup, setup_code


def _login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ---------------- first-run setup ----------------


def test_setup_status_before_setup(client: TestClient) -> None:
    body = client.get("/api/setup/status").json()
    assert body == {"completed": False, "code_required": True, "admin_role": "Admin"}
    assert client.get("/api/system/info").json()["setup_completed"] is False


def test_setup_kavita_test_button(client: TestClient, kavita) -> None:
    code = setup_code(client)
    r = client.post("/api/setup/kavita/test", json={"code": code, "url": KAVITA})
    assert r.json() == {"ok": True, "message": "Connected to Kavita."}

    r = client.post("/api/setup/kavita/test", json={"code": code, "url": "not a url"})
    assert r.status_code == 422

    kavita.get("http://nothere.test/api/health").respond(404)
    r = client.post("/api/setup/kavita/test", json={"code": code, "url": "http://nothere.test"})
    assert r.json()["ok"] is False
    assert "doesn't look like Kavita" in r.json()["message"]


def test_login_refused_before_setup(client: TestClient) -> None:
    r = _login(client, "admin", "adminpw")
    assert r.status_code == 409


def test_setup_requires_kavita_admin(client: TestClient) -> None:
    r = client.post(
        "/api/setup/complete",
        json={
            "code": setup_code(client),
            "kavita_url": KAVITA,
            "username": "reader",
            "password": "readerpw",
        },
    )
    assert r.status_code == 403
    assert "isn't a Kavita 'Admin'" in r.json()["detail"]
    assert client.get("/api/setup/status").json()["completed"] is False


def test_setup_completes_and_locks(client: TestClient, cfg) -> None:
    me = do_setup(client)
    assert me["username"] == "admin" and me["is_admin"] is True
    assert "wishpanel_session" in client.cookies

    assert client.get("/api/setup/status").json()["completed"] is True
    # Locked: nobody can re-run setup to point WishPanel at a different Kavita.
    r = client.post(
        "/api/setup/complete",
        json={"code": "x", "kavita_url": "http://evil.test", "username": "x", "password": "y"},
    )
    assert r.status_code == 409
    r = client.post("/api/setup/kavita/test", json={"code": "x", "url": KAVITA})
    assert r.status_code == 409

    actions = [r[0] for r in db_rows(cfg.db_path, "SELECT action FROM audit_log ORDER BY id")]
    assert actions[-2:] == ["auth.login", "setup.completed"]


# ---------------- login / session ----------------


def test_login_logout_flow(client: TestClient, setup_done) -> None:
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401

    r = _login(client, "reader", "readerpw")
    assert r.status_code == 200
    me = r.json()
    assert me["username"] == "reader" and me["is_admin"] is False
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie  # plain-http LAN: 'auto' mode leaves Secure off

    assert client.get("/api/auth/me").json()["username"] == "reader"

    # Logout without CSRF header is rejected
    assert client.post("/api/auth/logout").status_code == 403
    r = client.post("/api/auth/logout", headers={"X-CSRF-Token": me["csrf_token"]})
    assert r.status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_secure_cookie_over_https(client: TestClient, setup_done) -> None:
    client.cookies.clear()
    r = client.post(
        "https://testserver/api/auth/login", json={"username": "reader", "password": "readerpw"}
    )
    assert r.status_code == 200
    assert "secure" in r.headers["set-cookie"].lower()


def test_bad_password(client: TestClient, setup_done) -> None:
    client.cookies.clear()
    r = _login(client, "reader", "wrong")
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid username or password."


def test_login_requires_json(client: TestClient, setup_done) -> None:
    client.cookies.clear()
    r = client.post("/api/auth/login", data={"username": "reader", "password": "readerpw"})
    assert r.status_code == 415


def test_login_throttle(client: TestClient, setup_done) -> None:
    client.cookies.clear()
    for _ in range(5):
        assert _login(client, "reader", "wrong").status_code == 401
    r = _login(client, "reader", "readerpw")  # correct password, but throttled
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


def test_kavita_down_gives_friendly_error(client: TestClient, setup_done, kavita) -> None:
    import httpx

    client.cookies.clear()
    kavita.post(f"{KAVITA}/api/Account/login").mock(side_effect=httpx.ConnectError("boom"))
    r = _login(client, "reader", "readerpw")
    assert r.status_code == 502
    assert "Kavita is unreachable" in r.json()["detail"]


# ---------------- admin guard ----------------


def test_admin_overview_requires_admin(client: TestClient, setup_done) -> None:
    r = client.get("/api/admin/overview")
    assert r.status_code == 200
    names = [c["name"] for c in r.json()["connections"]]
    assert names == ["Kavita", "Kapowarr", "Suwayomi", "ComicVine"]
    assert r.json()["connections"][0]["url"] == KAVITA

    client.cookies.clear()
    assert client.get("/api/admin/overview").status_code == 401
    _login(client, "reader", "readerpw")
    assert client.get("/api/admin/overview").status_code == 403


def test_admin_services_reports_kavita_after_setup(client: TestClient, setup_done) -> None:
    body = client.get("/api/admin/services").json()
    assert body == {
        "status": "ok",
        "services": {
            "kavita": {"status": "ok"},
            "kapowarr": {"status": "not_configured"},
            "suwayomi": {"status": "not_configured"},
            "comicvine": {"status": "not_configured"},
        },
    }
    assert client.get("/health").json()["setup_completed"] is True
