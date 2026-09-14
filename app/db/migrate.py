"""Apply numbered .sql migrations at startup.

Not Alembic. Alembic exists mainly to autogenerate migrations by diffing ORM models against
the database; there are no ORM models here, so adopting it would mean pulling in SQLAlchemy
purely to get a version table. Raw .sql files are also directly readable by a reviewer
without tracing Python, which matters when the schema comments carry the design reasoning.

Three properties this needs to have, and the reasons they are not optional:

  * **Run once.** Two application instances, or a restart during startup, must not apply the
    same file twice. An advisory lock serialises them and the ledger makes re-application a
    no-op.
  * **Fail loudly on drift.** If a file that has already been applied changes on disk, stop.
    A schema that no longer matches its own definition is worse than a crash, because every
    later assumption is quietly wrong.
  * **Be fast.** This runs BEFORE the port binds, and verify.sh allows roughly ten seconds
    from container start. Applying six files takes well under a second; the archive import
    is what runs afterwards, on a background thread.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from psycopg import Connection

log = logging.getLogger("crm.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Arbitrary but fixed. The importer uses a different one (8140251) so the two never contend.
MIGRATION_LOCK_ID = 8140250

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version     text PRIMARY KEY,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    duration_ms int
)
"""


class MigrationDrift(RuntimeError):
    """An already-applied migration file has changed on disk."""


def _checksum(text: str) -> str:
    # Normalise line endings so a Windows checkout and a Linux container agree. Without
    # this, every migration would look modified inside the image.
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """Migration files in lexical order, which the NNN_ prefix makes numeric order."""
    return sorted(p for p in directory.glob("*.sql") if p.is_file())


def apply_all(conn: Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every unapplied migration. Returns the versions applied this call."""
    files = discover(directory)
    if not files:
        log.warning("no migration files found in %s", directory)
        return []

    with conn.cursor() as cur:
        cur.execute(_LEDGER_DDL)
        conn.commit()

        # Held for the whole run and released on commit of the final transaction. A second
        # instance blocks here rather than racing, then finds the ledger already populated.
        cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        try:
            cur.execute("SELECT version, checksum FROM schema_migration")
            applied = {r["version"]: r["checksum"] for r in cur.fetchall()}

            done: list[str] = []
            for path in files:
                version = path.stem
                sql = path.read_text(encoding="utf-8")
                digest = _checksum(sql)

                if version in applied:
                    if applied[version] != digest:
                        raise MigrationDrift(
                            f"migration {version} has changed since it was applied "
                            f"(recorded {applied[version][:12]}, file {digest[:12]}). "
                            f"Roll the change into a NEW migration file, or run ./reset.sh "
                            f"to rebuild the database from scratch."
                        )
                    continue

                started = time.perf_counter()
                # Each file in its own transaction: a failure leaves every earlier file
                # applied and recorded, so a fix plus a restart resumes rather than redoing.
                with conn.transaction():
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migration (version, checksum, duration_ms) "
                        "VALUES (%s, %s, %s)",
                        (version, digest, int((time.perf_counter() - started) * 1000)),
                    )
                elapsed = (time.perf_counter() - started) * 1000
                log.info("applied migration %s (%.0f ms)", version, elapsed)
                done.append(version)

            if not done:
                log.info("schema up to date (%d migrations already applied)", len(applied))
            return done
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))
            conn.commit()
