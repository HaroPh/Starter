"""Follow-ups: the queue, and scheduling, completing and rescheduling.

The brief:

    "The account managers need to know who to call and what they're waiting for. If a
     customer promises to confirm the floor area on Friday, that needs to turn into
     something they can find and act on."

So the queue answers "who do I call today", grouped the way a person plans a day -- overdue
first, then today, then this week, then later -- and each row says what is being waited for,
from which exhibitor, on which opportunity, with the contact's phone number on the same line.

It reads from the partial index `follow_up_pending_due_idx`, which holds only pending rows in
due-date order. The bucket counts at the top are index-only scans; the rows come back
already ordered, so keyset paging never sorts.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg import Connection

from app.db.pagination import Page, build_page, decode_cursor

BUCKETS = ("overdue", "today", "week", "later", "all")

_BUCKET_SQL = {
    "overdue": "fu.due_on <  current_date",
    "today":   "fu.due_on =  current_date",
    "week":    "fu.due_on >  current_date AND fu.due_on <= current_date + 7",
    "later":   "fu.due_on >  current_date + 7",
    "all":     "true",
}


def _text_filter(q: str | None, where: list[str], params: list[Any]) -> None:
    """Narrow the queue to one exhibitor, one opportunity, or one promise.

    Added after walking the brief's own example end to end: a follow-up scheduled for Friday
    landed at position 281 of 503 in "next 7 days" -- page ten. The archive is a snapshot
    from 1 September, so against today's date thousands of its follow-ups fall due at once,
    and "find it again later" stops meaning anything without a way to narrow the list.

    Company names go through the trigram index (search_key + company_name_trgm_idx); an
    opportunity code is an exact match; the note is a plain substring match, which is fine
    because it only runs over pending follow-ups, never the whole table.
    """
    term = (q or "").strip()
    if not term:
        return
    where.append(
        "(search_key(c.name) LIKE '%%' || search_key(%s) || '%%'"
        " OR o.legacy_code = upper(%s)"
        " OR fu.note ILIKE '%%' || %s || '%%')"
    )
    params += [term, term, term]


def bucket_counts(
    conn: Connection, *, rep_id: int | None = None, tasks_only: bool = False, q: str | None = None
) -> dict[str, int]:
    where = ["fu.status = 'pending'"]
    params: list[Any] = []
    if rep_id:
        where.append("fu.assigned_rep_id = %s")
        params.append(rep_id)
    if tasks_only:
        where.append("fu.origin = 'task'")
    _text_filter(q, where, params)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              count(*) FILTER (WHERE {_BUCKET_SQL['overdue']}) AS overdue,
              count(*) FILTER (WHERE {_BUCKET_SQL['today']})   AS today,
              count(*) FILTER (WHERE {_BUCKET_SQL['week']})    AS week,
              count(*) FILTER (WHERE {_BUCKET_SQL['later']})   AS later,
              count(*)                                          AS "all"
            FROM follow_up fu
            JOIN company c          ON c.id = fu.company_id
            LEFT JOIN opportunity o ON o.id = fu.opportunity_id
            WHERE {' AND '.join(where)}
            """,
            params,
        )
        return cur.fetchone()


def queue(
    conn: Connection,
    *,
    bucket: str = "overdue",
    rep_id: int | None = None,
    tasks_only: bool = False,
    q: str | None = None,
    cursor: str | None = None,
    page_size: int = 30,
) -> Page[dict[str, Any]]:
    bucket = bucket if bucket in _BUCKET_SQL else "overdue"
    where = ["fu.status = 'pending'", _BUCKET_SQL[bucket]]
    params: list[Any] = []
    if rep_id:
        where.append("fu.assigned_rep_id = %s")
        params.append(rep_id)
    if tasks_only:
        where.append("fu.origin = 'task'")
    _text_filter(q, where, params)

    key = decode_cursor(cursor)
    if key and len(key) == 2:
        where.append("(fu.due_on, fu.id) > (%s, %s)")
        params += [key[0], key[1]]

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT fu.id, fu.due_on, fu.origin, fu.note, fu.status,
                   (current_date - fu.due_on) AS days_overdue,
                   c.legacy_code AS company_code, c.name AS company_name,
                   o.legacy_code AS opportunity_code, o.id AS opportunity_id,
                   o.description AS opportunity_description,
                   fe.legacy_code AS edition_code,
                   r.display_name AS rep_name,
                   ct.first_name AS contact_first_name, ct.last_name AS contact_last_name,
                   ct.phone AS contact_phone, ct.email AS contact_email
            FROM follow_up fu
            JOIN company c              ON c.id  = fu.company_id
            LEFT JOIN opportunity o     ON o.id  = fu.opportunity_id
            LEFT JOIN fair_edition fe   ON fe.id = o.fair_edition_id
            LEFT JOIN sales_rep r       ON r.id  = fu.assigned_rep_id
            LEFT JOIN contact ct        ON ct.id = o.contact_id
            WHERE {' AND '.join(where)}
            ORDER BY fu.due_on, fu.id
            LIMIT %s
            """,
            (*params, page_size + 1),
        )
        rows = cur.fetchall()
    return build_page(rows, page_size, lambda r: (r["due_on"], r["id"]))


def get(conn: Connection, follow_up_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fu.*, (current_date - fu.due_on) AS days_overdue,"
            "       c.legacy_code AS company_code, c.name AS company_name,"
            "       o.legacy_code AS opportunity_code, o.description AS opportunity_description,"
            "       fe.legacy_code AS edition_code, r.display_name AS rep_name,"
            "       ct.first_name AS contact_first_name, ct.last_name AS contact_last_name,"
            "       ct.phone AS contact_phone, ct.email AS contact_email "
            "FROM follow_up fu JOIN company c ON c.id = fu.company_id "
            "LEFT JOIN opportunity o ON o.id = fu.opportunity_id "
            "LEFT JOIN fair_edition fe ON fe.id = o.fair_edition_id "
            "LEFT JOIN sales_rep r ON r.id = fu.assigned_rep_id "
            "LEFT JOIN contact ct ON ct.id = o.contact_id "
            "WHERE fu.id = %s",
            (follow_up_id,),
        )
        return cur.fetchone()


def schedule(
    conn: Connection,
    *,
    company_id: int,
    opportunity_id: int | None,
    due_on: date,
    note: str,
    rep_id: int | None,
    source_activity_id: int | None = None,
    origin: str = "manual",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO follow_up (company_id, opportunity_id, due_on, note, assigned_rep_id,"
            " source_activity_id, origin) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (company_id, opportunity_id, due_on, note, rep_id, source_activity_id, origin),
        )
        return cur.fetchone()["id"]


def complete(conn: Connection, follow_up_id: int, *, completed_activity_id: int | None = None) -> bool:
    """Mark done. Only a pending follow-up can be completed, so a double submit is a no-op."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE follow_up SET status = 'done', completed_at = now(),"
            " completed_activity_id = coalesce(%s, completed_activity_id), updated_at = now() "
            "WHERE id = %s AND status = 'pending'",
            (completed_activity_id, follow_up_id),
        )
        return cur.rowcount == 1


def reschedule(conn: Connection, follow_up_id: int, due_on: date) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE follow_up SET due_on = %s, updated_at = now() "
            "WHERE id = %s AND status = 'pending'",
            (due_on, follow_up_id),
        )
        return cur.rowcount == 1


def reps(conn: Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, display_name FROM sales_rep ORDER BY display_name")
        return cur.fetchall()
