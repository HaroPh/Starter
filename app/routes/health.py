"""Liveness and readiness endpoints.

The split matters. `/healthz` never touches the database and backs the container
HEALTHCHECK: a liveness probe that fails when the database blinks would restart a perfectly
healthy application. `/readyz` is the one that reports on dependencies, and it is allowed to
say "not ready" without anything being killed.
"""

from fastapi import APIRouter, Response

from app.config import get_settings
from app.db import pool

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": get_settings().app_version}


@router.get("/readyz")
def readyz(response: Response) -> dict[str, object]:
    out: dict[str, object] = {"version": get_settings().app_version}
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM schema_migration")
            out["migrations_applied"] = cur.fetchone()["n"]

            # to_regclass returns NULL rather than raising when the table does not exist,
            # so this stays correct during the window before the first migration runs.
            cur.execute("SELECT to_regclass('public.import_run') IS NOT NULL AS present")
            if cur.fetchone()["present"]:
                cur.execute(
                    "SELECT status, finished_at, row_counts FROM import_run "
                    "ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                out["import"] = row if row else {"status": "not_started"}
            else:
                out["import"] = {"status": "not_started"}

        out["database"] = "ok"
        out["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 -- readiness reports failures, never raises
        response.status_code = 503
        out["database"] = "unavailable"
        out["status"] = "degraded"
        out["error"] = str(exc)
    return out
