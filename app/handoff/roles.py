"""The three roles: preparer, checker, coordinator.

Plain functions over a shared state object, as the brief allows -- "ordinary functions in one
process are fine". No agent framework. Each role has one job and a clear boundary:

  preparer     gathers facts through tools and PROPOSES a next step. It speaks for sales.
               It never decides readiness.
  checker      rules on the facts with `policy.assess` and compares that ruling with the
               proposal. It speaks for the technical team. It re-implements no rule: every
               judgement it makes comes from the policy module.
  coordinator  decides whether to send the draft back or stop, and why. Pure, and it makes
               no model call at all -- choosing between "revise" and "stop" from a ruling
               that is already made needs no language model, and pretending otherwise would
               be theatre.

The split mirrors a pattern worth naming: a component that PROPOSES (the model, which may be
wrong) and a component that VERIFIES against deterministic rules (the policy). The proposer
can be swapped for a real model without touching the verifier, and the verifier's
correctness does not depend on the proposer being well-behaved.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import Connection

from app.handoff import tools
from app.handoff.model_client import (
    FIELD_QUESTIONS,
    HAND_OVER,
    HAND_OVER_PROVISIONAL,
    HOLD,
    RESOLVE_CONFLICT,
    ModelClient,
    ModelRequest,
)
from app.handoff.policy import (
    FIELD_LABELS,
    PolicyInput,
    Readiness,
    ReadinessVerdict,
    assess,
    is_actionable,
)

# A preparer that keeps asking for tools must still stop. Three rounds cover the dependency
# chain here (opportunity -> edition -> height check); a fourth would only ever be a model
# asking for something it already has.
MAX_TOOL_ROUNDS = 4
MAX_CALLS_PER_ROUND = 8   # the stand-in never asks for more than five in a whole run

# What the checker expects the preparer to have proposed, for each readiness state.
EXPECTED_PROPOSAL = {
    Readiness.READY: HAND_OVER,
    Readiness.PROVISIONAL: HAND_OVER_PROVISIONAL,
    Readiness.INCOMPLETE: HOLD,
    Readiness.BLOCKED: RESOLVE_CONFLICT,
}

DECISION_FOR = {
    Readiness.READY: "handoff",
    Readiness.PROVISIONAL: "handoff_provisional",
    Readiness.INCOMPLETE: "hold",
    Readiness.BLOCKED: "blocked",
}


@dataclass
class StepRecord:
    seq: int
    iteration: int
    role: str
    phase: str
    request: dict | None
    output_text: str
    output_data: dict
    findings: list[dict]
    duration_ms: int


@dataclass
class HandoffState:
    opportunity_ref: str
    today: date
    brief_override: str | None = None
    observed: dict[str, Any] = field(default_factory=dict)
    iteration: int = 0
    draft_text: str = ""
    draft: dict = field(default_factory=dict)
    verdict: ReadinessVerdict | None = None
    requires_revision: bool = False
    feedback: list[dict] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    tool_calls: list[tuple[int, tools.ToolCallRecord]] = field(default_factory=list)

    def next_seq(self) -> int:
        return len(self.steps) + 1

    def record(self, **kwargs) -> StepRecord:
        step = StepRecord(seq=self.next_seq(), iteration=self.iteration, **kwargs)
        self.steps.append(step)
        return step


@dataclass(frozen=True)
class Decision:
    action: str          # 'revise' | 'stop'
    decision: str        # handoff | handoff_provisional | hold | blocked | edition_finished
    reason: str


# ---------------------------------------------------------------------------
# preparer


def tool_scope(state: HandoffState) -> dict[str, list[str]]:
    """What "this run" means to each scoped tool argument. See tools.apply_scope.

    The opportunity is known from the start, by whatever reference the run was started
    with; once it has been read, its legacy code becomes the canonical value and its numeric
    id an alias. The edition is only known once the opportunity has named one -- so before
    that point, no edition tool can be called, and an opportunity with no edition has no
    edition to look up.
    """
    opp = state.observed.get("get_opportunity") or {}
    opportunity = [state.opportunity_ref]
    if opp.get("opportunity_code"):
        opportunity.insert(0, str(opp["opportunity_code"]))
    if opp.get("opportunity_id") is not None:
        opportunity.append(str(opp["opportunity_id"]))
    scope = {"opportunity_code": list(dict.fromkeys(opportunity))}
    if opp.get("fair_edition_code"):
        scope["fair_edition_code"] = [str(opp["fair_edition_code"])]
    return scope


def _as_dict(data: Any) -> dict:
    """A model's structured output is only usable if it is an object; anything else is noted."""
    return data if isinstance(data, dict) else {"malformed": repr(data)[:200]}


def _execute_plan(state: HandoffState, conn: Connection, step_seq: int, plan: dict,
                  tool_seq: int) -> int:
    """Run one round of tool calls from a plan the model returned. Returns the next seq.

    Nothing about the plan is trusted: `tool_calls` may not be a list, an entry may not be
    an object with a string `tool` and an object `arguments`, and there may be far too many.
    Each of those becomes a refused call in the trace, so a reviewer can see what the model
    asked for and why it did not happen, and the round carries on with what it has.
    """
    calls = plan.get("tool_calls") or []
    if not isinstance(calls, list):
        calls = [calls]   # a single non-list value: one entry, refused below as malformed
    scope = tool_scope(state)
    for n, call in enumerate(calls):
        tool_seq += 1
        if n >= MAX_CALLS_PER_ROUND:
            rec = tools.refused(tool_seq, "(limit)", {},
                                f"{len(calls) - MAX_CALLS_PER_ROUND} further call(s) not executed: "
                                f"at most {MAX_CALLS_PER_ROUND} per round")
            state.tool_calls.append((step_seq, rec))
            break
        name = call.get("tool") if isinstance(call, dict) else None
        arguments = (call.get("arguments") or {}) if isinstance(call, dict) else None
        if not isinstance(name, str) or not isinstance(arguments, dict):
            rec = tools.refused(tool_seq, "(malformed)", {"received": repr(call)[:200]},
                                'malformed tool call: expected {"tool": name, "arguments": {...}}')
        else:
            rec = tools.execute(conn, name, arguments, tool_seq, scope=scope)
        state.tool_calls.append((step_seq, rec))
        if rec.ok:
            state.observed[rec.tool_name] = rec.result
            scope = tool_scope(state)   # reading the opportunity is what makes the edition callable
    return tool_seq


def preparer(state: HandoffState, model: ModelClient, conn: Connection) -> None:
    """Gather facts through tools (first pass only), then draft the brief."""
    if state.iteration == 0:
        tool_seq = 0
        for _ in range(MAX_TOOL_ROUNDS):
            request = ModelRequest(
                role="preparer", phase="plan",
                instructions="Decide which tools to call next to prepare a technical brief. "
                             "Return an empty list when you have what you need.",
                context={"opportunity_code": state.opportunity_ref,
                         "today": state.today.isoformat(),
                         "observed": state.observed},
            )
            started = time.perf_counter()
            response = model.complete(request)
            plan = _as_dict(response.data)
            step = state.record(
                role="preparer", phase="plan", request=_request_summary(request),
                output_text=response.text, output_data=plan, findings=[],
                duration_ms=_ms(started),
            )
            if plan.get("done"):
                break
            tool_seq = _execute_plan(state, conn, step.seq, plan, tool_seq)

    opp = state.observed.get("get_opportunity") or {}
    request = ModelRequest(
        role="preparer", phase="draft",
        instructions="Write a technical brief from the observed facts and propose a next step.",
        context={
            "observed": state.observed,
            "feedback": state.feedback,
            "brief_notes": state.brief_override if state.brief_override is not None else opp.get("brief_notes"),
            "iteration": state.iteration,
        },
    )
    started = time.perf_counter()
    response = model.complete(request)
    state.draft_text = response.text if isinstance(response.text, str) else ""
    state.draft = _as_dict(response.data)
    state.record(
        role="preparer", phase="draft",
        request={"feedback_codes": [f.get("code") for f in state.feedback],
                 "brief_source": "edited" if state.brief_override is not None else "opportunity"},
        output_text=state.draft_text, output_data=state.draft, findings=[],
        duration_ms=_ms(started),
    )


# ---------------------------------------------------------------------------
# checker


def policy_input_from(observed: dict[str, Any]) -> PolicyInput:
    """Build the policy's input from what the TOOLS returned -- not from a fresh query.

    The checker rules on exactly the facts the preparer saw. If it re-read the database it
    could rule on different data from the brief it is checking, and a stored run would no
    longer be explainable from its own record.
    """
    opp = observed.get("get_opportunity") or {}
    ed = observed.get("get_fair_edition") or {}

    def dec(v):
        return None if v in (None, "") else Decimal(str(v))

    return PolicyInput(
        has_fair_edition=bool(ed),
        max_stand_height_m=dec(ed.get("max_stand_height_m")),
        edition_ends_on=date.fromisoformat(ed["ends_on"]) if ed.get("ends_on") else None,
        client_budget_eur=dec(opp.get("client_budget_eur")),
        stand_area_sqm=dec(opp.get("stand_area_sqm")),
        requested_height_m=dec(opp.get("requested_height_m")),
    )


def checker(state: HandoffState, model: ModelClient) -> None:
    verdict = assess(policy_input_from(state.observed))
    state.verdict = verdict

    expected = EXPECTED_PROPOSAL[verdict.readiness]
    proposal = state.draft.get("proposal")
    proposal = proposal if isinstance(proposal, str) else None
    agrees = proposal == expected
    questions = state.draft.get("open_questions")
    asked = {q for q in questions if isinstance(q, str)} if isinstance(questions, list) else set()
    unasked = [m for m in verdict.missing if FIELD_QUESTIONS.get(m) not in asked]
    state.requires_revision = (not agrees) or bool(unasked)
    state.feedback = [asdict(f) for f in verdict.findings]

    request = ModelRequest(
        role="checker", phase="review",
        instructions="Explain whether the draft matches the policy ruling, and why.",
        context={"verdict": verdict.to_json(), "agrees": agrees, "expected_proposal": expected,
                 "proposal": proposal, "unasked_missing": unasked},
    )
    started = time.perf_counter()
    response = model.complete(request)
    state.record(
        role="checker", phase="review", request={"proposal": proposal, "expected": expected},
        output_text=response.text,
        output_data={"readiness": verdict.readiness.value, "agrees": agrees,
                     "unasked_missing": unasked, "requires_revision": state.requires_revision},
        findings=state.feedback, duration_ms=_ms(started),
    )


# ---------------------------------------------------------------------------
# coordinator


def coordinator(state: HandoffState, *, max_iterations: int) -> Decision:
    """Revise or stop. Pure: no model, no clock, no database.

    Precedence, first match wins:
      1. the fair edition has already finished -> stop. Complete information about a stand
         for a fair that is over is not a handoff; it is history.
      2. the checker objected and a revision is still allowed -> revise once.
      3. otherwise stop, with the decision the readiness state calls for.
    """
    verdict = state.verdict
    assert verdict is not None, "coordinator runs after the checker"

    ends_on = policy_input_from(state.observed).edition_ends_on
    if not is_actionable(ends_on, state.today):
        return Decision("stop", "edition_finished",
                        f"The fair edition ended on {ends_on.strftime('%d/%m/%Y')}, before today "
                        f"({state.today.strftime('%d/%m/%Y')}). There is nothing left to build; "
                        "the record is history.")

    last_iteration = state.iteration >= max_iterations - 1
    if state.requires_revision and not last_iteration:
        return Decision("revise", DECISION_FOR[verdict.readiness],
                        "The checker disagreed with the draft, so it goes back to the preparer "
                        "once with the checker's findings.")

    decision = DECISION_FOR[verdict.readiness]
    reasons = {
        "handoff": "Fair edition, customer budget, stand area and requested height are all "
                   "known, and the height is within the edition limit. Technical can start.",
        "handoff_provisional": "The fair and the customer budget are known, which is enough to "
                               "involve technical now -- but "
                               + (_list(verdict.missing) if verdict.missing else "the edition height limit")
                               + " " + ("is" if len(verdict.missing) <= 1 else "are")
                               + " still open, so the brief goes over as provisional and no build "
                                 "work should start until "
                               + ("it is" if len(verdict.missing) <= 1 else "they are")
                               + " confirmed.",
        "hold": "Not handed over: " + _list(verdict.missing) + " "
                + ("is" if len(verdict.missing) == 1 else "are")
                + " missing, and without them even the sales director's threshold is not met.",
        "blocked": "Not handed over: " + "; ".join(c.message for c in verdict.conflicts)
                   + " The conflict has to be resolved with the customer first.",
    }
    reason = reasons[decision]
    if state.requires_revision and last_iteration:
        reason += " (The revision limit was reached with the checker still objecting; the policy ruling stands.)"
    return Decision("stop", decision, reason)


# ---------------------------------------------------------------------------


def _list(fields: tuple[str, ...] | list[str]) -> str:
    names = [FIELD_LABELS.get(f, f.replace("_", " ")) for f in fields]
    if len(names) <= 1:
        return "".join(names) or "nothing"
    return ", ".join(names[:-1]) + " and " + names[-1]


def _request_summary(request: ModelRequest) -> dict:
    """What the role was given, minus the bulky tool results already stored per call."""
    return {"role": request.role, "phase": request.phase,
            "already_observed": sorted(request.context.get("observed", {}).keys())}


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
