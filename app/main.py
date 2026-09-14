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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.routes import health, home

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("crm")

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log.info("starting exhibition CRM v%s", settings.app_version)
    # Phase 1 adds: pool open + migrations (synchronous, before the port binds).
    # Phase 2 adds: the importer, started here on a background thread.
    yield
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
