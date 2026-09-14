"""Running the handoff assistant: preparer, checker, coordinator, repeat at most once.

    for iteration in 0, 1:
        preparer   -- tools on the first pass, then a draft
        checker    -- policy ruling, compared with the draft
        coordinator -- revise, or stop with a decision and a reason

The bound is structural. The loop is a `for` over `range(MAX_ITERATIONS)`, and the
coordinator treats the last iteration as final, so a checker that objects forever still
produces a decision after two passes. There is no "while not satisfied" anywhere -- an
agent loop whose termination depends on the model agreeing with itself is a loop that can
run forever on the day the model does not.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from psycopg import Connection

from app.handoff import roles
from app.handoff.model_client import ModelClient
from app.handoff.policy import POLICY_VERSION

log = logging.getLogger("crm.handoff")

MAX_ITERATIONS = 2


@dataclass
class RunResult:
    opportunity_ref: str
    today: date
    brief_override: str | None
    model_name: str
    model_version: str
    model_is_stub: bool
    policy_version: str
    readiness: str
    decision: str
    decision_reason: str
    final_brief: str
    iterations: int
    observed: dict
    steps: list[roles.StepRecord] = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    duration_ms: int = 0

    @property
    def inputs(self) -> dict:
        """The snapshot stored on the run: everything the roles saw, as it was at run time.

        The run page renders from this and never re-queries the opportunity, so editing the
        opportunity afterwards cannot rewrite what an old run shows it was working from.
        """
        return {
            "opportunity_ref": self.opportunity_ref,
            "today": self.today.isoformat(),
            "brief_override": self.brief_override,
            "observed": self.observed,
            "policy_version": self.policy_version,
            "model": {"name": self.model_name, "version": self.model_version,
                      "is_stub": self.model_is_stub},
        }


def run_handoff(
    conn: Connection,
    opportunity_ref: str,
    model: ModelClient,
    *,
    today: date,
    brief_override: str | None = None,
    max_iterations: int = MAX_ITERATIONS,
) -> RunResult:
    """Run the assistant for one opportunity. Reads the database; writes nothing.

    `model` is passed in rather than imported, which is the dependency-injection seam: the
    application passes the deterministic stand-in, tests pass a recording double, and a real
    provider would be passed the same way. `today` is passed in too, so a run is a function
    of its arguments and a test can pin the date.

    Persisting the result is a separate step (app/handoff/store.py), so running and saving
    can be tested independently.
    """
    started = time.perf_counter()
    state = roles.HandoffState(opportunity_ref=opportunity_ref, today=today,
                               brief_override=brief_override)
    decision: roles.Decision | None = None

    for iteration in range(max_iterations):
        state.iteration = iteration
        roles.preparer(state, model, conn)

        if "get_opportunity" not in state.observed:
            decision = roles.Decision("stop", "hold",
                                      f"The opportunity {opportunity_ref!r} could not be read, "
                                      "so no brief could be prepared.")
            state.record(role="coordinator", phase="decide", request=None,
                         output_text=decision.reason,
                         output_data={"action": decision.action, "decision": decision.decision},
                         findings=[], duration_ms=0)
            break

        roles.checker(state, model)

        step_started = time.perf_counter()
        decision = roles.coordinator(state, max_iterations=max_iterations)
        state.record(
            role="coordinator", phase="decide",
            request={"readiness": state.verdict.readiness.value,
                     "requires_revision": state.requires_revision,
                     "iteration": iteration, "max_iterations": max_iterations},
            output_text=decision.reason,
            output_data={"action": decision.action, "decision": decision.decision},
            findings=[], duration_ms=int((time.perf_counter() - step_started) * 1000),
        )
        if decision.action == "stop":
            break

    assert decision is not None and decision.action == "stop", "the loop always ends on a stop"

    result = RunResult(
        opportunity_ref=opportunity_ref,
        today=today,
        brief_override=brief_override,
        model_name=model.name,
        model_version=model.version,
        model_is_stub=model.is_stub,
        policy_version=POLICY_VERSION,
        readiness=state.verdict.readiness.value if state.verdict else "incomplete",
        decision=decision.decision,
        decision_reason=decision.reason,
        final_brief=state.draft_text,
        iterations=state.iteration + 1,
        observed=state.observed,
        steps=state.steps,
        tool_calls=state.tool_calls,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    log.info("handoff %s: %s after %d iteration(s), %d steps, %d tool calls, %d ms",
             opportunity_ref, result.decision, result.iterations, len(result.steps),
             len(result.tool_calls), result.duration_ms)
    return result
