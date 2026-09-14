"""The model seam, and the deterministic stand-in that fills it.

The brief: "Use a deterministic local stand-in for model responses and label it as such. No
API keys, external model calls or model downloads."

So the orchestration talks to a `ModelClient` protocol and never to a concrete model. The
application wires in `DeterministicStubModel` once, at startup, through a factory; tests wire
in whatever they need. A real provider would be a second class implementing the same protocol
-- no role, orchestrator or storage code would change. That seam is the point of this module;
the stub is simply what is plugged into it here.

WHAT THE STUB DOES, HONESTLY
It is not a language model and does not pretend to be one. It is a set of explicit rules that
produce the SHAPES a model would produce -- a tool plan, a drafted brief, a written review --
from the facts it is given. Its output is a pure function of its input: no randomness, no
clock, no hidden state, dictionary keys iterated in sorted order. Run the same request twice
and the bytes are identical, which is what makes a stored run reproducible from its inputs.

Every run records `model_kind = "deterministic-stub"` and `model_is_stub = true`, and the
interface says so on the button and on the run page.

WHAT IT CANNOT DO
It cannot read prose. The archive's brief notes are passed into the brief verbatim and marked
as such, never "summarised" -- a rule-based stand-in summarising free text would be inventing
it. That is the most obvious place a real model would add value, and the protocol already
carries everything one would need.

ONE DELIBERATE BEHAVIOUR
On its first pass the preparer drafts from the SALES DIRECTOR'S rule: a named fair and a
stated budget are enough to propose handing over. It does not check dimensions or the height
limit -- that is the checker's job, and the checker speaks for the technical coordinator. So
on any enquiry that is not fully ready the checker finds something real to object to, the
coordinator sends the draft back once, and the revision loop is exercised rather than being
dead code. The trace of a run then reads as the dispute in the brief, played out: sales
proposes, technical objects, the policy decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.formatting import dmy, eur, metres, sqm
from app.handoff.policy import FIELD_LABELS

STUB_NAME = "deterministic-stub"
STUB_VERSION = "1"

# The proposals a preparer can make. The checker maps each readiness state to the one it
# expects, so these are the vocabulary the two roles argue in.
HAND_OVER = "hand_over"
HAND_OVER_PROVISIONAL = "hand_over_provisional"
HOLD = "hold"
RESOLVE_CONFLICT = "resolve_conflict_first"

FIELD_QUESTIONS = {
    "fair_edition": "Which fair edition is this stand for?",
    "client_budget_eur": "What budget has the customer stated for the stand?",
    "stand_area_sqm": "What floor area has the organiser allocated?",
    "requested_height_m": "What stand height does the customer want?",
}


@dataclass(frozen=True)
class ModelRequest:
    role: str                  # 'preparer' | 'checker'
    phase: str                 # 'plan' | 'draft' | 'review'
    instructions: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    data: dict[str, Any]
    model_name: str
    model_version: str
    is_stub: bool


class ModelClient(Protocol):
    name: str
    version: str
    is_stub: bool

    def complete(self, request: ModelRequest) -> ModelResponse: ...


class DeterministicStubModel:
    """Rule-based stand-in for a language model. See the module docstring."""

    name = STUB_NAME
    version = STUB_VERSION
    is_stub = True

    def complete(self, request: ModelRequest) -> ModelResponse:
        handler = {
            ("preparer", "plan"): self._plan,
            ("preparer", "draft"): self._draft,
            ("checker", "review"): self._review,
        }.get((request.role, request.phase))
        if handler is None:
            raise ValueError(f"stub has no behaviour for {request.role}/{request.phase}")
        text, data = handler(request.context)
        return ModelResponse(text, data, self.name, self.version, self.is_stub)

    # ------------------------------------------------------------------ preparer: plan

    def _plan(self, ctx: dict) -> tuple[str, dict]:
        """Choose the next tool calls from what has been observed so far.

        Each round can only ask for what the previous round made possible: the edition code
        comes from the opportunity, the height limit from the edition. Skips are explained,
        because "I did not check the height because none was requested" is as informative as
        a check.
        """
        code = ctx["opportunity_code"]
        seen = ctx.get("observed", {})
        calls: list[dict] = []
        notes: list[str] = []

        if "get_opportunity" not in seen:
            calls.append({"tool": "get_opportunity", "arguments": {"opportunity_code": code}})
            notes.append("Start from the opportunity itself.")
            return self._plan_result(calls, notes)

        opp = seen["get_opportunity"] or {}
        edition_code = opp.get("fair_edition_code")

        if edition_code and "get_fair_edition" not in seen:
            calls.append({"tool": "get_fair_edition", "arguments": {"fair_edition_code": edition_code}})
            notes.append(f"Look up {edition_code} for its dates and height limit.")
        elif not edition_code and "get_fair_edition" not in seen:
            notes.append("No fair edition is recorded, so there is no edition to look up.")

        if "list_recent_activities" not in seen:
            calls.append({"tool": "list_recent_activities",
                          "arguments": {"opportunity_code": code, "limit": 5}})
            notes.append("Read this opportunity's recent conversations -- this one only, "
                         "not the exhibitor's other editions.")

        if "list_open_follow_ups" not in seen:
            calls.append({"tool": "list_open_follow_ups",
                          "arguments": {"opportunity_code": code, "today": ctx.get("today")}})
            notes.append("See what the customer has already promised.")

        edition = seen.get("get_fair_edition") or {}
        if "check_height_limit" not in seen and "get_fair_edition" in seen:
            if opp.get("requested_height_m") and edition.get("max_stand_height_m"):
                calls.append({"tool": "check_height_limit", "arguments": {
                    "requested_height_m": opp["requested_height_m"],
                    "max_stand_height_m": edition["max_stand_height_m"],
                }})
                notes.append("Compare the requested height with the edition limit.")
            elif not opp.get("requested_height_m"):
                notes.append("No stand height has been requested yet, so there is nothing to check against the limit.")
            else:
                notes.append("The edition has no recorded height limit, so the height cannot be checked.")

        return self._plan_result(calls, notes)

    @staticmethod
    def _plan_result(calls: list[dict], notes: list[str]) -> tuple[str, dict]:
        text = " ".join(notes) if notes else "Nothing further to look up."
        return text, {"tool_calls": calls, "done": not calls}

    # ------------------------------------------------------------------ preparer: draft

    def _draft(self, ctx: dict) -> tuple[str, dict]:
        seen = ctx.get("observed", {})
        opp = seen.get("get_opportunity") or {}
        edition = seen.get("get_fair_edition") or {}
        height = seen.get("check_height_limit") or {}
        activities = (seen.get("list_recent_activities") or {}).get("activities", [])
        follow_ups = (seen.get("list_open_follow_ups") or {}).get("follow_ups", [])
        feedback = ctx.get("feedback") or []
        brief_notes = ctx.get("brief_notes")

        missing = [f for f in ("fair_edition", "client_budget_eur", "stand_area_sqm", "requested_height_m")
                   if not (edition if f == "fair_edition" else opp.get(f))]

        # First pass: the sales director's rule, and only that rule. See the module docstring.
        director_rule_met = bool(edition) and bool(opp.get("client_budget_eur"))
        if not feedback:
            proposal = HAND_OVER if director_rule_met else HOLD
            open_questions = [FIELD_QUESTIONS[f] for f in missing if f in ("fair_edition", "client_budget_eur")]
        else:
            # Revision: take the checker's objections on board.
            codes = {f.get("code") for f in feedback}
            if "height_exceeds_limit" in codes:
                proposal = RESOLVE_CONFLICT
            elif "missing_fair_edition" in codes or "missing_budget" in codes:
                proposal = HOLD
            elif codes & {"missing_area", "missing_height", "limit_unknown"}:
                proposal = HAND_OVER_PROVISIONAL
            else:
                proposal = HAND_OVER
            open_questions = [FIELD_QUESTIONS[f] for f in missing]
            if "height_exceeds_limit" in codes:
                open_questions.insert(0, (
                    f"The customer asked for {metres(opp.get('requested_height_m'))} but "
                    f"{edition.get('fair_edition_code')} allows {metres(edition.get('max_stand_height_m'))}. "
                    "Will they accept a lower stand?"
                ))

        lines: list[str] = []
        lines.append(f"TECHNICAL BRIEF — {opp.get('opportunity_code')}: {opp.get('description') or 'untitled'}")
        lines.append("")
        lines.append("Exhibitor")
        manager = opp.get("account_manager") or "unassigned"
        lines.append(f"  {opp.get('company_name')} ({opp.get('company_code')}), "
                     f"{opp.get('province_code') or '?'} · account manager {manager}")
        if opp.get("contact_first_name"):
            channel = opp.get("contact_phone") or opp.get("contact_email") or "no phone or email on file"
            lines.append(f"  Contact: {opp['contact_first_name']} {opp.get('contact_last_name') or ''} · {channel}")
        else:
            lines.append("  Contact: none recorded for this opportunity")
        lines.append("")
        lines.append("Fair edition")
        if edition:
            label = f"{edition.get('fair_name')} {edition.get('edition_label') or ''}".strip()
            lines.append(f"  {label} ({edition.get('fair_edition_code')}), "
                         f"{edition.get('city')}, {edition.get('venue')}")
            lines.append(f"  {dmy(edition.get('starts_on'))} to {dmy(edition.get('ends_on'))} · "
                         f"maximum stand height {metres(edition.get('max_stand_height_m'))}")
        else:
            lines.append("  Not recorded.")
        lines.append("")
        lines.append("Commercial frame (excl. VAT)")
        budget = opp.get("client_budget_eur")
        lines.append(f"  Customer budget:  {eur(budget) if budget else 'not stated'}")
        lines.append(f"  Recorded value:   {eur(opp.get('amount_eur'))}  (sales figure, not a budget)")
        lines.append("")
        lines.append("Stand requirements")
        area = opp.get("stand_area_sqm")
        lines.append(f"  Area:             {sqm(area) if area else 'not confirmed'}")
        if opp.get("requested_height_m"):
            fit = ""
            if height.get("within_limit") is True:
                fit = f"  — within the limit, {metres(height.get('margin_m'))} to spare"
            elif height.get("within_limit") is False:
                fit = "  — OVER THE EDITION LIMIT"
            lines.append(f"  Requested height: {metres(opp.get('requested_height_m'))}{fit}")
        else:
            lines.append("  Requested height: not decided")
        lines.append("")
        lines.append("Sales notes (quoted as recorded, not interpreted)")
        lines.append(f"  “{brief_notes}”" if brief_notes else "  None.")
        lines.append("")
        lines.append("Recent contact on this opportunity")
        if activities:
            for a in activities[:3]:
                lines.append(f"  {dmy(a.get('occurred_at'))} {a.get('type')}: {a.get('details')}")
        else:
            lines.append("  No conversations recorded against this opportunity.")
        if follow_ups:
            lines.append("")
            lines.append("Waiting on the customer")
            for f in follow_ups:
                flag = " (overdue)" if f.get("overdue") else ""
                lines.append(f"  by {dmy(f.get('due_on'))}{flag}: {f.get('note')}")
        if open_questions:
            lines.append("")
            lines.append("Open questions")
            for q in open_questions:
                lines.append(f"  - {q}")
        lines.append("")
        lines.append(f"Proposed next step: {self._next_step(proposal)}")

        return "\n".join(lines), {
            "proposal": proposal,
            "open_questions": open_questions,
            "missing_fields": missing,
            "used_director_rule_only": not feedback,
        }

    @staticmethod
    def _next_step(proposal: str) -> str:
        return {
            HAND_OVER: "hand this brief to the technical team to start work.",
            HAND_OVER_PROVISIONAL: "hand over now as PROVISIONAL so technical can plan, "
                                   "and chase the open questions before any build work starts.",
            HOLD: "hold. Sales needs the missing commercial information before technical is involved.",
            RESOLVE_CONFLICT: "do not start work. Resolve the conflict with the customer first, "
                              "then re-run this assistant.",
        }[proposal]

    # ------------------------------------------------------------------ checker: review

    def _review(self, ctx: dict) -> tuple[str, dict]:
        """Write the review prose. The JUDGEMENT is made by the checker role in code, from
        the policy; this only puts it into words."""
        verdict = ctx["verdict"]
        agrees = ctx["agrees"]
        expected = ctx["expected_proposal"]
        proposal = ctx["proposal"]
        unasked = ctx.get("unasked_missing", [])

        parts = [f"Policy {verdict.get('policy_version')} rates this enquiry "
                 f"{verdict['readiness'].upper()}."]
        for f in verdict.get("findings", []):
            parts.append(f.get("message", ""))
        if agrees and not unasked:
            parts.append("The draft proposal matches that rating. No changes needed.")
        else:
            if not agrees:
                proposed = f"“{proposal.replace('_', ' ')}”" if proposal else "no readable next step"
                parts.append(f"The draft proposes {proposed}, "
                             f"but the rating calls for “{expected.replace('_', ' ')}”.")
            if unasked:
                parts.append("The brief does not ask for the "
                             + ", ".join(sorted(FIELD_LABELS.get(f, f) for f in unasked)) + ".")
            parts.append("Sending it back for revision.")
        return " ".join(p for p in parts if p), {"agrees": agrees and not unasked}


def create_model_client(kind: str = STUB_NAME) -> ModelClient:
    """The one place a model is chosen. Called once at startup and passed down explicitly --
    never a module-level global -- so tests and a future real provider slot in here."""
    if kind == STUB_NAME:
        return DeterministicStubModel()
    raise ValueError(f"unknown model client {kind!r}; only {STUB_NAME!r} is available offline")
