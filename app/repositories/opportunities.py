"""Reading an opportunity, its timeline, and the filtered list.

The edition-scoping rule is enforced in exactly one place and it is the `timeline` query
below: it filters on `opportunity_id` and NEVER on `company_id`. Widening it to the company
is the single change that would reintroduce the behaviour the brief complains about --
last year's agreement appearing alongside this year's enquiry as though it still applied.

The measured stake: 4,995 company-and-fair pairs in this archive span more than one edition.
The demonstration case is CO000001, which holds a Won 2026 stand at EUR 12,500 whose own
notes say "this agreement does not cover next year's stand", next to an open 2027 enquiry.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection

from app.db.pagination import Page, build_page, decode_cursor
from app.repositories.common import ref_clause, ref_params

# Every readiness state except 'ready'. The distribution is extremely lopsided -- 13,906
# ready against 1,094 everything else -- so a plain status filter is close to useless and
# the list defaults to this instead.
ATTENTION = ("provisional", "incomplete", "blocked")


def get(conn: Connection, ref: str) -> dict[str, Any] | None:
    """One opportunity with everything the detail page header needs.

    A single query rather than several: the page renders the exhibitor, the contact, the
    fair edition and the readiness verdict together, and issuing four round trips to build
    one header is how template-driven N+1 starts.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.*, "
            "       c.legacy_code AS company_code, c.name AS company_name,"
            "       c.province_code, c.region,"
            "       r.display_name AS rep_name,"
            "       ct.legacy_code AS contact_code, ct.first_name AS contact_first_name,"
            "       ct.last_name AS contact_last_name, ct.email AS contact_email,"
            "       ct.phone AS contact_phone,"
            "       fe.legacy_code AS edition_code, fe.edition_label, fe.city, fe.venue,"
            "       fe.starts_on, fe.ends_on, fe.max_stand_height_m,"
            "       f.name AS fair_name,"
            "       (fe.ends_on IS NOT NULL AND fe.ends_on < current_date) AS edition_is_past,"
            "       st.label AS status_label "
            "FROM opportunity o "
            "JOIN company c            ON c.id  = o.company_id "
            "LEFT JOIN sales_rep r     ON r.id  = c.sales_rep_id "
            "LEFT JOIN contact ct      ON ct.id = o.contact_id "
            "LEFT JOIN fair_edition fe ON fe.id = o.fair_edition_id "
            "LEFT JOIN fair f          ON f.id  = fe.fair_id "
            "LEFT JOIN opportunity_status st ON st.code = o.status "
            f"WHERE {ref_clause('o')}",
            ref_params(ref),
        )
        return cur.fetchone()


def timeline(
    conn: Connection, opportunity_id: int, *, cursor: str | None = None, page_size: int = 25
) -> Page[dict[str, Any]]:
    """This opportunity's conversations, newest first.

    THE EDITION-SCOPING RULE LIVES HERE. The WHERE clause names opportunity_id and nothing
    else. Do not add `OR a.company_id = ...` to "show more context": the context it would
    add is another edition's, which is the thing the sales coordinator asked to stop seeing.

    Served by activity_opportunity_time_idx (opportunity_id, occurred_at DESC, id DESC), so
    the rows come back already ordered and paging never sorts.
    """
    key = decode_cursor(cursor)
    params: list[Any] = [opportunity_id]
    where = "a.opportunity_id = %s"
    if key and len(key) == 2:
        where += " AND (a.occurred_at, a.id) < (%s, %s)"
        params += [key[0], key[1]]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.id, a.legacy_code, a.activity_type, a.occurred_at, a.details,"
            "       a.legacy_completion_marker,"
            "       t.label AS type_label, t.is_contact, t.is_known AS type_known,"
            "       r.display_name AS author_name,"
            "       fu.id AS follow_up_id, fu.due_on AS follow_up_due_on,"
            "       fu.status AS follow_up_status "
            "FROM activity a "
            "JOIN activity_type t   ON t.code = a.activity_type "
            "LEFT JOIN sales_rep r  ON r.id = a.author_rep_id "
            "LEFT JOIN follow_up fu ON fu.source_activity_id = a.id "
            f"WHERE {where} "
            "ORDER BY a.occurred_at DESC, a.id DESC LIMIT %s",
            (*params, page_size + 1),
        )
        rows = cur.fetchall()

    return build_page(rows, page_size, lambda r: (r["occurred_at"], r["id"]))


def open_follow_ups(conn: Connection, opportunity_id: int) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fu.id, fu.due_on, fu.origin, fu.note, fu.status,"
            "       r.display_name AS rep_name,"
            "       (fu.due_on < current_date) AS is_overdue "
            "FROM follow_up fu LEFT JOIN sales_rep r ON r.id = fu.assigned_rep_id "
            "WHERE fu.opportunity_id = %s AND fu.status = 'pending' "
            "ORDER BY fu.due_on, fu.id",
            (opportunity_id,),
        )
        return cur.fetchall()


def sibling_editions(conn: Connection, opportunity_id: int) -> list[dict[str, Any]]:
    """Other opportunities for the same exhibitor at OTHER editions of the same fair.

    Shown deliberately, and deliberately limited. The coordinator's problem is not that
    people can see last year -- it is that last year gets mistaken for this year. So the
    page shows that prior editions EXIST, with their edition label and status, as a link.
    It does not inline their amounts or their agreements next to the current figures.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o2.id, o2.legacy_code, o2.status, o2.opened_on,
                   fe2.legacy_code AS edition_code, fe2.edition_label,
                   fe2.starts_on, fe2.ends_on,
                   (fe2.ends_on IS NOT NULL AND fe2.ends_on < current_date) AS is_past
            FROM opportunity o
            JOIN fair_edition fe  ON fe.id  = o.fair_edition_id
            JOIN fair_edition fe2 ON fe2.fair_id = fe.fair_id AND fe2.id <> fe.id
            JOIN opportunity o2   ON o2.fair_edition_id = fe2.id
                                 AND o2.company_id = o.company_id
            WHERE o.id = %s
            ORDER BY fe2.starts_on DESC NULLS LAST, o2.id DESC
            """,
            (opportunity_id,),
        )
        return cur.fetchall()


def listing(
    conn: Connection,
    *,
    edition: str | None = None,
    status: str | None = None,
    readiness: str | None = None,
    attention: bool = False,
    cursor: str | None = None,
    page_size: int = 25,
) -> Page[dict[str, Any]]:
    """The filtered opportunity list, keyset-paged.

    No total count: at 75,000 rows a COUNT(*) behind every page would cost more than the
    page. The buckets that ARE counted are the readiness tallies, which come from a partial
    index and are index-only.
    """
    where = ["true"]
    params: dict[str, Any] = {}

    if edition:
        where.append("fe.legacy_code = %(edition)s")
        params["edition"] = edition
    if status:
        where.append("o.status = %(status)s")
        params["status"] = status
    if readiness:
        where.append("o.readiness = %(readiness)s")
        params["readiness"] = readiness
    elif attention:
        where.append("o.readiness IS DISTINCT FROM 'ready'")

    key = decode_cursor(cursor)
    if key and len(key) == 2:
        where.append("(o.opened_on, o.id) < (%(cur_date)s, %(cur_id)s)")
        params["cur_date"] = key[0]
        params["cur_id"] = key[1]

    params["limit"] = page_size + 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.id, o.legacy_code, o.description, o.status, o.legacy_status_raw,"
            "       o.amount_eur, o.client_budget_eur, o.stand_area_sqm,"
            "       o.requested_height_m, o.readiness, o.opened_on, o.expected_close_on,"
            "       c.legacy_code AS company_code, c.name AS company_name,"
            "       c.province_code, r.display_name AS rep_name,"
            "       fe.legacy_code AS edition_code, fe.max_stand_height_m,"
            "       fa.name AS fair_name, fe.edition_label "
            "FROM opportunity o "
            "JOIN company c            ON c.id  = o.company_id "
            "LEFT JOIN sales_rep r     ON r.id  = c.sales_rep_id "
            "LEFT JOIN fair_edition fe ON fe.id = o.fair_edition_id "
            "LEFT JOIN fair fa         ON fa.id = fe.fair_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY o.opened_on DESC NULLS LAST, o.id DESC LIMIT %(limit)s",
            params,
        )
        rows = cur.fetchall()

    return build_page(rows, page_size, lambda r: (r["opened_on"], r["id"]))


def readiness_counts(conn: Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(readiness, 'unassessed') AS readiness, count(*) AS n "
            "FROM opportunity GROUP BY 1"
        )
        return {r["readiness"]: r["n"] for r in cur.fetchall()}


def filter_options(conn: Connection) -> dict[str, list[dict[str, Any]]]:
    """Values for the list filters, read from the data rather than hardcoded.

    Hardcoding the sixteen edition codes would break the moment the archive is replaced,
    which is the one thing the brief guarantees will happen.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fe.legacy_code, f.name AS fair_name, fe.edition_label, fe.starts_on,"
            "       count(o.id) AS opportunity_count "
            "FROM fair_edition fe JOIN fair f ON f.id = fe.fair_id "
            "LEFT JOIN opportunity o ON o.fair_edition_id = fe.id "
            "GROUP BY fe.id, fe.legacy_code, f.name, fe.edition_label, fe.starts_on "
            "ORDER BY fe.starts_on DESC NULLS LAST"
        )
        editions = cur.fetchall()

        cur.execute(
            "SELECT s.code, s.label, count(o.id) AS n "
            "FROM opportunity_status s LEFT JOIN opportunity o ON o.status = s.code "
            "GROUP BY s.code, s.label, s.sort_order ORDER BY s.sort_order"
        )
        statuses = cur.fetchall()

    return {"editions": editions, "statuses": statuses}
