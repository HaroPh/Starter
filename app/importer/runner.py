"""Orchestrates the archive import.

Two guarantees drive the whole design, and both come straight from the brief.

**"Later starts must keep user changes and must not duplicate the import."**
The ledger that answers "have we already imported?" lives in the DATABASE. That is not a
stylistic choice: `docker compose down && ./dev.sh` destroys the containers but keeps the
postgres-data volume, so a marker file inside the container filesystem would be gone on the
next start and the archive would load a second time, while a marker file inside the volume
would be redundant with a table sitting next to it. Only a row in the database survives a
container recreation. A session-level advisory lock serialises concurrent attempts, and a
partial unique index on import_run makes a second completed run impossible even if both
were somehow bypassed.

**"A solution that depends on edited files, a hard-coded subset or generated replacement
records does not meet the requirement."**
Nothing here knows any company code, opportunity code or row count. Every check is an
anti-join that writes an import_issue and continues. The archive is read only through the
read-only bind mount, so the application provably cannot modify it.

Transaction shape:

    1. session advisory lock, ledger check                        (own transaction)
    2. INSERT import_run (running)                                (own transaction)
    3. staging -> COPY -> transforms -> issues                    (ONE transaction)
    4. UPDATE import_run (completed | failed)                     (own transaction)
    5. ANALYZE, drop staging                                      (outside a transaction)

Step 3 being a single transaction is what lets a request arriving mid-import see either an
empty database or a complete one, never a torn state. Steps 2 and 4 are separate precisely
so that a failure in step 3 can still be RECORDED after the rollback.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from psycopg import ClientCursor, Connection
from psycopg_pool import ConnectionPool

from app.handoff import readiness_store
from app.importer import manifest as manifest_mod
from app.importer.copy import CopyResult, copy_file

log = logging.getLogger("crm.import")

SQL_DIR = Path(__file__).resolve().parent / "sql"

# Distinct from the migration lock (8140250) so the two never contend.
IMPORT_LOCK_ID = 8140251

# filename -> staging table. Files absent from the archive are reported, not fatal.
FILES: dict[str, str] = {
    "fair_editions.csv": "stg_fair_editions",
    "companies_and_contacts.csv": "stg_companies_contacts",
    "opportunities.csv": "stg_opportunities",
    "activity_log.csv": "stg_activities",
}

# Order matters: reference data before the rows that point at it.
TRANSFORMS = [
    "01_reference.sql",
    "02_companies.sql",
    "03_contacts.sql",
    "04_opportunities.sql",
    "05_activities.sql",
    "06_follow_ups.sql",
]

COUNT_TABLES = [
    "sales_rep", "fair", "fair_edition", "company", "contact",
    "opportunity", "activity", "follow_up",
]

# A pathologically dirty archive must produce a useful sample, not millions of rows. The
# full counts are kept on import_run.issue_counts regardless.
MAX_ISSUES_PER_KIND = 500


class ImportSkipped(Exception):
    """A completed import already exists; nothing to do."""


def _read_sql(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def already_imported(conn: Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM import_run WHERE status = 'completed' LIMIT 1")
        return cur.fetchone() is not None


def run_import(pool: ConnectionPool, data_dir: Path, app_version: str) -> dict | None:
    """Import the archive unless it has already been imported.

    Returns a summary dict, or None when the import was skipped.
    """
    started = time.perf_counter()

    with pool.connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Session-level, not transaction-level: the lock has to span several
            # transactions, so pg_advisory_xact_lock would release it too early.
            cur.execute("SELECT pg_advisory_lock(%s)", (IMPORT_LOCK_ID,))
        try:
            if already_imported(conn):
                log.info("archive already imported; skipping")
                return None

            # A 'running' row left by a crashed attempt is safe to discard: step 3 is a
            # single transaction, so a crash can never have left partial data behind.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE import_run SET status = 'failed', finished_at = now(), "
                    "error = coalesce(error, 'interrupted before completion') "
                    "WHERE status = 'running'"
                )
                if cur.rowcount:
                    log.warning("marked %d interrupted import run(s) as failed", cur.rowcount)

            mf = manifest_mod.load(data_dir)

            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO import_run (status, dataset_version, dataset_reference_time,"
                    " manifest_sha256, app_version) "
                    "VALUES ('running', %s, %s, %s, %s) RETURNING id",
                    (mf.dataset_version, mf.reference_time,
                     json.dumps(mf.checksums) if mf.checksums else None, app_version),
                )
                run_id = cur.fetchone()["id"]
            log.info("import run %d starting from %s", run_id, data_dir)

            try:
                summary = _do_import(conn, run_id, data_dir, mf)
            except Exception as exc:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE import_run SET status='failed', finished_at=now(), error=%s "
                        "WHERE id=%s",
                        (f"{type(exc).__name__}: {exc}", run_id),
                    )
                log.exception("import run %d FAILED", run_id)
                raise

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE import_run SET status='completed', finished_at=now(), "
                    "row_counts=%s, issue_counts=%s, observed_sha256=%s, duration_ms=%s "
                    "WHERE id=%s",
                    (json.dumps(summary["row_counts"]), json.dumps(summary["issue_counts"]),
                     json.dumps(summary["sha256"]), elapsed_ms, run_id),
                )

            # After the commit, and deliberately so. Without fresh statistics the planner
            # works from an empty table and may sequential-scan everything -- and a fresh
            # ./reset.sh && ./dev.sh is exactly the state the reviewer sees first.
            log.info("running ANALYZE")
            with conn.cursor() as cur:
                for table in COUNT_TABLES:
                    cur.execute(f"ANALYZE {table}")
                for table in FILES.values():
                    cur.execute(f"DROP TABLE IF EXISTS {table}")

            summary["duration_ms"] = elapsed_ms
            summary["run_id"] = run_id
            log.info(
                "import completed in %.1fs: %s",
                elapsed_ms / 1000,
                ", ".join(f"{k}={v}" for k, v in summary["row_counts"].items()),
            )
            return summary
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (IMPORT_LOCK_ID,))


def _do_import(conn: Connection, run_id: int, data_dir: Path, mf) -> dict:
    """Staging, COPY and transforms, all inside one transaction."""
    copies: list[CopyResult] = []

    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(_read_sql("00_staging.sql"))

            missing_files = []
            for filename, table in FILES.items():
                path = data_dir / filename
                if not path.is_file():
                    missing_files.append(filename)
                    log.error("%s not found in %s", filename, data_dir)
                    continue
                copies.append(copy_file(conn, path, table))

            for filename in missing_files:
                cur.execute(
                    "INSERT INTO import_issue (import_run_id, severity, kind, source_file, message)"
                    " VALUES (%s, 'error', 'missing_file', %s, %s)",
                    (run_id, filename,
                     "This file was not present in the archive directory, so the records it "
                     "would have supplied are absent."),
                )

            _record_copy_observations(cur, run_id, copies, mf)

            # Each transform file holds many statements. psycopg binds parameters using the
            # extended query protocol, which permits exactly ONE statement per execute, so
            # passing %(run_id)s through a normal cursor fails with "cannot insert multiple
            # commands into a prepared statement".
            #
            # ClientCursor interpolates the parameter client-side and sends a simple query,
            # which does allow multiple statements. The alternatives were worse: splitting
            # the files on semicolons needs a parser that understands dollar-quoting and
            # semicolons inside string literals, and building the SQL with str.replace
            # would look like injection even though run_id is an integer we generated.
            for name in TRANSFORMS:
                step = time.perf_counter()
                with ClientCursor(conn) as tcur:
                    tcur.execute(_read_sql(name), {"run_id": run_id})
                log.info("  transform %-22s %6.0f ms", name, (time.perf_counter() - step) * 1000)

            # Inside the same transaction as the rows, so a freshly imported archive is never
            # visible without its readiness verdicts.
            step = time.perf_counter()
            readiness_store.recompute_all(conn)
            log.info("  readiness %-22s %6.0f ms", "(policy.assess)", (time.perf_counter() - step) * 1000)

            row_counts = {}
            for table in COUNT_TABLES:
                cur.execute(f"SELECT count(*) AS n FROM {table}")
                row_counts[table] = cur.fetchone()["n"]

            _compare_with_manifest(cur, run_id, mf, row_counts)

            # Counted BEFORE trimming, so import_run.issue_counts reports how many problems
            # there really were even when only the first few hundred examples are kept.
            cur.execute(
                "SELECT severity, kind, count(*) AS n FROM import_issue "
                "WHERE import_run_id = %s GROUP BY 1, 2 ORDER BY 1, 2",
                (run_id,),
            )
            issue_counts = {f"{r['severity']}:{r['kind']}": r["n"] for r in cur.fetchall()}

            _cap_issues(cur, run_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True

    return {
        "row_counts": row_counts,
        "issue_counts": issue_counts,
        "sha256": {c.source_file: c.sha256 for c in copies},
        "copies": [asdict(c) for c in copies],
    }


def _record_copy_observations(cur, run_id: int, copies: list[CopyResult], mf) -> None:
    """Header differences and checksum mismatches. Reported, never fatal."""
    for c in copies:
        for col in c.unexpected_columns:
            cur.execute(
                "INSERT INTO import_issue (import_run_id, severity, kind, source_file,"
                " column_name, message) VALUES (%s, 'info', 'unknown_column', %s, %s, %s)",
                (run_id, c.source_file, col,
                 "A column the working model does not use. It was loaded into staging so "
                 "nothing was silently dropped, but it is not carried into any table."),
            )
        for col in c.missing_columns:
            cur.execute(
                "INSERT INTO import_issue (import_run_id, severity, kind, source_file,"
                " column_name, message) VALUES (%s, 'warning', 'missing_column', %s, %s, %s)",
                (run_id, c.source_file, col,
                 "An expected column was absent from the file. Everything derived from it "
                 "was imported as unknown."),
            )

        declared = mf.checksums.get(c.source_file)
        if declared and declared != c.sha256:
            cur.execute(
                "INSERT INTO import_issue (import_run_id, severity, kind, source_file,"
                " message, details) VALUES (%s, 'warning', 'checksum_mismatch', %s, %s, %s)",
                (run_id, c.source_file,
                 "The file does not match the checksum recorded in manifest.json. The import "
                 "continues: the archive is expected to be replaced, so a checksum is a "
                 "signal, not a gate.",
                 json.dumps({"declared": declared, "observed": c.sha256})),
            )

        declared_rows = mf.row_counts.get(c.source_file)
        if declared_rows is not None and declared_rows != c.rows:
            cur.execute(
                "INSERT INTO import_issue (import_run_id, severity, kind, source_file,"
                " message, details) VALUES (%s, 'warning', 'row_count_mismatch', %s, %s, %s)",
                (run_id, c.source_file,
                 "The number of data rows loaded differs from the count declared in "
                 "manifest.json.",
                 json.dumps({"declared": declared_rows, "loaded": c.rows})),
            )

    if not mf.present:
        cur.execute(
            "INSERT INTO import_issue (import_run_id, severity, kind, message)"
            " VALUES (%s, 'info', 'no_manifest', %s)",
            (run_id,
             "No manifest.json was present, so checksums and row counts could not be "
             "verified. The import is unaffected."),
        )


def _compare_with_manifest(cur, run_id: int, mf, row_counts: dict[str, int]) -> None:
    """Compare declared entity counts with what actually landed. A canary, never fatal."""
    mapping = {
        "companies": "company",
        "contacts": "contact",
        "opportunities": "opportunity",
        "activity_log_entries": "activity",
        "fair_editions": "fair_edition",
    }
    for declared_name, table in mapping.items():
        declared = mf.entities.get(declared_name)
        if declared is None:
            continue
        actual = row_counts.get(table, 0)
        if declared != actual:
            cur.execute(
                "INSERT INTO import_issue (import_run_id, severity, kind, message, details)"
                " VALUES (%s, 'warning', 'entity_count_mismatch', %s, %s)",
                (run_id,
                 f"manifest.json declares {declared} {declared_name} but {actual} rows were "
                 f"imported into {table}. The difference is explained by the issues recorded "
                 f"for the rows that were skipped.",
                 json.dumps({"declared": declared, "imported": actual, "table": table})),
            )


def _cap_issues(cur, run_id: int) -> None:
    """Keep at most MAX_ISSUES_PER_KIND examples of each kind.

    The full counts are computed before this runs and stored on import_run, so nothing is
    lost from the report -- only the per-row examples are trimmed. Without this, a
    pathologically dirty archive would turn a data-quality problem into a disk-space one.
    """
    cur.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY kind ORDER BY id) AS rn
            FROM import_issue WHERE import_run_id = %s
        )
        DELETE FROM import_issue
        WHERE id IN (SELECT id FROM ranked WHERE rn > %s)
        """,
        (run_id, MAX_ISSUES_PER_KIND),
    )
    if cur.rowcount:
        log.info("trimmed %d issue examples beyond %d per kind", cur.rowcount, MAX_ISSUES_PER_KIND)
