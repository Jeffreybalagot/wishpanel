"""M5: v1 importer, settings export/import, logs, backup, about, audit, update check."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import respx
from fastapi.testclient import TestClient

from tests.conftest import admin_headers, db_rows

V1_SCHEMA = """
CREATE TABLE requests (id INTEGER PRIMARY KEY, username TEXT, comicvine_id INTEGER, title TEXT, year TEXT,
  publisher TEXT, cover TEXT, kapowarr_id INTEGER, status TEXT, note TEXT, created REAL);
CREATE TABLE manga_requests (id INTEGER PRIMARY KEY, username TEXT, key TEXT, title TEXT, alt TEXT, year TEXT,
  kind TEXT, cover TEXT, suwayomi_id INTEGER, status TEXT, note TEXT, created REAL, tried REAL, known_ch INTEGER);
CREATE TABLE cbh_titles (key TEXT PRIMARY KEY, title TEXT, status TEXT, priority REAL, hint INTEGER,
  comicvine_id INTEGER, name TEXT, year TEXT, publisher TEXT, issues INTEGER, cover TEXT, score REAL,
  tries INTEGER, updated REAL, first_seen REAL);
"""


def make_v1(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con:
        con.executescript(V1_SCHEMA)
        con.execute(
            "INSERT INTO requests VALUES (1,'Admin',4050,'Saga','2012','Image','c',7,'added','',1700000000)"
        )
        con.execute(
            "INSERT INTO requests VALUES (2,'newbie',4051,'Paper Girls','2015','Image','c',8,'added','',1700000100)"
        )
        con.execute(
            "INSERT INTO requests VALUES (3,'newbie',NULL,'Broken','','','',NULL,'added','',1700000100)"
        )
        con.execute(
            "INSERT INTO manga_requests VALUES (1,'newbie','al:105398','Solo Leveling','[\"Only I Level Up\"]',"
            "'2018','manhwa','c',42,'added','',1700000200,NULL,12)"
        )
        con.execute(
            "INSERT INTO manga_requests VALUES (2,'newbie','al:5','Missing','','2018','manhwa','c',NULL,"
            "'failed','not found',1700000300,NULL,NULL)"
        )
        con.execute(
            "INSERT INTO cbh_titles VALUES ('k','Saga Vol. 1','ok',1,2012,4050,'Saga','2012','Image',66,"
            "'c',110,1,0,0)"
        )
        con.execute(
            "INSERT INTO cbh_titles VALUES ('k2','Nope','miss',1,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,3,0,0)"
        )
        con.commit()
    return path


def test_import_mounted_v1(client: TestClient, setup_done, tmp_path: Path) -> None:
    h = admin_headers(setup_done)
    st = client.get("/api/admin/import/v1").json()
    assert st["mounted"] is False
    assert client.post("/api/admin/import/v1/mounted", headers=h).status_code == 404

    make_v1(client.app.state.ctx.config.import_dir / "requests.db")
    assert client.get("/api/admin/import/v1").json()["mounted"] is True
    r = client.post("/api/admin/import/v1/mounted", headers=h)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["comics"] == 2 and res["manga"] == 2 and res["users_created"] == ["newbie"]
    assert res["matches"] == 2 and len(res["warnings"]) == 1
    # Idempotent.
    again = client.post("/api/admin/import/v1/mounted", headers=h).json()
    assert again["comics"] == 0 and again["skipped"] == 4

    reqs = client.get("/api/requests?scope=all").json()["requests"]
    by_title = {r["title"]: r for r in reqs}
    assert by_title["Saga"]["username"] == "admin" and by_title["Saga"]["status"] == "added"
    assert by_title["Saga"]["downloader_ref"] == "7" and by_title["Saga"]["imported_from"] == "v1"
    assert by_title["Solo Leveling"]["status"] == "downloading"
    assert by_title["Solo Leveling"]["alt_titles"] == ["Only I Level Up"]
    assert by_title["Missing"]["status"] == "waiting_for_source"
    # Imports don't use up anyone's daily quota.
    assert client.get("/api/requests/quota").json()["used"] == 0
    cache = db_rows(
        client.app.state.ctx.config.db_path,
        "SELECT title, status, comicvine_id FROM match_cache ORDER BY title",
    )
    assert cache == [("Nope", "miss", None), ("Saga Vol. 1", "ok", 4050)]
    assert client.get("/api/admin/import/v1").json()["last_result"]["summary"].startswith("0 comic")


def test_import_upload_and_placeholder_relinks(
    client: TestClient, setup_done, tmp_path: Path, kavita
) -> None:
    h = admin_headers(setup_done)
    data = make_v1(tmp_path / "up" / "requests.db").read_bytes()
    r = client.put("/api/admin/import/v1/upload", content=data, headers=h)
    assert r.status_code == 200 and r.json()["source"] == "upload"
    assert (
        client.put("/api/admin/import/v1/upload", content=b"x" * 200, headers=h).status_code == 422
    )
    # "newbie" signs in to Kavita for the first time: the placeholder is re-keyed, requests stay theirs.
    from tests.conftest import USERS

    USERS["newbie"] = ("pw", ["Pleb"], 9)
    try:
        client.cookies.clear()
        me = client.post("/api/auth/login", json={"username": "newbie", "password": "pw"}).json()
        mine = client.get("/api/requests").json()
        assert mine["total"] == 3 and me["username"] == "newbie"
    finally:
        del USERS["newbie"]


def test_settings_export_import(client: TestClient, setup_done) -> None:
    h = admin_headers(setup_done)
    client.put(
        "/api/admin/settings",
        json={"values": {"general.site_name": "Comics!", "kapowarr.api_key": "secret"}},
        headers=h,
    )
    exp = client.get("/api/admin/settings/export")
    assert "attachment" in exp.headers["content-disposition"]
    data = exp.json()
    assert (
        data["values"]["general.site_name"] == "Comics!"
        and "kapowarr.api_key" not in data["values"]
    )
    data["values"]["general.site_name"] = "Imported"
    data["values"]["setup.completed"] = False  # not editable -> ignored
    r = client.post("/api/admin/settings/import", content=json.dumps(data), headers=h)
    assert r.status_code == 200 and r.json()["changed"] == ["general.site_name"]
    assert "setup.completed" in r.json()["ignored"]
    data["values"]["requests.daily_limit"] = "lots"
    r = client.post("/api/admin/settings/import", content=json.dumps(data), headers=h)
    assert r.status_code == 422 and r.json()["detail"]["key"] == "requests.daily_limit"
    assert client.post("/api/admin/settings/import", content=b"{}", headers=h).status_code == 422


def test_about_backup_audit_logs(client: TestClient, setup_done) -> None:
    about = client.get("/api/admin/about").json()
    assert about["version"] and about["users"] == 1
    b = client.get("/api/admin/backup")
    assert b.status_code == 200 and b.content[:15] == b"SQLite format 3"
    entries = client.get("/api/admin/audit").json()["entries"]
    assert entries[0]["action"] == "backup.download"
    log_dir = client.app.state.ctx.config.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "wishpanel.log").write_text(
        json.dumps({"ts": "t", "level": "debug", "msg": "noise"})
        + "\n"
        + json.dumps({"ts": "t", "level": "error", "msg": "Kapowarr down"})
        + "\nnot json\n"
    )
    logs = client.get("/api/admin/logs?level=info").json()["entries"]
    assert [e["msg"] for e in logs] == ["Kapowarr down"]
    assert client.get("/api/admin/logs?level=debug&q=noise").json()["entries"][0]["msg"] == "noise"


def test_update_check(client: TestClient, setup_done) -> None:
    with respx.mock(assert_all_called=False) as r:
        r.get("https://api.github.com/repos/Jeffreybalagot/wishpanel/releases/latest").respond(
            200, json={"tag_name": "v99.0.0", "html_url": "https://github.com/x"}
        )
        res = client.get("/api/admin/update-check").json()
    assert res["update_available"] is True and res["latest"] == "99.0.0"


def test_reader_cannot_use_admin_maintenance(client: TestClient, setup_done) -> None:
    client.cookies.clear()
    me = client.post("/api/auth/login", json={"username": "reader", "password": "readerpw"}).json()
    h = {"X-CSRF-Token": me["csrf_token"]}
    for path in (
        "/api/admin/jobs",
        "/api/admin/logs",
        "/api/admin/backup",
        "/api/admin/settings/export",
        "/api/admin/audit",
        "/api/admin/import/v1",
        "/api/admin/discover/status",
    ):
        assert client.get(path).status_code == 403, path
    assert client.post("/api/admin/import/v1/mounted", headers=h).status_code == 403
    assert client.post("/api/admin/jobs/retry/run", headers=h).status_code == 403
