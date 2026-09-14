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

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.db import migrate, pool
from app.importer import runner as import_runner
from app.routes import health, home

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

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.globals["asset_version"] = settings.app_version
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    app.include_router(health.router)
    app.include_router(home.router)

    return app


app = create_app()
