"""Liveness and readiness endpoints.

/healthz must never touch the database. It backs the container HEALTHCHECK, and a health
probe that fails when the database is briefly unavailable would restart a perfectly healthy
application. /readyz is the one that reports on dependencies.
"""

from fastapi import APIRouter

from app.config import get_settings

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": get_settings().app_version}


@router.get("/readyz")
def readyz() -> dict[str, object]:
    # Filled in once the pool, migrations and importer exist (phases 1 and 2).
    return {
        "status": "ok",
        "database": "not_wired_yet",
        "migrations": "not_wired_yet",
        "import": "not_wired_yet",
    }
