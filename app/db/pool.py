"""The connection pool.

Synchronous psycopg3, not async. FastAPI runs `def` endpoint handlers in a threadpool, so a
synchronous driver is not a bottleneck for a single-user tool -- and it removes the entire
class of bug where a blocking call silently stalls the event loop. It also makes the chunked
COPY importer natural to write, since psycopg copy blocks are synchronous.

`/healthz` deliberately never touches this pool: a liveness probe that fails when the
database blinks would restart a perfectly healthy application.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("crm.db")

_pool: ConnectionPool | None = None


def open_pool(dsn: str, *, min_size: int = 2, max_size: int = 10) -> ConnectionPool:
    """Create the process-wide pool.

    `open=False` then an explicit `wait()` so a database that is not yet accepting
    connections produces one clear error at startup, rather than a confusing failure on the
    first request. Compose already gates the app on the database healthcheck, so the wait is
    a backstop, not the primary mechanism.
    """
    global _pool
    if _pool is not None:
        return _pool

    _pool = ConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={
            "row_factory": dict_row,
            # statement_timeout: a query that somehow escapes its index should fail visibly
            # rather than wedge the application in front of a reviewer.
            #
            # timezone: the database container runs in UTC, so without this `current_date`
            # would be yesterday for half an hour after midnight in Rome, and a follow-up
            # due "today" would show as due tomorrow. The sales team and every fair are in
            # Italy, and the archive's own timestamps are Europe/Rome, so that is the zone
            # "today" is measured in.
            "options": "-c statement_timeout=5000 -c timezone=Europe/Rome",
        },
    )
    _pool.open(wait=True, timeout=30)
    log.info("connection pool open (min=%d max=%d)", min_size, max_size)
    return _pool


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool not open; call open_pool() during startup")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[Connection]:
    """Borrow a connection. Commits on clean exit, rolls back on exception."""
    with get_pool().connection() as conn:
        yield conn
