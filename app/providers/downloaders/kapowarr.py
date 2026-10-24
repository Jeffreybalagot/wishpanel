"""Kapowarr downloader — connection test and root folders (M2). Adding volumes arrives in M3.

API (Kapowarr ``frontend/api.py``): every route is under ``/api`` and is authorised
by an ``api_key`` query parameter; responses are ``{"error": str|None, "result": ...}``
and a bad key returns HTTP 401 ``{"error": "ApiKeyInvalid"}``.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.http import ExternalServiceError, request_with_retry
from app.domain import ConnectionTestResult, Folder
from app.providers._util import json_body, status_message
from app.providers.base import ProviderError

SERVICE = "Kapowarr"


def _gib(n: Any) -> str:
    try:
        return f"{int(n) / 1024**3:,.0f} GB"
    except (TypeError, ValueError):
        return "?"


class KapowarrDownloader:
    name = "kapowarr"

    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def _api(self, path: str) -> Any:
        if not self.base_url:
            raise ProviderError("Kapowarr URL is not set.")
        if not self.api_key:
            raise ProviderError("Kapowarr API key is not set.")
        try:
            resp = await request_with_retry(
                self.client,
                SERVICE,
                "GET",
                f"{self.base_url}/api{path}",
                params={"api_key": self.api_key},
                retries=1,
            )
        except ExternalServiceError as exc:
            raise ProviderError(
                f"Could not reach Kapowarr: {exc.message}.", retryable=True
            ) from exc
        if resp.status_code == 401:
            raise ProviderError(
                "Kapowarr rejected the API key. Copy it from Kapowarr → Settings → General."
            )
        if resp.status_code != 200:
            raise ProviderError(status_message(SERVICE, resp.status_code))
        body = json_body(resp, SERVICE)
        if not isinstance(body, dict) or "result" not in body:
            raise ProviderError("That doesn't look like Kapowarr's API. Check the URL.")
        if body.get("error"):
            raise ProviderError(f"Kapowarr returned an error: {body['error']}.")
        return body["result"]

    async def list_root_folders(self) -> list[Folder]:
        result = await self._api("/rootfolder")
        if not isinstance(result, list):
            raise ProviderError("Kapowarr returned an unexpected root folder list.")
        folders = []
        for rf in result:
            if not isinstance(rf, dict) or rf.get("id") is None:
                continue
            size = rf.get("size") if isinstance(rf.get("size"), dict) else None
            label = str(rf.get("folder") or rf["id"])
            if size:
                label = f"{label}  ({_gib(size.get('free'))} free)"
            folders.append(Folder(id=str(rf["id"]), path=label))
        return folders

    async def list_sources(self) -> list:
        return []

    async def test_connection(self) -> ConnectionTestResult:
        try:
            about = await self._api("/system/about")
            folders = await self.list_root_folders()
        except ProviderError as exc:
            return ConnectionTestResult(False, exc.message)
        version = about.get("version") if isinstance(about, dict) else None
        msg = f"Connected to Kapowarr{f' {version}' if version else ''}"
        if not folders:
            return ConnectionTestResult(
                True, f"{msg}, but it has no root folders yet — add one in Kapowarr first."
            )
        plural = "s" if len(folders) != 1 else ""
        return ConnectionTestResult(
            True,
            f"{msg} — {len(folders)} root folder{plural}.",
            {"version": str(version or ""), "root_folders": str(len(folders))},
        )
