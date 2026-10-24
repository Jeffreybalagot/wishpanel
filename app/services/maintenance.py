"""Logs & About: log tail, database backup, settings export/import, update check."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx

from app import __version__
from app.core.http import ExternalServiceError, request_with_retry
from app.services.settings import SettingsService, SettingValidationError, serialise
from app.services.settings_pages import EDITABLE_KEYS
from app.services.settings_registry import REGISTRY

log = logging.getLogger(__name__)

REPO = "Jeffreybalagot/wishpanel"
EXPORT_FORMAT = "wishpanel-settings"
LEVELS = ["debug", "info", "warning", "error", "critical"]


def tail_logs(
    log_dir: Path, *, lines: int = 300, level: str = "info", q: str = ""
) -> list[dict[str, Any]]:
    """Newest-last JSON log entries at or above ``level`` (searches the current + previous file)."""
    min_idx = LEVELS.index(level) if level in LEVELS else 1
    files = [log_dir / "wishpanel.log.1", log_dir / "wishpanel.log"]
    out: deque[dict[str, Any]] = deque(maxlen=lines)
    needle = q.lower().strip()
    for f in files:
        if not f.is_file():
            continue
        with f.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                lvl = entry.get("level", "info")
                if (LEVELS.index(lvl) if lvl in LEVELS else 1) < min_idx:
                    continue
                if needle and needle not in raw.lower():
                    continue
                out.append(entry)
    return list(out)


def backup_database(db_path: Path, dest: Path) -> Path:
    """Consistent online copy using SQLite's backup API."""
    with closing(sqlite3.connect(db_path)) as src, closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)
    return dest


async def export_settings(settings: SettingsService) -> dict[str, Any]:
    values = {}
    for key in sorted(EDITABLE_KEYS):
        if REGISTRY[key].secret:
            continue
        values[key] = await settings.get(key)
    return {"format": EXPORT_FORMAT, "version": 1, "app_version": __version__, "values": values}


async def import_settings(settings: SettingsService, payload: Any) -> tuple[list[str], list[str]]:
    """Validate everything, then write. Returns (changed keys, ignored keys)."""
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        raise SettingValidationError("file", "This isn't a WishPanel settings export.")
    values = payload.get("values")
    if not isinstance(values, dict):
        raise SettingValidationError("file", "The export has no values.")
    ok: dict[str, Any] = {}
    ignored: list[str] = []
    for key, value in values.items():
        if key not in EDITABLE_KEYS or REGISTRY[key].secret:
            ignored.append(str(key))
            continue
        serialise(REGISTRY[key], value)  # raises SettingValidationError
        ok[key] = value
    changed = [k for k, v in ok.items() if await settings.set(k, v)]
    return sorted(changed), sorted(ignored)


_VER = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?(a|alpha|b|beta|rc)\.?(\d+)?)?")
_STAGE = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2}


def version_key(v: str) -> tuple[int, ...]:
    """PEP 440-ish ordering: 2.0.0a3 < 2.0.0b1 < 2.0.0rc1 < 2.0.0."""
    m = _VER.match(v.strip())
    if not m:
        return (0,)
    major, minor, patch, stage, n = m.groups()
    return (int(major), int(minor), int(patch or 0), _STAGE.get(stage or "", 3), int(n or 0))


async def check_update(http: httpx.AsyncClient) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    try:
        resp = await request_with_retry(
            http, "GitHub", "GET", url, retries=1, headers={"Accept": "application/vnd.github+json"}
        )
    except ExternalServiceError as exc:
        return {
            "ok": False,
            "current": __version__,
            "message": f"Couldn't reach GitHub: {exc.message}.",
        }
    if resp.status_code == 404:
        return {
            "ok": True,
            "current": __version__,
            "latest": None,
            "update_available": False,
            "message": "No releases published yet — you're on a pre-release build.",
        }
    if resp.status_code != 200:
        return {
            "ok": False,
            "current": __version__,
            "message": f"GitHub responded with HTTP {resp.status_code}.",
        }
    data = resp.json()
    latest = str(data.get("tag_name") or "").lstrip("v")
    newer = bool(latest) and version_key(latest) > version_key(__version__)
    return {
        "ok": True,
        "current": __version__,
        "latest": latest or None,
        "url": data.get("html_url"),
        "published_at": data.get("published_at"),
        "update_available": newer,
        "message": f"Version {latest} is available." if newer else "You're up to date.",
    }
