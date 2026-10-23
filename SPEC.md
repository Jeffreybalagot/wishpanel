> **Naming note:** this project ships as **WishPanel** (repo `wishpanel`, container `wishpanel`,
> image `ghcr.io/jeffreybalagot/wishpanel`, default host port **5658**, appdata
> `/mnt/user/appdata/wishpanel`). The spec below predates the rename and still says
> "Comic Requests" / `comicrequests` / 5657 — those refer to the v1 instance, which keeps
> running untouched on 5657 until you switch over. Deviations from the spec made during
> implementation are listed in `docs/DECISIONS.md`.

# Comic Requests v2 — Rebuild Specification (Refined)

**Version:** 2.0-spec  
**Date:** 2026-10-23  
**Author:** Jeff (refined for public release & Claude implementation)  
**Target audience:** Claude (or any agent) building the project + future public users on Unraid / Docker

---

## 1. Vision & Goals

Comic Requests is an **Ombi-style request portal** for **Comics, Manga, and Manhwa**.

Users log in → browse Discover shelves or search → click Request → the title is automatically added to the correct downloader (Kapowarr or Suwayomi) and appears in Kavita.

### Core goals for v2
- **Public-ready** GitHub project that anyone can self-host.
- **One Docker image**, one volume (`/config`), one port.
- **Zero configuration via environment variables after first boot** — everything (including every API key and instance URL) is managed in an Admin → Settings UI (exactly like Ombi).
- Comics, Manga, and Manhwa are **first-class, equal media types**. Each can be independently enabled and mapped to its own metadata provider, downloader, and library.
- Extremely easy Unraid installation via Community Applications template.
- Robust enough for multi-user home servers and small community instances.

### Non-goals for v2.0
- Built-in downloader (we stay a thin orchestration layer).
- Full Prowlarr/Torznab support (Kapowarr is GetComics-only; Mylar3 can be added later).
- Native mobile apps.
- Multi-tenancy / SaaS mode.

---

## 2. High-Level Architecture

```
Users (browser)
    │
    ▼
Comic Requests (this app)
    │  • Auth (Kavita primary)
    │  • Discover / Search / Requests UI
    │  • Admin Settings UI
    │  • Background jobs + rate-limited matching
    │
    ├──► Kavita          (login + library checks + optional scan trigger)
    ├──► Kapowarr        (comics download)
    ├──► Suwayomi        (manga / manhwa download)
    ├──► ComicVine       (comic metadata + covers)
    ├──► AniList         (manga / manhwa metadata + discover)
    ├──► MangaBaka       (manga / manhwa discover lists)
    └──► Comic Book Herald (comic discover lists)
```

The application **never downloads files itself**. It only orchestrates.

All external connections (URLs + API keys) are stored encrypted in the database and edited exclusively through the Admin Settings pages. No secrets live in environment variables or the Unraid template after the first start.

---

## 3. Technology Choices (opinionated for simplicity + robustness)

| Layer              | Choice                          | Why |
|--------------------|---------------------------------|-----|
| Language           | Python 3.12+                    | Existing code base, excellent ecosystem |
| Web framework      | FastAPI                         | Async, type hints, automatic OpenAPI |
| ORM / Migrations   | SQLAlchemy 2.0 + Alembic        | Robust, supports SQLite → Postgres later |
| Database           | SQLite (default) in `/config`   | Zero config, perfect for Unraid. Optional Postgres via settings later. |
| Background jobs    | APScheduler + asyncio           | Simple, no extra containers (Redis/Celery) |
| Auth               | Session cookies (Secure + HttpOnly) + Kavita as primary provider | Matches current behaviour |
| Frontend           | Vanilla HTML + Alpine.js or small Vue/Svelte SPA | Keep it lightweight; Ombi-like dark theme |
| Config storage     | Database (encrypted secrets)    | The whole point of v2 |
| Secrets            | Fernet (symmetric) encrypted at rest | Key derived from a generated `SECRET_KEY` stored in `/config` |
| Logging            | Structured JSON + human-readable | Easy to ship to Unraid logs / Loki |
| Container          | Multi-stage, non-root, PUID/PGID support | Unraid best practice |

---

## 4. Media Types (first-class)

| Media Type | Default Metadata     | Default Downloader | Default Library Mapping | Notes |
|------------|----------------------|--------------------|-------------------------|-------|
| Comic      | ComicVine            | Kapowarr           | Kavita “Comics”         | English publishers preferred |
| Manga      | AniList + MangaBaka  | Suwayomi           | Kavita “Manga”          | JP focus |
| Manhwa     | AniList + MangaBaka  | Suwayomi           | Kavita “Manga” or separate | KR focus; configurable |

Each media type can be:
- Enabled / disabled independently
- Mapped to any available MetadataProvider, Downloader, and LibraryChecker
- Given its own Discover rows, rate limits, and partial-coverage rules

Manhua (CN) is a trivial later addition (same providers).

---

## 5. Data Model (simplified)

### Core tables
- `users` — cached from Kavita (or local accounts later)
- `requests` — **single table** for all media types
  - `id`, `media_type` (comic/manga/manhwa), `external_id`, `title`, `status`, `user_id`, `downloader_id`, `source_id`, `progress` (e.g. “3 of ~223”), `created_at`, `updated_at`, …
- `settings` — key/value with encryption flag for secrets
- `discover_rows` / `discover_cache` — cached shelves
- `jobs` — last run, last error, next run for background tasks
- `audit_log` — admin actions (settings changes, request approvals, etc.)

### Request statuses
`pending` → `approved` → `added` → `downloading` → `partial` / `in_library` / `failed` / `waiting_for_source` / `denied`

Partial logic (kept from v1, made configurable):
- Expected chapters ≥ 5
- Have < 80 % of expected
- Gap ≥ 3 chapters

---

## 6. Provider Interfaces (the key to extensibility)

All external systems are behind clean interfaces so new ones can be added without touching core code.

```python
class AuthProvider(Protocol):
    async def login(self, username: str, password: str) -> User | None: ...
    async def get_user(self, token: str) -> User | None: ...
    async def is_admin(self, user: User) -> bool: ...

class MetadataProvider(Protocol):
    async def search(self, query: str, media_type: MediaType) -> list[SearchResult]: ...
    async def get_details(self, external_id: str) -> TitleDetails: ...

class DiscoverSource(Protocol):
    async def get_rows(self, media_type: MediaType) -> list[DiscoverRow]: ...

class Downloader(Protocol):
    async def add(self, request: Request) -> DownloaderResult: ...
    async def get_status(self, external_id: str) -> Status: ...
    async def list_sources(self) -> list[Source]: ...          # Suwayomi
    async def list_root_folders(self) -> list[Folder]: ...     # Kapowarr

class LibraryChecker(Protocol):
    async def is_in_library(self, title: str | external_id, media_type: MediaType) -> bool: ...
    async def trigger_scan(self, media_type: MediaType) -> None: ...
```

Concrete implementations:
- `KavitaAuthProvider`, `KavitaLibraryChecker`
- `ComicVineMetadataProvider`
- `AniListMetadataProvider`, `MangaBakaDiscoverSource`
- `ComicBookHeraldDiscoverSource`
- `KapowarrDownloader`
- `SuwayomiDownloader`

---

## 7. Admin Settings UI (Ombi-style — the heart of v2)

Left-side navigation, one page per topic. Every connection page has:
- **Test** button that actually calls the remote API and returns a clear human message
- Live dropdowns populated from the connected service (Kapowarr root folders, Suwayomi sources, Kavita libraries)
- Secrets shown as `••••••••abcd` (last 4 characters only)
- Changes apply immediately (no container restart)

### Required Settings pages for v2.0

1. **General**  
   Site name, base URL, logo upload, default landing tab, theme (dark/light), language

2. **Authentication**  
   Login provider (Kavita now; local accounts later), which Kavita role = admin, failed-login throttle (default 5 / 15 min)

3. **Users**  
   List of known users, per-user daily limit override, which media types they can request, block/unblock, auto-approve toggle

4. **Media Types**  
   Enable/disable Comic / Manga / Manhwa. For each: choose MetadataProvider, Downloader, LibraryChecker mapping

5. **Library — Kavita**  
   URL, API key or admin credentials, mapping of Kavita libraries → Comic / Manga / Manhwa, “Test Connection”, “Trigger Scan”

6. **Downloader — Kapowarr**  
   URL, API key, root folder (live dropdown), monitor new issues, auto-search, quality profile if available, “Test”

7. **Downloader — Suwayomi**  
   URL, optional auth, allowed sources (checkbox list loaded live), source priority order, auto-download new chapters, “Test”

8. **Metadata — ComicVine**  
   API key, cache TTL, preferred publishers (English first), blocked publishers (Panini, Glénat, etc.)

9. **Metadata — AniList / MangaBaka**  
   On/off, content rating filter (exclude adult/pornographic), country mapping (JP → Manga, KR → Manhwa)

10. **Discover**  
    Which rows appear for each media type, order, items per row, refresh interval, character list for comics, rate-limit seconds between ComicVine matches, “Refresh lists now”

11. **Requests**  
    Global daily limit, require approval vs auto-approve, duplicate rules, partial-coverage thresholds

12. **Jobs**  
    Schedules for: Discover refresh, not-found retry, availability sync, library scan. “Run now” buttons + last run / last error display

13. **Logs & About**  
    Recent structured logs, current version, check for updates, download DB backup, export/import settings (JSON, secrets excluded)

---

## 8. Bootstrap & First-Run Experience (critical for Unraid simplicity)

On first start with an empty `/config`:

1. Generate `SECRET_KEY` and store it in `/config/secret.key`
2. Run Alembic migrations
3. Create default settings rows
4. Show a simple first-run wizard (or just the Settings page) that forces the admin to:
   - Connect Kavita (required for login)
   - Optionally connect Kapowarr / Suwayomi / ComicVine
5. Import any old environment variables **once** (if present) into the DB, then ignore them forever.

**Only these environment variables are ever needed:**

```bash
PUID=99
PGID=100
TZ=America/Los_Angeles
PORT=8080                  # optional, default 8080
LOG_LEVEL=info             # optional
BASE_URL=                  # optional, for reverse-proxy correctness
```

Everything else lives in the database and is edited in the UI.

---

## 9. Security & Robustness Requirements

- All admin routes require server-side admin check (not just UI hiding).
- Secrets encrypted at rest (Fernet). Never logged, never returned in full via API.
- Rate limiting on login (per IP) and on external API calls (per provider).
- Request daily limits enforced server-side.
- Structured JSON logging + human-readable console output.
- `/health` endpoint that checks DB + critical external services (degraded status is OK).
- Docker HEALTHCHECK using `/health`.
- Graceful degradation: if Kapowarr is down, comics requests queue and show “waiting for downloader”.
- Input validation everywhere (Pydantic models).
- SQL injection impossible (ORM only).
- CSRF protection on state-changing endpoints.
- Session cookies: `Secure`, `HttpOnly`, `SameSite=Lax`.
- Audit log for all admin actions and request status changes.

---

## 10. Background Jobs

Managed via APScheduler (persistent job store in SQLite).

| Job                        | Default Schedule     | Purpose |
|----------------------------|----------------------|---------|
| Discover refresh (comics)  | Every 24 h           | Pull CBH lists + match to ComicVine |
| Discover refresh (manga)   | Every 12 h           | Pull AniList + MangaBaka rows |
| Not-found retry            | Every 24 h           | Re-search Suwayomi for previously unmatched titles |
| Availability sync          | Every 6 h            | Update partial / in_library status from downloaders + Kavita |
| Library scan trigger       | After successful download | Optional Kavita scan |

All jobs expose “Run now” + last success / last error in the Jobs settings page.

---

## 11. Unraid / Docker Deployment (must be dead simple)

### Official image
`ghcr.io/<user>/comicrequests:latest` (multi-arch amd64 + arm64)

### docker-compose.example.yml
```yaml
services:
  comicrequests:
    image: ghcr.io/<user>/comicrequests:latest
    container_name: comicrequests
    environment:
      - PUID=99
      - PGID=100
      - TZ=America/Los_Angeles
    volumes:
      - /mnt/user/appdata/comicrequests:/config
      # Optional read-only library mounts for “In library” folder checks
      # - /mnt/user/media/comics:/comics:ro
      # - /mnt/user/media/manga:/manga:ro
    ports:
      - "5657:8080"
    restart: unless-stopped
```

### Unraid Community Applications
- Full XML template in the repo
- Only the `/config` path and port need to be set by the user
- After first start the user opens the WebUI and configures everything in Settings

### Migration from v1
- One-time importer that reads the old `requests.db` and environment variables
- Side-by-side running on a different port until the user switches Nginx Proxy Manager

---

## 12. Build Plan (five milestones — each one is shippable)

### Milestone 1 — Skeleton
- Repo structure, Dockerfile (multi-stage, non-root), docker-compose, GitHub Actions (lint + test + multi-arch image)
- `/config` volume, Alembic, settings table, encrypted secrets
- `/health` + Docker HEALTHCHECK
- Kavita login working
- Basic dark UI shell (Discover / Search / Requests / Admin tabs)

### Milestone 2 — Admin Settings Framework
- Full Settings UI skeleton with left nav
- General + Authentication + Library (Kavita) + Downloader (Kapowarr + Suwayomi) + Metadata (ComicVine) pages
- Test buttons, live dropdowns, masked secrets, immediate apply
- Users page (list + basic controls)

### Milestone 3 — Requests Core
- Single `Request` model for all media types
- Kapowarr and Suwayomi downloader implementations
- Daily limits, duplicate detection, status machine
- My Requests page + Admin Requests management (approve / deny / retry / change source / delete)

### Milestone 4 — Discover + Search + Jobs
- All DiscoverSources and MetadataProviders
- Background matching queue with proper rate limits
- Discover and Jobs settings pages
- Search enrichment (real covers, issue counts, etc.)

### Milestone 5 — Polish + Release
- v1 database importer
- Unraid CA template
- Comprehensive README + setup guide
- Export/import settings
- Logs & About page
- Final security pass + test coverage
- Public GitHub release + GHCR image

---

## 13. Suggested Repository Structure

```
comicrequests/
├── .github/workflows/
│   ├── ci.yml
│   └── release.yml
├── app/
│   ├── main.py
│   ├── config.py                 # bootstrap only
│   ├── db/
│   │   ├── models.py
│   │   ├── session.py
│   │   └── migrations/
│   ├── auth/
│   ├── providers/                # all the interfaces + implementations
│   │   ├── auth/
│   │   ├── metadata/
│   │   ├── discover/
│   │   ├── downloaders/
│   │   └── library/
│   ├── api/                      # FastAPI routers
│   ├── services/                 # business logic
│   ├── jobs/
│   └── web/                      # static frontend or templates
├── tests/
├── docker/
│   ├── Dockerfile
│   └── entrypoint.sh
├── unraid/
│   └── comicrequests.xml
├── docs/
├── docker-compose.example.yml
├── .env.example
├── pyproject.toml
├── README.md
├── LICENSE
├── CHANGELOG.md
└── SPEC.md                       # this document
```

---

## 14. Open Decisions (defaults recommended)

| Question                              | Recommended Default for v2.0          | Notes |
|---------------------------------------|---------------------------------------|-------|
| Public or private repo?               | **Public**                            | Encourages contributions and visibility |
| Login                                 | Kavita only                           | Local accounts as Milestone 6 |
| Manhwa library                        | Same Kavita library as Manga          | Configurable in Settings |
| Approval workflow                     | Auto-approve for everyone             | Approval queue can be enabled per-user or globally later |
| Notifications                         | Later (v2.1)                          | Discord / email / Pushover |
| Database                              | SQLite                                | Postgres optional later via settings |

---

## 15. Success Criteria for v2.0

- A brand-new user can:
  1. Install the Unraid template (or docker-compose)
  2. Open the WebUI
  3. Connect Kavita + Kapowarr + Suwayomi + ComicVine entirely through the Settings UI
  4. Request a comic and a manga
  5. See them appear in Kavita
- Zero secrets in environment variables or the Unraid template after first configuration.
- All three media types work as first-class citizens.
- Background jobs and rate limits prevent API bans.
- The old v1 instance can be migrated with a single import command / button.
- The project has tests, CI, multi-arch images, and a clean public README.

---

## 16. Notes for the Implementing Agent (Claude)

1. Start with Milestone 1 and do not move on until the container boots cleanly with an empty `/config` and Kavita login works.
2. Keep the frontend simple. Prefer Alpine.js + plain HTML or a minimal Vue 3 SPA over heavy frameworks.
3. Every external call must have timeouts, retries with backoff, and clear error messages surfaced to the admin.
4. Never log API keys or full session tokens.
5. Write the provider interfaces first — they are the architectural backbone.
6. The Settings pages are the most important UX. Make the Test buttons excellent.
7. Preserve every piece of behaviour listed under “Behaviour worth keeping” in the original spec.
8. The live site on port 5657 must keep running until the user deliberately switches over.

This specification is intentionally opinionated so that Claude (or any implementer) can move quickly without re-deciding architecture at every step.

---

**End of refined specification.**  
This document should live in the root of the new repository as `SPEC.md`.
