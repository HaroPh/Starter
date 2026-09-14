"""Changing an opportunity and recording a conversation.

The two actions from the brief's second bullet: "update an opportunity and record a customer
conversation". Both go through here, and both do one extra thing that matters more than the
write itself.

**Editing an opportunity recomputes its readiness in the same transaction.** Filling in the
stand area is exactly what moves an enquiry from PROVISIONAL to READY, and the badge on the
page, the "needs attention" list and the handoff assistant all read the stored verdict. If the
recompute happened later, or separately, there would be a window where the page said one thing
and the assistant another.

**Recording a conversation can create the follow-up in the same submit.** The brief's example
-- "if a customer promises to confirm the floor area on Friday" -- is a promise made DURING a
call. Asking the user to log the call, then navigate somewhere else to schedule the follow-up,
is how the second step gets forgotten. So it is one form, and one transaction: either both
rows exist or neither does.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import Connection

from app.handoff import readiness_store
from app.repositories import followups

ROME = ZoneInfo("Europe/Rome")

EDITABLE = (
    "description", "status", "amount_eur", "client_budget_eur", "stand_area_sqm",
    "requested_height_m", "expected_close_on", "brief_notes", "contact_id", "fair_edition_id",
)


def update_opportunity(conn: Connection, opportunity_id: int, values: dict[str, Any]) -> dict | None:
    """Apply the edited fields and re-rule readiness. Returns the new verdict JSON.

    Only columns named in EDITABLE can be written, whatever the caller passes -- the column
    list is built from that tuple, never from the request.
    """
    fields = {k: v for k, v in values.items() if k in EDITABLE}
    with conn.transaction():
        if fields:
            assignments = ", ".join(f"{name} = %({name})s" for name in fields)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE opportunity SET {assignments}, updated_at = now() "
                    "WHERE id = %(opportunity_id)s",
                    {**fields, "opportunity_id": opportunity_id},
                )
        return readiness_store.recompute_one(conn, opportunity_id)


def contact_belongs_to_company(conn: Connection, contact_id: int, company_id: int) -> bool:
    """The import refuses a contact from another company; so does the edit form."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM contact WHERE id = %s AND company_id = %s", (contact_id, company_id))
        return cur.fetchone() is not None


def log_activity(
    conn: Connection,
    *,
    company_id: int,
    opportunity_id: int | None,
    activity_type: str,
    occurred_at: datetime,
    details: str,
    author_rep_id: int | None,
    follow_up_on: date | None = None,
    follow_up_note: str | None = None,
    completes_follow_up_id: int | None = None,
) -> dict[str, Any]:
    """Record a conversation, and optionally the promise it produced, atomically.

    `occurred_at` arrives naive from the form and is read as Europe/Rome local time -- the same
    convention the archive uses -- so a call logged at 10:30 sits correctly among imported
    calls logged at 10:30.
    """
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=ROME)

    # A completed call, email or meeting records customer contact; a task is work still to do.
    # Mirrors the archive's own marker so new rows read the same way as imported ones.
    marker = "N" if activity_type == "task" else ("Y" if activity_type in ("call", "email", "meeting") else None)

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO activity (company_id, opportunity_id, activity_type, occurred_at,"
                " details, author_rep_id, legacy_completion_marker) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (company_id, opportunity_id, activity_type, occurred_at, details,
                 author_rep_id, marker),
            )
            activity_id = cur.fetchone()["id"]

        follow_up_id = None
        if follow_up_on is not None:
            follow_up_id = followups.schedule(
                conn,
                company_id=company_id,
                opportunity_id=opportunity_id,
                due_on=follow_up_on,
                note=follow_up_note or details,
                rep_id=author_rep_id,
                source_activity_id=activity_id,
                origin="task" if activity_type == "task" else "manual",
            )

        completed = False
        if completes_follow_up_id is not None:
            completed = followups.complete(
                conn, completes_follow_up_id, completed_activity_id=activity_id
            )

    return {"activity_id": activity_id, "follow_up_id": follow_up_id, "completed": completed}


def edit_form_options(conn: Connection, company_id: int) -> dict[str, list[dict[str, Any]]]:
    """Choices for the edit form, read from the data so a replaced archive still works."""
    with conn.cursor() as cur:
        cur.execute("SELECT code, label FROM opportunity_status ORDER BY sort_order")
        statuses = cur.fetchall()
        cur.execute(
            "SELECT id, first_name, last_name, email FROM contact WHERE company_id = %s "
            "ORDER BY last_name, first_name", (company_id,)
        )
        contacts = cur.fetchall()
        cur.execute(
            "SELECT fe.id, fe.legacy_code, f.name AS fair_name, fe.edition_label,"
            "       fe.max_stand_height_m, fe.ends_on "
            "FROM fair_edition fe JOIN fair f ON f.id = fe.fair_id "
            "ORDER BY fe.starts_on DESC NULLS LAST"
        )
        editions = cur.fetchall()
        cur.execute("SELECT code, label FROM activity_type WHERE is_known ORDER BY sort_order")
        activity_types = cur.fetchall()
        cur.execute("SELECT id, display_name FROM sales_rep ORDER BY display_name")
        reps = cur.fetchall()
    return {"statuses": statuses, "contacts": contacts, "editions": editions,
            "activity_types": activity_types, "reps": reps}


# The edit form's numeric limits. Generous, but they stop a slipped keystroke turning a
# EUR 50,000 budget into EUR 50,000,000 without anyone noticing.
LIMITS = {
    "amount_eur":         Decimal("100000000"),
    "client_budget_eur":  Decimal("100000000"),
    "stand_area_sqm":     Decimal("100000"),
    "requested_height_m": Decimal("100"),
}
