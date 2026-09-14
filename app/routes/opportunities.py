"""Opportunity pages: the filtered list, the detail page, and the actions on it.

Every write here is a real `<form method="post">` that also carries htmx attributes. With
JavaScript, htmx sends the form, gets back just the fragment that changed, and swaps it in.
Without JavaScript -- or from a test client -- the same endpoint answers with a 303 redirect
back to the page, the classic post/redirect/get. One handler, two behaviours, chosen by the
`HX-Request` header htmx adds.

That is what makes every action here drivable by plain HTTP in the integration tests, and
means nothing breaks if a script fails to load.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import forms
from app.db import pool
from app.repositories import followups as fu_repo
from app.repositories import opportunities as repo
from app.repositories import writes

router = APIRouter()
ROME = ZoneInfo("Europe/Rome")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _page_context(conn, opp: dict, **extra) -> dict:
    oid = opp["id"]
    context = {
        "title": f"{opp['legacy_code'] or oid} · {opp['company_name']}",
        "nav": "opportunities",
        "opp": opp,
        "timeline": repo.timeline(conn, oid),
        "follow_ups": repo.open_follow_ups(conn, oid),
        "siblings": repo.sibling_editions(conn, oid),
        "options": writes.edit_form_options(conn, opp["company_id"]),
        "now_local": datetime.now(ROME).strftime("%Y-%m-%dT%H:%M"),
        "errors": forms.FormErrors(),
        "form": {},
        "edit_mode": False,
    }
    context.update(extra)
    return context


def _load(conn, ref: str) -> dict:
    opp = repo.get(conn, ref)
    if opp is None:
        raise HTTPException(status_code=404, detail=f"No opportunity matches {ref!r}.")
    return opp


def _ref_of(opp: dict) -> str:
    return str(opp["legacy_code"] or opp["id"])


# ---------------------------------------------------------------------------
# Reading


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
            conn, edition=edition or None, status=status or None,
            readiness=readiness or None, attention=attention, cursor=cursor,
        )
        context = {
            "title": "Opportunities", "nav": "opportunities", "page": page,
            "counts": repo.readiness_counts(conn),
            "options": repo.filter_options(conn),
            "filters": {"edition": edition or "", "status": status or "",
                        "readiness": readiness or "", "view": view},
        }
    if _is_htmx(request) and cursor:
        return templates.TemplateResponse(request, "partials/_opportunity_rows.html", context)
    return templates.TemplateResponse(request, "pages/opportunities.html", context)


@router.get("/opportunities/{ref}", response_class=HTMLResponse)
def opportunity_page(request: Request, ref: str, edit: int = 0):
    templates = request.app.state.templates
    with pool.connection() as conn:
        opp = _load(conn, ref)
        context = _page_context(conn, opp, edit_mode=bool(edit))
    return templates.TemplateResponse(request, "pages/opportunity.html", context)


@router.get("/opportunities/{ref}/timeline", response_class=HTMLResponse)
def opportunity_timeline(request: Request, ref: str, cursor: str | None = None):
    templates = request.app.state.templates
    with pool.connection() as conn:
        opp = _load(conn, ref)
        page = repo.timeline(conn, opp["id"], cursor=cursor)
    return templates.TemplateResponse(
        request, "partials/_activity_rows.html",
        {"page": page, "more_url": f"/opportunities/{_ref_of(opp)}/timeline"},
    )


# ---------------------------------------------------------------------------
# Editing the opportunity


@router.get("/opportunities/{ref}/edit", response_class=HTMLResponse)
def edit_form(request: Request, ref: str):
    templates = request.app.state.templates
    with pool.connection() as conn:
        opp = _load(conn, ref)
        context = _page_context(conn, opp, edit_mode=True)
    return templates.TemplateResponse(request, "partials/_opportunity_facts.html", context)


@router.get("/opportunities/{ref}/facts", response_class=HTMLResponse)
def facts_view(request: Request, ref: str):
    """The read-only facts block -- what "Cancel" on the edit form swaps back in."""
    templates = request.app.state.templates
    with pool.connection() as conn:
        opp = _load(conn, ref)
        context = _page_context(conn, opp)
    return templates.TemplateResponse(request, "partials/_opportunity_facts.html", context)


@router.post("/opportunities/{ref}", response_class=HTMLResponse)
async def update(request: Request, ref: str):
    templates = request.app.state.templates
    data = await request.form()
    errors = forms.FormErrors()

    with pool.connection() as conn:
        opp = _load(conn, ref)
        options = writes.edit_form_options(conn, opp["company_id"])

        values = {
            "description": forms.parse_text(data.get("description"), "description", errors,
                                            label="Description", required=True, max_length=300),
            "status": forms.parse_choice(data.get("status"), "status", errors, label="Status",
                                         allowed={s["code"] for s in options["statuses"]}),
            "amount_eur": forms.parse_decimal(data.get("amount_eur"), "amount_eur", errors,
                                              label="Recorded value", maximum=writes.LIMITS["amount_eur"]),
            "client_budget_eur": forms.parse_decimal(data.get("client_budget_eur"), "client_budget_eur",
                                                     errors, label="Client budget",
                                                     maximum=writes.LIMITS["client_budget_eur"]),
            "stand_area_sqm": forms.parse_decimal(data.get("stand_area_sqm"), "stand_area_sqm", errors,
                                                  label="Stand area", maximum=writes.LIMITS["stand_area_sqm"]),
            "requested_height_m": forms.parse_decimal(data.get("requested_height_m"), "requested_height_m",
                                                      errors, label="Requested height",
                                                      maximum=writes.LIMITS["requested_height_m"]),
            "expected_close_on": forms.parse_date(data.get("expected_close_on"), "expected_close_on",
                                                  errors, label="Expected close date"),
            "brief_notes": forms.parse_text(data.get("brief_notes"), "brief_notes", errors,
                                            label="Brief notes", max_length=4000),
            "contact_id": forms.parse_int(data.get("contact_id")),
            "fair_edition_id": forms.parse_int(data.get("fair_edition_id")),
        }

        if values["contact_id"] and not writes.contact_belongs_to_company(
            conn, values["contact_id"], opp["company_id"]
        ):
            errors.add("contact_id", "That contact belongs to a different exhibitor.")
        if values["fair_edition_id"] and values["fair_edition_id"] not in {e["id"] for e in options["editions"]}:
            errors.add("fair_edition_id", "Choose a fair edition from the list.")

        if errors:
            context = _page_context(conn, opp, edit_mode=True, errors=errors, form=dict(data))
            if _is_htmx(request):
                return templates.TemplateResponse(
                    request, "partials/_opportunity_facts.html", context, status_code=422
                )
            return templates.TemplateResponse(request, "pages/opportunity.html", context, status_code=422)

        writes.update_opportunity(conn, opp["id"], values)
        opp = repo.get(conn, str(opp["id"]))
        context = _page_context(conn, opp, saved=True)

    if not _is_htmx(request):
        return RedirectResponse(f"/opportunities/{_ref_of(opp)}", status_code=303)

    # The facts block swaps into #facts; the readiness badge in the page header is updated
    # out-of-band in the same response. One request updating both is the visible proof that
    # the badge reads the verdict the save just recomputed.
    return templates.TemplateResponse(request, "partials/_facts_saved.html", context)


# ---------------------------------------------------------------------------
# Recording a conversation


@router.post("/opportunities/{ref}/activities", response_class=HTMLResponse)
async def log_activity(request: Request, ref: str):
    templates = request.app.state.templates
    data = await request.form()
    errors = forms.FormErrors()

    with pool.connection() as conn:
        opp = _load(conn, ref)
        options = writes.edit_form_options(conn, opp["company_id"])

        activity_type = forms.parse_choice(data.get("activity_type"), "activity_type", errors,
                                           label="Type", allowed={t["code"] for t in options["activity_types"]})
        occurred = forms.parse_datetime(data.get("occurred_at"), "occurred_at", errors, label="When")
        details = forms.parse_text(data.get("details"), "details", errors, label="What was said",
                                   required=True, max_length=4000)
        author = forms.parse_int(data.get("author_rep_id"))
        follow_up_on = forms.parse_date(data.get("follow_up_on"), "follow_up_on", errors,
                                        label="Follow-up date")
        follow_up_note = forms.parse_text(data.get("follow_up_note"), "follow_up_note", errors,
                                          label="What we are waiting for", max_length=500)
        completes = forms.parse_int(data.get("completes_follow_up_id"))

        if follow_up_note and not follow_up_on:
            errors.add("follow_up_on", "Give a date for the follow-up, or clear what you are waiting for.")

        if errors:
            context = _page_context(conn, opp, errors=errors, form=dict(data))
            if _is_htmx(request):
                # The whole panel, not just the form: the form's target is #conversations,
                # so returning the form alone would swap the timeline out of the page.
                return templates.TemplateResponse(
                    request, "partials/_conversations.html", context, status_code=422
                )
            return templates.TemplateResponse(request, "pages/opportunity.html", context, status_code=422)

        writes.log_activity(
            conn,
            company_id=opp["company_id"],
            opportunity_id=opp["id"],
            activity_type=activity_type,
            occurred_at=occurred or datetime.now(ROME).replace(tzinfo=None),
            details=details,
            author_rep_id=author,
            follow_up_on=follow_up_on,
            follow_up_note=follow_up_note,
            completes_follow_up_id=completes,
        )
        context = _page_context(conn, opp, logged=True)

    if not _is_htmx(request):
        return RedirectResponse(f"/opportunities/{_ref_of(opp)}#timeline", status_code=303)
    return templates.TemplateResponse(request, "partials/_activity_logged.html", context)


# ---------------------------------------------------------------------------
# Follow-ups from the opportunity page


@router.post("/opportunities/{ref}/follow-ups", response_class=HTMLResponse)
async def schedule_follow_up(request: Request, ref: str):
    templates = request.app.state.templates
    data = await request.form()
    errors = forms.FormErrors()

    with pool.connection() as conn:
        opp = _load(conn, ref)
        due_on = forms.parse_date(data.get("due_on"), "due_on", errors, label="Due date")
        note = forms.parse_text(data.get("note"), "note", errors, label="What we are waiting for",
                                required=True, max_length=500)
        if due_on is None and not errors.get("due_on"):
            errors.add("due_on", "Choose a date.")

        if errors:
            context = _page_context(conn, opp, errors=errors, form=dict(data))
            if _is_htmx(request):
                return templates.TemplateResponse(request, "partials/_followups_panel.html", context,
                                                  status_code=422)
            return templates.TemplateResponse(request, "pages/opportunity.html", context, status_code=422)

        with conn.transaction():
            fu_repo.schedule(
                conn, company_id=opp["company_id"], opportunity_id=opp["id"], due_on=due_on,
                note=note, rep_id=forms.parse_int(data.get("rep_id")) or opp.get("sales_rep_id"),
            )
        context = _page_context(conn, opp)

    if not _is_htmx(request):
        return RedirectResponse(f"/opportunities/{_ref_of(opp)}", status_code=303)
    return templates.TemplateResponse(request, "partials/_followups_panel.html", context)
