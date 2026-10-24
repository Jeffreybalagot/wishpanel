"""AniList: manga/manhwa search, details and discover rows (GraphQL, no key needed).

Docs: https://docs.anilist.co — rate limit ~90 requests/minute, shared per IP.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.http import ExternalServiceError, request_with_retry
from app.core.ratelimit import throttle
from app.core.textmatch import dedupe, is_latin
from app.domain import ConnectionTestResult, MediaType, SearchResult
from app.providers._util import json_body
from app.providers.base import ProviderError

SERVICE = "AniList"
API_URL = "https://graphql.anilist.co"

_MEDIA_FIELDS = """
  id title { romaji english native } synonyms coverImage { large extraLarge }
  startDate { year } chapters volumes status countryOfOrigin isAdult format siteUrl
  description(asHtml: false)
"""
_SEARCH = (
    "query ($q: String, $adult: Boolean, $country: CountryCode, $per: Int) { Page(perPage: $per) "
    "{ media(search: $q, type: MANGA, isAdult: $adult, countryOfOrigin: $country, "
    "format_not_in: [NOVEL], sort: [SEARCH_MATCH, POPULARITY_DESC]) {" + _MEDIA_FIELDS + "} } }"
)
_ROW = (
    "query ($sort: [MediaSort], $status: MediaStatus, $adult: Boolean, $country: CountryCode, "
    "$per: Int) { Page(perPage: $per) { media(type: MANGA, sort: $sort, status: $status, "
    "isAdult: $adult, countryOfOrigin: $country, format_not_in: [NOVEL]) {"
    + _MEDIA_FIELDS
    + "} } }"
)
_DETAILS = "query ($id: Int) { Media(id: $id, type: MANGA) {" + _MEDIA_FIELDS + "} }"
_TAGS = re.compile(r"<[^>]+>")

STATUS = {
    "FINISHED": "finished",
    "RELEASING": "releasing",
    "NOT_YET_RELEASED": "upcoming",
    "CANCELLED": "cancelled",
    "HIATUS": "hiatus",
}


def media_to_result(m: dict[str, Any], media_type: MediaType) -> SearchResult:
    t = m.get("title") or {}
    title = t.get("english") or t.get("romaji") or t.get("native") or "Untitled"
    alts = [t.get("romaji") or "", t.get("english") or "", *list(m.get("synonyms") or [])]
    alts = [a for a in dedupe(alts) if a != title and is_latin(a)]
    desc = m.get("description")
    if desc:
        desc = _TAGS.sub("", str(desc)).replace("\n\n", "\n").strip()[:1500] or None
    cover = m.get("coverImage") or {}
    return SearchResult(
        external_id=str(m["id"]),
        title=title,
        media_type=media_type,
        year=(m.get("startDate") or {}).get("year"),
        cover_url=cover.get("extraLarge") or cover.get("large"),
        issue_count=m.get("chapters"),
        provider="anilist",
        alt_titles=tuple(alts[:8]),
        description=desc,
        anilist_id=int(m["id"]),
        status=STATUS.get(str(m.get("status") or "")),
        url=m.get("siteUrl"),
        country=m.get("countryOfOrigin"),
        adult=bool(m.get("isAdult")),
    )


class AniListProvider:
    name = "anilist"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        exclude_adult: bool = True,
        country_map: dict[str, str] | None = None,
        api_url: str = API_URL,
    ) -> None:
        self.client = client
        self.exclude_adult = exclude_adult
        # CountryCode -> "manga" | "manhwa" | "skip"
        self.country_map = country_map or {
            "JP": "manga",
            "KR": "manhwa",
            "CN": "manhwa",
            "TW": "manhwa",
        }
        self.api_url = api_url

    def kind_for(self, country: str | None) -> MediaType | None:
        kind = self.country_map.get(country or "", "manga" if not country else "skip")
        return MediaType(kind) if kind in ("manga", "manhwa") else None

    def countries_for(self, media_type: MediaType) -> list[str]:
        return [c for c, k in self.country_map.items() if k == media_type.value]

    async def _gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        await throttle("anilist")
        variables = {k: v for k, v in variables.items() if v is not None}
        try:
            resp = await request_with_retry(
                self.client,
                SERVICE,
                "POST",
                self.api_url,
                json={"query": query, "variables": variables},
                retries=2,
            )
        except ExternalServiceError as exc:
            raise ProviderError(f"Could not reach AniList: {exc.message}.", retryable=True) from exc
        if resp.status_code == 429:
            raise ProviderError(
                "AniList is rate-limiting requests. Try again shortly.", retryable=True
            )
        if resp.status_code == 404:
            raise ProviderError("AniList has no such title.")
        body = json_body(resp, SERVICE)
        if resp.status_code != 200 or body.get("errors"):
            errs = body.get("errors") or [{}]
            raise ProviderError(
                f"AniList error: {str(errs[0].get('message', resp.status_code))[:160]}"
            )
        return body.get("data") or {}

    def _adult(self) -> bool | None:
        return False if self.exclude_adult else None

    async def search(self, query: str, media_type: MediaType) -> list[SearchResult]:
        out: list[SearchResult] = []
        seen: set[str] = set()
        countries = self.countries_for(media_type) or [None]  # type: ignore[list-item]
        for country in countries[:2]:
            data = await self._gql(
                _SEARCH, {"q": query, "adult": self._adult(), "country": country, "per": 30}
            )
            for m in ((data.get("Page") or {}).get("media")) or []:
                if str(m.get("id")) in seen:
                    continue
                seen.add(str(m.get("id")))
                out.append(media_to_result(m, media_type))
        return out

    async def get_details(self, external_id: str) -> SearchResult:
        if not str(external_id).isdigit():
            raise ProviderError("Not an AniList id.")
        data = await self._gql(_DETAILS, {"id": int(external_id)})
        m = data.get("Media")
        if not m:
            raise ProviderError("AniList has no such title.")
        kind = self.kind_for(m.get("countryOfOrigin")) or MediaType.MANGA
        return media_to_result(m, kind)

    async def row(
        self, media_type: MediaType, sort: str, status: str | None = None, per: int = 24
    ) -> list[SearchResult]:
        countries = self.countries_for(media_type)
        if not countries:
            return []
        # Primary country only (JP for manga, KR for manhwa) keeps rows focused.
        primary = "JP" if "JP" in countries else ("KR" if "KR" in countries else countries[0])
        data = await self._gql(
            _ROW,
            {
                "sort": [sort],
                "status": status,
                "adult": self._adult(),
                "country": primary,
                "per": per,
            },
        )
        return [
            media_to_result(m, media_type) for m in ((data.get("Page") or {}).get("media")) or []
        ]

    async def test_connection(self) -> ConnectionTestResult:
        try:
            items = await self.row(MediaType.MANGA, "TRENDING_DESC", per=1)
        except ProviderError as exc:
            return ConnectionTestResult(False, exc.message)
        return ConnectionTestResult(
            True, f"AniList is reachable{' — ' + items[0].title + ' is trending' if items else ''}."
        )
