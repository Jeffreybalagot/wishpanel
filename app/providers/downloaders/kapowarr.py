"""Kapowarr downloader: search (via Kapowarr's own ComicVine key), add volumes, status.

API (Kapowarr ``frontend/api.py``): every route is under ``/api`` and is authorised
by an ``api_key`` query parameter; responses are ``{"error": str|None, "result": ...}``
and a bad key returns HTTP 401 ``{"error": "ApiKeyInvalid"}``.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.http import ExternalServiceError, request_with_retry
from app.domain import ConnectionTestResult, Folder, MediaType, SearchResult
from app.providers._util import json_body, status_message
from app.providers.base import ProviderError


class VolumeNotFound(ProviderError):
    pass


class KapowarrVolume:
    def __init__(self, data: dict[str, Any]) -> None:
        self.id = str(data.get("id"))
        self.comicvine_id = data.get("comicvine_id")
        self.title = data.get("title")
        self.issue_count = int(data.get("issue_count") or 0)
        self.issues_downloaded = int(data.get("issues_downloaded") or 0)
        self.monitored = bool(data.get("monitored", True))


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

    async def _api(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,  # noqa: ASYNC109 - httpx timeout
    ) -> Any:
        if not self.base_url:
            raise ProviderError("Kapowarr URL is not set.")
        if not self.api_key:
            raise ProviderError("Kapowarr API key is not set.")
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if timeout:
            kwargs["timeout"] = timeout
        try:
            resp = await request_with_retry(
                self.client,
                SERVICE,
                method,
                f"{self.base_url}/api{path}",
                params={"api_key": self.api_key, **(params or {})},
                retries=1 if method == "GET" else 0,
                **kwargs,
            )
        except ExternalServiceError as exc:
            raise ProviderError(
                f"Could not reach Kapowarr: {exc.message}.", retryable=True
            ) from exc
        if resp.status_code == 401:
            raise ProviderError(
                "Kapowarr rejected the API key. Copy it from Kapowarr → Settings → General."
            )
        body: Any = None
        try:
            body = resp.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if error == "VolumeAlreadyAdded":
            return {"already_added": True, **(body.get("result") or {})}
        if error == "VolumeNotFound" or (resp.status_code == 404 and path.startswith("/volumes/")):
            raise VolumeNotFound("Kapowarr no longer has that volume.")
        if resp.status_code not in (200, 201):
            if error:
                raise ProviderError(f"Kapowarr returned an error: {error}.")
            raise ProviderError(
                status_message(SERVICE, resp.status_code), retryable=resp.status_code >= 500
            )
        if body is None:
            json_body(resp, SERVICE)  # raises the friendly "not JSON" error
        if not isinstance(body, dict) or "result" not in body:
            raise ProviderError("That doesn't look like Kapowarr's API. Check the URL.")
        if error:
            raise ProviderError(f"Kapowarr returned an error: {error}.")
        return body["result"]

    async def search(
        self, query: str, media_type: MediaType = MediaType.COMIC
    ) -> list[SearchResult]:
        """ComicVine search through Kapowarr (uses the key configured in Kapowarr)."""
        result = await self._api("/volumes/search", params={"query": query}, timeout=30)
        out = []
        for v in result or []:
            if not isinstance(v, dict) or not v.get("comicvine_id"):
                continue
            year = v.get("year")
            out.append(
                SearchResult(
                    external_id=str(v["comicvine_id"]),
                    title=str(v.get("title") or "Untitled"),
                    media_type=MediaType.COMIC,
                    year=int(year) if str(year or "").isdigit() else None,
                    cover_url=v.get("cover_link"),
                    publisher=v.get("publisher"),
                    issue_count=v.get("issue_count"),
                    provider="comicvine",
                    description=(v.get("description") or None),
                    url=v.get("site_url"),
                    translated=bool(v.get("translated")),
                )
            )
        return out

    async def add_volume(
        self,
        comicvine_id: str | int,
        root_folder_id: str | int,
        *,
        monitor_new_issues: bool = True,
        auto_search: bool = True,
    ) -> tuple[str, bool]:
        """Add a volume. Returns (kapowarr volume id, already_existed)."""
        payload = {
            "comicvine_id": int(comicvine_id),
            "root_folder_id": int(root_folder_id),
            "monitor": True,
            "monitoring_scheme": "all",
            "monitor_new_issues": monitor_new_issues,
            "volume_folder": None,
            "special_version": "auto",
            "auto_search": auto_search,
        }
        result = await self._api("/volumes", method="POST", json=payload, timeout=60)
        if not isinstance(result, dict):
            raise ProviderError("Kapowarr returned an unexpected response when adding.")
        if result.get("already_added"):
            vid = result.get("volume_id") or result.get("id")
            if not vid:
                vid = await self.find_volume_id(comicvine_id)
            return str(vid or ""), True
        return str(result.get("id")), False

    async def find_volume_id(self, comicvine_id: str | int) -> str | None:
        for v in await self._api("/volumes") or []:
            if isinstance(v, dict) and str(v.get("comicvine_id")) == str(comicvine_id):
                return str(v.get("id"))
        return None

    async def get_volume(self, volume_id: str | int) -> KapowarrVolume:
        result = await self._api(f"/volumes/{volume_id}")
        if not isinstance(result, dict):
            raise ProviderError("Kapowarr returned an unexpected volume.")
        return KapowarrVolume(result)

    async def library_ids(self) -> dict[str, KapowarrVolume]:
        """comicvine_id -> volume, for 'already in Kapowarr' checks."""
        out: dict[str, KapowarrVolume] = {}
        for v in await self._api("/volumes") or []:
            if isinstance(v, dict) and v.get("comicvine_id"):
                out[str(v["comicvine_id"])] = KapowarrVolume(v)
        return out

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
