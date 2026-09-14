"""The handoff readiness policy.

This module is the answer to the disagreement at the centre of the brief:

    "The sales director wants an enquiry passed over as soon as the customer names a fair
     and gives a budget. Waiting for the rest of the brief costs time. The technical
     coordinator wants nothing passed over until the stand area and requested height are
     known and checked against the fair information, because the team keeps starting work
     on requests it can't deliver."

The two positions look opposed, but they are about different things. The director wants
technical to KNOW EARLY. The coordinator wants technical not to START WORK on something
undeliverable. Those do not conflict -- they only collide when "hand over" and "start work"
are treated as one event. Splitting them dissolves the dispute:

    INCOMPLETE   no fair edition, or no customer budget.
                 Not even the director's rule is met. Sales has to obtain these first.

    PROVISIONAL  fair edition and budget known; stand area and/or requested height not yet.
                 The director's rule is met, so the brief goes over NOW -- marked
                 provisional, naming exactly what is missing, so technical can plan without
                 starting build work. Each gap becomes a follow-up for the account manager.

    READY        everything known, and the requested height is within the edition limit.
                 The coordinator's rule is met. Technical can start.

    BLOCKED      the request conflicts with the fair's rules -- today, a requested height
                 above the edition's maximum, for which "no exceptions are recorded".
                 Not missing information: a conflict. It is reported as one.

The archive measures the gap between the two rules: 14,635 opportunities meet the
director's rule and 13,906 meet the coordinator's, so 728 sit exactly in the space a
binary policy cannot express. Siding with technical hides them; siding with the director
lets them into the queue looking ready.

WHY THIS IS A PURE FUNCTION
No database, no clock, no network, no module state. The input is a frozen value and the
output is a frozen value. That is what lets three different consumers -- the opportunity
badge, the "needs attention" list, and the handoff assistant's checker -- share one
definition with no possibility of drifting apart, and what makes the rules testable as a
plain truth table.

CHANGING THE POLICY
Edit `assess`, bump POLICY_VERSION, restart. The application recomputes every stored
verdict at boot when the version differs. There is no CHECK constraint on the readiness
column and templates render unknown states through a default, so collapsing four states to
two -- or adding a fifth -- needs no migration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

POLICY_VERSION = "4state-v1"


class Readiness(StrEnum):
    INCOMPLETE = "incomplete"
    PROVISIONAL = "provisional"
    READY = "ready"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class PolicyInput:
    """Exactly the facts the policy rules on, and nothing else.

    Deliberately narrower than the full opportunity. A policy that accepted the whole record
    could quietly start depending on fields nobody meant it to read; this makes its
    dependencies visible in one line each.
    """

    has_fair_edition: bool
    max_stand_height_m: Decimal | None
    edition_ends_on: date | None
    client_budget_eur: Decimal | None
    stand_area_sqm: Decimal | None
    requested_height_m: Decimal | None


@dataclass(frozen=True)
class Finding:
    """One thing the policy noticed.

    `code` is stable and machine-readable -- the checker and the templates switch on it.
    `message` is for a person and must make sense on its own.
    """

    code: str
    severity: str  # 'missing' | 'conflict' | 'warning'
    field: str | None
    message: str
    observed: str | None = None
    limit: str | None = None


@dataclass(frozen=True)
class ReadinessVerdict:
    readiness: Readiness
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    missing: tuple[str, ...] = field(default_factory=tuple)
    conflicts: tuple[Finding, ...] = field(default_factory=tuple)
    policy_version: str = POLICY_VERSION

    def to_json(self) -> dict:
        """Plain JSON types only -- this is written straight into a jsonb column."""
        return {
            "readiness": self.readiness.value,
            "missing": list(self.missing),
            "findings": [asdict(f) for f in self.findings],
            "conflicts": [asdict(f) for f in self.conflicts],
            "policy_version": self.policy_version,
        }


# Human-facing names for the fields the policy asks about. Kept here, next to the rules,
# so a finding message and the rule that produced it cannot disagree about wording.
FIELD_LABELS = {
    "fair_edition": "fair edition",
    "client_budget_eur": "customer budget",
    "stand_area_sqm": "stand area",
    "requested_height_m": "requested stand height",
}


def _fmt(value: Decimal) -> str:
    return f"{value:.2f}"


def assess(facts: PolicyInput) -> ReadinessVerdict:
    """Rule on how ready an enquiry is to go to the technical team."""
    findings: list[Finding] = []
    missing: list[str] = []
    conflicts: list[Finding] = []

    # -- What the director's rule needs: a fair edition and a customer budget.
    if not facts.has_fair_edition:
        missing.append("fair_edition")
        findings.append(Finding(
            code="missing_fair_edition", severity="missing", field="fair_edition",
            message="No fair edition is recorded, so neither the dates nor the height limit "
                    "are known.",
        ))
    if facts.client_budget_eur is None:
        missing.append("client_budget_eur")
        findings.append(Finding(
            code="missing_budget", severity="missing", field="client_budget_eur",
            message="The customer has not stated a budget. The recorded opportunity value is "
                    "a sales figure, not a budget, and is not used in its place.",
        ))

    # -- What the coordinator's rule adds: the stand dimensions.
    if facts.stand_area_sqm is None:
        missing.append("stand_area_sqm")
        findings.append(Finding(
            code="missing_area", severity="missing", field="stand_area_sqm",
            message="The stand area is not known yet, so the allocated plot cannot be planned.",
        ))
    if facts.requested_height_m is None:
        missing.append("requested_height_m")
        findings.append(Finding(
            code="missing_height", severity="missing", field="requested_height_m",
            message="The customer has not given a stand height, so it cannot be checked "
                    "against the fair limit.",
        ))

    # -- The check the coordinator insists on: the request against the edition's rules.
    limit_known = facts.has_fair_edition and facts.max_stand_height_m is not None
    if facts.has_fair_edition and facts.max_stand_height_m is None:
        findings.append(Finding(
            code="limit_unknown", severity="warning", field="max_stand_height_m",
            message="The height limit for this fair edition is not recorded, so the requested "
                    "height cannot be confirmed as allowed.",
        ))

    if (
        limit_known
        and facts.requested_height_m is not None
        and facts.requested_height_m > facts.max_stand_height_m  # type: ignore[operator]
    ):
        conflict = Finding(
            code="height_exceeds_limit", severity="conflict", field="requested_height_m",
            message=(
                f"The customer asked for a {_fmt(facts.requested_height_m)} m stand, but this "
                f"edition allows at most {_fmt(facts.max_stand_height_m)} m and no exceptions "  # type: ignore[arg-type]
                f"are recorded."
            ),
            observed=_fmt(facts.requested_height_m),
            limit=_fmt(facts.max_stand_height_m),  # type: ignore[arg-type]
        )
        conflicts.append(conflict)
        findings.append(conflict)

    # -- Resolve the state. Order encodes precedence.
    if conflicts:
        # A request that already breaks the rules is blocked even while other fields are open:
        # there is no point chasing a budget for a stand that cannot be built as asked.
        state = Readiness.BLOCKED
    elif "fair_edition" in missing or "client_budget_eur" in missing:
        state = Readiness.INCOMPLETE
    elif missing or not limit_known:
        # Dimensions still open, or the limit unknown so the height could not be verified.
        # An unverified height is never READY -- that would pass over a request nobody
        # checked -- and never BLOCKED, which would reject one nobody disproved.
        state = Readiness.PROVISIONAL
    else:
        state = Readiness.READY

    return ReadinessVerdict(
        readiness=state,
        findings=tuple(findings),
        missing=tuple(missing),
        conflicts=tuple(conflicts),
        policy_version=POLICY_VERSION,
    )


def is_actionable(edition_ends_on: date | None, today: date) -> bool:
    """Whether the fair edition is still ahead, or under way.

    A separate question from readiness, and kept separate on purpose. 7,823 opportunities in
    the archive have complete information -- READY by `assess` -- but belong to editions that
    have already finished. Their information is not incomplete; it is simply too late. Folding
    that into `assess` would muddle one clear rule into two. The handoff coordinator combines
    the two answers instead.

    An unknown end date returns True: refusing an enquiry because a date is missing would
    turn a data gap into a lost customer.
    """
    if edition_ends_on is None:
        return True
    return edition_ends_on >= today
