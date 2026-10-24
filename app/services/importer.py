"""One-time importer for Comic Requests v1's ``requests.db``.

Reads a *copy* of the v1 database (uploaded, or mounted read-only at
``/import/requests.db``) — the original is never opened for writing. Imports:

* ``requests`` (comics → Kapowarr) and ``manga_requests`` (manga/manhwa → Suwayomi)
  as WishPanel requests, keeping their downloader ids so the availability sync
  picks up real progress;
* v1 users by username. Someone who hasn't signed in to WishPanel yet gets a
  placeholder that is linked automatically the first time they sign in;
* ``cbh_titles`` ComicVine matches into the match cache, so Discover doesn't
  have to re-match hundreds of titles.

Running it twice is safe: anything already imported (same title id for the same
user) is skipped.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.textmatch import dedupe
from app.db.base import utcnow
from app.db.models import MatchCache, Request, User
from app.domain import RequestStatus
from app.services.discover import match_key

log = logging.getLogger(__name__)
S = RequestStatus
MAX_UPLOAD = 200 * 1024 * 1024


class ImportError_(Exception):
    pass


@dataclass
class ImportResult:
    comics: int = 0
    manga: int = 0
    skipped: int = 0
    users_created: list[str] = field(default_factory=list)
    users_matched: int = 0
    matches: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.comics} comic and {self.manga} manga request(s) imported"]
        if self.skipped:
            parts.append(f"{self.skipped} already present")
        if self.matches:
            parts.append(f"{self.matches} Discover match(es)")
        if self.users_created:
            parts.append(f"{len(self.users_created)} user(s) waiting for first sign-in")
        return "; ".join(parts) + "."


def _ts(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (TypeError, ValueError, OSError):
        return utcnow()


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return []
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]  # noqa: S608 - fixed names


def read_v1(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Copy the database (and its WAL, if any) to a temp dir and read the tables we need."""
    if not path.is_file():
        raise ImportError_("No v1 database found.")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "requests.db"
        shutil.copyfile(path, copy)
        for suffix in ("-wal", "-shm"):
            side = path.with_name(path.name + suffix)
            if side.is_file():
                shutil.copyfile(side, copy.with_name(copy.name + suffix))
        try:
            with closing(sqlite3.connect(copy)) as conn:
                header = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                names = {h[0] for h in header}
                if not names & {"requests", "manga_requests"}:
                    raise ImportError_("That doesn't look like a Comic Requests v1 database.")
                return {t: _rows(conn, t) for t in ("requests", "manga_requests", "cbh_titles")}
        except sqlite3.DatabaseError as exc:
            raise ImportError_(f"Couldn't read the database: {exc}") from exc


async def _user_for(
    db: AsyncSession, username: str | None, cache: dict[str, User], result: ImportResult
) -> User | None:
    name = (username or "").strip()
    if not name:
        return None
    key = name.lower()
    if key in cache:
        return cache[key]
    user = await db.scalar(select(User).where(func.lower(User.username) == key).limit(1))
    if user is None:
        user = User(provider="kavita", external_id=key, username=name)
        db.add(user)
        await db.flush()
        result.users_created.append(name)
    else:
        result.users_matched += 1
    cache[key] = user
    return user


def _alts(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x]
        except ValueError:
            pass
        return [p.strip() for p in raw.replace("|", "\n").split("\n") if p.strip()]
    return []


async def _exists(db: AsyncSession, user: User | None, provider: str, external_id: str) -> bool:
    stmt = select(Request.id).where(
        Request.provider == provider, Request.external_id == external_id
    )
    stmt = stmt.where(Request.user_id == user.id) if user else stmt.where(Request.user_id.is_(None))
    return (await db.scalar(stmt.limit(1))) is not None


async def run_import(db: AsyncSession, data: dict[str, list[dict[str, Any]]]) -> ImportResult:
    result = ImportResult()
    users: dict[str, User] = {}

    for r in data.get("requests", []):
        cv = str(r.get("comicvine_id") or "").strip()
        if not cv.isdigit():
            result.warnings.append(f"Skipped comic “{r.get('title')}”: no ComicVine id.")
            continue
        user = await _user_for(db, r.get("username"), users, result)
        if await _exists(db, user, "comicvine", cv):
            result.skipped += 1
            continue
        ref = str(r.get("kapowarr_id") or "").strip() or None
        v1_status = str(r.get("status") or "")
        if v1_status == "failed" or not ref:
            status, msg = S.FAILED.value, r.get("note") or "Imported from v1 (failed there)."
        else:
            status, msg = (
                S.ADDED.value,
                "Imported from v1 — progress updates on the next availability sync.",
            )
        db.add(
            Request(
                user_id=user.id if user else None,
                media_type="comic",
                provider="comicvine",
                external_id=cv,
                title=str(r.get("title") or f"ComicVine {cv}")[:500],
                year=str(r.get("year") or "")[:8] or None,
                publisher=r.get("publisher"),
                cover_url=r.get("cover"),
                downloader="kapowarr",
                downloader_ref=ref,
                status=status,
                status_message=str(msg)[:500],
                approved_by="v1",
                imported_from="v1",
                created_at=_ts(r.get("created")),
            )
        )
        result.comics += 1

    for r in data.get("manga_requests", []):
        key = str(r.get("key") or "")
        prefix, _, ext = key.partition(":")
        provider = {"al": "anilist", "mb": "mangabaka"}.get(prefix)
        if not provider or not ext.isdigit():
            result.warnings.append(f"Skipped manga “{r.get('title')}”: unknown id {key!r}.")
            continue
        kind = r.get("kind") if r.get("kind") in ("manga", "manhwa") else "manga"
        user = await _user_for(db, r.get("username"), users, result)
        if await _exists(db, user, provider, ext):
            result.skipped += 1
            continue
        ref = str(r.get("suwayomi_id") or "").strip() or None
        alts = dedupe(_alts(r.get("alt")))
        if r.get("status") == "failed" or not ref:
            status = S.WAITING_FOR_SOURCE.value
            msg = "Imported from v1 (not found there) — will search Suwayomi again."
            next_at = utcnow() + timedelta(minutes=5)
        else:
            status, msg, next_at = (
                S.DOWNLOADING.value,
                "Imported from v1 — progress updates on the next availability sync.",
                None,
            )
        known = r.get("known_ch")
        db.add(
            Request(
                user_id=user.id if user else None,
                media_type=kind,
                provider=provider,
                external_id=ext,
                title=str(r.get("title") or key)[:500],
                alt_titles=json.dumps(alts) if alts else None,
                year=str(r.get("year") or "")[:8] or None,
                cover_url=r.get("cover"),
                anilist_id=int(ext) if provider == "anilist" else None,
                downloader="suwayomi",
                downloader_ref=ref,
                status=status,
                status_message=msg,
                have_count=int(known) if str(known or "").isdigit() else None,
                next_attempt_at=next_at,
                approved_by="v1",
                imported_from="v1",
                created_at=_ts(r.get("created")),
            )
        )
        result.manga += 1

    for t in data.get("cbh_titles", []):
        title = str(t.get("title") or "").strip()
        if not title or t.get("status") not in ("ok", "miss"):
            continue
        hint = t.get("hint")
        try:
            hint = int(hint) if hint not in (None, "") else None
        except (TypeError, ValueError):
            hint = None
        k = match_key(title, hint)
        row = await db.get(MatchCache, k)
        if row is not None and row.status == "ok":
            continue
        if row is None:
            row = MatchCache(key=k, title=title, hint_year=hint)
            db.add(row)
        if t.get("status") == "ok" and str(t.get("comicvine_id") or "").isdigit():
            row.status = "ok"
            row.comicvine_id = int(t["comicvine_id"])
            row.name = t.get("name")
            row.year = str(t.get("year") or "")[:8] or None
            row.publisher = t.get("publisher")
            row.issues = int(t["issues"]) if str(t.get("issues") or "").isdigit() else None
            row.cover_url = t.get("cover")
            row.score = t.get("score")
        else:
            row.status = "miss"
        row.tries = int(t.get("tries") or 1)
        row.updated_at = utcnow()
        result.matches += 1

    await db.flush()
    return result


def result_json(r: ImportResult) -> dict[str, Any]:
    return {**asdict(r), "summary": r.summary()}
