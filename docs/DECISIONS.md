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

**First-run security** — Before setup there is no admin, so `/api/setup/*` is open. It can only
finish by signing in with an account holding the admin role *on the Kavita being configured*,
and it locks permanently afterwards (returns 409). The remaining window is "someone on your LAN
completes setup before you do"; a one-time setup token printed to the container log would close
it at the cost of an extra step. **Open question for review.**

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
