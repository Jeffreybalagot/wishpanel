"""Provider interfaces — the architectural backbone (SPEC §6).

Core code depends only on these protocols. Each external system (Kavita,
Kapowarr, Suwayomi, ComicVine, AniList, MangaBaka, Comic Book Herald) is a
concrete implementation living under ``app/providers/<kind>/``.

Conventions every implementation follows:

* Construction is cheap and side-effect free; connection details are passed
  in (read from Settings by the caller at call time, so edits apply live).
* All network I/O goes through ``app.core.http.request_with_retry``.
* Failures the admin should see raise ``ProviderError`` with a plain-English
  message. Never include API keys or tokens in messages or logs.
* ``test_connection`` powers the Settings "Test" buttons and must never raise.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain import (
    AuthenticatedUser,
    ConnectionTestResult,
    DiscoverRow,
    DownloaderResult,
    DownloadStatus,
    Folder,
    Library,
    MediaType,
    SearchResult,
    Source,
    TitleDetails,
)


class ProviderError(Exception):
    """A provider failure with an admin-facing message."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class InvalidCredentials(ProviderError):
    """Username/password rejected by the auth provider."""


@runtime_checkable
class Testable(Protocol):
    async def test_connection(self) -> ConnectionTestResult: ...


@runtime_checkable
class AuthProvider(Testable, Protocol):
    name: str

    async def login(self, username: str, password: str) -> AuthenticatedUser:
        """Return the user, or raise InvalidCredentials / ProviderError."""
        ...

    def is_admin(self, user: AuthenticatedUser, admin_role: str) -> bool: ...


@runtime_checkable
class MetadataProvider(Testable, Protocol):
    name: str

    async def search(self, query: str, media_type: MediaType) -> list[SearchResult]: ...

    async def get_details(self, external_id: str) -> TitleDetails: ...


@runtime_checkable
class DiscoverSource(Protocol):
    name: str

    async def get_rows(self, media_type: MediaType) -> list[DiscoverRow]: ...


@runtime_checkable
class Downloader(Testable, Protocol):
    name: str

    async def add(self, title: TitleDetails) -> DownloaderResult: ...

    async def get_status(self, downloader_ref: str) -> DownloadStatus: ...

    async def list_sources(self) -> list[Source]:
        """Suwayomi: available sources. Others return []."""
        ...

    async def list_root_folders(self) -> list[Folder]:
        """Kapowarr: root folders. Others return []."""
        ...


@runtime_checkable
class LibraryChecker(Testable, Protocol):
    name: str

    async def list_libraries(self) -> list[Library]: ...

    async def is_in_library(self, title: str, media_type: MediaType) -> bool: ...

    async def trigger_scan(self, media_type: MediaType) -> None: ...
