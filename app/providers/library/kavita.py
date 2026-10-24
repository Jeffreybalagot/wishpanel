"""Kavita as a library: list libraries, trigger scans (M2). Presence checks arrive in M3.

Auth: Kavita 0.8+ "Auth Keys" go in the ``x-api-key`` header. Older servers only
accept the OPDS API key via ``POST /api/Plugin/authenticate``, which returns a
JWT; if the header is rejected we fall back to that flow automatically.
"""

from __future__ import annotations

import logging

import httpx

from app.core.http import ExternalServiceError, request_with_retry
from app.domain import ConnectionTestResult, Library
from app.providers._util import json_body, status_message
from app.providers.base import ProviderError

log = logging.getLogger(__name__)

SERVICE = "Kavita"
PLUGIN_NAME = "WishPanel"

# Kavita LibraryType enum (openapi.json, 0.9.x)
LIBRARY_KINDS = {0: "manga", 1: "comic", 2: "book", 3: "image", 4: "lightnovel", 5: "comic"}


class KavitaLibraryChecker:
    name = "kavita"

    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._bearer: str | None = None
        self.version: str | None = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def _plugin_token(self) -> str:
        resp = await request_with_retry(
            self.client,
            SERVICE,
            "POST",
            self._url("/api/Plugin/authenticate"),
            params={"apiKey": self.api_key, "pluginName": PLUGIN_NAME},
            retries=1,
        )
        if resp.status_code in (400, 401, 403):
            raise ProviderError("Kavita rejected the API key.")
        if resp.status_code != 200:
            raise ProviderError(status_message(SERVICE, resp.status_code))
        body = json_body(resp, SERVICE)
        token = body.get("token") if isinstance(body, dict) else None
        if not token:
            raise ProviderError("Kavita accepted the API key but returned no session token.")
        self.version = body.get("kavitaVersion") or self.version
        return str(token)

    async def _get(self, path: str, **kwargs) -> httpx.Response:  # type: ignore[no-untyped-def]
        return await self._request("GET", path, **kwargs)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:  # type: ignore[no-untyped-def]
        if not self.base_url:
            raise ProviderError("Kavita URL is not set.")
        if not self.api_key:
            raise ProviderError("Kavita API key is not set.")
        headers = (
            {"Authorization": f"Bearer {self._bearer}"}
            if self._bearer
            else {"x-api-key": self.api_key}
        )
        try:
            resp = await request_with_retry(
                self.client, SERVICE, method, self._url(path), headers=headers, retries=1, **kwargs
            )
            if resp.status_code == 401 and not self._bearer:
                # Older Kavita: header auth unsupported -> exchange the key for a JWT.
                self._bearer = await self._plugin_token()
                resp = await request_with_retry(
                    self.client,
                    SERVICE,
                    method,
                    self._url(path),
                    headers={"Authorization": f"Bearer {self._bearer}"},
                    retries=1,
                    **kwargs,
                )
        except ExternalServiceError as exc:
            raise ProviderError(f"Could not reach Kavita: {exc.message}.", retryable=True) from exc
        if resp.status_code in (401, 403):
            raise ProviderError("Kavita rejected the API key.")
        return resp

    async def list_libraries(self) -> list[Library]:
        resp = await self._get("/api/Library/libraries")
        if resp.status_code != 200:
            raise ProviderError(status_message(SERVICE, resp.status_code))
        body = json_body(resp, SERVICE)
        if not isinstance(body, list):
            raise ProviderError("Kavita returned an unexpected library list.")
        libs = [
            Library(
                id=str(item.get("id")),
                name=str(item.get("name") or f"Library {item.get('id')}"),
                kind=LIBRARY_KINDS.get(item.get("type")),
            )
            for item in body
            if isinstance(item, dict) and item.get("id") is not None
        ]
        return sorted(libs, key=lambda lib: lib.name.lower())

    async def trigger_scan(self, library_id: str) -> None:
        resp = await self._request(
            "POST", "/api/Library/scan", params={"libraryId": library_id, "force": "false"}
        )
        if resp.status_code not in (200, 202, 204):
            raise ProviderError(status_message(SERVICE, resp.status_code))

    async def test_connection(self) -> ConnectionTestResult:
        if not self.base_url:
            return ConnectionTestResult(False, "Kavita URL is not set.")
        try:
            health = await request_with_retry(
                self.client, SERVICE, "GET", self._url("/api/health"), retries=0
            )
        except ExternalServiceError as exc:
            return ConnectionTestResult(False, f"Could not reach Kavita: {exc.message}.")
        if health.status_code != 200:
            return ConnectionTestResult(
                False,
                "Something answered at that URL, but it doesn't look like Kavita "
                "(no /api/health). Check the address and port.",
            )
        if not self.api_key:
            return ConnectionTestResult(
                True,
                "Kavita is reachable. Add an API key to enable library lists and scans.",
            )
        try:
            libs = await self.list_libraries()
        except ProviderError as exc:
            return ConnectionTestResult(False, exc.message)
        names = ", ".join(lib.name for lib in libs[:5]) + ("…" if len(libs) > 5 else "")
        msg = f"Connected to Kavita — {len(libs)} librar{'y' if len(libs) == 1 else 'ies'} visible"
        return ConnectionTestResult(
            True, f"{msg}: {names}." if libs else f"{msg}.", {"libraries": str(len(libs))}
        )
