"""Application factory."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import admin_settings as admin_settings_api
from app.api import admin_users as admin_users_api
from app.api import auth as auth_api
from app.api import setup as setup_api
from app.api import system as system_api
from app.config import BootstrapConfig
from app.context import AppContext
from app.core.http import build_client
from app.core.logging import configure_logging
from app.core.security import LoginThrottle
from app.db.session import create_engine, create_sessionmaker
from app.providers.base import ProviderError
from app.services import bootstrap
from app.services.auth import purge_expired_sessions
from app.services.health import HealthService

log = logging.getLogger("app")

WEB_DIR = Path(__file__).parent / "web" / "static"

# Alpine.js evaluates x-* expressions with Function(), hence 'unsafe-eval'.
# No inline <script> is used anywhere, so 'unsafe-inline' is not needed for scripts.
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'self'; form-action 'self'"
)


def create_app(cfg: BootstrapConfig | None = None, *, configure_logs: bool = True) -> FastAPI:
    cfg = cfg or BootstrapConfig.from_env()
    if configure_logs:
        configure_logging(cfg.log_level, cfg.log_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "Starting WishPanel",
            extra={
                "version": __version__,
                "config_dir": str(cfg.config_dir),
                "base_path": cfg.base_path or "/",
            },
        )
        # Migrations are synchronous (Alembic); keep them off the event loop.
        box = await asyncio.to_thread(bootstrap.prepare_filesystem, cfg)
        engine = create_engine(cfg.db_url_async)
        sessionmaker = create_sessionmaker(engine)
        await bootstrap.seed(sessionmaker, box, cfg.config_dir)
        async with sessionmaker() as db:
            if purged := await purge_expired_sessions(db):
                log.info("Purged expired sessions", extra={"count": purged})
            await db.commit()

        http = build_client()
        app.state.ctx = AppContext(
            config=cfg,
            engine=engine,
            sessionmaker=sessionmaker,
            box=box,
            http=http,
            throttle=LoginThrottle(),
            health=HealthService(sessionmaker, box, http, cfg.config_dir),
        )
        log.info("WishPanel ready", extra={"port": cfg.port})
        try:
            yield
        finally:
            await http.aclose()
            await engine.dispose()
            log.info("WishPanel stopped")

    app = FastAPI(
        title="WishPanel",
        version=__version__,
        lifespan=lifespan,
        docs_url=f"{cfg.base_path}/api/docs",
        redoc_url=None,
        openapi_url=f"{cfg.base_path}/api/openapi.json",
    )

    _install_middleware(app, cfg.base_path)
    _install_error_handlers(app)

    prefix = cfg.base_path
    # /health always answers at the root so Docker's HEALTHCHECK doesn't need BASE_URL.
    app.include_router(system_api.health_router)
    routes = APIRouter()
    routes.include_router(auth_api.router)
    routes.include_router(setup_api.router)
    routes.include_router(system_api.router)
    routes.include_router(admin_settings_api.router)
    routes.include_router(admin_settings_api.public_router)
    routes.include_router(admin_users_api.router)
    if prefix:
        routes.include_router(system_api.health_router)
    app.include_router(routes, prefix=prefix)

    app.mount(f"{prefix}/static", StaticFiles(directory=WEB_DIR), name="static")

    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.add_api_route(f"{prefix}/", index, methods=["GET"], include_in_schema=False)
    if prefix:

        async def to_prefix() -> RedirectResponse:
            return RedirectResponse(f"{prefix}/")

        app.add_api_route(prefix, to_prefix, methods=["GET"], include_in_schema=False)
        app.add_api_route("/", to_prefix, methods=["GET"], include_in_schema=False)

    return app


def _install_middleware(app: FastAPI, prefix: str) -> None:
    access = logging.getLogger("app.access")

    @app.middleware("http")
    async def headers_and_access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith(f"{prefix}/api/"):
            response.headers.setdefault("Cache-Control", "no-store")

        path = request.url.path
        level = logging.DEBUG if (path.endswith("/health") or "/static/" in path) else logging.INFO
        access.log(
            level,
            "%s %s %s",
            request.method,
            path,
            response.status_code,
            extra={
                "status": response.status_code,
                "ms": round((time.perf_counter() - start) * 1000, 1),
                "ip": request.client.host if request.client else None,
            },
        )
        return response


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProviderError)
    async def provider_error(_: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=502)
