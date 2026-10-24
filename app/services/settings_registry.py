"""Every setting WishPanel knows about, with its type, default and secrecy.

Adding a setting = adding one line here. Bootstrap creates the row with its
default; the service validates writes against it; ``settings_pages`` decides
where (and whether) it appears in Admin -> Settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SettingType(StrEnum):
    STR = "str"
    URL = "url"
    INT = "int"
    BOOL = "bool"
    CHOICE = "choice"
    LIST = "list"  # ordered list of short strings, stored as JSON


@dataclass(frozen=True)
class SettingDef:
    key: str
    type: SettingType
    default: str | int | bool | tuple[str, ...] | None
    secret: bool = False
    choices: tuple[str, ...] = ()
    min_value: int | None = None
    # v1 environment variable(s) imported once on first boot, if present.
    legacy_env: tuple[str, ...] = ()
    description: str = ""


def _s(key: str, default: str | None = "", **kw) -> SettingDef:  # type: ignore[no-untyped-def]
    return SettingDef(key, SettingType.STR, default, **kw)


def _u(key: str, **kw) -> SettingDef:  # type: ignore[no-untyped-def]
    return SettingDef(key, SettingType.URL, "", **kw)


def _i(key: str, default: int, min_value: int = 0, **kw) -> SettingDef:  # type: ignore[no-untyped-def]
    return SettingDef(key, SettingType.INT, default, min_value=min_value, **kw)


def _b(key: str, default: bool, **kw) -> SettingDef:  # type: ignore[no-untyped-def]
    return SettingDef(key, SettingType.BOOL, default, **kw)


def _l(key: str, default: tuple[str, ...] = (), **kw) -> SettingDef:  # type: ignore[no-untyped-def]
    return SettingDef(key, SettingType.LIST, default, **kw)


def _c(key: str, default: str, choices: tuple[str, ...], **kw) -> SettingDef:  # type: ignore[no-untyped-def]
    return SettingDef(key, SettingType.CHOICE, default, choices=choices, **kw)


SETTINGS: tuple[SettingDef, ...] = (
    # --- Internal state -------------------------------------------------------
    _b("setup.completed", False, description="First-run wizard finished"),
    _b("bootstrap.env_imported", False, description="Legacy env vars imported (one-time)"),
    _s("setup.claim_hash", "", description="SHA-256 of the one-time setup code (cleared on setup)"),
    # --- General --------------------------------------------------------------
    _s("general.site_name", "WishPanel"),
    _u("general.public_url", description="Public URL used in links/notifications"),
    _c("general.default_tab", "discover", ("discover", "search", "requests")),
    _c("general.theme", "dark", ("dark", "light")),
    _c("general.language", "en", ("en",)),
    _i("general.logo_version", 0, min_value=0, description="Bumped on logo upload/removal"),
    # --- Authentication -------------------------------------------------------
    _c("auth.provider", "kavita", ("kavita",)),
    _s("auth.admin_role", "Admin", description="Kavita role that grants WishPanel admin"),
    _i("auth.login_max_attempts", 5, min_value=1),
    _i("auth.login_window_minutes", 15, min_value=1),
    _i("auth.session_days", 30, min_value=1),
    _c(
        "auth.cookie_secure",
        "auto",
        ("auto", "always", "never"),
        description="auto = Secure flag when the request arrived over HTTPS",
    ),
    # --- Kavita ---------------------------------------------------------------
    _u("kavita.url", legacy_env=("KAVITA_URL",)),
    _s("kavita.api_key", "", secret=True, legacy_env=("KAVITA_API_KEY",)),
    _s("kavita.library.comic", ""),
    _s("kavita.library.manga", ""),
    _s("kavita.library.manhwa", ""),
    _b("kavita.scan_after_download", True),
    # --- Kapowarr -------------------------------------------------------------
    _u("kapowarr.url", legacy_env=("KAPOWARR_URL",)),
    _s("kapowarr.api_key", "", secret=True, legacy_env=("KAPOWARR_API_KEY",)),
    _s("kapowarr.root_folder_id", ""),
    _b("kapowarr.monitor_new_issues", True),
    _b("kapowarr.auto_search", True),
    # --- Suwayomi -------------------------------------------------------------
    _u("suwayomi.url", legacy_env=("SUWAYOMI_URL",)),
    _c("suwayomi.auth_mode", "none", ("none", "basic", "login")),
    _s("suwayomi.username", "", legacy_env=("SUWAYOMI_USERNAME", "SUWAYOMI_USER")),
    _s("suwayomi.password", "", secret=True, legacy_env=("SUWAYOMI_PASSWORD", "SUWAYOMI_PASS")),
    _l("suwayomi.source_languages", ("en",)),
    _l("suwayomi.sources", description="Allowed source ids, highest priority first"),
    _b("suwayomi.auto_download_new", True),
    # --- Metadata -------------------------------------------------------------
    _s("comicvine.api_key", "", secret=True, legacy_env=("COMICVINE_API_KEY", "CV_API_KEY")),
    _i("comicvine.cache_ttl_hours", 24, min_value=1),
    _l(
        "comicvine.preferred_publishers",
        ("Marvel", "DC Comics", "Image", "Dark Horse Comics", "IDW Publishing", "BOOM! Studios"),
    ),
    _l("comicvine.blocked_publishers", ("Panini Comics", "Panini Verlag", "Glénat")),
    # --- Media types ----------------------------------------------------------
    _b("media.comic.enabled", True),
    _b("media.manga.enabled", True),
    _b("media.manhwa.enabled", True),
    # --- Requests -------------------------------------------------------------
    _i("requests.daily_limit", 10, min_value=0, legacy_env=("DAILY_REQUEST_LIMIT",)),
    _b("requests.auto_approve", True),
)

REGISTRY: dict[str, SettingDef] = {d.key: d for d in SETTINGS}
