"""Shared helpers for the repository layer."""

from __future__ import annotations

import re

_NUMERIC = re.compile(r"^\d+$")


def ref_clause(table_alias: str) -> str:
    """SQL fragment matching a record by surrogate id OR by legacy code.

    One route parameter accepts both, so `/opportunities/OP000003` works for a human reading
    the archive and `/opportunities/15001` works for a record the application created, which
    has no legacy code at all. The caller passes the same value twice.

    The id comparison is guarded by a regex on the Python side (`is_numeric`), so a code
    like 'OP000003' never reaches an integer cast.
    """
    return f"({table_alias}.id = %(ref_id)s OR {table_alias}.legacy_code = %(ref_code)s)"


def ref_params(ref: str) -> dict[str, object]:
    return {
        "ref_id": int(ref) if _NUMERIC.match(ref or "") else None,
        "ref_code": (ref or "").strip(),
    }


def is_numeric(ref: str) -> bool:
    return bool(_NUMERIC.match(ref or ""))
