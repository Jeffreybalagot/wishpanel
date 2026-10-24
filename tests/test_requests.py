"""Requests (M3): creation rules, Kapowarr/Suwayomi hand-off, sync, admin actions."""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.services.requests import Rules, availability
from tests.conftest import admin_headers, db_rows

KAP = "http://kapowarr.test:5656"
SUW = "http://suwayomi.test:4567"
CV = "https://comicvine.gamespot.com/api"
AL = "https://graphql.anilist.co"


def cv_volume(vid: int, name: str = "Saga", publisher: str = "Image") -> dict:
    return {
        "id": vid,
        "name": name,
        "start_year": "2012",
        "publisher": {"name": publisher},
        "image": {"medium_url": f"https://img.test/{vid}.jpg"},
        "count_of_issues": 66,
        "deck": "Space opera.",
    }


def al_media(mid: int, title: str = "Solo Leveling", country: str = "KR", chapters=200) -> dict:
    return {
        "id": mid,
        "title": {"romaji": "Na Honjaman Level Up", "english": title, "native": "나 혼자만 레벨업"},
        "synonyms": ["Only I Level Up"],
        "coverImage": {"large": "https://img.test/al.jpg"},
        "startDate": {"year": 2018},
        "chapters": chapters,
        "status": "FINISHED",
        "countryOfOrigin": country,
        "isAdult": False,
        "format": "MANGA",
        "siteUrl": f"https://anilist.co/manga/{mid}",
        "description": "Hunters <br>and gates.",
    }


@pytest.fixture
def services(kavita: respx.MockRouter) -> respx.MockRouter:
    r = kavita
    r.get(url__startswith=f"{CV}/volume/4050-").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "status_code": 1,
                "results": cv_volume(int(req.url.path.rstrip("/").split("-")[-1])),
            },
        )
    )
    r.get(f"{CV}/search/").respond(
        200,
        json={"status_code": 1, "results": [cv_volume(1), cv_volume(2, "Saga", "Panini Comics")]},
    )
    r.get(f"{KAP}/api/rootfolder").respond(
        200, json={"error": None, "result": [{"id": 1, "folder": "/comics/"}]}
    )
    return r


def configure(client: TestClient, setup: dict, **values) -> None:
    base = {
        "comicvine.api_key": "cvkey",
        "kapowarr.url": KAP,
        "kapowarr.api_key": "kapkey",
        "kapowarr.root_folder_id": "1",
        "suwayomi.url": SUW,
        "suwayomi.sources": ["11", "22"],
    }
    base.update(values)
    r = client.put("/api/admin/settings", json={"values": base}, headers=admin_headers(setup))
    assert r.status_code == 200, r.text


def wait_status(client: TestClient, rid: int, not_in=("approved",), timeout=5.0) -> dict:
    deadline = time.time() + timeout
    while True:
        data = client.get(f"/api/requests/{rid}").json()
        if data["status"] not in not_in or time.time() > deadline:
            return data
        time.sleep(0.05)


def login(client: TestClient, user: str, pw: str) -> dict:
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def test_partial_rule() -> None:
    rules = Rules(5, 80, 3)
    assert availability(3, 223, rules) == "partial"
    assert availability(220, 223, rules) == "in_library"  # gap < 3
    assert availability(4, 4, rules) == "in_library"
    assert availability(2, 4, rules) == "in_library"  # too short to call partial
    assert availability(80, 100, rules) == "in_library"  # at the 80 % line


def test_comic_request_goes_to_kapowarr(client: TestClient, setup_done, services) -> None:
    configure(client, setup_done)
    add = services.post(f"{KAP}/api/volumes").respond(
        201, json={"error": None, "result": {"id": 7}}
    )
    services.get(f"{KAP}/api/volumes/7").respond(
        200, json={"error": None, "result": {"id": 7, "issue_count": 66, "issues_downloaded": 0}}
    )
    r = client.post(
        "/api/requests",
        json={"media_type": "comic", "provider": "comicvine", "external_id": "1"},
        headers=admin_headers(setup_done),
    )
    assert r.status_code == 201, r.text
    req = wait_status(client, r.json()["id"])
    assert req["status"] == "added" and req["downloader_ref"] == "7"
    sent = json.loads(add.calls.last.request.content)
    assert (
        sent["comicvine_id"] == 1
        and sent["root_folder_id"] == 1
        and sent["monitor_new_issues"] is True
    )
    assert "api_key=kapkey" in str(add.calls.last.request.url)

    # Duplicate is refused with the existing request's id.
    dup = client.post(
        "/api/requests",
        json={"media_type": "comic", "provider": "comicvine", "external_id": "1"},
        headers=admin_headers(setup_done),
    )
    assert dup.status_code == 409 and dup.json()["detail"]["request_id"] == req["id"]

    # Availability: 60/66 is in the library (gap 6 >= 3 but 90 % >= 80 %).
    services.get(f"{KAP}/api/volumes/7").respond(
        200, json={"error": None, "result": {"id": 7, "issue_count": 66, "issues_downloaded": 60}}
    )
    scan = services.post("http://kavita.test:5000/api/Library/scan").respond(200)
    r = client.post(f"/api/requests/{req['id']}/refresh", headers=admin_headers(setup_done))
    assert r.json()["status"] == "in_library" and r.json()["progress"] == "60 of 66 issues"
    assert not scan.called  # no library mapped, no scan


def test_kapowarr_already_added_and_down(client: TestClient, setup_done, services) -> None:
    configure(client, setup_done)
    services.post(f"{KAP}/api/volumes").respond(
        400, json={"error": "VolumeAlreadyAdded", "result": {"comicvine_id": 1, "volume_id": 3}}
    )
    services.get(f"{KAP}/api/volumes/3").respond(
        200, json={"error": None, "result": {"id": 3, "issue_count": 10, "issues_downloaded": 10}}
    )
    h = admin_headers(setup_done)
    rid = client.post(
        "/api/requests",
        json={"media_type": "comic", "provider": "comicvine", "external_id": "1"},
        headers=h,
    ).json()["id"]
    req = wait_status(client, rid)
    assert req["status"] == "in_library" and req["downloader_ref"] == "3"

    services.post(f"{KAP}/api/volumes").mock(side_effect=httpx.ConnectError("down"))
    rid = client.post(
        "/api/requests",
        json={"media_type": "comic", "provider": "comicvine", "external_id": "2"},
        headers=h,
    ).json()["id"]
    req = wait_status(client, rid)
    assert req["status"] == "waiting_for_downloader" and "retry" in req["status_message"]


def test_limits_approval_and_permissions(client: TestClient, setup_done, services) -> None:
    configure(client, setup_done, **{"requests.daily_limit": 1, "requests.auto_approve": False})
    reader = login(client, "reader", "readerpw")
    h = {"X-CSRF-Token": reader["csrf_token"]}
    r = client.post(
        "/api/requests",
        json={"media_type": "comic", "provider": "comicvine", "external_id": "5"},
        headers=h,
    )
    assert r.status_code == 201 and r.json()["status"] == "pending"
    rid = r.json()["id"]
    r = client.post(
        "/api/requests",
        json={"media_type": "comic", "provider": "comicvine", "external_id": "6"},
        headers=h,
    )
    assert r.status_code == 429 and "1 requests" in r.json()["detail"]
    assert client.get("/api/requests/quota").json()["remaining"] == 0
    # Readers can't see everyone's requests or approve.
    assert client.get("/api/requests?scope=all").status_code == 403
    assert client.post(f"/api/requests/{rid}/approve", headers=h).status_code == 403
    # Mine shows up; reader may cancel a pending one.
    mine = client.get("/api/requests").json()
    assert mine["total"] == 1 and mine["requests"][0]["mine"]

    admin = login(client, "admin", "adminpw")
    ah = {"X-CSRF-Token": admin["csrf_token"]}
    all_ = client.get("/api/requests?scope=all").json()
    assert all_["requests"][0]["username"] == "reader"
    r = client.post(f"/api/requests/{rid}/deny", json={"reason": "not on GetComics"}, headers=ah)
    assert r.json()["status"] == "denied" and "GetComics" in r.json()["status_message"]
    audit = db_rows(
        client.app.state.ctx.config.db_path,
        "SELECT action FROM audit_log WHERE action LIKE 'request.%'",
    )
    assert ("request.deny",) in audit and ("request.create",) in audit


def test_media_type_permissions(client: TestClient, setup_done, services) -> None:
    configure(client, setup_done, **{"media.manga.enabled": False})
    h = admin_headers(setup_done)
    r = client.post(
        "/api/requests",
        json={"media_type": "manga", "provider": "anilist", "external_id": "1"},
        headers=h,
    )
    assert r.status_code == 403 and "turned off" in r.json()["detail"]
    users = client.get("/api/admin/users").json()
    reader_id = None
    login(client, "reader", "readerpw")
    admin = login(client, "admin", "adminpw")
    users = client.get("/api/admin/users").json()
    reader_id = next(u["id"] for u in users if u["username"] == "reader")
    client.patch(
        f"/api/admin/users/{reader_id}",
        json={"allowed_media_types": ["manga"]},
        headers={"X-CSRF-Token": admin["csrf_token"]},
    )
    reader = login(client, "reader", "readerpw")
    r = client.post(
        "/api/requests",
        json={"media_type": "comic", "provider": "comicvine", "external_id": "1"},
        headers={"X-CSRF-Token": reader["csrf_token"]},
    )
    assert r.status_code == 403 and "can't request comic" in r.json()["detail"]
    # Provider must match the media type.
    r = client.post(
        "/api/requests",
        json={"media_type": "manga", "provider": "comicvine", "external_id": "1"},
        headers={"X-CSRF-Token": reader["csrf_token"]},
    )
    assert r.status_code in (400, 403)


def _suwayomi(router: respx.MockRouter, results: dict[str, list[dict]]) -> list[dict]:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        q, v = body["query"], body.get("variables") or {}
        calls.append(body)
        if "fetchSourceManga" in q:
            return httpx.Response(
                200, json={"data": {"fetchSourceManga": {"mangas": results.get(v["s"], [])}}}
            )
        if "sources" in q:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "sources": {
                            "nodes": [
                                {
                                    "id": "11",
                                    "name": "Asura",
                                    "lang": "en",
                                    "displayName": "Asura Scans",
                                },
                                {
                                    "id": "22",
                                    "name": "MangaDex",
                                    "lang": "en",
                                    "displayName": "MangaDex (EN)",
                                },
                            ]
                        }
                    }
                },
            )
        if "updateManga" in q:
            return httpx.Response(
                200, json={"data": {"updateManga": {"manga": {"id": v["id"], "inLibrary": True}}}}
            )
        if "fetchChapters" in q:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "fetchChapters": {
                            "chapters": [
                                {"id": 100 + i, "chapterNumber": i, "isDownloaded": i < 2}
                                for i in range(5)
                            ]
                        }
                    }
                },
            )
        if "enqueueChapterDownloads" in q:
            return httpx.Response(
                200, json={"data": {"enqueueChapterDownloads": {"clientMutationId": None}}}
            )
        if "manga(id" in q:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "manga": {
                            "id": v["id"],
                            "downloadCount": 5,
                            "chapters": {
                                "totalCount": 5,
                                "nodes": [
                                    {"id": 100 + i, "chapterNumber": i, "isDownloaded": True}
                                    for i in range(5)
                                ],
                            },
                        }
                    }
                },
            )
        return httpx.Response(200, json={"data": {}})

    router.post(f"{SUW}/api/graphql").mock(side_effect=handler)
    return calls


def test_manga_request_matches_priority_source(client: TestClient, setup_done, services) -> None:
    configure(client, setup_done)
    services.post(AL).respond(200, json={"data": {"Media": al_media(105398)}})
    calls = _suwayomi(
        services,
        {
            "11": [
                {"id": 1, "title": "Solo Leveling (Doujinshi)"},
                {"id": 2, "title": "Solo Leveling [Official]"},
            ],
            "22": [{"id": 3, "title": "Solo Leveling"}],
        },
    )
    h = admin_headers(setup_done)
    r = client.post(
        "/api/requests",
        json={"media_type": "manhwa", "provider": "anilist", "external_id": "105398"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    req = wait_status(client, r.json()["id"])
    # Source 11 is first priority: its plain-ish edition beats the threshold; doujinshi is skipped.
    assert req["status"] == "downloading", req
    assert (
        req["source_id"] == "11"
        and req["downloader_ref"] == "2"
        and req["source_name"] == "Asura Scans"
    )
    enqueue = next(c for c in calls if "enqueueChapterDownloads" in c["query"])
    assert enqueue["variables"]["ids"] == [102, 103, 104]
    assert req["progress"] == "2 of ~200 chapters"

    # Same AniList id requested again (even as manga) -> duplicate.
    r = client.post(
        "/api/requests",
        json={"media_type": "manhwa", "provider": "anilist", "external_id": "105398"},
        headers=h,
    )
    assert r.status_code == 409


def test_manga_not_found_waits_and_admin_picks_source(
    client: TestClient, setup_done, services
) -> None:
    configure(client, setup_done)
    services.post(AL).respond(200, json={"data": {"Media": al_media(1, "Obscure Title", "JP")}})
    _suwayomi(services, {"11": [{"id": 9, "title": "Something Else"}], "22": []})
    h = admin_headers(setup_done)
    rid = client.post(
        "/api/requests",
        json={"media_type": "manga", "provider": "anilist", "external_id": "1"},
        headers=h,
    ).json()["id"]
    req = wait_status(client, rid)
    assert req["status"] == "waiting_for_source" and req["next_attempt_at"]

    cands = client.get(f"/api/requests/{rid}/candidates?q=Something").json()
    assert cands["candidates"][0]["manga_id"] == 9 and cands["sources"][0]["name"] == "Asura Scans"
    r = client.post(
        f"/api/requests/{rid}/source", json={"source_id": "11", "manga_id": 9}, headers=h
    )
    assert r.status_code == 200 and r.json()["status"] == "downloading"


def test_no_sources_and_unconfigured_downloader(client: TestClient, setup_done, services) -> None:
    configure(client, setup_done, **{"suwayomi.sources": [], "kapowarr.url": ""})
    services.post(AL).respond(200, json={"data": {"Media": al_media(1, "X", "JP")}})
    h = admin_headers(setup_done)
    rid = client.post(
        "/api/requests",
        json={"media_type": "manga", "provider": "anilist", "external_id": "1"},
        headers=h,
    ).json()["id"]
    assert wait_status(client, rid)["status"] == "waiting_for_source"
    rid = client.post(
        "/api/requests",
        json={"media_type": "comic", "provider": "comicvine", "external_id": "1"},
        headers=h,
    ).json()["id"]
    req = wait_status(client, rid)
    assert req["status"] == "waiting_for_downloader" and "Kapowarr" in req["status_message"]


def test_search_annotates_requests(client: TestClient, setup_done, services) -> None:
    configure(client, setup_done)
    services.post(f"{KAP}/api/volumes").respond(201, json={"error": None, "result": {"id": 7}})
    services.get(f"{KAP}/api/volumes/7").respond(
        200, json={"error": None, "result": {"id": 7, "issue_count": 66, "issues_downloaded": 0}}
    )
    h = admin_headers(setup_done)
    rid = client.post(
        "/api/requests",
        json={"media_type": "comic", "provider": "comicvine", "external_id": "1"},
        headers=h,
    ).json()["id"]
    wait_status(client, rid)
    res = client.get("/api/search?media_type=comic&q=saga").json()["results"]
    # Panini (blocked publisher) is filtered out; the Image volume is marked requested.
    assert [r["external_id"] for r in res] == ["1"]
    assert res[0]["request"]["status"] == "added" and res[0]["key"] == "cv:1"


def test_retry_job_and_sync_job(client: TestClient, setup_done, services) -> None:
    configure(client, setup_done, **{"kapowarr.url": ""})
    h = admin_headers(setup_done)
    rid = client.post(
        "/api/requests",
        json={"media_type": "comic", "provider": "comicvine", "external_id": "1"},
        headers=h,
    ).json()["id"]
    assert wait_status(client, rid)["status"] == "waiting_for_downloader"
    configure(client, setup_done)
    services.post(f"{KAP}/api/volumes").respond(201, json={"error": None, "result": {"id": 7}})
    services.get(f"{KAP}/api/volumes/7").respond(
        200, json={"error": None, "result": {"id": 7, "issue_count": 4, "issues_downloaded": 4}}
    )
    # Make it due now and run the retry job directly.
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(client.app.state.ctx.config.db_path)) as con:
        con.execute("UPDATE requests SET next_attempt_at = NULL")
        con.commit()
    r = client.post("/api/admin/jobs/retry/run", headers=h)
    assert r.status_code == 200
    req = wait_status(client, rid, not_in=("waiting_for_downloader", "approved"))
    assert req["status"] == "in_library"
    jobs = {j["id"]: j for j in client.get("/api/admin/jobs").json()["jobs"]}
    for _ in range(50):
        if jobs["retry"]["last_status"]:
            break
        time.sleep(0.05)
        jobs = {j["id"]: j for j in client.get("/api/admin/jobs").json()["jobs"]}
    assert jobs["retry"]["last_status"] == "ok" and "retried 1" in jobs["retry"]["last_message"]
    assert client.post("/api/admin/jobs/nope/run", headers=h).status_code == 404
