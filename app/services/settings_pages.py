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
    # source-list: allow typing entries that aren't in the options list
    free: bool = False


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
        "media-types",
        "Media Types",
        "Turn comics, manga and manhwa on or off, and choose what serves each one. "
        "Kavita libraries are mapped on the Library — Kavita page.",
        (
            Field("media.comic.enabled", "Comics", "toggle"),
            Field(
                "media.comic.metadata",
                "Comic search uses",
                "select",
                "Kapowarr can search with its own ComicVine key if you don't want to add one here.",
                labels={
                    "comicvine": "ComicVine (direct)",
                    "kapowarr": "Kapowarr's ComicVine search",
                },
            ),
            Field(
                "media.comic.downloader",
                "Comics download with",
                "select",
                labels={"kapowarr": "Kapowarr"},
            ),
            Field("media.manga.enabled", "Manga", "toggle"),
            Field(
                "media.manga.metadata",
                "Manga search uses",
                "select",
                "MangaBaka is used when AniList is off or unavailable.",
                labels={"anilist": "AniList (+ MangaBaka)"},
            ),
            Field(
                "media.manga.downloader",
                "Manga download with",
                "select",
                labels={"suwayomi": "Suwayomi"},
            ),
            Field("media.manhwa.enabled", "Manhwa", "toggle"),
            Field(
                "media.manhwa.metadata",
                "Manhwa search uses",
                "select",
                labels={"anilist": "AniList (+ MangaBaka)"},
            ),
            Field(
                "media.manhwa.downloader",
                "Manhwa download with",
                "select",
                labels={"suwayomi": "Suwayomi"},
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
    Page(
        "anilist",
        "Metadata — AniList / MangaBaka",
        "Manga and manhwa search and Discover shelves. Neither service needs an API key.",
        (
            Field(
                "anilist.enabled",
                "Use AniList",
                "toggle",
                "Search, details and the Trending/Popular shelves.",
            ),
            Field(
                "anilist.exclude_adult",
                "Hide adult titles",
                "toggle",
                show_if=("anilist.enabled", ("true",)),
            ),
            Field(
                "anilist.country.JP",
                "Titles from Japan are",
                "select",
                show_if=("anilist.enabled", ("true",)),
                labels={"manga": "Manga", "manhwa": "Manhwa", "skip": "Hidden"},
            ),
            Field(
                "anilist.country.KR",
                "Titles from Korea are",
                "select",
                show_if=("anilist.enabled", ("true",)),
                labels={"manga": "Manga", "manhwa": "Manhwa", "skip": "Hidden"},
            ),
            Field(
                "anilist.country.CN",
                "Titles from China (manhua) are",
                "select",
                show_if=("anilist.enabled", ("true",)),
                labels={"manga": "Manga", "manhwa": "Manhwa", "skip": "Hidden"},
            ),
            Field(
                "anilist.country.TW",
                "Titles from Taiwan are",
                "select",
                show_if=("anilist.enabled", ("true",)),
                labels={"manga": "Manga", "manhwa": "Manhwa", "skip": "Hidden"},
            ),
            Field(
                "mangabaka.enabled",
                "Use MangaBaka",
                "toggle",
                "The Completed and Highest Rated shelves, and search fallback.",
            ),
            Field(
                "mangabaka.content_ratings",
                "MangaBaka content ratings",
                "tags",
                "One per line: safe, suggestive, erotica. Pornographic is always excluded.",
                show_if=("mangabaka.enabled", ("true",)),
            ),
        ),
        connection="manga-metadata",
    ),
    Page(
        "discover",
        "Discover",
        "Which shelves appear, in what order, and how fast comic lists are matched to ComicVine.",
        (
            Field("discover.items_per_row", "Titles per shelf", "number"),
            Field(
                "discover.comic.rows",
                "Comic shelves",
                "source-list",
                "Lists from Comic Book Herald. None selected = all, in this order.",
                options="discover.comic_rows",
            ),
            Field(
                "discover.characters",
                "Character & team shelves (comics)",
                "source-list",
                "Reading orders from Comic Book Herald, with ComicVine as the fallback. "
                "You can add any character name.",
                options="discover.characters",
                free=True,
            ),
            Field(
                "discover.manga.rows",
                "Manga shelves",
                "source-list",
                "None selected = all.",
                options="discover.manga_rows",
            ),
            Field(
                "discover.manhwa.rows",
                "Manhwa shelves",
                "source-list",
                "None selected = all.",
                options="discover.manhwa_rows",
            ),
            Field(
                "discover.match_interval_seconds",
                "Seconds between ComicVine lookups",
                "number",
                "Matching list titles to ComicVine runs in the background at this pace "
                "(ComicVine allows about 200 searches an hour).",
            ),
        ),
        actions=("discover.refresh",),
    ),
    Page(
        "requests",
        "Requests",
        "Limits, approval and how progress is judged.",
        (
            Field(
                "requests.daily_limit",
                "Requests per person per day",
                "number",
                "Rolling 24 hours; 0 = nobody can request. Per-user limits are on the Users page.",
            ),
            Field("requests.admins_unlimited", "Admins have no limit", "toggle"),
            Field(
                "requests.auto_approve",
                "Approve requests automatically",
                "toggle",
                "Off = requests wait in the queue for an admin. Can be overridden per user.",
            ),
            Field(
                "requests.allow_rerequest_failed",
                "Allow requesting again after failed/denied",
                "toggle",
            ),
            Field(
                "requests.source_match_percent",
                "Suwayomi match threshold (%)",
                "number",
                "How closely a source's title must match before it's added. 85 is a good start.",
            ),
            Field(
                "requests.retry_hours",
                "Search Suwayomi again after (hours)",
                "number",
                "For titles none of your sources have yet.",
            ),
            Field(
                "requests.partial_min_expected",
                "Partial: only for series with at least … chapters/issues",
                "number",
            ),
            Field(
                "requests.partial_have_percent",
                "Partial: when fewer than …% are downloaded",
                "number",
            ),
            Field("requests.partial_min_gap", "Partial: and at least … are missing", "number"),
        ),
    ),
    Page(
        "jobs",
        "Jobs",
        "Background tasks. Schedules apply immediately.",
        (
            Field("jobs.discover_comic.enabled", "Discover refresh — comics", "toggle"),
            Field(
                "jobs.discover_comic.hours",
                "…every (hours)",
                "number",
                show_if=("jobs.discover_comic.enabled", ("true",)),
            ),
            Field("jobs.discover_manga.enabled", "Discover refresh — manga & manhwa", "toggle"),
            Field(
                "jobs.discover_manga.hours",
                "…every (hours)",
                "number",
                show_if=("jobs.discover_manga.enabled", ("true",)),
            ),
            Field("jobs.retry.enabled", "Not-found retry", "toggle"),
            Field(
                "jobs.retry.hours",
                "…every (hours)",
                "number",
                show_if=("jobs.retry.enabled", ("true",)),
            ),
            Field("jobs.availability.enabled", "Availability sync", "toggle"),
            Field(
                "jobs.availability.hours",
                "…every (hours)",
                "number",
                show_if=("jobs.availability.enabled", ("true",)),
            ),
            Field("jobs.library_scan.enabled", "Kavita library scan", "toggle"),
            Field(
                "jobs.library_scan.hours",
                "…every (hours)",
                "number",
                show_if=("jobs.library_scan.enabled", ("true",)),
            ),
        ),
        actions=("jobs.table",),
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
