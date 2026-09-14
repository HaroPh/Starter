"""Number and date formatting, shared by the templates and the handoff assistant.

Numbers follow the source file's Italian convention -- `50.000,00` -- because the archive
writes them that way, every fair is in Italy, and the brief the assistant writes goes to a
technical team working from the same figures.

Kept free of any web or database import so the assistant's model stand-in can use it
without pulling in the template layer.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

DASH = "—"


def to_decimal(value: Decimal | float | int | str) -> Decimal:
    """Convert without inheriting binary floating-point noise.

    Values from ordinary columns arrive as Decimal. Values nested inside a jsonb aggregate,
    or stored as JSON strings in a run record, arrive as float or str. `Decimal(3.3)` is
    3.2999999999999998223..., which would render as a fifty-digit height, so floats go
    through `str()` first to recover the shortest decimal that round-trips.
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


def eur(value, *, symbol: bool = True) -> str:
    if value is None or value == "":
        return DASH
    q = to_decimal(value).quantize(Decimal("0.01"))
    sign = "-" if q < 0 else ""
    whole, _, frac = f"{abs(q):.2f}".partition(".")
    text = f"{sign}{_group_thousands(whole)},{frac}"
    return f"{text} €" if symbol else text


def num(value, unit: str = "") -> str:
    """A decimal-comma number with trailing zeros trimmed: 4.00 renders as 4, 4.50 as 4,5."""
    if value is None or value == "":
        return DASH
    q = to_decimal(value).normalize()
    text = format(q, "f").replace(".", ",")
    return f"{text} {unit}" if unit else text


def sqm(value) -> str:
    return num(value, "m²")


def metres(value) -> str:
    return num(value, "m")


def _as_date(value) -> date | datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, date | datetime):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def dmy(value) -> str:
    d = _as_date(value)
    return d.strftime("%d/%m/%Y") if d else DASH


def dmyhm(value) -> str:
    d = _as_date(value)
    return d.strftime("%d/%m/%Y %H:%M") if isinstance(d, datetime) else dmy(value)


def day_label(value) -> str:
    """A date with its weekday, for anything a person has to act on by a given day."""
    d = _as_date(value)
    return d.strftime("%a %d %b %Y") if d else DASH
