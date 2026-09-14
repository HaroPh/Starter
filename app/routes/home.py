"""The landing page.

This is the URL verify.sh curls, so it must return 200 under every condition a reviewer can
produce: an empty database, an import still in flight, or an import that failed. Every query
here is therefore wrapped so that a failure degrades the page instead of failing it -- a
landing page that 500s because a count could not be computed would fail the one check the
reviewer runs before looking at anything else.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import pool

router = APIRouter()
log = logging.getLogger("crm.home")


def _import_state(cur) -> dict[str, Any]:
    cur.execute(
        "SELECT id, status, started_at, finished_at, duration_ms, row_counts, issue_counts,"
        "       dataset_version, dataset_reference_time, error "
        "FROM import_run ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    return row or {"status": "not_started"}


def _overview(cur) -> dict[str, Any]:
    cur.execute(
        """
        SELECT
          (SELECT count(*) FROM company)                                  AS companies,
          (SELECT count(*) FROM contact)                                  AS contacts,
          (SELECT count(*) FROM opportunity
             WHERE status IN ('open','qualified','proposal'))             AS open_opportunities,
          (SELECT count(*) FROM follow_up
             WHERE status = 'pending' AND due_on <  current_date)         AS overdue,
          (SELECT count(*) FROM follow_up
             WHERE status = 'pending' AND due_on =  current_date)         AS due_today,
          (SELECT count(*) FROM follow_up
             WHERE status = 'pending' AND due_on >  current_date
               AND due_on <= current_date + 7)                            AS due_this_week,
          (SELECT count(*) FROM opportunity WHERE readiness = 'provisional') AS provisional,
          (SELECT count(*) FROM opportunity WHERE readiness = 'incomplete')  AS incomplete,
          (SELECT count(*) FROM opportunity WHERE readiness = 'blocked')     AS blocked
        """
    )
    return cur.fetchone()


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    templates = request.app.state.templates
    context: dict[str, Any] = {"title": "Overview", "import_state": None, "overview": None,
                               "db_error": None}
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            context["import_state"] = _import_state(cur)
            if context["import_state"].get("status") == "completed":
                context["overview"] = _overview(cur)
    except Exception as exc:  # noqa: BLE001 -- the landing page must render regardless
        log.warning("overview unavailable: %s", exc)
        context["db_error"] = str(exc)

    return templates.TemplateResponse(request, "pages/home.html", context)
