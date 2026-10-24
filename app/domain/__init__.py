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
    external_id: str
    title: str
    media_type: MediaType
    year: int | None = None
    cover_url: str | None = None
    publisher: str | None = None
    issue_count: int | None = None


@dataclass(frozen=True)
class TitleDetails:
    external_id: str
    title: str
    media_type: MediaType
    description: str | None = None
    cover_url: str | None = None
    year: int | None = None
    publisher: str | None = None
    issue_count: int | None = None
    country: str | None = None
    adult: bool = False


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
