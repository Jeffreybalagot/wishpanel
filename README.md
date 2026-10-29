<p align="center"><img src="app/web/static/favicon.svg" width="72" alt=""></p>

# WishPanel

An Ombi-style request portal for **comics, manga and manhwa**. People sign in with their
Kavita account, find a title, and request it — WishPanel hands it to the right downloader
(Kapowarr for comics, Suwayomi for manga/manhwa) and it shows up in Kavita.

WishPanel never downloads anything itself; it's a thin, friendly orchestration layer.

> **Status: 2.0.0.** Kavita sign-in, admin settings, requests for comics/manga/manhwa, Discover
> and Search, background jobs, the v1 importer and Logs & About. See [`CHANGELOG.md`](CHANGELOG.md).

## Highlights

- **One container, one volume, one port.** Everything lives in `/config`.
- **No configuration in environment variables.** Every URL, API key and option is set in
  the web UI and stored encrypted at rest (Fernet, key generated on first boot).
- **Kavita accounts.** No new passwords; Kavita's `Admin` role (configurable) = WishPanel admin.
- **Discover & Search.** AniList and MangaBaka shelves for manga/manhwa; Comic Book Herald lists and
  character reading orders for comics, matched to ComicVine in the background.
- **Requests that finish.** Comics → Kapowarr, manga/manhwa → your chosen Suwayomi sources in
  priority order; progress, "partially available" and Kavita scans are tracked automatically.
- **Unraid friendly.** `PUID`/`PGID`/`UMASK`, Docker `HEALTHCHECK`, multi-arch (amd64 + arm64).

## Quick start

### docker compose

```yaml
services:
  wishpanel:
    image: ghcr.io/jeffreybalagot/wishpanel:latest   # :edge = latest main-branch build
    container_name: wishpanel
    environment:
      - PUID=99
      - PGID=100
      - TZ=America/Los_Angeles
    volumes:
      - /mnt/user/appdata/wishpanel:/config
    ports:
      - "5658:8080"
    restart: unless-stopped
```

Then open `http://<server>:5658` and follow the three-step setup:

1. **Setup code** — a one-time code that proves you control the container. It's printed in the
   container log (Unraid: Docker → wishpanel → Logs) and saved as `setup-code.txt` in the
   appdata folder. It's deleted automatically once setup is done.
2. **Kavita URL** — the address the WishPanel container can reach (e.g. `http://10.0.0.5:5000`).
   Use **Test connection** to check it.
3. **Admin sign-in** — sign in with a Kavita account that has the `Admin` role. That account
   becomes the first WishPanel admin and setup locks.

### Unraid

A Community Applications template lives in [`unraid/wishpanel.xml`](unraid/wishpanel.xml).
Until it's listed in CA, add the container manually with the values from the compose file above.

### Running next to Comic Requests v1

WishPanel is designed to run side-by-side with v1: different container name (`wishpanel`),
different host port (`5658`), different appdata folder.

To bring v1's requests over, either upload v1's `requests.db` in **Admin → Logs & About**, or add
v1's data folder as a **read-only** path mapped to `/import` and press *Import*. v1 is only read,
never changed, and the import can be repeated safely.

## After setup — a 5-minute checklist

1. **Library — Kavita**: add an API key (admin user), map the Comics / Manga / Manhwa libraries.
2. **Downloader — Kapowarr**: URL, API key, root folder → Test → Save.
3. **Downloader — Suwayomi**: URL, auth mode, then add the sources WishPanel may use, in priority order.
4. **Metadata — ComicVine**: API key (or set *Media Types → Comic search uses* to Kapowarr).
5. **Jobs**: press *Run now* on both Discover refreshes to fill the shelves straight away.
6. **Requests / Users**: daily limits, approval, per-user media types.

## Environment variables

None are required beyond the usual Unraid trio. All are optional:

| Variable | Default | Purpose |
|---|---|---|
| `PUID` / `PGID` | `99` / `100` | User/group the app runs as (it never runs as root) |
| `UMASK` | `022` | File creation mask |
| `TZ` | `UTC` | Time zone for logs |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `BASE_URL` | — | Serve under a sub-path, e.g. `/wishpanel` (full URLs are accepted too) |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse-proxy IPs trusted for `X-Forwarded-*` (real client IPs, HTTPS detection) |
| `PORT` | `8080` | Port inside the container |

Coming from v1? On the very first boot only, WishPanel imports `KAVITA_URL`, `KAVITA_API_KEY`,
`KAPOWARR_URL`, `KAPOWARR_API_KEY`, `SUWAYOMI_URL`, `SUWAYOMI_USERNAME`, `SUWAYOMI_PASSWORD`,
`COMICVINE_API_KEY` and `DAILY_REQUEST_LIMIT` if they are set — then ignores them forever.

## What's in `/config`

| Path | What |
|---|---|
| `wishpanel.db` | SQLite database (settings, users, requests, discover cache, jobs, audit log) |
| `setup-code.txt` | One-time setup code (only until setup is completed) |
| `secret.key` | Encryption key for stored secrets. **Back it up with the database** — without it, saved API keys must be re-entered. |
| `logs/wishpanel.log` | Structured JSON logs (rotated, 5 × 5 MB) — easy to ship to Loki |

## Health

`GET /health` reports only on the container itself (database reachable, `/config` writable):
`ok` → HTTP 200, `error` → HTTP 503. It never contacts Kavita or anything else, so an outage
elsewhere can't get WishPanel marked unhealthy or restarted. The image's `HEALTHCHECK` uses it.

Reachability of Kavita (and later Kapowarr, Suwayomi, …) is shown separately to admins on
Admin → Overview, backed by the admin-only `GET /api/admin/services`.

## Images

`:edge` is built from `main`, `:latest` and `:X.Y.Z` from release tags, and every build also
gets a digest. Both architectures are smoke-tested from the built archive before anything is
pushed, and the pushed digest is verified to be the tested one. For reproducible deployments
pin the digest shown in the workflow run summary: `ghcr.io/jeffreybalagot/wishpanel@sha256:…`.

## Reverse proxy

Works behind Nginx Proxy Manager, SWAG, Caddy, Traefik. Set `FORWARDED_ALLOW_IPS` to your
proxy's IP so login throttling sees real client IPs and session cookies get the `Secure` flag
on HTTPS. For a sub-path, also set `BASE_URL`.

## Development

Dependencies are locked in `uv.lock`; CI and the Docker image install exactly those versions.

```bash
uv sync --locked --extra dev   # create .venv from the lockfile
uv run pytest                  # tests
uv run ruff check .            # lint
CONFIG_DIR=./.devconfig uv run python -m app   # http://localhost:8080
```

After changing dependencies in `pyproject.toml`, run `uv lock` and commit `uv.lock`.

Layout: `app/providers/` holds the provider interfaces (`base.py`) and implementations
(Kavita, Kapowarr, Suwayomi, ComicVine, AniList, MangaBaka, Comic Book Herald). `app/jobs/` has the
scheduler and job list.
`app/services/` is business logic, `app/api/` the FastAPI routers, `app/web/static/` the
Alpine.js frontend (no build step). Design notes: [`docs/DECISIONS.md`](docs/DECISIONS.md).

## License

MIT
