"""Jinja environment: formatting filters and the small helpers templates need.

Numbers are formatted the way the source file writes them -- Italian convention, so
`50.000,00` with a dot for thousands and a comma for decimals. The archive uses a decimal
comma throughout and the fairs are all in Italy; rendering `50,000.00` back at a user whose
own data says `50000,00` would be a small, constant irritation.

Money, areas and heights all come out of the database as `Decimal`, never float, so nothing
here has to worry about a value drifting in the last cent.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi.templating import Jinja2Templates

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


def _to_decimal(value: Decimal | float | int | str) -> Decimal:
    """Convert without inheriting binary floating-point noise.

    Values read from ordinary columns arrive as Decimal. Values nested inside a jsonb
    aggregate -- the per-edition opportunity lists on the company page -- arrive as float,
    because JSON has no decimal type. `Decimal(3.3)` is 3.2999999999999998223..., which would
    render as a 50-digit height. Going through `str()` first recovers the shortest decimal
    that round-trips, which is the value that was actually stored.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def _group_thousands(whole: str) -> str:
    out = []
    for i, ch in enumerate(reversed(whole)):
        if i and i % 3 == 0:
            out.append(".")
        out.append(ch)
    return "".join(reversed(out))


def eur(value: Decimal | float | None, *, symbol: bool = True) -> str:
    if value is None:
        return "—"
    q = _to_decimal(value).quantize(Decimal("0.01"))
    sign = "-" if q < 0 else ""
    whole, _, frac = f"{abs(q):.2f}".partition(".")
    text = f"{sign}{_group_thousands(whole)},{frac}"
    return f"{text} €" if symbol else text


def num(value: Decimal | float | None, unit: str = "") -> str:
    """A plain decimal-comma number, trailing zeros trimmed. 4.00 renders as 4."""
    if value is None:
        return "—"
    q = _to_decimal(value).normalize()
    text = format(q, "f").replace(".", ",")
    return f"{text} {unit}" if unit else text


def sqm(value: Decimal | float | None) -> str:
    return num(value, "m²")


def metres(value: Decimal | float | None) -> str:
    return num(value, "m")


def dmy(value: date | datetime | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


def dmyhm(value: datetime | None) -> str:
    return value.strftime("%d/%m/%Y %H:%M") if value else "—"


def day_label(value: date | datetime | None) -> str:
    """A date with its weekday, for anything a person has to act on by a given day."""
    return value.strftime("%a %d %b %Y") if value else "—"


def readiness_class(value: str | None) -> str:
    return READINESS_STYLE.get(value or "", ("", ""))[0]


def readiness_label(value: str | None) -> str:
    known = READINESS_STYLE.get(value or "")
    if known:
        return known[1]
    return (value or "not assessed").replace("_", " ").capitalize()


def status_class(value: str | None) -> str:
    return STATUS_STYLE.get(value or "", "")


def initials(first: str | None, last: str | None) -> str:
    return ((first or " ")[0] + (last or " ")[0]).strip().upper() or "?"


def build_templates(directory: str, asset_version: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=directory)
    env: Any = templates.env
    env.globals["asset_version"] = asset_version
    env.filters.update(
        eur=eur,
        num=num,
        sqm=sqm,
        metres=metres,
        dmy=dmy,
        dmyhm=dmyhm,
        day_label=day_label,
        readiness_class=readiness_class,
        readiness_label=readiness_label,
        status_class=status_class,
        initials=initials,
    )
    return templates
