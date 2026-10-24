"""Suwayomi downloader — connection test and source list (M2). Adding manga arrives in M3.

Suwayomi-Server 2.x exposes GraphQL at ``/api/graphql``. Auth modes (server
setting ``authMode``): NONE, BASIC_AUTH (HTTP Basic), SIMPLE_LOGIN / UI_LOGIN
(``login`` mutation returning a JWT ``accessToken``).
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from app.core.http import ExternalServiceError, request_with_retry
from app.domain import ConnectionTestResult, Source
from app.providers._util import json_body, status_message
from app.providers.base import ProviderError

SERVICE = "Suwayomi"
LOCAL_SOURCE_ID = "0"

_ABOUT = "query { aboutServer { name version buildType } }"
_SOURCES = "query { sources { nodes { id name lang displayName } } }"
_LOGIN = (
    "mutation Login($u: String!, $p: String!) "
    "{ login(input: {username: $u, password: $p}) { accessToken } }"
)


class SuwayomiDownloader:
    name = "suwayomi"

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        auth_mode: str = "none",
        username: str = "",
        password: str = "",
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.auth_mode = auth_mode
        self.username = username
        self.password = password
        self._token: str | None = None

    async def _post(self, query: str, variables: dict[str, Any], headers: dict[str, str]):  # type: ignore[no-untyped-def]
        try:
            return await request_with_retry(
                self.client,
                SERVICE,
                "POST",
                f"{self.base_url}/api/graphql",
                json={"query": query, "variables": variables},
                headers=headers,
                retries=1,
            )
        except ExternalServiceError as exc:
            raise ProviderError(
                f"Could not reach Suwayomi: {exc.message}.", retryable=True
            ) from exc

    async def _auth_headers(self) -> dict[str, str]:
        if self.auth_mode == "basic":
            if not self.username:
                raise ProviderError("Suwayomi basic auth needs a username and password.")
            raw = f"{self.username}:{self.password}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        if self.auth_mode == "login":
            if not self._token:
                if not self.username:
                    raise ProviderError("Suwayomi login needs a username and password.")
                resp = await self._post(_LOGIN, {"u": self.username, "p": self.password}, {})
                data = self._data(resp)
                token = (data.get("login") or {}).get("accessToken")
                if not token:
                    raise ProviderError("Suwayomi did not accept the username or password.")
                self._token = str(token)
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _data(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code == 401:
            hint = (
                " Set the authentication mode to match Suwayomi's."
                if self.auth_mode == "none"
                else ""
            )
            raise ProviderError(f"Suwayomi requires authentication.{hint}")
        if resp.status_code == 404:
            raise ProviderError(
                "No GraphQL API at that URL — check the address (Suwayomi 2.x is required)."
            )
        if resp.status_code != 200:
            raise ProviderError(status_message(SERVICE, resp.status_code))
        body = json_body(resp, SERVICE)
        if not isinstance(body, dict):
            raise ProviderError("Suwayomi returned an unexpected response.")
        if body.get("errors"):
            first = body["errors"][0] if isinstance(body["errors"], list) else {}
            msg = str(first.get("message", "unknown error"))[:160]
            raise ProviderError(f"Suwayomi returned an error: {msg}")
        return body.get("data") or {}

    async def _query(self, query: str) -> dict[str, Any]:
        if not self.base_url:
            raise ProviderError("Suwayomi URL is not set.")
        resp = await self._post(query, {}, await self._auth_headers())
        return self._data(resp)

    async def list_sources(self) -> list[Source]:
        data = await self._query(_SOURCES)
        nodes = ((data.get("sources") or {}).get("nodes")) or []
        sources = [
            Source(
                id=str(n["id"]),
                name=str(n.get("displayName") or n.get("name") or n["id"]),
                language=n.get("lang"),
            )
            for n in nodes
            if isinstance(n, dict) and n.get("id") is not None and str(n["id"]) != LOCAL_SOURCE_ID
        ]
        return sorted(sources, key=lambda s: (s.name.lower(), s.language or ""))

    async def list_root_folders(self) -> list:
        return []

    async def test_connection(self) -> ConnectionTestResult:
        try:
            about = (await self._query(_ABOUT)).get("aboutServer") or {}
            sources = await self.list_sources()
        except ProviderError as exc:
            return ConnectionTestResult(False, exc.message)
        version = about.get("version")
        msg = f"Connected to {about.get('name') or 'Suwayomi'}{f' {version}' if version else ''}"
        if not sources:
            return ConnectionTestResult(
                True, f"{msg}, but no sources are installed — add extensions in Suwayomi first."
            )
        return ConnectionTestResult(
            True,
            f"{msg} — {len(sources)} sources installed.",
            {"version": str(version or ""), "sources": str(len(sources))},
        )
