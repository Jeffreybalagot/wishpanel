"""Single sign-on (OpenID Connect) next to the Kavita login."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.conftest import admin_headers, db_rows, fake_jwt

ISSUER = "https://sso.test/application/o/wishpanel"
CLIENT_ID = "wp-client"
CALLBACK = "http://testserver/api/auth/oidc/callback"


class IdP:
    """A fake OpenID provider. ``user`` is what the next sign-in returns."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.user: dict = {}
        self.nonce = ""
        self.token_requests: list[dict] = []
        router.get(f"{ISSUER}/.well-known/openid-configuration").respond(
            200,
            json={
                "issuer": ISSUER + "/",
                "authorization_endpoint": "https://sso.test/application/o/authorize/",
                "token_endpoint": "https://sso.test/application/o/token/",
                "userinfo_endpoint": "https://sso.test/application/o/userinfo/",
            },
        )
        router.post("https://sso.test/application/o/token/").mock(side_effect=self._token)
        router.get("https://sso.test/application/o/userinfo/").mock(side_effect=self._userinfo)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        self.token_requests.append({k: v[0] for k, v in form.items()})
        id_token = fake_jwt(
            {"iss": ISSUER + "/", "aud": CLIENT_ID, "sub": self.user["sub"], "nonce": self.nonce}
        )
        return httpx.Response(200, json={"access_token": "at-123", "id_token": id_token})

    def _userinfo(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer at-123"
        return httpx.Response(200, json=self.user)


@pytest.fixture
def idp(kavita: respx.MockRouter) -> IdP:
    return IdP(kavita)


def enable_sso(client: TestClient, setup: dict, **extra) -> None:
    values = {
        "auth.oidc.enabled": True,
        "auth.oidc.issuer_url": ISSUER + "/",
        "auth.oidc.client_id": CLIENT_ID,
        "auth.oidc.client_secret": "s3cret",
        "auth.oidc.button_label": "Log in with Plex",
        **extra,
    }
    r = client.put("/api/admin/settings", json={"values": values}, headers=admin_headers(setup))
    assert r.status_code == 200, r.text


def sso_sign_in(client: TestClient, idp: IdP, user: dict, *, state: str | None = None):
    """Run the browser side of the flow; return the callback response."""
    client.cookies.clear()
    r = client.get("/api/auth/oidc/login", follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert loc.netloc == "sso.test"
    assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == CALLBACK
    assert q["code_challenge_method"] == "S256" and q["scope"] == "openid email profile"
    idp.nonce, idp.user = q["nonce"], user
    return client.get(
        "/api/auth/oidc/callback",
        params={"code": "abc", "state": state or q["state"]},
        follow_redirects=False,
    )


def test_site_info_advertises_sso_only_when_configured(client: TestClient, setup_done) -> None:
    info = client.get("/api/system/info").json()
    assert info["sso_enabled"] is False and info["sso_label"] == ""
    enable_sso(client, setup_done)
    info = client.get("/api/system/info").json()
    assert info["sso_enabled"] is True and info["sso_label"] == "Log in with Plex"


def test_new_sso_user_is_created_and_signed_in(client: TestClient, setup_done, idp, cfg) -> None:
    enable_sso(client, setup_done)
    r = sso_sign_in(
        client, idp, {"sub": "u-1", "email": "new@example.com", "preferred_username": "newbie"}
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "wishpanel_session" in client.cookies
    me = client.get("/api/auth/me").json()
    assert me["username"] == "newbie" and me["is_admin"] is False
    # PKCE verifier and client secret went to the token endpoint, not the browser.
    sent = idp.token_requests[-1]
    assert sent["client_secret"] == "s3cret" and sent["code_verifier"]
    assert sent["redirect_uri"] == CALLBACK
    rows = db_rows(cfg.db_path, "SELECT provider, external_id FROM users WHERE username='newbie'")
    assert rows == [("oidc", "u-1")]
    audit = db_rows(cfg.db_path, "SELECT details FROM audit_log ORDER BY id DESC LIMIT 1")
    assert '"via": "sso"' in audit[0][0]

    # Second sign-in reuses the same account.
    sso_sign_in(client, idp, {"sub": "u-1", "email": "new@example.com"})
    assert db_rows(cfg.db_path, "SELECT count(*) FROM users WHERE provider='oidc'") == [(1,)]


def test_sso_links_to_existing_kavita_account_by_email(
    client: TestClient, setup_done, idp, cfg
) -> None:
    enable_sso(client, setup_done)
    client.cookies.clear()
    kav = client.post("/api/auth/login", json={"username": "reader", "password": "readerpw"})
    reader_id = kav.json()["id"]

    sso_sign_in(client, idp, {"sub": "plex-77", "email": "READER@example.com"})
    me = client.get("/api/auth/me").json()
    assert me["id"] == reader_id and me["username"] == "reader"
    # The Kavita identity is untouched, so password sign-in still lands on it.
    rows = db_rows(cfg.db_path, "SELECT id, provider FROM users WHERE username='reader'")
    assert rows == [(reader_id, "kavita")]
    client.cookies.clear()
    again = client.post("/api/auth/login", json={"username": "reader", "password": "readerpw"})
    assert again.json()["id"] == reader_id


def test_kavita_admin_stays_admin_via_sso(client: TestClient, setup_done, idp) -> None:
    enable_sso(client, setup_done, **{"auth.oidc.admin_group": "wishpanel-admins"})
    sso_sign_in(client, idp, {"sub": "a-1", "email": "admin@example.com", "groups": []})
    me = client.get("/api/auth/me").json()
    assert me["username"] == "admin" and me["is_admin"] is True


def test_admin_group_controls_sso_only_accounts(client: TestClient, setup_done, idp) -> None:
    enable_sso(client, setup_done, **{"auth.oidc.admin_group": "WishPanel-Admins"})
    user = {"sub": "g-1", "email": "g@example.com", "groups": ["wishpanel-admins"]}
    sso_sign_in(client, idp, user)
    assert client.get("/api/auth/me").json()["is_admin"] is True

    sso_sign_in(client, idp, {**user, "groups": ["readers"]})
    assert client.get("/api/auth/me").json()["is_admin"] is False


def test_link_by_email_can_be_turned_off(client: TestClient, setup_done, idp) -> None:
    enable_sso(client, setup_done, **{"auth.oidc.link_by_email": False})
    sso_sign_in(
        client, idp, {"sub": "x-1", "email": "admin@example.com", "preferred_username": "x"}
    )
    me = client.get("/api/auth/me").json()
    assert me["username"] == "x" and me["is_admin"] is False


def test_unverified_email_is_not_linked(client: TestClient, setup_done, idp) -> None:
    enable_sso(client, setup_done)
    sso_sign_in(
        client,
        idp,
        {"sub": "x-2", "email": "admin@example.com", "email_verified": False, "name": "Mallory"},
    )
    me = client.get("/api/auth/me").json()
    assert me["id"] != setup_done["id"] and me["is_admin"] is False


def test_auto_create_off_refuses_unknown_users(client: TestClient, setup_done, idp) -> None:
    enable_sso(client, setup_done, **{"auth.oidc.auto_create": False})
    r = sso_sign_in(client, idp, {"sub": "n-1", "email": "nobody@example.com"})
    assert r.status_code == 303 and "sso_error=" in r.headers["location"]
    assert "wishpanel_session" not in client.cookies


def test_state_mismatch_is_rejected(client: TestClient, setup_done, idp) -> None:
    enable_sso(client, setup_done)
    r = sso_sign_in(client, idp, {"sub": "u-9", "email": "z@example.com"}, state="forged")
    assert "sso_error=" in r.headers["location"]
    assert "wishpanel_session" not in client.cookies
    assert idp.token_requests == []


def test_callback_without_state_cookie_is_rejected(client: TestClient, setup_done, idp) -> None:
    enable_sso(client, setup_done)
    client.cookies.clear()
    r = client.get(
        "/api/auth/oidc/callback", params={"code": "abc", "state": "s"}, follow_redirects=False
    )
    assert r.status_code == 303 and "sso_error=" in r.headers["location"]


def test_nonce_mismatch_is_rejected(client: TestClient, setup_done, idp) -> None:
    enable_sso(client, setup_done)
    client.cookies.clear()
    r = client.get("/api/auth/oidc/login", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    idp.nonce, idp.user = "something-else", {"sub": "u-5", "email": "q@example.com"}
    r = client.get(
        "/api/auth/oidc/callback", params={"code": "abc", "state": state}, follow_redirects=False
    )
    assert "sso_error=" in r.headers["location"]
    assert "wishpanel_session" not in client.cookies


def test_provider_error_param_is_reported(client: TestClient, setup_done, idp) -> None:
    enable_sso(client, setup_done)
    client.get("/api/auth/oidc/login", follow_redirects=False)
    r = client.get(
        "/api/auth/oidc/callback", params={"error": "access_denied"}, follow_redirects=False
    )
    assert "cancelled" in r.headers["location"]


def test_sso_disabled(client: TestClient, setup_done, idp) -> None:
    r = client.get("/api/auth/oidc/login", follow_redirects=False)
    assert r.status_code == 303 and "sso_error=" in r.headers["location"]


def test_blocked_user_is_refused(client: TestClient, setup_done, idp, cfg) -> None:
    enable_sso(client, setup_done)
    sso_sign_in(client, idp, {"sub": "b-1", "email": "b@example.com", "preferred_username": "bee"})
    with closing(sqlite3.connect(cfg.db_path)) as con:
        con.execute("UPDATE users SET is_blocked=1 WHERE username='bee'")
        con.commit()
    r = sso_sign_in(client, idp, {"sub": "b-1", "email": "b@example.com"})
    assert "blocked" in r.headers["location"]


def test_connection_test_button(client: TestClient, setup_done, idp) -> None:
    r = client.post(
        "/api/admin/connections/oidc/test",
        json={
            "values": {
                "auth.oidc.issuer_url": ISSUER,
                "auth.oidc.client_id": CLIENT_ID,
                "auth.oidc.client_secret": "x",
            }
        },
        headers=admin_headers(setup_done),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_password_login_still_works_with_sso_on(client: TestClient, setup_done, idp) -> None:
    enable_sso(client, setup_done)
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": "reader", "password": "readerpw"})
    assert r.status_code == 200 and r.json()["username"] == "reader"
