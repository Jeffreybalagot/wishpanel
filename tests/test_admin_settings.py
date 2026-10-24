"""Admin settings API: schema, masking, validation, audit, live tests/options, scan, logo (M2)."""

from __future__ import annotations

import json

import httpx
import respx
from fastapi.testclient import TestClient

from tests.conftest import KAVITA, admin_headers, db_rows

KAP = "http://kapowarr.test:5656"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _put(client: TestClient, setup: dict, values: dict):
    return client.put("/api/admin/settings", json={"values": values}, headers=admin_headers(setup))


def _login_reader(client: TestClient) -> None:
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "reader", "password": "readerpw"})


# ---------------------------------------------------------------- access control


def test_admin_endpoints_require_admin(client: TestClient, setup_done) -> None:
    paths = ["/api/admin/settings/schema", "/api/admin/settings", "/api/admin/users"]
    for p in paths:
        assert client.get(p).status_code == 200
    _login_reader(client)
    for p in paths:
        assert client.get(p).status_code == 403
    client.cookies.clear()
    for p in paths:
        assert client.get(p).status_code == 401


def test_put_requires_csrf(client: TestClient, setup_done) -> None:
    r = client.put("/api/admin/settings", json={"values": {"general.site_name": "X"}})
    assert r.status_code == 403


# ---------------------------------------------------------------- schema & values


def test_schema_lists_m2_pages(client: TestClient, setup_done) -> None:
    pages = client.get("/api/admin/settings/schema").json()["pages"]
    assert [p["id"] for p in pages] == [
        "general",
        "authentication",
        "kavita",
        "kapowarr",
        "suwayomi",
        "comicvine",
    ]
    kav = next(p for p in pages if p["id"] == "kavita")
    assert kav["connection"] == "kavita" and "kavita.scan" in kav["actions"]
    key_field = next(f for f in kav["fields"] if f["key"] == "kavita.api_key")
    assert key_field["widget"] == "secret"


def test_secrets_are_masked_and_never_returned(client: TestClient, setup_done, cfg) -> None:
    secret = "kapowarr-secret-abcd1234"
    r = _put(client, setup_done, {"kapowarr.api_key": secret})
    assert r.status_code == 200
    assert secret not in r.text
    values = client.get("/api/admin/settings").json()["values"]
    assert values["kapowarr.api_key"] == {"set": True, "masked": "••••••••1234"}
    # Stored encrypted; audit names the key but never the value.
    [(raw,)] = db_rows(cfg.db_path, "SELECT value FROM settings WHERE key='kapowarr.api_key'")
    assert secret not in raw
    [(details,)] = db_rows(
        cfg.db_path, "SELECT details FROM audit_log WHERE action='settings.update'"
    )
    assert json.loads(details) == {"changed": ["kapowarr.api_key"]} and secret not in details


def test_secret_null_keeps_empty_clears(client: TestClient, setup_done) -> None:
    _put(client, setup_done, {"comicvine.api_key": "cv-key-0000-9999"})
    r = _put(client, setup_done, {"comicvine.api_key": None, "general.site_name": "Wishes"})
    assert r.json()["values"]["comicvine.api_key"]["set"] is True
    assert r.json()["changed"] == ["general.site_name"]
    r = _put(client, setup_done, {"comicvine.api_key": ""})
    assert r.json()["values"]["comicvine.api_key"] == {"set": False, "masked": ""}


def test_validation_is_all_or_nothing(client: TestClient, setup_done) -> None:
    r = _put(client, setup_done, {"general.site_name": "Changed", "kapowarr.url": "kapowarr:5656"})
    assert r.status_code == 422
    assert r.json()["detail"]["key"] == "kapowarr.url"
    values = client.get("/api/admin/settings").json()["values"]
    assert values["general.site_name"] == "WishPanel"  # nothing written


def test_internal_settings_not_writable(client: TestClient, setup_done) -> None:
    for key in ("setup.completed", "setup.claim_hash", "general.logo_version", "nope.nope"):
        r = _put(client, setup_done, {key: "true"})
        assert r.status_code == 422, key
        assert r.json()["detail"]["key"] == key


def test_list_setting_order_and_dedup(client: TestClient, setup_done) -> None:
    r = _put(client, setup_done, {"suwayomi.sources": ["9", "3", "9", " ", "1"]})
    assert r.json()["values"]["suwayomi.sources"] == ["9", "3", "1"]
    r = _put(client, setup_done, {"comicvine.blocked_publishers": "Panini\nGlénat\n"})
    assert r.json()["values"]["comicvine.blocked_publishers"] == ["Panini", "Glénat"]


def test_kavita_url_checked_before_saving(client: TestClient, setup_done, kavita) -> None:
    kavita.get("http://wrong.test/api/health").respond(404)
    r = _put(client, setup_done, {"kavita.url": "http://wrong.test"})
    assert r.status_code == 422 and "Not saved" in r.json()["detail"]["message"]
    r = _put(client, setup_done, {"kavita.url": ""})
    assert r.status_code == 422
    kavita.get("http://kavita2.test/api/health").respond(200, text="Ok")
    r = _put(client, setup_done, {"kavita.url": "http://kavita2.test/"})
    assert r.status_code == 200 and r.json()["values"]["kavita.url"] == "http://kavita2.test"


# ---------------------------------------------------------------- tests & options


def test_connection_test_uses_unsaved_values_and_stored_secret(
    client: TestClient, setup_done, kavita: respx.MockRouter
) -> None:
    _put(client, setup_done, {"kapowarr.api_key": "stored-key"})
    about = kavita.get(f"{KAP}/api/system/about").respond(
        200, json={"error": None, "result": {"version": "v1.2.0"}}
    )
    kavita.get(f"{KAP}/api/rootfolder").respond(200, json={"error": None, "result": []})
    r = client.post(
        "/api/admin/connections/kapowarr/test",
        json={"values": {"kapowarr.url": KAP, "kapowarr.api_key": None}},
        headers=admin_headers(setup_done),
    )
    assert r.json()["ok"] is True and "v1.2.0" in r.json()["message"]
    assert about.calls.last.request.url.params["api_key"] == "stored-key"
    # Testing does not save.
    assert client.get("/api/admin/settings").json()["values"]["kapowarr.url"] == ""


def test_connection_test_rejects_bad_override(client: TestClient, setup_done) -> None:
    r = client.post(
        "/api/admin/connections/kapowarr/test",
        json={"values": {"kapowarr.url": "nope"}},
        headers=admin_headers(setup_done),
    )
    assert r.status_code == 422
    r = client.post(
        "/api/admin/connections/nosuch/test", json={}, headers=admin_headers(setup_done)
    )
    assert r.status_code == 404


def test_options_kavita_libraries(client: TestClient, setup_done, kavita) -> None:
    kavita.get(f"{KAVITA}/api/Library/libraries").respond(
        200, json=[{"id": 1, "name": "Comics", "type": 5}, {"id": 2, "name": "Manga", "type": 0}]
    )
    r = client.post(
        "/api/admin/options/kavita.libraries",
        json={"values": {"kavita.api_key": "k"}},
        headers=admin_headers(setup_done),
    )
    body = r.json()
    assert body["ok"] is True
    assert [(o["value"], o["label"], o["hint"]) for o in body["options"]] == [
        ("1", "Comics", "comic"),
        ("2", "Manga", "manga"),
    ]


def test_options_report_provider_errors(client: TestClient, setup_done) -> None:
    r = client.post(
        "/api/admin/options/kavita.libraries", json={}, headers=admin_headers(setup_done)
    )
    assert r.json() == {"ok": False, "message": "Kavita API key is not set.", "options": []}


def test_kavita_scan(client: TestClient, setup_done, kavita, cfg) -> None:
    h = admin_headers(setup_done)
    assert client.post("/api/admin/kavita/scan", json={}, headers=h).status_code == 409
    _put(client, setup_done, {"kavita.api_key": "k", "kavita.library.manga": "2"})
    _put(client, setup_done, {"kavita.library.manhwa": "2", "kavita.library.comic": "1"})
    scan = kavita.post(f"{KAVITA}/api/Library/scan").respond(200)
    r = client.post("/api/admin/kavita/scan", json={}, headers=h)
    assert r.status_code == 200 and "2 libraries" in r.json()["message"]
    assert sorted(c.request.url.params["libraryId"] for c in scan.calls) == ["1", "2"]
    r = client.post("/api/admin/kavita/scan", json={"media_type": "manga"}, headers=h)
    assert "1 library" in r.json()["message"]
    kavita.post(f"{KAVITA}/api/Library/scan").mock(side_effect=httpx.ConnectError("x"))
    r = client.post("/api/admin/kavita/scan", json={}, headers=h)
    assert r.status_code == 502 and "Could not reach Kavita" in r.json()["detail"]


# ---------------------------------------------------------------- logo


def test_logo_upload_serve_delete(client: TestClient, setup_done, cfg) -> None:
    h = {**admin_headers(setup_done), "Content-Type": "image/png"}
    assert client.get("/api/branding/logo").status_code == 404
    r = client.put("/api/admin/branding/logo", content=PNG, headers=h)
    assert r.status_code == 200 and r.json()["logo_version"] == 1
    got = client.get("/api/branding/logo")
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png"
    assert client.get("/api/system/info").json()["logo_version"] == 1
    r = client.delete("/api/admin/branding/logo", headers=admin_headers(setup_done))
    assert r.status_code == 200
    assert client.get("/api/branding/logo").status_code == 404


def test_logo_rejects_svg_and_large(client: TestClient, setup_done) -> None:
    h = {**admin_headers(setup_done), "Content-Type": "image/svg+xml"}
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    r = client.put("/api/admin/branding/logo", content=svg, headers=h)
    assert r.status_code == 415
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (600 * 1024)
    r = client.put("/api/admin/branding/logo", content=big, headers=h)
    assert r.status_code == 413
    _login_reader(client)
    r = client.put("/api/admin/branding/logo", content=PNG, headers=h)
    assert r.status_code == 403
