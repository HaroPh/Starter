"""Search routes.

Two endpoints for one feature, on purpose. `/search` renders the whole page; `/search/results`
renders only the result block and is what htmx requests as the user types. Keeping them
separate -- rather than sniffing the `HX-Request` header inside one handler -- means the
partial is directly testable with a plain HTTP client and the page still works with
JavaScript disabled.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import pool
from app.repositories import search as search_repo

router = APIRouter()


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", go: int = 0):
    templates = request.app.state.templates

    outcome = None
    if q.strip():
        with pool.connection() as conn:
            outcome = search_repo.find(conn, q)

        # Typing a code and landing on a one-row result list is a wasted click, so a single
        # hit goes straight to the record. `go=0` suppresses it, which is what the "showing
        # results for" link back from a record uses.
        single = outcome.single_hit if go == 0 else None
        if single:
            kind, code = single
            return RedirectResponse(f"/{kind}/{code}", status_code=303)

    return templates.TemplateResponse(
        request,
        "pages/search.html",
        {"title": f"Search: {q}" if q else "Search", "q": q, "outcome": outcome},
    )


@router.get("/search/results", response_class=HTMLResponse)
def search_results(request: Request, q: str = ""):
    """The live-results partial. Never redirects -- it is swapped into the page in place."""
    templates = request.app.state.templates
    outcome = None
    if q.strip():
        with pool.connection() as conn:
            outcome = search_repo.find(conn, q)
    return templates.TemplateResponse(
        request, "partials/_search_results.html", {"q": q, "outcome": outcome}
    )
