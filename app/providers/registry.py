"""Builds provider instances from current Settings.

Providers are constructed per use with settings read at that moment, so a
change saved in Admin -> Settings takes effect on the next call without a
restart. Later milestones register metadata, discover, downloader and library
implementations here.
"""

from __future__ import annotations

import httpx

from app.providers.auth.kavita import KavitaAuthProvider
from app.providers.base import AuthProvider, ProviderError
from app.services.settings import SettingsService


async def get_auth_provider(
    settings: SettingsService, client: httpx.AsyncClient, *, kavita_url: str | None = None
) -> AuthProvider:
    provider = await settings.get("auth.provider")
    if provider == "kavita":
        url = kavita_url if kavita_url is not None else await settings.get("kavita.url")
        return KavitaAuthProvider(client, url)
    raise ProviderError(f"Unknown auth provider '{provider}'.")
