# Changelog

All notable changes to WishPanel are documented here. Format: [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

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
