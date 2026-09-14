"""Writing the readiness verdict into opportunity.readiness.

The column is a CACHE of `policy.assess()`, and this module is the only thing that writes it.
It cannot be a generated column -- a generation expression may reference only its own row,
and the height check needs fair_edition.max_stand_height_m -- and expressing the rules as a
SQL view would put the policy in two languages that could drift apart. So the rules run in
Python, exactly once per definition, and this module stores the result.

The cache exists for one reason: the "needs attention" list has to filter and sort on
readiness in SQL, across tens of thousands of rows, using an index.

Three triggers keep it current:
  * at the end of the import, inside the same transaction, so a freshly imported archive is
    never visible without its verdicts;
  * whenever an opportunity is edited (see the write routes);
  * at startup, when the stored policy version differs from POLICY_VERSION -- which is what
    makes "change the policy, bump the version, restart" the whole procedure.

Bulk recomputation streams the verdicts into a temporary table with COPY and applies them
with one UPDATE ... FROM, rather than issuing an UPDATE per row. At 75,000 opportunities the
difference is seconds against minutes.
"""

from __future__ import annotations

import json
import logging
import time

from psycopg import Connection

from app.handoff.policy import POLICY_VERSION, PolicyInput, assess

log = logging.getLogger("crm.readiness")

_SELECT_INPUTS = """
    SELECT o.id,
           (fe.id IS NOT NULL)    AS has_fair_edition,
           fe.max_stand_height_m,
           fe.ends_on             AS edition_ends_on,
           o.client_budget_eur,
           o.stand_area_sqm,
           o.requested_height_m
    FROM opportunity o
    LEFT JOIN fair_edition fe ON fe.id = o.fair_edition_id
"""


def _input_from_row(row: dict) -> PolicyInput:
    return PolicyInput(
        has_fair_edition=row["has_fair_edition"],
        max_stand_height_m=row["max_stand_height_m"],
        edition_ends_on=row["edition_ends_on"],
        client_budget_eur=row["client_budget_eur"],
        stand_area_sqm=row["stand_area_sqm"],
        requested_height_m=row["requested_height_m"],
    )


def recompute_all(conn: Connection, *, where: str = "", params: tuple = ()) -> int:
    """Recompute and store the verdict for every matching opportunity.

    Does not commit: the caller owns the transaction. The importer relies on that, so the
    verdicts land atomically with the rows they describe.
    """
    started = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(_SELECT_INPUTS + (f" WHERE {where}" if where else ""), params)
        rows = cur.fetchall()
        if not rows:
            return 0

        cur.execute(
            "CREATE TEMP TABLE IF NOT EXISTS tmp_readiness "
            "(id bigint PRIMARY KEY, readiness text, findings jsonb) ON COMMIT DROP"
        )
        cur.execute("TRUNCATE tmp_readiness")

        with cur.copy("COPY tmp_readiness (id, readiness, findings) FROM STDIN") as cp:
            for row in rows:
                verdict = assess(_input_from_row(row))
                cp.write_row((row["id"], verdict.readiness.value, json.dumps(verdict.to_json())))

        cur.execute(
            "UPDATE opportunity o SET readiness = t.readiness, readiness_findings = t.findings,"
            " readiness_policy_version = %s, readiness_computed_at = now() "
            "FROM tmp_readiness t WHERE o.id = t.id",
            (POLICY_VERSION,),
        )
        updated = cur.rowcount

    log.info(
        "readiness recomputed for %d opportunities under %s in %.0f ms",
        updated, POLICY_VERSION, (time.perf_counter() - started) * 1000,
    )
    return updated


def recompute_one(conn: Connection, opportunity_id: int) -> dict | None:
    """Recompute a single opportunity after an edit. Returns the stored verdict JSON."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_INPUTS + " WHERE o.id = %s", (opportunity_id,))
        row = cur.fetchone()
        if row is None:
            return None
        verdict = assess(_input_from_row(row))
        payload = verdict.to_json()
        cur.execute(
            "UPDATE opportunity SET readiness = %s, readiness_findings = %s,"
            " readiness_policy_version = %s, readiness_computed_at = now() WHERE id = %s",
            (verdict.readiness.value, json.dumps(payload), POLICY_VERSION, opportunity_id),
        )
    return payload


def recompute_for_edition(conn: Connection, fair_edition_id: int) -> int:
    """An edition's height limit changed: every opportunity at that edition is re-ruled."""
    return recompute_all(conn, where="o.fair_edition_id = %s", params=(fair_edition_id,))


def ensure_current(conn: Connection) -> int:
    """Recompute everything if any stored verdict predates the current policy version.

    Called once at startup, after the import. This is the mechanism that makes changing the
    policy a one-file edit: nothing to migrate, nothing to backfill by hand.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM opportunity "
            "WHERE readiness_policy_version IS DISTINCT FROM %s) AS stale",
            (POLICY_VERSION,),
        )
        stale = cur.fetchone()["stale"]
    if not stale:
        log.info("readiness verdicts are current (%s)", POLICY_VERSION)
        return 0
    log.info("stored readiness predates %s; recomputing", POLICY_VERSION)
    with conn.transaction():
        return recompute_all(conn)
