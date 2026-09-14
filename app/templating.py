"""Jinja environment: formatting filters and the small helpers templates need.

The number and date formatting itself lives in app/formatting.py, shared with the handoff
assistant so the brief it writes uses the same conventions as the screens.
"""

from __future__ import annotations

from typing import Any

from fastapi.templating import Jinja2Templates

from app.formatting import day_label, dmy, dmyhm, eur, metres, num, sqm

# Readiness is written by app/handoff/policy.py. The mapping lives here with an explicit
# fallback so that changing the policy -- adding a state, or collapsing four into two --
# cannot break rendering. An unknown value degrades to a neutral badge.
READINESS_STYLE = {
    "ready":       ("pill-ok",     "Ready"),
    "provisional": ("pill-wait",   "Provisional"),
    "incomplete":  ("",            "Incomplete"),
    "blocked":     ("pill-danger", "Blocked"),
}

STATUS_STYLE = {
    "open":      "pill-info",
    "qualified": "pill-info",
    "proposal":  "pill-wait",
    "won":       "pill-ok",
    "lost":      "",
}

DECISION_STYLE = {
    "handoff":             ("pill-ok",     "Hand over"),
    "handoff_provisional": ("pill-wait",   "Hand over, provisional"),
    "hold":                ("",            "Hold"),
    "blocked":             ("pill-danger", "Blocked"),
    "edition_finished":    ("",            "Edition finished"),
}


def readiness_class(value: str | None) -> str:
    return READINESS_STYLE.get(value or "", ("", ""))[0]


def readiness_label(value: str | None) -> str:
    known = READINESS_STYLE.get(value or "")
    if known:
        return known[1]
    return (value or "not assessed").replace("_", " ").capitalize()


def status_class(value: str | None) -> str:
    return STATUS_STYLE.get(value or "", "")


def decision_class(value: str | None) -> str:
    return DECISION_STYLE.get(value or "", ("", ""))[0]


def decision_label(value: str | None) -> str:
    known = DECISION_STYLE.get(value or "")
    return known[1] if known else (value or "").replace("_", " ").capitalize()


def initials(first: str | None, last: str | None) -> str:
    return ((first or " ")[0] + (last or " ")[0]).strip().upper() or "?"


def build_templates(directory: str, asset_version: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=directory)
    env: Any = templates.env
    env.globals["asset_version"] = asset_version
    env.filters.update(
        eur=eur, num=num, sqm=sqm, metres=metres, dmy=dmy, dmyhm=dmyhm, day_label=day_label,
        readiness_class=readiness_class, readiness_label=readiness_label,
        status_class=status_class, decision_class=decision_class, decision_label=decision_label,
        initials=initials,
    )
    return templates
