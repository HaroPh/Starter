"""What a misbehaving model can and cannot do to a run.

The deterministic stand-in is well behaved by construction, so nothing in
test_orchestration.py ever makes the runtime refuse anything. A real model will: it will
ask for another opportunity "for context", look up last year's edition, return a call in
the wrong shape, or ask for far too much. Each double below does one of those things on
purpose, and each test pins what the runtime does about it.

The rule under test: the model chooses WHETHER to look, never WHERE. Everything it does
that the runtime rejects is a refused call in the trace, not an exception, and the run
always ends with a decision the policy can stand behind.

Two of these tests failed before the guards existed. `test_a_wandering_model_cannot_swap_
the_opportunity` produced a brief about the sibling opportunity, and
`test_a_call_in_the_wrong_shape_is_refused_not_fatal` raised KeyError inside the preparer.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.handoff import orchestrator, roles, tools
from app.handoff.model_client import DeterministicStubModel, ModelResponse

TODAY = date(2026, 9, 14)

# One exhibitor, two editions of the same fair. OPX is this year's enquiry and is complete.
# OPY is last year's won order, the sibling a "helpful" model would reach for. Its edition
# has a LOWER height limit, so a brief that mixed the two up would not just be stale --
# it would report a false conflict.
OPPORTUNITIES = {
    "OPX": {
        "opportunity_code": "OPX", "opportunity_id": 1, "description": "Launch stand",
        "status": "open", "brief_notes": "Reception and a store.",
        "amount_eur": "48000.00", "client_budget_eur": "50000.00",
        "stand_area_sqm": "80.00", "requested_height_m": "4.00",
        "opened_on": "2026-01-15", "expected_close_on": "2026-11-30",
        "company_code": "COX", "company_name": "Aster Cosmetics S.r.l.",
        "province_code": "BA", "region": "Apulia", "account_manager": "Casey Martin",
        "contact_first_name": "Chris", "contact_last_name": "Conti",
        "contact_email": "chris@example", "contact_phone": "+39 02 1",
        "fair_edition_code": "BEAUTY-2027",
    },
    "OPY": {
        "opportunity_code": "OPY", "opportunity_id": 2, "description": "Summer stand",
        "status": "won", "brief_notes": "This agreement does not cover next year's stand.",
        "amount_eur": "12500.00", "client_budget_eur": "12500.00",
        "stand_area_sqm": "40.00", "requested_height_m": "2.50",
        "opened_on": "2025-01-10", "expected_close_on": "2025-11-30",
        "company_code": "COX", "company_name": "Aster Cosmetics S.r.l.",
        "province_code": "BA", "region": "Apulia", "account_manager": "Casey Martin",
        "contact_first_name": "Chris", "contact_last_name": "Conti",
        "contact_email": "chris@example", "contact_phone": "+39 02 1",
        "fair_edition_code": "BEAUTY-2026",
    },
}
EDITIONS = {
    "BEAUTY-2027": {
        "fair_edition_code": "BEAUTY-2027", "fair_name": "Beauty Trade Forum",
        "edition_label": "2027", "city": "Bologna", "venue": "East Exhibition Centre",
        "starts_on": "2027-06-24", "ends_on": "2027-06-27", "max_stand_height_m": "4.50",
    },
    "BEAUTY-2026": {
        "fair_edition_code": "BEAUTY-2026", "fair_name": "Beauty Trade Forum",
        "edition_label": "2026", "city": "Bologna", "venue": "East Exhibition Centre",
        "starts_on": "2026-06-25", "ends_on": "2026-06-28", "max_stand_height_m": "3.00",
    },
}
ACTIVITIES = {
    "OPX": [],
    "OPY": [{"type": "note", "occurred_at": "2026-07-01T10:00:00",
             "details": "do not reuse the summer order value", "author": "c.martin"}],
}
SIBLING_MARKERS = ("OPY", "12500", "summer order", "BEAUTY-2026")


@pytest.fixture(autouse=True)
def world(monkeypatch):
    """Tools that actually honour their arguments, so a wandering call gets what it asked for.

    The fakes in test_orchestration.py ignore arguments, which is fine there; here the whole
    point is that a call for OPY must be able to return OPY -- the runtime, not the tool, has
    to stop it.
    """
    executed: list[tuple[str, dict]] = []

    def get_opportunity(conn, args):
        executed.append(("get_opportunity", args))
        try:
            return OPPORTUNITIES[args["opportunity_code"]]
        except KeyError as exc:
            raise tools.ToolError(f"no opportunity {args.get('opportunity_code')!r}") from exc

    def get_fair_edition(conn, args):
        executed.append(("get_fair_edition", args))
        try:
            return EDITIONS[args["fair_edition_code"]]
        except KeyError as exc:
            raise tools.ToolError(f"no edition {args.get('fair_edition_code')!r}") from exc

    def list_recent_activities(conn, args):
        executed.append(("list_recent_activities", args))
        code = args["opportunity_code"]
        return {"opportunity_code": code, "count": len(ACTIVITIES[code]), "activities": ACTIVITIES[code]}

    def list_open_follow_ups(conn, args):
        executed.append(("list_open_follow_ups", args))
        return {"opportunity_code": args["opportunity_code"], "count": 0, "follow_ups": []}

    def check_height_limit(conn, args):
        executed.append(("check_height_limit", args))
        return tools.check_height_limit(conn, args)

    registry = {
        name: tools.Tool(name, "", fn, scope_key=tools.REGISTRY[name].scope_key)
        for name, fn in (
            ("get_opportunity", get_opportunity),
            ("get_fair_edition", get_fair_edition),
            ("list_recent_activities", list_recent_activities),
            ("list_open_follow_ups", list_open_follow_ups),
            ("check_height_limit", check_height_limit),
        )
    }
    monkeypatch.setattr(tools, "REGISTRY", registry)
    return executed


def run(model):
    return orchestrator.run_handoff(None, "OPX", model, today=TODAY)


def refused(result):
    return [rec for _, rec in result.tool_calls if not rec.ok]


def everything_the_run_wrote(result) -> str:
    """The stored snapshot plus every step's text: what a reviewer of the run could read."""
    return json.dumps(result.inputs) + result.final_brief + " ".join(s.output_text for s in result.steps)


# ---------------------------------------------------------------------------
# Where to look is not the model's choice


class Wanderer(DeterministicStubModel):
    """Once it has the opportunity, also reads the exhibitor's other enquiry 'for context'."""

    def _plan(self, ctx):
        text, data = super()._plan(ctx)
        if "get_opportunity" in ctx.get("observed", {}) and not ctx["observed"].get("list_recent_activities"):
            data["tool_calls"] = data["tool_calls"] + [
                {"tool": "get_opportunity", "arguments": {"opportunity_code": "OPY"}},
                {"tool": "list_recent_activities", "arguments": {"opportunity_code": "OPY", "limit": 5}},
            ]
            data["done"] = False
        return text, data


def test_a_wandering_model_cannot_swap_the_opportunity(world):
    result = run(Wanderer())

    # The run stayed about OPX and reached the decision OPX deserves.
    assert result.observed["get_opportunity"]["opportunity_code"] == "OPX"
    assert result.decision == "handoff"
    # Both stray calls are in the trace as refusals, and neither reached a tool.
    strays = [r for r in refused(result) if r.arguments.get("opportunity_code") == "OPY"]
    assert [r.tool_name for r in strays] == ["get_opportunity", "list_recent_activities"]
    assert all("scoped to opportunity 'OPX'" in r.error for r in strays)
    assert not any(args.get("opportunity_code") == "OPY" for _, args in world)
    # Nothing from the sibling edition leaked into what the run recorded.
    written = everything_the_run_wrote(result)
    assert not any(marker in written for marker in SIBLING_MARKERS)


class WrongEdition(DeterministicStubModel):
    """Looks up last year's edition of the fair instead of the one the enquiry names."""

    def _plan(self, ctx):
        text, data = super()._plan(ctx)
        for call in data["tool_calls"]:
            if call["tool"] == "get_fair_edition":
                call["arguments"]["fair_edition_code"] = "BEAUTY-2026"
        return text, data


def test_a_model_cannot_check_the_height_against_another_edition(world):
    """Before the scope guard this run came back BLOCKED: 4.00 m against BEAUTY-2026's 3.00 m.

    A false conflict is worse than a missing edition -- it sends sales to argue with the
    customer about a limit that does not apply. With the guard the edition is never read,
    the policy sees no edition, and the run holds and says so.
    """
    result = run(WrongEdition())

    assert result.readiness != "blocked"
    assert result.decision == "hold"
    assert "fair edition" in result.decision_reason
    assert all(r.tool_name == "get_fair_edition" and "BEAUTY-2026" in r.error for r in refused(result))
    assert not any(name == "get_fair_edition" for name, _ in world)


class Forgetful(DeterministicStubModel):
    """Asks for the right tools but leaves the code out of the arguments."""

    def _plan(self, ctx):
        text, data = super()._plan(ctx)
        for call in data["tool_calls"]:
            call["arguments"].pop("opportunity_code", None)
            call["arguments"].pop("fair_edition_code", None)
        return text, data


def test_a_missing_scope_argument_is_filled_in_by_the_runtime(world):
    """Leaving the target blank is fine: the runtime knows which run this is."""
    result = run(Forgetful())

    assert result.decision == "handoff"
    assert refused(result) == []
    assert any(args.get("fair_edition_code") == "BEAUTY-2027" for name, args in world if name == "get_fair_edition")
    recorded = {rec.tool_name: rec.arguments for _, rec in result.tool_calls}
    assert recorded["list_recent_activities"]["opportunity_code"] == "OPX"


def test_scope_matching_ignores_case_and_accepts_the_numeric_id(world):
    """'opx' and '1' both mean this run's opportunity; the tool still receives the canonical code."""

    class Sloppy(DeterministicStubModel):
        def _plan(self, ctx):
            text, data = super()._plan(ctx)
            for call in data["tool_calls"]:
                if call["tool"] == "list_recent_activities":
                    call["arguments"]["opportunity_code"] = "opx"
                if call["tool"] == "list_open_follow_ups":
                    call["arguments"]["opportunity_code"] = "1"
            return text, data

    result = run(Sloppy())
    assert refused(result) == []
    assert result.decision == "handoff"
    scoped = ("get_opportunity", "list_recent_activities", "list_open_follow_ups")
    assert all(args["opportunity_code"] == "OPX" for name, args in world if name in scoped)


# ---------------------------------------------------------------------------
# The shape of what the model returns is not trusted either


class Malformed(DeterministicStubModel):
    """First round: four calls in shapes a model produces when it is not following the schema."""

    misbehaved = False

    def _plan(self, ctx):
        text, data = super()._plan(ctx)
        if not self.misbehaved:
            self.misbehaved = True
            data["tool_calls"] = [
                {"name": "get_opportunity", "arguments": {"opportunity_code": "OPX"}},  # wrong key
                "get_opportunity",                                                     # bare string
                None,
                {"tool": "get_opportunity", "arguments": "opportunity_code=OPX"},      # arguments not a dict
            ]
        return text, data


def test_a_call_in_the_wrong_shape_is_refused_not_fatal(world):
    result = run(Malformed())

    bad = [r for r in refused(result) if r.tool_name == "(malformed)"]
    assert len(bad) == 4
    assert all("expected" in r.error for r in bad)
    assert all("received" in r.arguments for r in bad)
    # The next round proceeded normally and the run reached its decision.
    assert result.observed["get_opportunity"]["opportunity_code"] == "OPX"
    assert result.decision == "handoff"


class NullPlan(DeterministicStubModel):
    """Returns `tool_calls: null` and never says it is done."""

    def _plan(self, ctx):
        return "I will look into it.", {"tool_calls": None, "done": False}


def test_a_null_plan_ends_in_a_hold_not_a_crash(world):
    result = run(NullPlan())

    assert result.decision == "hold"
    assert "could not be read" in result.decision_reason
    assert result.tool_calls == []
    assert len([s for s in result.steps if s.phase == "plan"]) == roles.MAX_TOOL_ROUNDS


class Flooder(DeterministicStubModel):
    """Asks for the opportunity five hundred times in one round."""

    def _plan(self, ctx):
        text, data = super()._plan(ctx)
        if "get_opportunity" not in ctx.get("observed", {}):
            data["tool_calls"] = data["tool_calls"] * 500
        return text, data


def test_calls_per_round_are_capped_and_the_cap_is_in_the_trace(world):
    result = run(Flooder())

    first_round = [rec for step_seq, rec in result.tool_calls if step_seq == result.steps[0].seq]
    assert len(first_round) == roles.MAX_CALLS_PER_ROUND + 1
    assert sum(1 for name, _ in world if name == "get_opportunity") == roles.MAX_CALLS_PER_ROUND
    summary = first_round[-1]
    assert summary.tool_name == "(limit)" and not summary.ok
    assert f"{500 - roles.MAX_CALLS_PER_ROUND} further call" in summary.error
    assert result.decision == "handoff"


class ListDraft(DeterministicStubModel):
    """Returns the draft's data as a list instead of an object, and a plan as a string."""

    def complete(self, request):
        response = super().complete(request)
        if request.phase == "draft":
            return ModelResponse(response.text, ["hand_over"], self.name, self.version, self.is_stub)
        return response


def test_a_draft_in_the_wrong_shape_is_still_checked_by_the_policy(world):
    result = run(ListDraft())

    # No proposal could be read, so the checker objects; after the revision limit the
    # policy ruling stands, exactly as with a model that never agrees.
    assert result.iterations == orchestrator.MAX_ITERATIONS
    assert result.decision == "handoff"
    assert "revision limit" in result.decision_reason
    assert all(isinstance(s.output_data, dict) for s in result.steps)


# ---------------------------------------------------------------------------
# What the model says about the facts does not decide the outcome


class LyingHeightCheck(DeterministicStubModel):
    """Checks a 6 m stand against a limit it made up, and reports it fits."""

    def _plan(self, ctx):
        text, data = super()._plan(ctx)
        for call in data["tool_calls"]:
            if call["tool"] == "check_height_limit":
                call["arguments"]["max_stand_height_m"] = "9.00"
        return text, data


def test_the_checker_rules_on_the_edition_not_on_the_preparers_measurement(world, monkeypatch):
    monkeypatch.setitem(OPPORTUNITIES, "OPX", dict(OPPORTUNITIES["OPX"], requested_height_m="6.00"))

    result = run(LyingHeightCheck())

    assert result.observed["check_height_limit"]["within_limit"] is True   # the preparer believed it
    assert result.readiness == "blocked"                                   # the policy did not
    assert result.decision == "blocked"
    assert "4,50 m" in result.decision_reason or "4.50" in result.decision_reason
