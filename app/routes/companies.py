"""Company pages: the exhibitor, its contacts, and its opportunities grouped by edition."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import pool
from app.repositories import companies as repo
from app.repositories.common import ref_clause, ref_params

router = APIRouter()


@router.get("/companies/{ref}", response_class=HTMLResponse)
def company_page(request: Request, ref: str):
    templates = request.app.state.templates
    with pool.connection() as conn:
        company = repo.get(conn, ref)
        if company is None:
            raise HTTPException(status_code=404, detail=f"No company matches {ref!r}.")
        cid = company["id"]
        context = {
            "title": company["name"],
            "company": company,
            "summary": repo.summary(conn, cid),
            "contacts": repo.contacts(conn, cid),
            "editions": repo.opportunities_by_edition(conn, cid),
            "notes": repo.company_notes(conn, cid),
        }
    return templates.TemplateResponse(request, "pages/company.html", context)


@router.get("/companies/{ref}/notes", response_class=HTMLResponse)
def company_notes_page(request: Request, ref: str, cursor: str | None = None):
    """Next page of company-level notes, swapped in by htmx when the last row scrolls into view."""
    templates = request.app.state.templates
    with pool.connection() as conn:
        company = repo.get(conn, ref)
        if company is None:
            raise HTTPException(status_code=404)
        page = repo.company_notes(conn, company["id"], cursor=cursor)
    return templates.TemplateResponse(
        request,
        "partials/_activity_rows.html",
        {"page": page, "more_url": f"/companies/{company['legacy_code'] or company['id']}/notes"},
    )


@router.get("/contacts/{ref}", response_class=HTMLResponse)
def contact_redirect(request: Request, ref: str):
    """Contacts live on their company's page rather than on a screen of their own.

    A contact in this archive has a name, an email, a phone and a company -- not enough to
    justify a separate page, and splitting them out would mean one more click to reach the
    opportunities the brief actually asks to see. So the contact URL resolves to the company
    page, anchored at that contact.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ct.id, ct.legacy_code, c.legacy_code AS company_code, c.id AS company_id "
            "FROM contact ct JOIN company c ON c.id = ct.company_id "
            f"WHERE {ref_clause('ct')}",
            ref_params(ref),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No contact matches {ref!r}.")
    target = row["company_code"] or row["company_id"]
    return RedirectResponse(f"/companies/{target}#contact-{row['id']}", status_code=303)
