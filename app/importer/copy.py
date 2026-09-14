"""Streaming COPY of the source CSVs into text staging tables.

Why COPY rather than a Python loop over `csv.reader`:

  * **The quoted-delimiter trap.** 2,654 of the 40,000 rows in activity_log.csv carry a
    semicolon INSIDE a quoted `details` field -- for example
    `"Confirmed the plot for the 2027 launch stand; do not reuse the summer order value"`.
    Anything that splits on `;` shifts every later column on those rows, silently, with no
    error: `completion_marker` simply starts holding dates. Postgres COPY with
    `FORMAT csv, DELIMITER ';'` is a real RFC-4180 reader and handles it natively.
  * **Speed.** One streamed COPY per file beats 75,000 individual INSERTs by an order of
    magnitude, and the transforms afterwards are set-based.
  * **Reviewability.** The transformation logic ends up as readable SQL rather than buried
    in a loop.

The file is streamed in fixed-size chunks, so memory stays flat regardless of archive size,
and the SHA-256 is computed over the same chunks rather than by reading the file twice.

COPY ... FROM STDIN sends the bytes from the application, which means the database container
never needs access to the host filesystem -- no server-side COPY, no superuser file
permissions, and no Docker bind-mount surprises on the database side.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from psycopg import Connection, sql

log = logging.getLogger("crm.import.copy")

CHUNK = 64 * 1024
DELIMITER = ";"


@dataclass
class CopyResult:
    table: str
    source_file: str
    rows: int
    sha256: str
    header: list[str]
    # Columns present in the file but not in the staging table. Loaded into columns added on
    # the fly so nothing is lost, then reported.
    unexpected_columns: list[str] = field(default_factory=list)
    # Columns the staging table expects but the file did not supply. They stay NULL and the
    # transforms COALESCE around them.
    missing_columns: list[str] = field(default_factory=list)


def _read_header(path: Path) -> tuple[list[str], int]:
    """Return the parsed header and the byte offset where the data rows begin.

    Read as bytes and split on the first newline so the offset is exact regardless of
    encoding width. The header itself is simple enough not to need a CSV parser -- but the
    DATA is not, which is why only this line is handled by hand.
    """
    with path.open("rb") as fh:
        first = fh.readline()
    offset = len(first)
    text = first.decode("utf-8-sig").rstrip("\r\n")
    names = [c.strip().strip('"') for c in text.split(DELIMITER)]
    return names, offset


def _staging_columns(conn: Connection, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        return [r["column_name"] for r in cur.fetchall()]


def copy_file(conn: Connection, path: Path, table: str) -> CopyResult:
    """Stream one CSV into its staging table, returning what was loaded and what differed.

    The header drives the COPY column list rather than the staging table definition. That is
    the graceful-degradation seam for a replaced archive: a reordered file still lands in the
    right columns, an extra column is added to the staging table rather than rejected, and a
    missing column simply stays NULL.
    """
    header, data_offset = _read_header(path)
    expected = _staging_columns(conn, table)

    unexpected = [c for c in header if c and c not in expected]
    missing = [c for c in expected if c not in header]

    if unexpected:
        # Add them as text so the COPY column list can name them. Nothing downstream reads
        # these, but loading them means the issue report can show real values rather than
        # just a column name.
        with conn.cursor() as cur:
            for col in unexpected:
                cur.execute(
                    sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} text").format(
                        sql.Identifier(table), sql.Identifier(col)
                    )
                )
        log.warning("%s: unexpected columns %s (loaded, unused)", path.name, unexpected)

    if missing:
        log.warning("%s: expected columns absent %s (left NULL)", path.name, missing)

    # Blank header cells would produce an unnamed column; drop them rather than guess.
    copy_columns = [c for c in header if c]

    statement = sql.SQL(
        "COPY {} ({}) FROM STDIN WITH (FORMAT csv, DELIMITER {}, HEADER false, ENCODING 'UTF8')"
    ).format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in copy_columns),
        sql.Literal(DELIMITER),
    )

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        # Hash the header too, so the checksum matches the file as published in
        # manifest.json rather than only its body.
        digest.update(fh.read(data_offset))
        with conn.cursor() as cur, cur.copy(statement) as cp:
            while chunk := fh.read(CHUNK):
                digest.update(chunk)
                cp.write(chunk)

    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table)))
        rows = cur.fetchone()["n"]

    log.info("copied %-28s -> %-24s %7d rows", path.name, table, rows)
    return CopyResult(
        table=table,
        source_file=path.name,
        rows=rows,
        sha256=digest.hexdigest(),
        header=header,
        unexpected_columns=unexpected,
        missing_columns=missing,
    )
