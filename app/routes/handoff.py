"""The handoff assistant: HTML pages and the JSON API.

Two front doors onto the same orchestration. The HTML routes serve the sales team; the JSON
routes serve anything else -- the evaluation harness drives the assistant through them, and
they are what a future integration would call. Both go through `_run_and_save`, so there is
no way for the two to behave differently.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import forms
from app.db import pool
from app.handoff import orchestrator, store
from app.repositories import opportunities as opp_repo
from app.repositories import writes

router = APIRouter()
ROME = ZoneInfo("Europe/Rome")


def _today():
    # The sales team and every fair are in Italy; "has this edition finished" is asked there.
    return datetime.now(ROME).date()


def _run_and_save(request: Request, conn, opp: dict, *, brief_override: str | None,
                  save_brief: bool, previous_run_id: int | None) -> int:
    if save_brief and brief_override is not None:
        # Saved first, so the opportunity page and the run agree about the notes afterwards.
        writes.update_opportunity(conn, opp["id"], {"brief_notes": brief_override})

    result = orchestrator.run_handoff(
        conn,
        str(opp["legacy_code"] or opp["id"]),
        request.app.state.model_client,
        today=_today(),
        brief_override=brief_override,
    )
    return store.save_run(conn, result, opportunity_id=opp["id"], previous_run_id=previous_run_id)


# ---------------------------------------------------------------------------
# HTML


@router.post("/opportunities/{ref}/handoff/runs")
async def start_run(request: Request, ref: str):
    data = await request.form()
    raw_notes = data.get("brief_notes")
    with pool.connection() as conn:
        opp = opp_repo.get(conn, ref)
        if opp is None:
            raise HTTPException(status_code=404, detail=f"No opportunity matches {ref!r}.")

        # Only treat the notes as an edit if they actually differ from what is stored. Running
        # again without touching the textarea is a plain re-run, not an edited one.
        override = None
        if raw_notes is not None:
            edited = str(raw_notes).strip()
            if edited != (opp.get("brief_notes") or "").strip():
                override = edited

        run_id = _run_and_save(
            request, conn, opp,
            brief_override=override,
            save_brief=bool(data.get("save_brief")),
            previous_run_id=forms.parse_int(data.get("previous_run_id")),
        )
    return RedirectResponse(f"/handoff/runs/{run_id}", status_code=303)


@router.get("/handoff/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int):
    """Rendered entirely from what the run stored. It never re-reads the opportunity, so a
    later edit cannot change what an old run shows it was working from."""
    templates = request.app.state.templates
    with pool.connection() as conn:
        run = store.get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"No handoff run {run_id}.")
        siblings = store.list_runs(conn, run["opportunity_id"])
        # The current notes are needed only to prefill the "run again" form.
        opp = opp_repo.get(conn, str(run["opportunity_id"]))
    return templates.TemplateResponse(request, "pages/handoff_run.html", {
        "title": f"Handoff run {run['run_no']} · {run['opportunity_code']}",
        "run": run,
        "runs": siblings,
        "current_notes": (opp or {}).get("brief_notes") or "",
    })


# ---------------------------------------------------------------------------
# JSON API


@router.post("/api/opportunities/{ref}/handoff/runs")
async def api_start_run(request: Request, ref: str):
    """Start a run. Optional JSON body: {"brief_notes": "...", "save_brief": false}."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- an empty or non-JSON body means "no options"
        body = {}
    if not isinstance(body, dict):
        body = {}

    with pool.connection() as conn:
        opp = opp_repo.get(conn, ref)
        if opp is None:
            return JSONResponse({"detail": f"No opportunity matches {ref!r}."}, status_code=404)
        notes = body.get("brief_notes")
        run_id = _run_and_save(
            request, conn, opp,
            brief_override=str(notes) if notes is not None else None,
            save_brief=bool(body.get("save_brief")),
            previous_run_id=body.get("previous_run_id") if isinstance(body.get("previous_run_id"), int) else None,
        )
        run = store.get_run(conn, run_id)
    return JSONResponse(store.run_as_json(run), status_code=201)


@router.get("/api/handoff/runs/{run_id}")
def api_get_run(run_id: int):
    with pool.connection() as conn:
        run = store.get_run(conn, run_id)
    if run is None:
        return JSONResponse({"detail": f"No handoff run {run_id}."}, status_code=404)
    return store.run_as_json(run)


@router.get("/api/opportunities/{ref}/handoff/runs")
def api_list_runs(ref: str):
    with pool.connection() as conn:
        opp = opp_repo.get(conn, ref)
        if opp is None:
            return JSONResponse({"detail": f"No opportunity matches {ref!r}."}, status_code=404)
        runs = store.list_runs(conn, opp["id"])
    return [{**r, "started_at": r["started_at"].isoformat()} for r in runs]
