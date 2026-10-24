"""Admin -> Settings page definitions.

The UI renders every settings page from this schema, so adding a field to a
page is one line here (plus its definition in ``settings_registry``). Only keys
listed on a page can be written through the settings API; internal state such
as ``setup.completed`` is never editable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.settings_registry import REGISTRY

# Widgets the frontend knows how to draw.
WIDGETS = {
    "text",
    "url",
    "secret",
    "number",
    "toggle",
    "select",
    "tags",
    "remote-select",
    "source-list",
}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    widget: str
    help: str = ""
    placeholder: str = ""
    # remote-select / source-list: which options endpoint feeds it
    options: str | None = None
    # select: optional display labels for the registry's choices
    labels: dict[str, str] = field(default_factory=dict)
    # Show only when another field has one of these values, e.g. ("suwayomi.auth_mode", ("basic",))
    show_if: tuple[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class Page:
    id: str
    title: str
    description: str
    fields: tuple[Field, ...]
    connection: str | None = None  # service id for the Test button
    actions: tuple[str, ...] = ()  # extra buttons, e.g. "kavita.scan"


PAGES: tuple[Page, ...] = (
    Page(
        "general",
        "General",
        "How WishPanel presents itself.",
        (
            Field("general.site_name", "Site name", "text", "Shown in the header and tab title."),
            Field(
                "general.public_url",
                "Public URL",
                "url",
                "The address people use, e.g. https://wishpanel.example.com. Used in links. "
                "Serving under a sub-path also needs the BASE_URL container variable.",
                placeholder="https://wishpanel.example.com",
            ),
            Field(
                "general.default_tab",
                "Default landing tab",
                "select",
                labels={"discover": "Discover", "search": "Search", "requests": "Requests"},
            ),
            Field("general.theme", "Theme", "select", labels={"dark": "Dark", "light": "Light"}),
            Field(
                "general.language",
                "Language",
                "select",
                "More languages can be added later.",
                labels={"en": "English"},
            ),
        ),
        actions=("general.logo",),
    ),
    Page(
        "authentication",
        "Authentication",
        "Who can sign in, and how hard it is to guess passwords.",
        (
            Field(
                "auth.provider",
                "Login provider",
                "select",
                "Local accounts are planned for a later release.",
                labels={"kavita": "Kavita"},
            ),
            Field(
                "auth.admin_role",
                "Kavita role that makes someone a WishPanel admin",
                "text",
                "Checked at every sign-in. Removing your own role takes effect next sign-in.",
            ),
            Field("auth.login_max_attempts", "Failed sign-ins allowed", "number"),
            Field("auth.login_window_minutes", "…within this many minutes", "number"),
            Field("auth.session_days", "Stay signed in for (days)", "number"),
            Field(
                "auth.cookie_secure",
                "Secure cookies",
                "select",
                "Auto marks cookies Secure when the request arrived over HTTPS "
                "(including via a trusted reverse proxy).",
                labels={"auto": "Auto", "always": "Always (HTTPS only)", "never": "Never"},
            ),
        ),
    ),
    Page(
        "kavita",
        "Library — Kavita",
        "Kavita is where people sign in and where finished downloads appear.",
        (
            Field(
                "kavita.url",
                "Kavita URL",
                "url",
                "The address the WishPanel container can reach. Sign-ins use it, "
                "so it's checked before saving.",
                placeholder="http://10.0.0.5:5000",
            ),
            Field(
                "kavita.api_key",
                "API key",
                "secret",
                "Kavita → User settings → API keys (or the key at the end of your OPDS URL). "
                "Use an admin account's key so every library is visible.",
            ),
            Field(
                "kavita.library.comic",
                "Comics library",
                "remote-select",
                options="kavita.libraries",
            ),
            Field(
                "kavita.library.manga",
                "Manga library",
                "remote-select",
                options="kavita.libraries",
            ),
            Field(
                "kavita.library.manhwa",
                "Manhwa library",
                "remote-select",
                "Can be the same library as manga.",
                options="kavita.libraries",
            ),
            Field(
                "kavita.scan_after_download",
                "Scan the library after a download finishes",
                "toggle",
            ),
        ),
        connection="kavita",
        actions=("kavita.scan",),
    ),
    Page(
        "kapowarr",
        "Downloader — Kapowarr",
        "Kapowarr fetches comics.",
        (
            Field(
                "kapowarr.url",
                "Kapowarr URL",
                "url",
                "Include the URL base if you set one in Kapowarr.",
                placeholder="http://10.0.0.5:5656",
            ),
            Field("kapowarr.api_key", "API key", "secret", "Kapowarr → Settings → General."),
            Field(
                "kapowarr.root_folder_id",
                "Root folder",
                "remote-select",
                "Where Kapowarr puts new comics.",
                options="kapowarr.root_folders",
            ),
            Field("kapowarr.monitor_new_issues", "Monitor new issues", "toggle"),
            Field(
                "kapowarr.auto_search",
                "Search for issues as soon as a volume is added",
                "toggle",
            ),
        ),
        connection="kapowarr",
    ),
    Page(
        "suwayomi",
        "Downloader — Suwayomi",
        "Suwayomi fetches manga and manhwa.",
        (
            Field(
                "suwayomi.url",
                "Suwayomi URL",
                "url",
                placeholder="http://10.0.0.5:4567",
            ),
            Field(
                "suwayomi.auth_mode",
                "Authentication",
                "select",
                "Match Suwayomi → Settings → Server → Authentication.",
                labels={"none": "None", "basic": "Basic auth", "login": "Login (UI / simple)"},
            ),
            Field(
                "suwayomi.username",
                "Username",
                "text",
                show_if=("suwayomi.auth_mode", ("basic", "login")),
            ),
            Field(
                "suwayomi.password",
                "Password",
                "secret",
                show_if=("suwayomi.auth_mode", ("basic", "login")),
            ),
            Field(
                "suwayomi.source_languages",
                "Source languages to show",
                "tags",
                "Language codes, e.g. en, ja, ko. Filters the list below; leave empty for all.",
            ),
            Field(
                "suwayomi.sources",
                "Allowed sources (in priority order)",
                "source-list",
                "Add the sources WishPanel may use and order them with ▲ ▼ (first is tried first).",
                options="suwayomi.sources",
            ),
            Field("suwayomi.auto_download_new", "Download new chapters automatically", "toggle"),
        ),
        connection="suwayomi",
    ),
    Page(
        "comicvine",
        "Metadata — ComicVine",
        "ComicVine supplies comic titles, covers and issue counts.",
        (
            Field(
                "comicvine.api_key",
                "API key",
                "secret",
                "Free from comicvine.gamespot.com/api (requires an account).",
            ),
            Field(
                "comicvine.cache_ttl_hours",
                "Cache results for (hours)",
                "number",
                "Longer caching means fewer requests and less risk of rate limiting.",
            ),
            Field(
                "comicvine.preferred_publishers",
                "Preferred publishers",
                "tags",
                "Ranked first in search results. One per line.",
            ),
            Field(
                "comicvine.blocked_publishers",
                "Blocked publishers",
                "tags",
                "Hidden from results — typically non-English reprints. One per line.",
            ),
        ),
        connection="comicvine",
    ),
)

PAGE_BY_ID: dict[str, Page] = {p.id: p for p in PAGES}
EDITABLE_KEYS: frozenset[str] = frozenset(f.key for p in PAGES for f in p.fields)


def validate_schema() -> None:
    """Fail fast at import time if a page references something that doesn't exist."""
    for page in PAGES:
        for f in page.fields:
            if f.key not in REGISTRY:
                raise RuntimeError(f"settings page {page.id!r}: unknown key {f.key!r}")
            if f.widget not in WIDGETS:
                raise RuntimeError(f"settings page {page.id!r}: unknown widget {f.widget!r}")
            defn = REGISTRY[f.key]
            if (f.widget == "secret") != defn.secret:
                raise RuntimeError(f"{f.key}: secret widget must match registry secrecy")


validate_schema()


def schema_json() -> list[dict[str, Any]]:
    out = []
    for page in PAGES:
        d = asdict(page)
        for f, fd in zip(page.fields, d["fields"], strict=True):
            defn = REGISTRY[f.key]
            fd["type"] = defn.type.value
            fd["choices"] = list(defn.choices)
            fd["min"] = defn.min_value
            fd["show_if"] = (
                {"key": f.show_if[0], "values": list(f.show_if[1])} if f.show_if else None
            )
        out.append(d)
    return out
