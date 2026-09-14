"""Reading a company and everything hanging off it.

This screen is where the sales coordinator's complaint gets answered. The brief:

    "The sales coordinator wants this year's enquiry to show only this edition's
     conversations and follow-ups: people have been treating last year's agreement as if it
     still applied."

So the company page groups opportunities BY FAIR EDITION, orders the groups newest first,
and separates editions that have already finished into a read-only history block whose
amounts are labelled as agreed for that edition. What it never does is present a figure from
one edition where a reader could mistake it for the current one.

The measured reason this matters: 4,995 company-and-fair pairs in this archive span more
than one edition. It is not an edge case, it is half the dataset.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection

from app.db.pagination import Page, build_page, decode_cursor
from app.repositories.common import ref_clause, ref_params


def get(conn: Connection, ref: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.legacy_code, c.name, c.province_code, c.region,"
            "       c.created_at, c.updated_at,"
            "       r.display_name AS rep_name, r.legacy_username AS rep_username "
            "FROM company c LEFT JOIN sales_rep r ON r.id = c.sales_rep_id "
            f"WHERE {ref_clause('c')}",
            ref_params(ref),
        )
        return cur.fetchone()


def contacts(conn: Connection, company_id: int) -> list[dict[str, Any]]:
    """Contacts, with the count of opportunities each one is primary on.

    The brief says account managers "need to know who to call", so a contact missing a
    channel is worth surfacing rather than rendering as an empty cell -- the template shows
    "no email on file" instead of nothing.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ct.id, ct.legacy_code, ct.first_name, ct.last_name,"
            "       ct.email, ct.phone, ct.fax,"
            "       (SELECT count(*) FROM opportunity o WHERE o.contact_id = ct.id) AS opportunity_count "
            "FROM contact ct WHERE ct.company_id = %s "
            "ORDER BY ct.last_name, ct.first_name, ct.legacy_code",
            (company_id,),
        )
        return cur.fetchall()


def opportunities_by_edition(conn: Connection, company_id: int) -> list[dict[str, Any]]:
    """Every opportunity for the company, grouped into its fair edition.

    Returns one row per edition, each carrying its opportunities as a JSON array, so the
    template iterates groups directly and the page costs a single round trip. `is_past` is
    computed here rather than in the template so the rule lives in one place.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                fe.legacy_code                       AS edition_code,
                f.name                               AS fair_name,
                fe.edition_label,
                fe.city, fe.venue, fe.starts_on, fe.ends_on,
                fe.max_stand_height_m,
                (fe.ends_on IS NOT NULL AND fe.ends_on < current_date) AS is_past,
                jsonb_agg(jsonb_build_object(
                    'id',                 o.id,
                    'legacy_code',        o.legacy_code,
                    'description',        o.description,
                    'status',             o.status,
                    'legacy_status_raw',  o.legacy_status_raw,
                    'amount_eur',         o.amount_eur,
                    'client_budget_eur',  o.client_budget_eur,
                    'stand_area_sqm',     o.stand_area_sqm,
                    'requested_height_m', o.requested_height_m,
                    'readiness',          o.readiness,
                    'opened_on',          o.opened_on,
                    'expected_close_on',  o.expected_close_on,
                    'contact_name',
                        nullif(btrim(coalesce(ct.first_name, '') || ' ' || coalesce(ct.last_name, '')), '')
                ) ORDER BY o.opened_on DESC NULLS LAST, o.id DESC) AS opportunities
            FROM opportunity o
            JOIN company c       ON c.id  = o.company_id
            LEFT JOIN fair_edition fe ON fe.id = o.fair_edition_id
            LEFT JOIN fair f          ON f.id  = fe.fair_id
            LEFT JOIN contact ct      ON ct.id = o.contact_id
            WHERE o.company_id = %s
            GROUP BY fe.id, fe.legacy_code, f.name, fe.edition_label, fe.city, fe.venue,
                     fe.starts_on, fe.ends_on, fe.max_stand_height_m
            ORDER BY fe.starts_on DESC NULLS LAST, fe.legacy_code DESC
            """,
            (company_id,),
        )
        return cur.fetchall()


def company_notes(
    conn: Connection, company_id: int, *, cursor: str | None = None, page_size: int = 25
) -> Page[dict[str, Any]]:
    """Activities attached to the company but to NO opportunity.

    5,001 of the 40,000 imported entries are like this. They belong to the exhibitor
    relationship rather than to any one enquiry, so they get their own tab. Mixing them into
    an opportunity timeline is exactly the behaviour that makes last year look current.
    """
    key = decode_cursor(cursor)
    params: list[Any] = [company_id]
    where = "a.company_id = %s AND a.opportunity_id IS NULL"
    if key and len(key) == 2:
        where += " AND (a.occurred_at, a.id) < (%s, %s)"
        params += [key[0], key[1]]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.id, a.legacy_code, a.activity_type, a.occurred_at, a.details,"
            "       t.label AS type_label, t.is_known AS type_known,"
            "       r.display_name AS author_name "
            "FROM activity a "
            "JOIN activity_type t ON t.code = a.activity_type "
            "LEFT JOIN sales_rep r ON r.id = a.author_rep_id "
            f"WHERE {where} "
            "ORDER BY a.occurred_at DESC, a.id DESC LIMIT %s",
            (*params, page_size + 1),
        )
        rows = cur.fetchall()

    return build_page(rows, page_size, lambda r: (r["occurred_at"], r["id"]))


def summary(conn: Connection, company_id: int) -> dict[str, Any]:
    """Counts for the company header, in one query rather than four."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT count(*) FROM contact     WHERE company_id = %(cid)s) AS contacts,
                (SELECT count(*) FROM opportunity WHERE company_id = %(cid)s) AS opportunities,
                (SELECT count(*) FROM opportunity WHERE company_id = %(cid)s
                   AND status IN ('open','qualified','proposal'))             AS open_opportunities,
                (SELECT count(*) FROM activity    WHERE company_id = %(cid)s) AS activities,
                (SELECT count(*) FROM activity    WHERE company_id = %(cid)s
                   AND opportunity_id IS NULL)                                AS company_notes,
                (SELECT count(*) FROM follow_up   WHERE company_id = %(cid)s
                   AND status = 'pending')                                    AS open_follow_ups,
                (SELECT count(*) FROM follow_up   WHERE company_id = %(cid)s
                   AND status = 'pending' AND due_on < current_date)          AS overdue_follow_ups
            """,
            {"cid": company_id},
        )
        return cur.fetchone()
