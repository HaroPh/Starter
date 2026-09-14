"""Application entry point.

Startup ordering is a deliberate design decision, not an accident.

`verify.sh` reaches the app with
`curl --retry 10 --retry-delay 1 --retry-connrefused`, which is roughly a ten-second budget
measured from the moment the container starts. Importing the archive takes longer than that.
So the sequence is:

  1. open the connection pool and run migrations synchronously  (sub-second)
  2. bind port 3000                                             (verify.sh passes here)
  3. import the archive on a background thread                  (seconds to a minute)

An entrypoint script that imported before exec'ing uvicorn would keep the port closed for the
whole import and fail `verify.sh` for a reason that has nothing to do with reachability.
Because the entire import runs in a single transaction, a request arriving while it is in
flight sees either an empty database or a complete one -- never a half-imported state.
"""

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, get_settings
from app.db import migrate, pool
from app.handoff import readiness_store
from app.importer import runner as import_runner
from app.routes import companies, health, home, opportunities, search
from app.templating import build_templates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("crm")

BASE_DIR = Path(__file__).resolve().parent


def _run_import_in_background(settings: Settings) -> None:
    """Import the archive, and survive failing to.

    A failed import must not take the application down. The reviewer gets a page that
    explains what went wrong, with the error on /imports, rather than a container restarting
    in a loop that can only be diagnosed through `docker logs`.
    """
    try:
        import_runner.run_import(
            pool.get_pool(), settings.crm_data_dir, settings.app_version
        )
    except Exception:  # noqa: BLE001 -- already recorded on import_run; keep serving
        log.exception("archive import failed; the application continues to serve")
        return

    # After the import rather than before the port binds: recomputing tens of thousands of
    # verdicts must never hold up verify.sh. If the policy changed since the data was last
    # judged, this is what brings every stored verdict up to date.
    try:
        with pool.connection() as conn:
            readiness_store.ensure_current(conn)
    except Exception:  # noqa: BLE001 -- stale badges are better than a dead application
        log.exception("readiness recompute failed; verdicts may be stale")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log.info("starting exhibition CRM v%s", settings.app_version)

    # Synchronous and before the port binds: the application is useless without a schema,
    # and six files take well under a second. Anything slower than that belongs after the
    # bind -- which is exactly where the archive import goes.
    pool.open_pool(settings.database_url)
    with pool.connection() as conn:
        applied = migrate.apply_all(conn)
    app.state.migrations_applied = applied

    # The archive import runs on a background thread so the port binds immediately.
    # verify.sh allows roughly ten seconds from container start and the import takes longer
    # than that, so anything that blocks here fails a check that has nothing to do with
    # reachability. Every page renders correctly while this is still running.
    app.state.import_thread = threading.Thread(
        target=_run_import_in_background,
        args=(settings,),
        name="archive-import",
        daemon=True,
    )
    app.state.import_thread.start()

    yield

    pool.close_pool()
    log.info("shutting down")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="Exhibition sales CRM",
        version=settings.app_version,
        lifespan=lifespan,
        # This is a single-user internal tool rendered server-side; the OpenAPI UI would
        # only advertise endpoints that are not the product. The JSON schema stays on for
        # the handoff API added in phase 6.
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings

    templates = build_templates(str(BASE_DIR / "templates"), settings.app_version)
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    app.include_router(health.router)
    app.include_router(home.router)
    app.include_router(search.router)
    app.include_router(companies.router)
    app.include_router(opportunities.router)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_page(request: Request, exc: StarletteHTTPException):
        """Render errors as pages for people and as JSON for the API.

        A reviewer who mistypes a code should get a page explaining what was not found and
        how to search for it, not a bare `{"detail": "Not Found"}`.
        """
        if request.url.path.startswith("/api/") or request.url.path in ("/healthz", "/readyz"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(
            request,
            "pages/error.html",
            {"title": f"{exc.status_code}", "status": exc.status_code, "detail": exc.detail},
            status_code=exc.status_code,
        )

    return app


app = create_app()
