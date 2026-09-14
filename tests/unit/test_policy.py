"""The readiness truth table.

This is the highest-value test in the project. The policy is the answer to the competing
requests in the brief, it is consumed by three different parts of the application (the
opportunity badge, the "needs attention" list, and the handoff assistant's checker), and it
is a pure function -- so it can be tested exhaustively with no database, no clock and no
fixtures.

Each case below maps to a sentence in the brief or a population measured in the archive.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from app.handoff.policy import (
    POLICY_VERSION,
    PolicyInput,
    Readiness,
    assess,
    is_actionable,
)


def facts(**overrides) -> PolicyInput:
    """A complete, unproblematic enquiry. Each test removes or changes one thing."""
    base = dict(
        has_fair_edition=True,
        max_stand_height_m=D("4.50"),
        edition_ends_on=date(2027, 6, 27),
        client_budget_eur=D("50000.00"),
        stand_area_sqm=D("80.00"),
        requested_height_m=D("4.00"),
    )
    base.update(overrides)
    return PolicyInput(**base)


# ---------------------------------------------------------------------------
# The four states


def test_complete_enquiry_is_ready():
    """OP000001 in the archive: fair, budget, area and a height under the limit."""
    verdict = assess(facts())
    assert verdict.readiness is Readiness.READY
    assert verdict.missing == ()
    assert verdict.conflicts == ()


def test_missing_area_is_provisional():
    """The director's side of the dispute: fair and budget are enough to hand over."""
    verdict = assess(facts(stand_area_sqm=None))
    assert verdict.readiness is Readiness.PROVISIONAL
    assert verdict.missing == ("stand_area_sqm",)


def test_missing_height_is_provisional():
    verdict = assess(facts(requested_height_m=None))
    assert verdict.readiness is Readiness.PROVISIONAL
    assert verdict.missing == ("requested_height_m",)


def test_missing_area_and_height_is_provisional_and_names_both():
    """OP000003 in the archive: "Plot size is still with the organiser; client has not
    decided the height." Both gaps must be named, because each becomes a follow-up."""
    verdict = assess(facts(stand_area_sqm=None, requested_height_m=None))
    assert verdict.readiness is Readiness.PROVISIONAL
    assert set(verdict.missing) == {"stand_area_sqm", "requested_height_m"}


def test_missing_budget_is_incomplete():
    """Without a budget even the director's rule is not met. 365 rows in the archive."""
    verdict = assess(facts(client_budget_eur=None))
    assert verdict.readiness is Readiness.INCOMPLETE
    assert "client_budget_eur" in verdict.missing


def test_missing_fair_edition_is_incomplete():
    """A replaced archive may reference an edition that does not exist."""
    verdict = assess(facts(has_fair_edition=False, max_stand_height_m=None, edition_ends_on=None))
    assert verdict.readiness is Readiness.INCOMPLETE
    assert "fair_edition" in verdict.missing


# ---------------------------------------------------------------------------
# The height rule -- the technical coordinator's side


def test_height_over_limit_is_blocked():
    """OP000005 in the archive: 6.00 m requested at an edition allowing 5.00 m, and "no
    exception to the edition limit is recorded". A conflict, not missing information."""
    verdict = assess(facts(requested_height_m=D("6.00"), max_stand_height_m=D("5.00")))
    assert verdict.readiness is Readiness.BLOCKED
    assert len(verdict.conflicts) == 1
    conflict = verdict.conflicts[0]
    assert conflict.code == "height_exceeds_limit"
    assert conflict.observed == "6.00"
    assert conflict.limit == "5.00"


def test_height_exactly_at_limit_is_ready_not_blocked():
    """The comparison is <=, not <. 1,210 opportunities in the archive sit exactly at their
    edition's limit. Getting this operator wrong would block every one of them."""
    verdict = assess(facts(requested_height_m=D("4.50"), max_stand_height_m=D("4.50")))
    assert verdict.readiness is Readiness.READY


def test_conflict_outranks_missing_information():
    """A request that already breaks the rules is blocked even while other fields are still
    open -- there is no point chasing the budget for a stand that cannot be built."""
    verdict = assess(
        facts(requested_height_m=D("6.00"), max_stand_height_m=D("5.00"),
              client_budget_eur=None, stand_area_sqm=None)
    )
    assert verdict.readiness is Readiness.BLOCKED
    assert set(verdict.missing) == {"client_budget_eur", "stand_area_sqm"}


def test_unknown_limit_never_yields_ready_or_blocked():
    """If the edition limit is unknown the height cannot be checked in either direction.
    Claiming READY would pass over a request nobody verified; claiming BLOCKED would
    reject one nobody disproved. It stays PROVISIONAL, with the reason stated."""
    verdict = assess(facts(max_stand_height_m=None))
    assert verdict.readiness is Readiness.PROVISIONAL
    assert any(f.code == "limit_unknown" for f in verdict.findings)


def test_unknown_limit_with_missing_budget_is_still_incomplete():
    verdict = assess(facts(max_stand_height_m=None, client_budget_eur=None))
    assert verdict.readiness is Readiness.INCOMPLETE


# ---------------------------------------------------------------------------
# Properties of the verdict


def test_verdict_carries_policy_version():
    """Stored on every handoff run, so an old run keeps showing which rules judged it."""
    assert assess(facts()).policy_version == POLICY_VERSION


def test_every_finding_has_a_human_message():
    verdict = assess(facts(stand_area_sqm=None, requested_height_m=D("6.00"),
                           max_stand_height_m=D("5.00")))
    assert verdict.findings
    assert all(f.message and f.message[0].isupper() for f in verdict.findings)


def test_assess_is_deterministic():
    """No clock, no randomness, no hidden state: identical input, identical output."""
    a = assess(facts(stand_area_sqm=None))
    b = assess(facts(stand_area_sqm=None))
    assert a == b


def test_to_json_round_trips_to_plain_types():
    """The verdict is persisted into jsonb; nothing in it may be a Decimal or an enum."""
    import json

    data = assess(facts(stand_area_sqm=None)).to_json()
    assert json.loads(json.dumps(data)) == data
    assert data["readiness"] == "provisional"


# ---------------------------------------------------------------------------
# Time: a separate question from readiness


@pytest.mark.parametrize(
    ("ends_on", "today", "expected"),
    [
        (date(2027, 6, 27), date(2026, 9, 14), True),   # future edition
        (date(2026, 9, 14), date(2026, 9, 14), True),   # last day of the fair
        (date(2026, 6, 27), date(2026, 9, 14), False),  # finished
        (None, date(2026, 9, 14), True),                 # unknown dates: do not refuse
    ],
)
def test_is_actionable(ends_on, today, expected):
    """7,823 opportunities in the archive are READY by information but belong to an edition
    that has already finished. That is not a readiness question -- the information IS
    complete -- so it is answered by a second function rather than by complicating the
    first. `today` is a parameter, so both functions stay pure."""
    assert is_actionable(ends_on, today) is expected
