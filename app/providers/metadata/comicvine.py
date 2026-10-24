"""ComicVine metadata: key test, volume search, volume details, character/team volumes.

ComicVine wraps every response in ``{"status_code": int, "error": str, ...}``:
1 = OK, 100 = invalid API key, 107 = rate limited. It also requires a
descriptive User-Agent, which the shared client sets.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.http import ExternalServiceError, request_with_retry
from app.core.ratelimit import throttle
from app.domain import ConnectionTestResult, MediaType, SearchResult
from app.providers._util import json_body
from app.providers.base import ProviderError

SERVICE = "ComicVine"
API_BASE = "https://comicvine.gamespot.com/api"
VOLUME_FIELDS = "id,name,start_year,publisher,image,count_of_issues,deck,site_detail_url"
_TAGS = re.compile(r"<[^>]+>")


def _year(value: Any) -> int | None:
    try:
        y = int(str(value)[:4])
    except (TypeError, ValueError):
        return None
    return y if 1900 < y < 2200 else None


def _cover(image: dict[str, Any]) -> str | None:
    url = image.get("medium_url") or image.get("small_url") or image.get("original_url")
    # ComicVine's "no cover" placeholder is a generic "Blank!" image.
    return None if not url or "blank" in url.rsplit("/", 1)[-1].lower() else url


def volume_to_result(v: dict[str, Any]) -> SearchResult:
    image = v.get("image") or {}
    publisher = v.get("publisher") or {}
    deck = v.get("deck") or v.get("description") or None
    if deck:
        deck = _TAGS.sub("", str(deck)).strip()[:1200] or None
    return SearchResult(
        external_id=str(v.get("id")),
        title=str(v.get("name") or "Untitled"),
        media_type=MediaType.COMIC,
        year=_year(v.get("start_year")),
        cover_url=_cover(image),
        publisher=publisher.get("name") if isinstance(publisher, dict) else None,
        issue_count=v.get("count_of_issues"),
        provider="comicvine",
        description=deck,
        url=v.get("site_detail_url"),
    )


class ComicVineMetadataProvider:
    name = "comicvine"

    def __init__(self, client: httpx.AsyncClient, api_key: str, base_url: str = API_BASE) -> None:
        self.client = client
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if not self.api_key:
            raise ProviderError("ComicVine API key is not set (Admin → Metadata — ComicVine).")
        await throttle("comicvine")
        try:
            resp = await request_with_retry(
                self.client,
                SERVICE,
                "GET",
                f"{self.base_url}{path}",
                params={"api_key": self.api_key, "format": "json", **params},
                retries=2,
            )
        except ExternalServiceError as exc:
            raise ProviderError(
                f"Could not reach ComicVine: {exc.message}.", retryable=True
            ) from exc
        if resp.status_code == 404:
            raise ProviderError("ComicVine has no such item.")
        if resp.status_code in (420, 429):
            raise ProviderError(
                "ComicVine is rate-limiting requests. Try again later.", retryable=True
            )
        body = json_body(resp, SERVICE)
        code = body.get("status_code") if isinstance(body, dict) else None
        if code == 100:
            raise ProviderError("ComicVine says the API key is invalid.")
        if code == 107:
            raise ProviderError(
                "ComicVine is rate-limiting requests. Try again later.", retryable=True
            )
        if code == 101:
            raise ProviderError("ComicVine has no such item.")
        if code != 1:
            raise ProviderError(f"ComicVine error: {body.get('error') or resp.status_code}.")
        return body.get("results")

    async def search(
        self, query: str, media_type: MediaType = MediaType.COMIC, limit: int = 30
    ) -> list[SearchResult]:
        results = await self._get(
            "/search/",
            {"query": query, "resources": "volume", "limit": limit, "field_list": VOLUME_FIELDS},
        )
        found = [volume_to_result(v) for v in results or [] if isinstance(v, dict) and v.get("id")]
        if found and not any(r.publisher for r in found):
            # /search/ often omits the publisher; one /volumes/ call fills it in (order kept).
            try:
                ids = [int(r.external_id) for r in found]
                detailed = {r.external_id: r for r in await self.volumes_by_ids(ids)}
                found = [detailed.get(r.external_id, r) for r in found]
            except ProviderError:
                pass
        return found

    async def get_details(self, external_id: str) -> SearchResult:
        if not str(external_id).isdigit():
            raise ProviderError("Not a ComicVine volume id.")
        v = await self._get(
            f"/volume/4050-{external_id}/", {"field_list": VOLUME_FIELDS + ",description"}
        )
        if not isinstance(v, dict) or not v.get("id"):
            raise ProviderError("ComicVine has no such volume.")
        return volume_to_result(v)

    async def volumes_by_ids(self, ids: list[int]) -> list[SearchResult]:
        if not ids:
            return []
        results = await self._get(
            "/volumes/",
            {
                "filter": "id:" + "|".join(str(i) for i in ids[:100]),
                "field_list": VOLUME_FIELDS,
                "limit": 100,
            },
        )
        return [volume_to_result(v) for v in results or [] if isinstance(v, dict)]

    async def credited_volumes(self, kind: str, cv_id: int, limit: int = 60) -> list[SearchResult]:
        """Volumes a character ("character") or team ("team") appears in, best known first."""
        prefix = {"character": "4005", "team": "4060"}[kind]
        data = await self._get(f"/{kind}/{prefix}-{cv_id}/", {"field_list": "volume_credits"})
        credits = (data or {}).get("volume_credits") or []
        ids = [int(c["id"]) for c in credits if isinstance(c, dict) and c.get("id")]
        # Recent ids first (newer volumes are what people tend to want), then rank by size.
        ids = sorted(ids, reverse=True)[:100]
        vols = await self.volumes_by_ids(ids)
        return sorted(vols, key=lambda r: -(r.issue_count or 0))[:limit]

    async def search_character(self, name: str) -> tuple[str, int] | None:
        results = await self._get(
            "/search/",
            {
                "query": name,
                "resources": "character,team",
                "limit": 5,
                "field_list": "id,name,resource_type",
            },
        )
        for r in results or []:
            if (
                isinstance(r, dict)
                and r.get("id")
                and str(r.get("name", "")).lower() == name.lower()
            ):
                return (r.get("resource_type") or "character", int(r["id"]))
        return None

    async def test_connection(self) -> ConnectionTestResult:
        if not self.api_key:
            return ConnectionTestResult(
                False, "ComicVine API key is not set. Get one at comicvine.gamespot.com/api."
            )
        try:
            resp = await request_with_retry(
                self.client,
                SERVICE,
                "GET",
                f"{self.base_url}/types/",
                params={"api_key": self.api_key, "format": "json"},
                retries=1,
            )
        except ExternalServiceError as exc:
            return ConnectionTestResult(False, f"Could not reach ComicVine: {exc.message}.")
        try:
            body = json_body(resp, SERVICE)
        except Exception:
            return ConnectionTestResult(False, f"ComicVine responded with HTTP {resp.status_code}.")
        code = body.get("status_code") if isinstance(body, dict) else None
        if code == 1:
            return ConnectionTestResult(True, "ComicVine accepted the API key.")
        if code == 100:
            return ConnectionTestResult(False, "ComicVine says the API key is invalid.")
        if code == 107:
            return ConnectionTestResult(
                False, "ComicVine is rate-limiting this key right now. Try again in a few minutes."
            )
        err = body.get("error") if isinstance(body, dict) else None
        return ConnectionTestResult(
            False, f"ComicVine responded with an error: {err or f'HTTP {resp.status_code}'}."
        )
