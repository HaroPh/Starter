"""The importer against a small archive with a deliberate problem in every row.

Elsewhere the importer is verified by running it on the supplied archive, which is clean:
0 errors, 0 warnings. That proves the happy path and nothing about what happens when the
reviewer's copy is not identical -- and the brief is explicit that the import must survive
the data being replaced. So this test feeds it tests/fixtures/data/, a hand-written archive
(see its README) in which each row breaks one rule, and pins what the importer does about
each: the row is kept or skipped, an issue is recorded, and the load never aborts.

The fixture is test data, not a sample of the export. The application never reads it.

Runs only where DATABASE_URL is set -- the `tests` compose profile -- and in its own
database (CRM_TEST_DATABASE), so it never touches the archive a reviewer is about to inspect.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from app.db import migrate
from app.db.pool import close_pool, open_pool
from app.importer.runner import run_import

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "data"
DATABASE_URL = os.environ.get("DATABASE_URL")
TEST_DB = os.environ.get("CRM_TEST_DATABASE", "crm_test")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="needs DATABASE_URL; run through `docker compose --profile test run --rm tests`"
)


@pytest.fixture(scope="module")
def imported():
    """A fresh database, migrated, with the fixture archive imported once. Yields (pool, summary)."""
    with connect(DATABASE_URL, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{TEST_DB}"')

    params = conninfo_to_dict(DATABASE_URL)
    params["dbname"] = TEST_DB
    pool = open_pool(make_conninfo(**params), min_size=1, max_size=3)
    try:
        with pool.connection() as conn:
            migrate.apply_all(conn)
        summary = run_import(pool, FIXTURE, app_version="test")
        assert summary is not None, "the first import must run, not skip"
        yield pool, summary
    finally:
        close_pool()


def rows(pool, sql: str, *params):
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def one(pool, sql: str, *params):
    result = rows(pool, sql, *params)
    assert len(result) == 1, f"expected one row, got {len(result)}: {sql}"
    return result[0]


def issues(pool, kind: str):
    return rows(pool, "SELECT * FROM import_issue WHERE kind = %s ORDER BY id", kind)


def opportunity(pool, code: str):
    return one(pool, "SELECT * FROM opportunity WHERE legacy_code = %s", code)


def activity(pool, code: str):
    return one(pool, "SELECT * FROM activity WHERE legacy_code = %s", code)


# ---------------------------------------------------------------------------
# The load completed, with the rows it should have and without the rows it should not


def test_the_import_completes_and_counts_what_it_kept(imported):
    pool, summary = imported
    run = one(pool, "SELECT status, error, row_counts FROM import_run")
    assert run["status"] == "completed" and run["error"] is None
    # 7 opportunities and 8 activities in the files; one of each names a company that does
    # not exist and is skipped. Everything else is kept, however dirty.
    assert summary["row_counts"] == {
        "sales_rep": 4, "company": 3, "contact": 5, "fair": 2, "fair_edition": 3,
        "opportunity": 6, "activity": 7, "follow_up": 3,
    }
    assert run["row_counts"] == summary["row_counts"]


def test_no_error_is_fatal(imported):
    pool, _ = imported
    errors = rows(pool, "SELECT kind, source_row_ref FROM import_issue WHERE severity = 'error' ORDER BY 2")
    assert [(e["kind"], e["source_row_ref"]) for e in errors] == [
        ("orphan_company", "AC900004"), ("orphan_company", "OP900003"),
    ]
    assert rows(pool, "SELECT 1 FROM opportunity WHERE legacy_code = 'OP900003'") == []
    assert rows(pool, "SELECT 1 FROM activity WHERE legacy_code = 'AC900004'") == []


# ---------------------------------------------------------------------------
# The two things a naive importer gets silently wrong


def test_a_quoted_semicolon_does_not_shift_the_columns(imported):
    """A split-on-';' parser would end the details at 'plot' and move every later column."""
    pool, _ = imported
    a = one(pool, """
        SELECT a.details, a.legacy_completion_marker, r.legacy_username, f.due_on
        FROM activity a
        LEFT JOIN sales_rep r ON r.id = a.author_rep_id
        LEFT JOIN follow_up f ON f.source_activity_id = a.id
        WHERE a.legacy_code = 'AC900001'
    """)
    assert a["details"] == "Confirmed the plot; do not reuse last year's order value"
    assert a["due_on"] == date(2027, 1, 24)          # the column after the quoted field
    assert a["legacy_completion_marker"] == "Y"      # and the one after that
    assert a["legacy_username"] == "a.morgan"        # and the last


def test_accented_names_survive(imported):
    pool, _ = imported
    c = one(pool, "SELECT name FROM company WHERE legacy_code = 'CO900001'")
    assert c["name"] == "Società Prova S.r.l."
    # and the search expression folds the accent, so 'societa' finds it
    assert one(pool, "SELECT 1 AS ok FROM company WHERE search_key(name) LIKE %s", "%societa%")["ok"] == 1


# ---------------------------------------------------------------------------
# Dirty values: unknown plus an issue, never an abort and never a guess


def test_status_spellings_are_canonicalised_and_the_raw_value_kept(imported):
    pool, _ = imported
    o = opportunity(pool, "OP900002")
    assert o["status"] == "open" and o["legacy_status_raw"] == " OPEN "

    unknown = opportunity(pool, "OP900006")
    assert unknown["status"] is None and unknown["legacy_status_raw"] == "quotation sent"
    (issue,) = issues(pool, "unmapped_status")
    assert issue["severity"] == "warning" and issue["source_row_ref"] == "OP900006"


def test_unparseable_numbers_and_dates_become_unknown_with_a_warning(imported):
    pool, _ = imported
    o = opportunity(pool, "OP900004")
    assert o["amount_eur"] is None                       # '12.500,00' is not the documented format
    assert o["client_budget_eur"] == Decimal("12500.00")  # the well-formed neighbour parsed
    assert o["opened_on"] is None                        # 31 February
    assert o["expected_close_on"] == date(2027, 3, 15)

    number = [i for i in issues(pool, "unparsed_number") if i["source_row_ref"] == "OP900004"]
    assert [(i["column_name"], i["raw_value"]) for i in number] == [("amount_eur", "12.500,00")]
    dates = [i for i in issues(pool, "unparsed_date") if i["source_row_ref"] == "OP900004"]
    assert [(i["column_name"], i["raw_value"]) for i in dates] == [("opened_on", "31/02/2027")]


def test_links_to_the_wrong_company_or_to_nothing_are_cut_not_trusted(imported):
    pool, _ = imported
    o = opportunity(pool, "OP900005")
    assert o["contact_id"] is None            # CO900001-P01 belongs to the other company
    assert o["fair_edition_id"] is None       # NOPE-2030 does not exist
    assert [i["source_row_ref"] for i in issues(pool, "contact_wrong_company")] == ["OP900005"]
    assert [i["source_row_ref"] for i in issues(pool, "orphan_fair_edition")] == ["OP900005"]

    other = activity(pool, "AC900003")        # a note on CO900002 pointing at CO900001's enquiry
    assert other["opportunity_id"] is None and other["company_id"] is not None
    assert [i["source_row_ref"] for i in issues(pool, "activity_wrong_company")] == ["AC900003"]

    missing = activity(pool, "AC900008")      # an opportunity code that is not in the export
    assert missing["opportunity_id"] is None
    assert [i["source_row_ref"] for i in issues(pool, "orphan_opportunity")] == ["AC900008"]


def test_an_unknown_activity_type_is_admitted_and_flagged(imported):
    pool, _ = imported
    t = one(pool, "SELECT is_known FROM activity_type WHERE code = 'webinar'")
    assert t["is_known"] is False
    a = activity(pool, "AC900006")
    assert a["activity_type"] == "webinar"
    assert a["occurred_at"] is not None       # '31/02/2027 25:00' -> dated to the reference time, kept
    assert issues(pool, "unknown_activity_type") != []
    assert [i["source_row_ref"] for i in issues(pool, "unparsed_timestamp")] == ["AC900006"]
    bad_dates = [i["raw_value"] for i in issues(pool, "unparsed_date") if i["source_row_ref"] == "AC900006"]
    assert bad_dates == ["30/02/2027"]
    assert rows(pool, "SELECT 1 FROM follow_up WHERE source_activity_id = %s", a["id"]) == []


def test_an_unknown_author_is_admitted_but_an_ambiguous_one_is_not_guessed(imported):
    """Reps are linked by 'first initial + dot + surname'. Two edges, treated differently."""
    pool, _ = imported
    # z.nobody matches no company rep: a stand-in rep is created so the entry keeps an author.
    a = one(pool, """
        SELECT r.display_name, r.legacy_username, r.derived_from
        FROM activity a JOIN sales_rep r ON r.id = a.author_rep_id WHERE a.legacy_code = 'AC900007'
    """)
    assert a == {"display_name": "z.nobody", "legacy_username": "z.nobody", "derived_from": "author_only"}

    # j.chen could be Jamie Chen or John Chen: nothing is linked, and both facts are recorded.
    assert activity(pool, "AC900003")["author_rep_id"] is None
    (issue,) = issues(pool, "unmatched_author")
    assert (issue["source_row_ref"], issue["raw_value"]) == ("AC900003", "j.chen")
    (ambiguous,) = issues(pool, "ambiguous_author_mapping")
    assert ambiguous["details"]["username"] == "j.chen"
    assert sorted(ambiguous["details"]["display_names"]) == ["Jamie Chen", "John Chen"]


# ---------------------------------------------------------------------------
# What is derived, not just loaded


def test_follow_ups_are_derived_with_origin_and_status(imported):
    pool, _ = imported
    fus = rows(pool, """
        SELECT a.legacy_code, f.due_on, f.status, f.origin, f.completed_at IS NOT NULL AS done_at
        FROM follow_up f JOIN activity a ON a.id = f.source_activity_id
        ORDER BY a.legacy_code
    """)
    assert [(f["legacy_code"], f["due_on"], f["status"], f["origin"], f["done_at"]) for f in fus] == [
        ("AC900001", date(2027, 1, 24), "pending", "interaction", False),  # a completed call's promise
        ("AC900002", date(2027, 1, 28), "pending", "task", False),         # a task marked N
        ("AC900007", date(2027, 1, 6), "done", "task", True),              # a task marked Y: kept as history
    ]


def test_readiness_is_computed_at_import(imported):
    pool, _ = imported
    all_rows = rows(pool, "SELECT legacy_code, readiness, readiness_findings FROM opportunity")
    by_code = {o["legacy_code"]: o for o in all_rows}
    assert by_code["OP900001"]["readiness"] == "ready"
    assert by_code["OP900002"]["readiness"] == "provisional"
    assert by_code["OP900005"]["readiness"] == "incomplete"     # no edition
    assert by_code["OP900007"]["readiness"] == "ready"          # readiness ignores time; the assistant does not
    # Everything known, but the edition has no height limit: cannot be READY, cannot be BLOCKED.
    capped = by_code["OP900006"]
    assert capped["readiness"] == "provisional"
    assert "limit_unknown" in {f["code"] for f in capped["readiness_findings"]["findings"]}


def test_fairs_and_reps_are_derived(imported):
    pool, _ = imported
    fairs = rows(pool, "SELECT code FROM fair ORDER BY 1")
    assert [f["code"] for f in fairs] == ["OPEN", "TEST"]    # from the edition-code prefix
    reps = rows(pool, "SELECT display_name, legacy_username, derived_from FROM sales_rep ORDER BY 1")
    assert [tuple(r.values()) for r in reps] == [
        ("Alex Morgan", "a.morgan", "both"),      # name on companies, username on activities
        ("Jamie Chen", None, "name_only"),        # ambiguous with John Chen: no username attached
        ("John Chen", None, "name_only"),
        ("z.nobody", "z.nobody", "author_only"),  # seen only in the activity log
    ]


# ---------------------------------------------------------------------------
# The manifest is a canary, and the import happens once


def test_manifest_deltas_are_recorded_not_enforced(imported):
    pool, _ = imported
    deltas = {i["details"]["table"]: i["details"] for i in issues(pool, "entity_count_mismatch")}
    assert deltas["opportunity"] == {"declared": 7, "imported": 6, "table": "opportunity"}
    assert deltas["activity"] == {"declared": 8, "imported": 7, "table": "activity"}
    assert issues(pool, "checksum_mismatch") == []       # the fixture's checksums are real
    assert issues(pool, "row_count_mismatch") == []


def test_a_second_run_is_a_no_op(imported):
    pool, summary = imported
    assert run_import(pool, FIXTURE, app_version="test") is None
    counts = one(pool, """
        SELECT (SELECT count(*) FROM company) AS company, (SELECT count(*) FROM opportunity) AS opportunity,
               (SELECT count(*) FROM activity) AS activity, (SELECT count(*) FROM follow_up) AS follow_up,
               (SELECT count(*) FROM import_run WHERE status = 'completed') AS completed_runs
    """)
    assert counts == {"company": 3, "opportunity": 6, "activity": 7, "follow_up": 3, "completed_runs": 1}
    assert summary["row_counts"]["opportunity"] == counts["opportunity"]
