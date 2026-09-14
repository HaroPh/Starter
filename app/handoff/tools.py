"""The tools the handoff preparer can call.

This is what makes the assistant an agent rather than a templated pipeline. The preparer is
not handed a pre-assembled bundle of facts. It starts knowing only which opportunity it was
asked about, asks the model which tools to call, the runtime executes them, and the results
are fed back so the next round can decide what else it needs -- a fair edition can only be
looked up once the opportunity has named one, and a height can only be checked once both
the request and the limit are known.

Every call is recorded with its arguments, result and duration, so a run can be audited
step by step afterwards.

Each tool is a plain function taking a connection and a dict of arguments and returning a
dict of JSON-safe values: Decimals as strings, dates as ISO strings. That is not incidental.
The results are stored verbatim in the run record, and they are exactly what a real model
would receive as tool output -- so the stand-in and a real model see the same shapes.

EDITION SCOPING IS ENFORCED HERE TOO. `list_recent_activities` filters on the opportunity
and never on the company. An assistant that gathered "everything about this exhibitor"
would feed a 2027 brief the 2026 order value -- precisely the mistake the sales coordinator
complains about, and one that 4,995 company-and-fair pairs in the archive are exposed to.

THE MODEL CHOOSES WHETHER TO LOOK, NEVER WHERE. The SQL filter above only helps if the
model asks about the right opportunity, and a real model will sometimes ask about the
sibling "for context". So each tool declares which argument names its target (`scope_key`),
and `execute` fills that argument from the run: left blank, it is supplied; set to anything
else, the call is refused and the refusal is recorded. That was found with a deliberately
misbehaving model double (tests/unit/test_misbehaving_model.py): before the guard, one
stray call replaced the run's facts with last year's order.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from psycopg import Connection

from app.handoff.policy import height_within_limit
from app.repositories.common import ref_clause, ref_params


class ToolError(Exception):
    """A tool was called with arguments it cannot use."""


def _j(value: Any) -> Any:
    """JSON-safe conversion, applied to everything a tool returns."""
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _row(row: dict | None) -> dict | None:
    return None if row is None else {k: _j(v) for k, v in row.items()}


# ---------------------------------------------------------------------------
# The tools


def get_opportunity(conn: Connection, args: dict) -> dict:
    code = str(args.get("opportunity_code") or "").strip()
    if not code:
        raise ToolError("opportunity_code is required")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT o.legacy_code AS opportunity_code, o.id AS opportunity_id,
                   o.description, o.status, o.brief_notes,
                   o.amount_eur, o.client_budget_eur, o.stand_area_sqm, o.requested_height_m,
                   o.opened_on, o.expected_close_on,
                   c.legacy_code AS company_code, c.name AS company_name,
                   c.province_code, c.region, r.display_name AS account_manager,
                   ct.first_name AS contact_first_name, ct.last_name AS contact_last_name,
                   ct.email AS contact_email, ct.phone AS contact_phone,
                   fe.legacy_code AS fair_edition_code
            FROM opportunity o
            JOIN company c            ON c.id  = o.company_id
            LEFT JOIN sales_rep r     ON r.id  = c.sales_rep_id
            LEFT JOIN contact ct      ON ct.id = o.contact_id
            LEFT JOIN fair_edition fe ON fe.id = o.fair_edition_id
            WHERE {ref_clause('o')}
            """,
            ref_params(code),
        )
        row = cur.fetchone()
    if row is None:
        raise ToolError(f"no opportunity {code!r}")
    return _row(row)


def get_fair_edition(conn: Connection, args: dict) -> dict:
    code = str(args.get("fair_edition_code") or "").strip()
    if not code:
        raise ToolError("fair_edition_code is required")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fe.legacy_code AS fair_edition_code, f.name AS fair_name, fe.edition_label,"
            "       fe.city, fe.venue, fe.starts_on, fe.ends_on, fe.max_stand_height_m "
            "FROM fair_edition fe JOIN fair f ON f.id = fe.fair_id WHERE fe.legacy_code = %s",
            (code,),
        )
        row = cur.fetchone()
    if row is None:
        raise ToolError(f"no fair edition {code!r}")
    return _row(row)


def list_recent_activities(conn: Connection, args: dict) -> dict:
    """This opportunity's conversations only. See the module note on edition scoping."""
    code = str(args.get("opportunity_code") or "").strip()
    limit = max(1, min(int(args.get("limit") or 10), 25))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT a.activity_type AS type, a.occurred_at, a.details,
                   r.display_name AS author
            FROM activity a
            JOIN opportunity o     ON o.id = a.opportunity_id
            LEFT JOIN sales_rep r  ON r.id = a.author_rep_id
            WHERE {ref_clause('o')}
            ORDER BY a.occurred_at DESC, a.id DESC
            LIMIT %(limit)s
            """,
            {**ref_params(code), "limit": limit},
        )
        rows = [_row(r) for r in cur.fetchall()]
    return {"opportunity_code": code, "count": len(rows), "activities": rows}


def list_open_follow_ups(conn: Connection, args: dict) -> dict:
    code = str(args.get("opportunity_code") or "").strip()
    today = args.get("today")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT fu.due_on, fu.note, fu.origin
            FROM follow_up fu JOIN opportunity o ON o.id = fu.opportunity_id
            WHERE {ref_clause('o')} AND fu.status = 'pending'
            ORDER BY fu.due_on, fu.id
            """,
            ref_params(code),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        item = _row(r)
        # "today" arrives as an argument rather than being read from the clock here, so the
        # recorded result is reproducible from the recorded arguments.
        item["overdue"] = bool(today and r["due_on"] and r["due_on"].isoformat() < today)
        out.append(item)
    return {"opportunity_code": code, "count": len(out), "follow_ups": out}


def check_height_limit(conn: Connection, args: dict) -> dict:
    """Compare a requested height with an edition limit.

    The only tool that computes rather than reads. It delegates the comparison itself to
    `policy.height_within_limit`, so "is this height allowed" has exactly one definition in
    the codebase -- the tool reports a measurement for the brief, the policy rules on it, and
    they cannot disagree.
    """
    def dec(name: str) -> Decimal | None:
        raw = args.get(name)
        return None if raw in (None, "") else Decimal(str(raw))

    requested, limit = dec("requested_height_m"), dec("max_stand_height_m")
    ok = height_within_limit(requested, limit)
    return {
        "requested_height_m": _j(requested),
        "max_stand_height_m": _j(limit),
        "within_limit": ok,
        "margin_m": _j(limit - requested) if ok is not None else None,
    }


# ---------------------------------------------------------------------------
# Registry and execution


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    fn: Callable[[Connection, dict], dict]
    scope_key: str | None = None


REGISTRY: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool("get_opportunity", "The enquiry, its exhibitor, contact and fair edition code.", get_opportunity,
             scope_key="opportunity_code"),
        Tool("get_fair_edition", "Dates, venue and the maximum stand height for an edition.", get_fair_edition,
             scope_key="fair_edition_code"),
        Tool("list_recent_activities", "Conversations logged against THIS opportunity only.", list_recent_activities,
             scope_key="opportunity_code"),
        Tool("list_open_follow_ups", "Promises still outstanding on this opportunity.", list_open_follow_ups,
             scope_key="opportunity_code"),
        # Pure arithmetic over values the model passes. The checker never reads its result --
        # it rules from the edition itself (roles.policy_input_from) -- so there is nothing
        # here to scope.
        Tool("check_height_limit", "Whether a requested height fits an edition limit.", check_height_limit),
    )
}

SCOPE_LABELS = {"opportunity_code": "opportunity", "fair_edition_code": "fair edition"}


def apply_scope(tool: Tool, arguments: dict, scope: dict[str, list[str]]) -> dict:
    """Fill the tool's scoped argument from the run, or refuse the call.

    `scope` maps an argument name to the values that mean "this run": the first is the
    canonical one the tool receives, the rest are aliases a model might reasonably use (the
    numeric id, the reference the run was started with). Matching ignores case. A scoped
    tool whose scope is not known yet -- the edition before the opportunity has been read --
    cannot be called at all, which is the order the stand-in follows anyway.
    """
    if tool.scope_key is None:
        return arguments
    allowed = scope.get(tool.scope_key) or []
    label = SCOPE_LABELS.get(tool.scope_key, tool.scope_key)
    if not allowed:
        raise ToolError(f"refused: {tool.name} is scoped to the run's {label}, "
                        "which is not known yet -- read the opportunity first")
    asked = str(arguments.get(tool.scope_key) or "").strip()
    if asked and asked.upper() not in {a.upper() for a in allowed}:
        raise ToolError(f"refused: this run is scoped to {label} {allowed[0]!r}; "
                        f"the model asked for {asked!r}")
    return {**arguments, tool.scope_key: allowed[0]}


@dataclass
class ToolCallRecord:
    seq: int
    tool_name: str
    arguments: dict
    result: dict | None
    ok: bool
    error: str | None
    duration_ms: int


def refused(seq: int, tool_name: str, arguments: dict, error: str) -> ToolCallRecord:
    """A call the runtime would not execute, recorded like any other so the trace shows it."""
    return ToolCallRecord(seq, tool_name, dict(arguments), None, False, error, 0)


def execute(conn: Connection, name: str, arguments: dict, seq: int, *,
            scope: dict[str, list[str]] | None = None) -> ToolCallRecord:
    """Run one tool call and record it. Never raises: a failed call is a recorded result.

    The preparer then carries on with what it has. An unknown tool name, a bad argument or
    a call outside the run's scope is exactly what a real model produces sometimes, and a
    run that crashed on one would record nothing useful about why.
    """
    started = time.perf_counter()
    tool = REGISTRY.get(name)
    asked = dict(arguments)
    try:
        if tool is None:
            raise ToolError(f"unknown tool {name!r}")
        effective = apply_scope(tool, asked, scope or {})
        result = tool.fn(conn, effective)
        return ToolCallRecord(seq, name, effective, result, True, None,
                              int((time.perf_counter() - started) * 1000))
    except Exception as exc:  # noqa: BLE001 -- recorded, not propagated
        return ToolCallRecord(seq, name, asked, None, False, str(exc),
                              int((time.perf_counter() - started) * 1000))
