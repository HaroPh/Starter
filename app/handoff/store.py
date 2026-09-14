"""Saving and reading handoff runs.

A run is written once and never changed. "Run again" inserts a new row pointing at the
previous one, so the history of an opportunity's briefs is an append-only chain -- which is
what lets someone compare what the assistant said before and after the brief was edited.

The run, its steps and its tool calls go in one transaction: a half-saved run with a decision
but no trace would be worse than no run at all.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg import Connection

from app.handoff.orchestrator import RunResult


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def save_run(
    conn: Connection,
    result: RunResult,
    *,
    opportunity_id: int,
    previous_run_id: int | None = None,
) -> int:
    with conn.transaction(), conn.cursor() as cur:
        # Taking the next number under a row lock on the opportunity keeps two simultaneous
        # runs from both claiming the same run_no. The unique constraint would reject the
        # loser anyway; the lock means there is no loser.
        cur.execute("SELECT id FROM opportunity WHERE id = %s FOR UPDATE", (opportunity_id,))
        cur.execute(
            "SELECT coalesce(max(run_no), 0) + 1 AS n FROM handoff_run WHERE opportunity_id = %s",
            (opportunity_id,),
        )
        run_no = cur.fetchone()["n"]

        cur.execute(
            """
            INSERT INTO handoff_run (
                opportunity_id, run_no, previous_run_id, finished_at, status,
                model_kind, model_version, model_is_stub, policy_version, inputs,
                readiness, decision, decision_reason, final_brief, iterations,
                brief_source, edited_brief_notes)
            VALUES (%s, %s, %s, now(), 'completed', %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                opportunity_id, run_no, previous_run_id,
                result.model_name, result.model_version, result.model_is_stub,
                result.policy_version, _json(result.inputs),
                result.readiness, result.decision, result.decision_reason, result.final_brief,
                result.iterations,
                "edited" if result.brief_override is not None else "opportunity",
                result.brief_override,
            ),
        )
        run_id = cur.fetchone()["id"]

        step_ids: dict[int, int] = {}
        for s in result.steps:
            cur.execute(
                "INSERT INTO handoff_run_step (run_id, seq, iteration, role, phase, request,"
                " output_text, output_data, findings, duration_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (run_id, s.seq, s.iteration, s.role, s.phase, _json(s.request),
                 s.output_text, _json(s.output_data), _json(s.findings), s.duration_ms),
            )
            step_ids[s.seq] = cur.fetchone()["id"]

        for step_seq, call in result.tool_calls:
            cur.execute(
                "INSERT INTO handoff_tool_call (run_id, step_id, seq, tool_name, arguments,"
                " result, ok, error, duration_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (run_id, step_ids.get(step_seq), call.seq, call.tool_name, _json(call.arguments),
                 _json(call.result) if call.result is not None else None, call.ok, call.error,
                 call.duration_ms),
            )
    return run_id


def get_run(conn: Connection, run_id: int) -> dict[str, Any] | None:
    """A run with its steps and tool calls, read entirely from what was stored."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT hr.*, o.legacy_code AS opportunity_code, o.description AS opportunity_description,"
            "       c.name AS company_name, c.legacy_code AS company_code "
            "FROM handoff_run hr JOIN opportunity o ON o.id = hr.opportunity_id "
            "JOIN company c ON c.id = o.company_id WHERE hr.id = %s",
            (run_id,),
        )
        run = cur.fetchone()
        if run is None:
            return None
        cur.execute("SELECT * FROM handoff_run_step WHERE run_id = %s ORDER BY seq", (run_id,))
        steps = cur.fetchall()
        cur.execute("SELECT * FROM handoff_tool_call WHERE run_id = %s ORDER BY seq", (run_id,))
        calls = cur.fetchall()

    by_step: dict[int, list] = {}
    for c in calls:
        by_step.setdefault(c["step_id"], []).append(c)
    for s in steps:
        s["tool_calls"] = by_step.get(s["id"], [])
    run["steps"] = steps
    run["tool_call_count"] = len(calls)
    return run


def list_runs(conn: Connection, opportunity_id: int) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, run_no, previous_run_id, started_at, readiness, decision, iterations,"
            "       brief_source, model_kind, model_is_stub "
            "FROM handoff_run WHERE opportunity_id = %s ORDER BY run_no DESC",
            (opportunity_id,),
        )
        return cur.fetchall()


def run_as_json(run: dict[str, Any]) -> dict[str, Any]:
    """The API representation. Plain JSON types throughout."""
    def clean(v):
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v

    return {
        "id": run["id"],
        "opportunity_code": run["opportunity_code"],
        "run_no": run["run_no"],
        "previous_run_id": run["previous_run_id"],
        "started_at": clean(run["started_at"]),
        "model": {"kind": run["model_kind"], "version": run["model_version"],
                  "is_stub": run["model_is_stub"],
                  "note": "Deterministic local stand-in, not a language model. No network calls."},
        "policy_version": run["policy_version"],
        "readiness": run["readiness"],
        "decision": run["decision"],
        "decision_reason": run["decision_reason"],
        "iterations": run["iterations"],
        "brief_source": run["brief_source"],
        "final_brief": run["final_brief"],
        "inputs": run["inputs"],
        "steps": [
            {"seq": s["seq"], "iteration": s["iteration"], "role": s["role"], "phase": s["phase"],
             "output_text": s["output_text"], "output_data": s["output_data"],
             "findings": s["findings"], "duration_ms": s["duration_ms"],
             "tool_calls": [{"seq": c["seq"], "tool": c["tool_name"], "arguments": c["arguments"],
                             "result": c["result"], "ok": c["ok"], "error": c["error"],
                             "duration_ms": c["duration_ms"]} for c in s["tool_calls"]]}
            for s in run["steps"]
        ],
    }
