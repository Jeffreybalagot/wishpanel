"""MangaBaka: manga/manhwa discover lists and search (public REST API, no key).

Docs: https://mangabaka.org/api — ``/v1/series/search`` returns ``{data: [...], pagination}``.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.http import ExternalServiceError, request_with_retry
from app.core.ratelimit import throttle
from app.core.textmatch import dedupe, is_latin
from app.domain import ConnectionTestResult, MediaType, SearchResult
from app.providers._util import json_body
from app.providers.base import ProviderError

SERVICE = "MangaBaka"
API_BASE = "https://api.mangabaka.org/v1"
TYPE = {MediaType.MANGA: "manga", MediaType.MANHWA: "manhwa"}


def _cover(item: dict[str, Any]) -> str | None:
    cover = item.get("cover") or {}
    if isinstance(cover, str):
        return cover
    for size in ("x350", "x250", "x150", "raw"):
        v = cover.get(size)
        if isinstance(v, dict):
            v = v.get("x1") or v.get("url")
        if isinstance(v, str) and v:
            return v
    return None


def series_to_result(s: dict[str, Any], media_type: MediaType) -> SearchResult:
    title = s.get("title") or s.get("romanized_title") or "Untitled"
    secondary = s.get("secondary_titles") or {}
    alts: list[str] = [s.get("romanized_title") or ""]
    if isinstance(secondary, dict):
        for lst in secondary.values():
            for t in lst or []:
                alts.append(t.get("title") if isinstance(t, dict) else str(t))
    alts = [a for a in dedupe(alts) if a and a != title and is_latin(a)]
    anilist = (((s.get("source") or {}).get("anilist")) or {}).get("id")
    try:
        anilist_id = int(anilist) if anilist else None
    except (TypeError, ValueError):
        anilist_id = None
    year = s.get("year")
    chapters = s.get("total_chapters")
    try:
        chapters = int(float(chapters)) if chapters else None
    except (TypeError, ValueError):
        chapters = None
    return SearchResult(
        external_id=str(s.get("id")),
        title=str(title),
        media_type=media_type,
        year=int(year) if str(year or "").isdigit() else None,
        cover_url=_cover(s),
        issue_count=chapters,
        provider="mangabaka",
        alt_titles=tuple(alts[:8]),
        description=(str(s.get("description"))[:1500] if s.get("description") else None),
        anilist_id=anilist_id,
        status=s.get("status"),
        url=f"https://mangabaka.org/{s.get('id')}",
        adult=s.get("content_rating") in ("erotica", "pornographic"),
        score=s.get("rating"),
    )


class MangaBakaProvider:
    name = "mangabaka"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        content_ratings: list[str] | None = None,
        base_url: str = API_BASE,
    ) -> None:
        self.client = client
        ratings = [r for r in (content_ratings or ["safe", "suggestive"]) if r != "pornographic"]
        self.content_ratings = ratings or ["safe"]
        self.base_url = base_url.rstrip("/")

    async def _get(self, path: str, params: list[tuple[str, Any]]) -> Any:
        await throttle("mangabaka")
        try:
            resp = await request_with_retry(
                self.client, SERVICE, "GET", f"{self.base_url}{path}", params=params, retries=2
            )
        except ExternalServiceError as exc:
            raise ProviderError(
                f"Could not reach MangaBaka: {exc.message}.", retryable=True
            ) from exc
        if resp.status_code == 404:
            raise ProviderError("MangaBaka has no such series.")
        if resp.status_code != 200:
            raise ProviderError(
                f"MangaBaka responded with HTTP {resp.status_code}.", retryable=True
            )
        return json_body(resp, SERVICE)

    def _ratings(self) -> list[tuple[str, str]]:
        return [("content_rating", r) for r in self.content_ratings]

    async def row(
        self, media_type: MediaType, sort_by: str, status: str | None = None, limit: int = 24
    ) -> list[SearchResult]:
        params: list[tuple[str, Any]] = [
            ("type", TYPE[media_type]),
            ("sort_by", sort_by),
            ("limit", limit),
            *self._ratings(),
        ]
        if status:
            params.append(("status", status))
        body = await self._get("/series/search", params)
        out = []
        for s in (body or {}).get("data") or []:
            if isinstance(s, dict) and s.get("id") and not s.get("merged_with"):
                out.append(series_to_result(s, media_type))
        return out

    async def search(self, query: str, media_type: MediaType) -> list[SearchResult]:
        body = await self._get(
            "/series/search",
            [("q", query), ("type", TYPE[media_type]), ("limit", 30), *self._ratings()],
        )
        return [
            series_to_result(s, media_type)
            for s in (body or {}).get("data") or []
            if isinstance(s, dict) and s.get("id") and not s.get("merged_with")
        ]

    async def get_details(
        self, external_id: str, media_type: MediaType = MediaType.MANGA
    ) -> SearchResult:
        body = await self._get(f"/series/{int(external_id)}", [])
        data = (body or {}).get("data")
        if not isinstance(data, dict):
            raise ProviderError("MangaBaka has no such series.")
        kind = MediaType.MANHWA if data.get("type") == "manhwa" else media_type
        return series_to_result(data, kind)

    async def test_connection(self) -> ConnectionTestResult:
        try:
            items = await self.row(MediaType.MANGA, "popularity_asc", limit=1)
        except ProviderError as exc:
            return ConnectionTestResult(False, exc.message)
        return ConnectionTestResult(True, f"MangaBaka is reachable ({len(items)} result).")
