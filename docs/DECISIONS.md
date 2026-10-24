# Implementation decisions & deviations from SPEC.md

Short, reviewable notes on choices made while building. Each lists the spec section it touches.

## Milestone 1

**Names, port, volume** — Project ships as WishPanel: container `wishpanel`, image
`ghcr.io/jeffreybalagot/wishpanel` (GHCR requires lowercase), host port `5658`, appdata
`/mnt/user/appdata/wishpanel`. v1 keeps `comicrequests` / `5657`.

**Session cookie `Secure` flag (§9)** — The spec asks for `Secure` always. Most Unraid users
first open the UI over plain `http://<lan-ip>:5658`, where browsers silently drop `Secure`
cookies and login would appear broken. Setting `auth.cookie_secure` defaults to `auto`: Secure
whenever the request arrived over HTTPS (directly or via a trusted proxy's
`X-Forwarded-Proto`). `always` / `never` are available. `HttpOnly` and `SameSite=Lax` are
always set.

**First-run security** — Before setup there is no admin. Setup can only finish by signing in
with an account holding the admin role *on the Kavita being configured*, and it locks
permanently afterwards (returns 409). The "someone on the LAN claims it first" window is closed
by the M1.1 setup code (below).

**Server-side sessions** — Sessions are rows in SQLite keyed by SHA-256 of the cookie token, so
a leaked database can't be replayed as cookies, logout is real, and blocking a user ends their
sessions. Kavita's JWT is read once at login for its role claims and then discarded; WishPanel
never stores Kavita user tokens or passwords.

**CSRF (§9)** — Authenticated state-changing requests require an `X-CSRF-Token` header matching
the session. Unauthenticated POSTs (login, setup) require `Content-Type: application/json`,
which forces a CORS preflight for cross-site callers; combined with `SameSite=Lax` this blocks
login CSRF.

**Login throttling (§9)** — Per IP *and* per username (default 5 failures / 15 min, both
adjustable in Settings). Per-username stops a spoofed `X-Forwarded-For` from bypassing the
per-IP limit.

**Extra optional env vars (§8)** — `UMASK` (Unraid convention) and `FORWARDED_ALLOW_IPS`
(which proxies may set `X-Forwarded-*`; without it uvicorn can't safely see real client IPs
or HTTPS behind NPM). Both optional; neither holds configuration or secrets. `CONFIG_DIR` exists
for development/tests.

**Encryption key (§3)** — `secret.key` holds 32 random bytes (0600). The Fernet key is derived
with HKDF (`info="wishpanel/settings/v1"`) so future uses (e.g. signed export files) get their
own derived keys.

**Provider interfaces (§6)** — Adjusted for real use: every provider has `test_connection()`
(drives the Settings Test buttons, never raises); `AuthProvider.login` raises
`InvalidCredentials` instead of returning `None` so "wrong password" and "Kavita is down" are
distinguishable; `is_admin` takes the configured role name; `get_user(token)` was dropped
because sessions are WishPanel's own; `LibraryChecker` gains `list_libraries()` for the
library-mapping dropdown; `Downloader.add` takes `TitleDetails` rather than the DB model so
providers stay free of ORM imports.

**Tables (§5)** — Milestone 1 creates `settings`, `users`, `sessions`, `audit_log`. `requests`,
`discover_*` and `jobs` arrive with their milestones as new Alembic revisions.

**User identity** — Kavita's login response has no numeric user id, so users are keyed on
lower-cased Kavita username. If an admin renames a Kavita user, they'd appear as a new
WishPanel user. Revisit when the Kavita API key is configured (M2) and `/api/Users` can be used.

**Status `waiting_for_downloader`** — Added to the status enum because §9 calls for it, though
§5's list omits it.

**Repo layout (§13)** — Auth logic lives in `app/services/auth.py` + `app/api/deps.py` rather
than a separate `app/auth/` package, to avoid a third place for the same concern.

**Frontend** — Alpine.js 3.17.5 is vendored (`app/web/static/vendor/`), so the UI works with no
internet access and no CDN. CSP allows `'unsafe-eval'` for scripts only because Alpine evaluates
`x-*` expressions with `Function()`; there are no inline scripts.

## Milestone 1.1 (hardening)

**Setup claim code** — 16 characters from a 30-symbol alphabet with no look-alikes
(≈78 bits), grouped `XXXX-XXXX-XXXX-XXXX`, case/space/dash-insensitive. Required by *every*
setup action — `claim`, `kavita/test` and `complete` — so an unauthenticated visitor can't even
use the Kavita test as a probe of your network. Shown in the container log on every boot while
unclaimed and in `/config/setup-code.txt` (0600). Only a SHA-256 is stored in the database; a
code file that doesn't match the stored hash is ignored and replaced, so writing your own code
into the file doesn't work. Deleting the file rotates the code on next boot. Wrong codes are
throttled per IP with the login limits and audited. The code is deliberately *not* matched by
the log-redaction filter (it has to be readable in the log); it is worthless once setup
completes, at which point the hash is cleared and the file deleted.

**Health split** — `/health` (Docker HEALTHCHECK, CI smoke test) checks only the container:
database and `/config` writability. External reachability moved to admin-only
`/api/admin/services` (cached 30 s). Reason: a Kavita outage made the old `/health` report
`degraded`; harmless today, but any orchestrator treating that as unhealthy would restart
WishPanel for someone else's problem, and the endpoint was also revealing topology to anyone.
This supersedes the spec's "/health checks DB + critical external services" (§9).

**Locked dependencies** — `uv.lock` is the single source of versions. CI runs `uv lock --check`
and `uv sync --locked --extra dev`; the Dockerfile runs `uv sync --locked --no-dev` (no dev
tools in the image). uv itself is pinned to the same version in CI and Docker; a test enforces
that they match. The base image is still referenced by tag (`python:3.13-slim`); pinning it by
digest (with Dependabot to bump it) is a reasonable follow-up.

**Build → smoke → publish** — One reusable workflow (`image.yml`) used by both `ci.yml` (main →
`:edge` + `:sha-<commit>`) and `release.yml` (tags → `:X.Y.Z`, `:X.Y`, `:latest`). It builds
amd64 + arm64 once into an OCI archive (nothing pushed), boots each architecture *from that
archive* with an empty `/config` (`docker/smoke-test.sh`; arm64 under QEMU), and only then
copies the same archive to GHCR with `skopeo --preserve-digests`, verifying each pushed tag's
digest equals the built one. So the published digest is exactly the tested digest. Pull
requests run the same build and smoke tests but never publish. Provenance/SBOM attestations are
off for now to keep the archive a plain two-platform index; they can be added back in M5.

**What the smoke test asserts** — healthy within 180 s; `/health` ok and free of external
checks; UI and vendored Alpine served; PID 1 runs as the requested PUID (1001), not root;
`secret.key`, `setup-code.txt`, `wishpanel.db` owned by PUID:PGID, key and code mode 0600;
setup code printed in the log; wrong/missing code rejected on claim and Kavita test; right code
accepted; after `docker restart` it's healthy again with the same key and code.

## Milestone 2 (admin settings)

**Schema-driven pages** — `app/services/settings_pages.py` lists every page, field, widget and
help text; the frontend renders from `GET /api/admin/settings/schema`. Only keys on a page are
writable through the API, so internal state (`setup.completed`, `setup.claim_hash`,
`general.logo_version`) can never be changed from the browser. A startup check fails fast if a
page references an unknown key or a secret field uses a non-secret widget.

**Saving is all-or-nothing** — the whole payload is validated before anything is written; a bad
field returns `422 {key, message}` and the UI highlights it.

**Test before save** — Test buttons and dropdowns post the form's current values; any secret the
admin hasn't retyped is taken from storage. Testing never saves.

**Kavita URL guard** — Kavita is the login provider, so a changed Kavita URL must answer
`/api/health` before it's saved; otherwise a typo would lock everyone out.

**API shapes verified** — Kavita against its published `openapi.json` (0.9.1: `x-api-key`
header, `/api/Library/libraries`, `/api/Library/scan`, with the `/api/Plugin/authenticate`
fallback for older servers); Kapowarr against its source (`/api/system/about`,
`/api/rootfolder`, `api_key` query parameter); Suwayomi by querying Jeff's v2.4.2366 instance
(`aboutServer`, `sources`, `login` mutation, auth modes NONE/BASIC_AUTH/SIMPLE_LOGIN/UI_LOGIN);
ComicVine's documented `status_code` (1 OK, 100 bad key, 107 rate limited).

**Stable user ids** — Kavita's login response includes a numeric `id`; users are now keyed
`id:<n>`. Rows from 2.0.0a2 (keyed by lower-cased username) are re-keyed on the next sign-in.

**Logo** — PNG/JPEG/WebP only, detected by magic bytes, ≤512 KB, stored in `/config/branding`.
SVG is refused because it can carry script.

**Not in M2** — Media Types mapping, Requests, AniList/MangaBaka, Discover, Jobs and Logs pages
are visible in the nav but marked for their milestones. Per-user limits and auto-approve are
stored now and enforced when requests exist (M3).
