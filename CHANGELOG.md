# Changelog

All notable changes to WishPanel are documented here. Format: [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added — Milestone 1.1 (hardening)
- One-time setup claim code: printed to the container log and `/config/setup-code.txt` (0600),
  required for every setup action (claim, Kavita connection test, completion), throttled,
  only its hash stored, destroyed when setup completes.
- `uv.lock`: CI and the Docker image install exactly the locked dependency versions; CI fails
  if the lockfile is stale. uv version pinned identically in both.
- Image pipeline: build both architectures once, smoke-test each from that build, publish only
  if all pass — for `main` and for release tags. Published digests are verified to equal the
  tested digest and recorded in the run summary.
- Regression tests for the claim code, health split, pipeline ordering and lockfile usage.

### Changed — Milestone 1.1
- `/health` is now local-only (database + `/config` writable). External service status moved
  to the admin-only `/api/admin/services` and is shown separately on Admin → Overview.
- `/api/setup/status` no longer reveals a pre-filled Kavita URL; it is returned only after a
  valid setup code.

### Added — Milestone 1 (skeleton)
- FastAPI app, SQLite + Alembic migrations run automatically on boot.
- Encrypted settings store (Fernet; key generated in `/config/secret.key` on first boot) with a typed settings registry.
- First-run wizard: connect Kavita, sign in as a Kavita admin, setup locks.
- Kavita sign-in with server-side sessions, CSRF protection and per-IP/per-user login throttling.
- One-time import of v1 environment variables into the database.
- `/health` endpoint (database + Kavita) and Docker `HEALTHCHECK`.
- Dark UI shell (Alpine.js, vendored): Discover / Search / Requests / Admin tabs, admin overview.
- Multi-stage Docker image with PUID/PGID/UMASK, never runs as root.
- GitHub Actions: lint, tests (Python 3.12/3.13), container smoke test, multi-arch GHCR images.
- Audit log of logins, failed logins, setup and env imports.
