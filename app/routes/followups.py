"""The follow-up queue: who to call, and what they are waiting for."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import forms
from app.db import pool
from app.repositories import followups as repo

router = APIRouter()


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _queue_context(conn, *, bucket: str, rep: int | None, tasks: bool, q: str,
                   cursor: str | None) -> dict:
    return {
        "title": "Follow-ups",
        "nav": "followups",
        "bucket": bucket if bucket in repo.BUCKETS else "overdue",
        "rep": rep,
        "tasks": tasks,
        "q": q,
        "counts": repo.bucket_counts(conn, rep_id=rep, tasks_only=tasks, q=q),
        "page": repo.queue(conn, bucket=bucket, rep_id=rep, tasks_only=tasks, q=q, cursor=cursor),
        "reps": repo.reps(conn),
    }


@router.get("/follow-ups", response_class=HTMLResponse)
def queue_page(
    request: Request,
    bucket: str | None = None,
    rep: str | None = None,
    tasks: int = 0,
    q: str = "",
    cursor: str | None = None,
):
    """Overdue first, because that is the order a person plans a day in.

    `tasks=1` narrows to the reading data/README.md states literally -- only a task marked N
    is pending work. The default is broader: any entry that carried a follow-up date is an
    outstanding promise, which is what the brief's "confirm the floor area on Friday"
    example describes. Both readings are kept at import (follow_up.origin), so this is a
    view choice and not a data one.
    """
    templates = request.app.state.templates
    rep_id = forms.parse_int(rep)
    q = q.strip()
    # Looking for one particular follow-up is a search across every date, so a text filter
    # with no bucket chosen widens to all of them rather than hiding matches in other tabs.
    bucket = bucket or ("all" if q else "overdue")
    with pool.connection() as conn:
        context = _queue_context(conn, bucket=bucket, rep=rep_id, tasks=bool(tasks), q=q,
                                 cursor=cursor)
    if _is_htmx(request) and cursor:
        return templates.TemplateResponse(request, "partials/_queue_rows.html", context)
    return templates.TemplateResponse(request, "pages/followups.html", context)


@router.post("/follow-ups/{follow_up_id}/complete", response_class=HTMLResponse)
async def complete(request: Request, follow_up_id: int):
    templates = request.app.state.templates
    data = await request.form()
    with pool.connection() as conn:
        row = repo.get(conn, follow_up_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such follow-up.")
        with conn.transaction():
            repo.complete(conn, follow_up_id)
        row = repo.get(conn, follow_up_id)

    back = data.get("back") or "/follow-ups"
    if not _is_htmx(request):
        return RedirectResponse(back, status_code=303)
    return templates.TemplateResponse(request, "partials/_queue_row_done.html", {"f": row})


@router.post("/follow-ups/{follow_up_id}/reschedule", response_class=HTMLResponse)
async def reschedule(request: Request, follow_up_id: int):
    templates = request.app.state.templates
    data = await request.form()
    errors = forms.FormErrors()
    due_on = forms.parse_date(data.get("due_on"), "due_on", errors, label="New date")

    with pool.connection() as conn:
        row = repo.get(conn, follow_up_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such follow-up.")
        if due_on is None:
            errors.add("due_on", "Choose the new date.")
        else:
            with conn.transaction():
                repo.reschedule(conn, follow_up_id, due_on)
            row = repo.get(conn, follow_up_id)

    back = data.get("back") or "/follow-ups"
    if not _is_htmx(request):
        return RedirectResponse(back, status_code=303)
    return templates.TemplateResponse(
        request, "partials/_queue_row.html", {"f": row, "errors": errors, "rescheduled": not errors},
        status_code=422 if errors else 200,
    )
