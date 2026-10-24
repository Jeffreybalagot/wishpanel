"""Domain types shared by providers, services and the API.

These are plain dataclasses/enums with no database or HTTP dependencies so
provider implementations can be written and tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class MediaType(StrEnum):
    COMIC = "comic"
    MANGA = "manga"
    MANHWA = "manhwa"


class RequestStatus(StrEnum):
    """Lifecycle from SPEC §5. Used from Milestone 3 onward."""

    PENDING = "pending"
    APPROVED = "approved"
    ADDED = "added"
    DOWNLOADING = "downloading"
    PARTIAL = "partial"
    IN_LIBRARY = "in_library"
    FAILED = "failed"
    WAITING_FOR_SOURCE = "waiting_for_source"
    WAITING_FOR_DOWNLOADER = "waiting_for_downloader"
    DENIED = "denied"


@dataclass(frozen=True)
class AuthenticatedUser:
    """What an AuthProvider returns after a successful login."""

    provider: str
    external_id: str
    username: str
    email: str | None = None
    roles: tuple[str, ...] = ()
    # Older identifiers for the same account; a matching user row is re-keyed.
    legacy_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConnectionTestResult:
    """Outcome of a Settings 'Test' button. ``message`` is shown verbatim to the admin."""

    ok: bool
    message: str
    details: dict[str, str] = field(default_factory=dict)


# --- Shapes used by later milestones; defined now so the interfaces are complete. ---


@dataclass(frozen=True)
class SearchResult:
    """A title card: search hit, discover item or request target.

    ``provider``/``external_id`` identify it in a metadata source ("comicvine",
    "anilist", "mangabaka"); ``key`` is the stable ``<prefix>:<id>`` form the UI uses.
    """

    external_id: str
    title: str
    media_type: MediaType
    year: int | None = None
    cover_url: str | None = None
    publisher: str | None = None
    issue_count: int | None = None
    provider: str = ""
    alt_titles: tuple[str, ...] = ()
    description: str | None = None
    anilist_id: int | None = None
    status: str | None = None  # publication status: releasing / finished / …
    url: str | None = None
    country: str | None = None
    adult: bool = False
    translated: bool = False
    score: float | None = None

    @property
    def key(self) -> str:
        prefix = {"comicvine": "cv", "anilist": "al", "mangabaka": "mb"}.get(self.provider, "x")
        return f"{prefix}:{self.external_id}"

    def to_json(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["media_type"] = str(self.media_type)
        d["alt_titles"] = list(self.alt_titles)
        d["key"] = self.key
        return d


# Details use the same shape; a separate name keeps signatures readable.
TitleDetails = SearchResult


@dataclass(frozen=True)
class DiscoverRow:
    key: str
    title: str
    media_type: MediaType
    items: tuple[SearchResult, ...] = ()


@dataclass(frozen=True)
class DownloaderResult:
    ok: bool
    downloader_ref: str | None = None
    source_id: str | None = None
    message: str = ""


@dataclass(frozen=True)
class DownloadStatus:
    status: RequestStatus
    have: int | None = None
    expected: int | None = None


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    language: str | None = None


@dataclass(frozen=True)
class Folder:
    id: str
    path: str


@dataclass(frozen=True)
class Library:
    id: str
    name: str
    kind: str | None = None
