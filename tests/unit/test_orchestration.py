"""The handoff orchestration, with no database.

The tools are replaced by fakes that serve a scenario from memory, so every path through
preparer -> checker -> coordinator can be exercised in milliseconds. The model is the real
deterministic stand-in: it IS the thing that ships, and it is pure, so there is nothing to
fake.

The four scenarios mirror the four readiness states and the demo rows in the archive:
OP000001 (complete), OP000003 (no area, no height), OP000005 (6 m against a 5 m limit), and
an enquiry with no customer budget.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.handoff import orchestrator, tools
from app.handoff.model_client import (
    HAND_OVER,
    HAND_OVER_PROVISIONAL,
    DeterministicStubModel,
    ModelResponse,
)
from app.handoff.policy import PolicyInput, assess
from app.handoff.roles import policy_input_from

TODAY = date(2026, 9, 14)


def scenario(**opp_overrides):
    opp = {
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
    }
    opp.update(opp_overrides)
    edition = {
        "fair_edition_code": "BEAUTY-2027", "fair_name": "Beauty Trade Forum",
        "edition_label": "2027", "city": "Bologna", "venue": "East Exhibition Centre",
        "starts_on": "2027-06-24", "ends_on": "2027-06-27", "max_stand_height_m": "4.50",
    }
    return opp, edition


@pytest.fixture
def fake_tools(monkeypatch):
    """Install tools that answer from a scenario. Returns a function to set the scenario."""
    state = {}
    calls: list[str] = []

    def install(opp, edition, activities=None, follow_ups=None):
        state.update(opp=opp, edition=edition, activities=activities or [], follow_ups=follow_ups or [])

    def get_opportunity(conn, args):
        calls.append("get_opportunity")
        return state["opp"]

    def get_fair_edition(conn, args):
        calls.append("get_fair_edition")
        if not state["edition"]:
            raise tools.ToolError("no edition")
        return state["edition"]

    def list_recent_activities(conn, args):
        calls.append("list_recent_activities")
        return {"opportunity_code": "OPX", "count": len(state["activities"]), "activities": state["activities"]}

    def list_open_follow_ups(conn, args):
        calls.append("list_open_follow_ups")
        return {"opportunity_code": "OPX", "count": len(state["follow_ups"]), "follow_ups": state["follow_ups"]}

    def check_height_limit(conn, args):
        calls.append("check_height_limit")
        return tools.check_height_limit(conn, args)

    registry = {
        "get_opportunity": tools.Tool("get_opportunity", "", get_opportunity),
        "get_fair_edition": tools.Tool("get_fair_edition", "", get_fair_edition),
        "list_recent_activities": tools.Tool("list_recent_activities", "", list_recent_activities),
        "list_open_follow_ups": tools.Tool("list_open_follow_ups", "", list_open_follow_ups),
        "check_height_limit": tools.Tool("check_height_limit", "", check_height_limit),
    }
    monkeypatch.setattr(tools, "REGISTRY", registry)
    install.calls = calls
    return install


def run(**kwargs):
    return orchestrator.run_handoff(None, "OPX", DeterministicStubModel(), today=TODAY, **kwargs)


# ---------------------------------------------------------------------------
# The four states


def test_complete_enquiry_hands_over_in_one_pass(fake_tools):
    fake_tools(*scenario())
    result = run()
    assert result.readiness == "ready"
    assert result.decision == "handoff"
    assert result.iterations == 1
    # The dependency chain: opportunity, then edition/activities/follow-ups, then the height.
    assert fake_tools.calls == ["get_opportunity", "get_fair_edition", "list_recent_activities",
                                "list_open_follow_ups", "check_height_limit"]


def test_missing_dimensions_is_revised_once_then_handed_over_provisionally(fake_tools):
    """OP000003. The preparer drafts from the director's rule, the checker objects that the
    dimensions are unknown, and the second draft marks the brief provisional."""
    fake_tools(*scenario(stand_area_sqm=None, requested_height_m=None))
    result = run()
    assert result.decision == "handoff_provisional"
    assert result.iterations == 2

    drafts = [s for s in result.steps if s.phase == "draft"]
    assert drafts[0].output_data["proposal"] == HAND_OVER          # sales' first instinct
    assert drafts[1].output_data["proposal"] == HAND_OVER_PROVISIONAL
    assert "stand area" in result.decision_reason and "requested stand height" in result.decision_reason
    # With no requested height there is nothing to check, and the preparer says so.
    assert "check_height_limit" not in fake_tools.calls


def test_height_over_limit_is_blocked(fake_tools):
    """OP000005: 6 m requested where the edition allows 5 m."""
    opp, edition = scenario(requested_height_m="6.00")
    edition["max_stand_height_m"] = "5.00"
    fake_tools(opp, edition)
    result = run()
    assert result.decision == "blocked"
    assert "6.00 m" in result.decision_reason and "5.00 m" in result.decision_reason
    height = result.observed["check_height_limit"]
    assert height["within_limit"] is False


def test_missing_budget_holds(fake_tools):
    fake_tools(*scenario(client_budget_eur=None))
    result = run()
    assert result.decision == "hold"
    assert "customer budget" in result.decision_reason


# ---------------------------------------------------------------------------
# Structural guarantees


def test_loop_is_bounded_even_when_the_model_never_agrees(fake_tools):
    """A proposer that is always wrong must still produce a decision after two passes."""
    fake_tools(*scenario(stand_area_sqm=None))

    class Stubborn(DeterministicStubModel):
        def complete(self, request):
            response = super().complete(request)
            if request.phase == "draft":
                data = dict(response.data, proposal=HAND_OVER, open_questions=[])
                return ModelResponse(response.text, data, self.name, self.version, self.is_stub)
            return response

    result = orchestrator.run_handoff(None, "OPX", Stubborn(), today=TODAY)
    assert result.iterations == orchestrator.MAX_ITERATIONS == 2
    assert result.decision == "handoff_provisional"          # the policy ruling stands
    assert "revision limit was reached" in result.decision_reason


def test_finished_edition_stops_regardless_of_readiness(fake_tools):
    """7,823 opportunities in the archive are READY but their fair is already over."""
    opp, edition = scenario()
    edition.update(starts_on="2025-06-24", ends_on="2025-06-27")
    fake_tools(opp, edition)
    result = run()
    assert result.readiness == "ready"
    assert result.decision == "edition_finished"


def test_checker_uses_the_policy_not_a_copy_of_it(fake_tools):
    """Whatever the checker concluded must equal policy.assess on the same observed facts."""
    fake_tools(*scenario(stand_area_sqm=None))
    result = run()
    expected = assess(policy_input_from(result.observed)).readiness.value
    assert result.readiness == expected


def test_run_is_deterministic(fake_tools):
    fake_tools(*scenario(stand_area_sqm=None, requested_height_m=None))
    a, b = run(), run()
    assert a.final_brief == b.final_brief
    assert a.decision == b.decision and a.decision_reason == b.decision_reason
    assert [s.output_text for s in a.steps] == [s.output_text for s in b.steps]


def test_edited_brief_notes_are_used_and_marked(fake_tools):
    fake_tools(*scenario())
    result = run(brief_override="Client now wants a double-decker.")
    assert "Client now wants a double-decker." in result.final_brief
    assert result.inputs["brief_override"] == "Client now wants a double-decker."


def test_unknown_tool_is_recorded_not_fatal(fake_tools):
    """A real model will sometimes ask for a tool that does not exist."""
    fake_tools(*scenario())

    class Confused(DeterministicStubModel):
        def _plan(self, ctx):
            text, data = super()._plan(ctx)
            if "get_opportunity" in ctx.get("observed", {}) and "nonsense" not in str(ctx):
                data = dict(data, tool_calls=data["tool_calls"] + [{"tool": "nonsense", "arguments": {}}])
            return text, data

    result = orchestrator.run_handoff(None, "OPX", Confused(), today=TODAY)
    failed = [c for _, c in result.tool_calls if not c.ok]
    assert failed and failed[0].tool_name == "nonsense"
    assert result.decision == "handoff"


def test_stub_is_labelled_as_a_stub(fake_tools):
    fake_tools(*scenario())
    result = run()
    assert result.model_is_stub is True
    assert result.model_name == "deterministic-stub"
    assert result.inputs["model"]["is_stub"] is True


def test_policy_input_round_trip():
    opp, edition = scenario()
    pi = policy_input_from({"get_opportunity": opp, "get_fair_edition": edition})
    assert isinstance(pi, PolicyInput)
    assert str(pi.max_stand_height_m) == "4.50"
    assert pi.edition_ends_on == date(2027, 6, 27)
