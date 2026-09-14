"""Opportunity pages: the filtered list and the detail page."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.db import pool
from app.repositories import opportunities as repo

router = APIRouter()


@router.get("/opportunities", response_class=HTMLResponse)
def opportunity_list(
    request: Request,
    edition: str | None = None,
    status: str | None = None,
    readiness: str | None = None,
    view: str = "attention",
    cursor: str | None = None,
):
    """The opportunity list.

    Defaults to "needs attention" -- everything not yet ready for a technical handoff --
    rather than to all 15,000 rows. 13,906 of them are ready, so an unfiltered list is mostly
    noise, and the 1,094 that are not are where the work is. `view=all` shows everything.
    """
    templates = request.app.state.templates
    attention = view == "attention" and not readiness
    with pool.connection() as conn:
        page = repo.listing(
            conn,
            edition=edition or None,
            status=status or None,
            readiness=readiness or None,
            attention=attention,
            cursor=cursor,
        )
        context = {
            "title": "Opportunities",
            "nav": "opportunities",
            "page": page,
            "counts": repo.readiness_counts(conn),
            "options": repo.filter_options(conn),
            "filters": {"edition": edition or "", "status": status or "",
                        "readiness": readiness or "", "view": view},
        }

    # htmx "load more" asks for the rows only, and appends them to the existing table.
    if request.headers.get("HX-Request") and cursor:
        return templates.TemplateResponse(request, "partials/_opportunity_rows.html", context)
    return templates.TemplateResponse(request, "pages/opportunities.html", context)


@router.get("/opportunities/{ref}", response_class=HTMLResponse)
def opportunity_page(request: Request, ref: str):
    templates = request.app.state.templates
    with pool.connection() as conn:
        opp = repo.get(conn, ref)
        if opp is None:
            raise HTTPException(status_code=404, detail=f"No opportunity matches {ref!r}.")
        oid = opp["id"]
        context = {
            "title": f"{opp['legacy_code'] or oid} · {opp['company_name']}",
            "nav": "opportunities",
            "opp": opp,
            "timeline": repo.timeline(conn, oid),
            "follow_ups": repo.open_follow_ups(conn, oid),
            "siblings": repo.sibling_editions(conn, oid),
        }
    return templates.TemplateResponse(request, "pages/opportunity.html", context)


@router.get("/opportunities/{ref}/timeline", response_class=HTMLResponse)
def opportunity_timeline(request: Request, ref: str, cursor: str | None = None):
    templates = request.app.state.templates
    with pool.connection() as conn:
        opp = repo.get(conn, ref)
        if opp is None:
            raise HTTPException(status_code=404)
        page = repo.timeline(conn, opp["id"], cursor=cursor)
    return templates.TemplateResponse(
        request,
        "partials/_activity_rows.html",
        {"page": page, "more_url": f"/opportunities/{opp['legacy_code'] or opp['id']}/timeline"},
    )
