"""Builds provider clients from settings, optionally overlaid with unsaved form values.

This is what lets an admin press "Test" (or load a dropdown) *before* saving:
the form sends its current values, and anything it doesn't send — notably a
secret that's still showing as ``••••abcd`` — falls back to what's stored.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.domain import ConnectionTestResult
from app.providers.base import ProviderError
from app.providers.downloaders.kapowarr import KapowarrDownloader
from app.providers.downloaders.suwayomi import SuwayomiDownloader
from app.providers.library.kavita import KavitaLibraryChecker
from app.providers.metadata.anilist import AniListProvider
from app.providers.metadata.comicvine import ComicVineMetadataProvider
from app.providers.metadata.mangabaka import MangaBakaProvider
from app.services.settings import SettingsService, deserialise, serialise
from app.services.settings_registry import REGISTRY

SERVICE_KEYS: dict[str, tuple[str, ...]] = {
    "kavita": ("kavita.url", "kavita.api_key"),
    "kapowarr": ("kapowarr.url", "kapowarr.api_key"),
    "suwayomi": ("suwayomi.url", "suwayomi.auth_mode", "suwayomi.username", "suwayomi.password"),
    "comicvine": ("comicvine.api_key",),
    "anilist": (
        "anilist.exclude_adult",
        "anilist.country.JP",
        "anilist.country.KR",
        "anilist.country.CN",
        "anilist.country.TW",
    ),
    "mangabaka": ("mangabaka.content_ratings",),
    "manga-metadata": (
        "anilist.enabled",
        "anilist.exclude_adult",
        "anilist.country.JP",
        "anilist.country.KR",
        "anilist.country.CN",
        "anilist.country.TW",
        "mangabaka.enabled",
        "mangabaka.content_ratings",
    ),
}

# options id -> service that provides it
OPTION_SOURCES: dict[str, str] = {
    "kavita.libraries": "kavita",
    "kapowarr.root_folders": "kapowarr",
    "suwayomi.sources": "suwayomi",
}


def _discover_rows(media: str) -> Any:
    def load() -> list[Option]:
        from app.domain import MediaType
        from app.services.discover import row_options

        return [Option(i, label) for i, label in row_options(MediaType(media))]

    return load


def _characters() -> list[Option]:
    from app.providers.discover.cbh import CHARACTERS

    return [
        Option(name, name, "reading order" if path else "ComicVine")
        for name, (path, _) in sorted(CHARACTERS.items())
    ]


# Lists that don't need a connection (built-in catalogues).
STATIC_OPTIONS = {
    "discover.comic_rows": _discover_rows("comic"),
    "discover.manga_rows": _discover_rows("manga"),
    "discover.manhwa_rows": _discover_rows("manhwa"),
    "discover.characters": _characters,
}


class _MangaMetadataTester:
    """The AniList / MangaBaka page tests both services with one button."""

    def __init__(self, anilist: Any, mangabaka: Any) -> None:
        self.anilist, self.mangabaka = anilist, mangabaka

    async def test_connection(self) -> ConnectionTestResult:
        if not self.anilist and not self.mangabaka:
            return ConnectionTestResult(False, "Both are turned off — manga search won't work.")
        parts, ok = [], True
        for label, p in (("AniList", self.anilist), ("MangaBaka", self.mangabaka)):
            if p is None:
                parts.append(f"{label}: off")
                continue
            r = await p.test_connection()
            ok = ok and r.ok
            parts.append(r.message if r.ok else f"{label}: {r.message}")
        return ConnectionTestResult(ok, " · ".join(parts))


class UnknownService(KeyError):
    pass


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    hint: str = ""
    group: str = ""


async def resolve_values(
    settings: SettingsService, service: str, overrides: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Stored values for ``service``, replaced by any (validated) overrides.

    A secret override of ``None`` (or absent) means "use the stored one".
    Raises SettingValidationError for malformed overrides.
    """
    if service not in SERVICE_KEYS:
        raise UnknownService(service)
    overrides = overrides or {}
    values: dict[str, Any] = {}
    for key in SERVICE_KEYS[service]:
        defn = REGISTRY[key]
        if key in overrides and overrides[key] is not None:
            values[key] = deserialise(defn, serialise(defn, overrides[key]))
        else:
            values[key] = await settings.get(key)
    return values


def build(service: str, values: Mapping[str, Any], http: httpx.AsyncClient) -> Any:
    match service:
        case "kavita":
            return KavitaLibraryChecker(http, values["kavita.url"], values["kavita.api_key"])
        case "kapowarr":
            return KapowarrDownloader(http, values["kapowarr.url"], values["kapowarr.api_key"])
        case "suwayomi":
            return SuwayomiDownloader(
                http,
                values["suwayomi.url"],
                values["suwayomi.auth_mode"],
                values["suwayomi.username"],
                values["suwayomi.password"],
            )
        case "comicvine":
            return ComicVineMetadataProvider(http, values["comicvine.api_key"])
        case "anilist":
            return AniListProvider(
                http,
                exclude_adult=values["anilist.exclude_adult"],
                country_map={c: values[f"anilist.country.{c}"] for c in ("JP", "KR", "CN", "TW")},
            )
        case "mangabaka":
            return MangaBakaProvider(http, content_ratings=values["mangabaka.content_ratings"])
        case "manga-metadata":
            return _MangaMetadataTester(
                build("anilist", values, http) if values["anilist.enabled"] else None,
                build("mangabaka", values, http) if values["mangabaka.enabled"] else None,
            )
    raise UnknownService(service)


async def provider_for(
    settings: SettingsService,
    service: str,
    http: httpx.AsyncClient,
    overrides: Mapping[str, Any] | None = None,
) -> Any:
    return build(service, await resolve_values(settings, service, overrides), http)


async def load_options(
    settings: SettingsService,
    option_id: str,
    http: httpx.AsyncClient,
    overrides: Mapping[str, Any] | None = None,
) -> list[Option]:
    """Live dropdown contents. Raises ProviderError with an admin-facing message."""
    if option_id in STATIC_OPTIONS:
        return STATIC_OPTIONS[option_id]()
    service = OPTION_SOURCES.get(option_id)
    if service is None:
        raise UnknownService(option_id)
    provider = await provider_for(settings, service, http, overrides)
    match option_id:
        case "kavita.libraries":
            return [
                Option(lib.id, lib.name, (lib.kind or "").replace("lightnovel", "light novel"))
                for lib in await provider.list_libraries()
            ]
        case "kapowarr.root_folders":
            return [Option(f.id, f.path) for f in await provider.list_root_folders()]
        case "suwayomi.sources":
            return [
                Option(s.id, s.name, s.language or "", s.language or "")
                for s in await provider.list_sources()
            ]
    raise ProviderError(f"No options for {option_id}.")  # pragma: no cover
