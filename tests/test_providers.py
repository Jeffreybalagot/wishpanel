"""Provider clients against mocked APIs shaped like the real ones (M2)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from app.providers.base import ProviderError
from app.providers.downloaders.kapowarr import KapowarrDownloader
from app.providers.downloaders.suwayomi import SuwayomiDownloader
from app.providers.library.kavita import KavitaLibraryChecker
from app.providers.metadata.comicvine import API_BASE, ComicVineMetadataProvider

KAV = "http://kavita.test:5000"
KAP = "http://kapowarr.test:5656"
SUW = "http://suwayomi.test:4567"

LIBS = [
    {"id": 3, "name": "Manga", "type": 0},
    {"id": 1, "name": "Comics", "type": 5},
    {"id": 2, "name": "Books", "type": 2},
]


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


# ---------------------------------------------------------------- Kavita


@respx.mock
async def test_kavita_libraries_with_header_key(http) -> None:
    route = respx.get(f"{KAV}/api/Library/libraries").respond(200, json=LIBS)
    libs = await KavitaLibraryChecker(http, KAV, "k3y").list_libraries()
    assert [(lib.id, lib.name, lib.kind) for lib in libs] == [
        ("2", "Books", "book"),
        ("1", "Comics", "comic"),
        ("3", "Manga", "manga"),
    ]
    assert route.calls.last.request.headers["x-api-key"] == "k3y"


@respx.mock
async def test_kavita_falls_back_to_plugin_auth(http) -> None:
    def libraries(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") == "Bearer jwt-1":
            return httpx.Response(200, json=LIBS)
        return httpx.Response(401)

    respx.get(f"{KAV}/api/Library/libraries").mock(side_effect=libraries)
    auth = respx.post(f"{KAV}/api/Plugin/authenticate").respond(
        200, json={"token": "jwt-1", "kavitaVersion": "0.7.14"}
    )
    checker = KavitaLibraryChecker(http, KAV, "old-opds-key")
    assert len(await checker.list_libraries()) == 3
    params = auth.calls.last.request.url.params
    assert params["apiKey"] == "old-opds-key" and params["pluginName"] == "WishPanel"
    assert checker.version == "0.7.14"


@respx.mock
async def test_kavita_bad_key(http) -> None:
    respx.get(f"{KAV}/api/Library/libraries").respond(401)
    respx.post(f"{KAV}/api/Plugin/authenticate").respond(401)
    with pytest.raises(ProviderError, match="rejected the API key"):
        await KavitaLibraryChecker(http, KAV, "nope").list_libraries()


@respx.mock
async def test_kavita_test_connection_messages(http) -> None:
    respx.get(f"{KAV}/api/health").respond(200, text="Ok")
    respx.get(f"{KAV}/api/Library/libraries").respond(200, json=LIBS)
    no_key = await KavitaLibraryChecker(http, KAV, "").test_connection()
    assert no_key.ok and "Add an API key" in no_key.message
    ok = await KavitaLibraryChecker(http, KAV, "k").test_connection()
    assert ok.ok and "3 libraries" in ok.message and "Comics" in ok.message

    respx.get("http://other.test/api/health").respond(404)
    bad = await KavitaLibraryChecker(http, "http://other.test", "k").test_connection()
    assert not bad.ok and "doesn't look like Kavita" in bad.message


@respx.mock
async def test_kavita_scan(http) -> None:
    route = respx.post(f"{KAV}/api/Library/scan").respond(200)
    await KavitaLibraryChecker(http, KAV, "k").trigger_scan("3")
    assert route.calls.last.request.url.params["libraryId"] == "3"


# ---------------------------------------------------------------- Kapowarr


@respx.mock
async def test_kapowarr_ok(http) -> None:
    about = respx.get(f"{KAP}/api/system/about").respond(
        200, json={"error": None, "result": {"version": "v1.2.0"}}
    )
    respx.get(f"{KAP}/api/rootfolder").respond(
        200,
        json={
            "error": None,
            "result": [
                {"id": 1, "folder": "/comics/", "size": {"total": 4e12, "used": 1e12, "free": 3e12}}
            ],
        },
    )
    kap = KapowarrDownloader(http, KAP + "/", "abc")
    res = await kap.test_connection()
    assert res.ok and "v1.2.0" in res.message and "1 root folder" in res.message
    assert about.calls.last.request.url.params["api_key"] == "abc"
    [folder] = await kap.list_root_folders()
    assert folder.id == "1" and folder.path.startswith("/comics/") and "GB free" in folder.path


@respx.mock
async def test_kapowarr_errors(http) -> None:
    respx.get(f"{KAP}/api/system/about").respond(401, json={"error": "ApiKeyInvalid", "result": {}})
    res = await KapowarrDownloader(http, KAP, "bad").test_connection()
    assert not res.ok and "rejected the API key" in res.message

    respx.get("http://notkap.test/api/system/about").respond(200, text="<html>hi</html>")
    res = await KapowarrDownloader(http, "http://notkap.test", "k").test_connection()
    assert not res.ok and "not with JSON" in res.message

    res = await KapowarrDownloader(http, KAP, "").test_connection()
    assert not res.ok and "API key is not set" in res.message


@respx.mock
async def test_kapowarr_no_root_folders(http) -> None:
    respx.get(f"{KAP}/api/system/about").respond(200, json={"error": None, "result": {}})
    respx.get(f"{KAP}/api/rootfolder").respond(200, json={"error": None, "result": []})
    res = await KapowarrDownloader(http, KAP, "k").test_connection()
    assert res.ok and "no root folders" in res.message


# ---------------------------------------------------------------- Suwayomi

SOURCES = {
    "data": {
        "sources": {
            "nodes": [
                {"id": "0", "name": "Local source", "lang": "localsourcelang", "displayName": "L"},
                {"id": "222", "name": "MangaDex", "lang": "en", "displayName": "MangaDex (EN)"},
                {"id": "111", "name": "Asura", "lang": "en", "displayName": "Asura Scans"},
            ]
        }
    }
}
ABOUT = {"data": {"aboutServer": {"name": "Suwayomi-Server", "version": "v2.4.2366"}}}


def _graphql(auth_check=None):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        if auth_check and not auth_check(request):
            return httpx.Response(401)
        q = json.loads(request.content)["query"]
        if "login" in q:
            return httpx.Response(200, json={"data": {"login": {"accessToken": "tok"}}})
        return httpx.Response(200, json=ABOUT if "aboutServer" in q else SOURCES)

    return handler


@respx.mock
async def test_suwayomi_no_auth(http) -> None:
    respx.post(f"{SUW}/api/graphql").mock(side_effect=_graphql())
    suw = SuwayomiDownloader(http, SUW)
    res = await suw.test_connection()
    assert res.ok and "v2.4.2366" in res.message and "2 sources" in res.message
    sources = await suw.list_sources()
    assert [(s.id, s.name, s.language) for s in sources] == [
        ("111", "Asura Scans", "en"),
        ("222", "MangaDex (EN)", "en"),
    ]


@respx.mock
async def test_suwayomi_basic_auth(http) -> None:
    expected = "Basic " + base64.b64encode(b"u:p").decode()
    respx.post(f"{SUW}/api/graphql").mock(
        side_effect=_graphql(lambda r: r.headers.get("authorization") == expected)
    )
    assert (await SuwayomiDownloader(http, SUW, "basic", "u", "p").test_connection()).ok
    res = await SuwayomiDownloader(http, SUW, "none").test_connection()
    assert not res.ok and "requires authentication" in res.message


@respx.mock
async def test_suwayomi_login_mode(http) -> None:
    def check(r: httpx.Request) -> bool:
        return "login" in r.content.decode() or r.headers.get("authorization") == "Bearer tok"

    route = respx.post(f"{SUW}/api/graphql").mock(side_effect=_graphql(check))
    suw = SuwayomiDownloader(http, SUW, "login", "u", "p")
    assert (await suw.test_connection()).ok
    login_calls = [c for c in route.calls if "login" in c.request.content.decode()]
    assert len(login_calls) == 1  # token reused for later queries


@respx.mock
async def test_suwayomi_graphql_error(http) -> None:
    respx.post(f"{SUW}/api/graphql").respond(200, json={"errors": [{"message": "boom"}]})
    res = await SuwayomiDownloader(http, SUW).test_connection()
    assert not res.ok and "boom" in res.message


# ---------------------------------------------------------------- ComicVine


@pytest.mark.parametrize(
    ("payload", "ok", "needle"),
    [
        ({"status_code": 1, "error": "OK"}, True, "accepted"),
        ({"status_code": 100, "error": "Invalid API Key"}, False, "invalid"),
        ({"status_code": 107, "error": "Rate limit"}, False, "rate-limiting"),
    ],
)
@respx.mock
async def test_comicvine(http, payload, ok, needle) -> None:
    route = respx.get(f"{API_BASE}/types/").respond(200, json=payload)
    res = await ComicVineMetadataProvider(http, "cvkey").test_connection()
    assert res.ok is ok and needle in res.message
    assert route.calls.last.request.url.params["api_key"] == "cvkey"


async def test_comicvine_no_key(http) -> None:
    res = await ComicVineMetadataProvider(http, "").test_connection()
    assert not res.ok and "not set" in res.message
