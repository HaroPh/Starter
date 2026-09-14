"""Parsing what people type into forms.

The archive writes numbers with a decimal comma (`12500,00`) and dates as `DD/MM/YYYY`, and
the screens display them the same way. So a user editing a value will naturally type it back
in that form -- but a browser date picker submits ISO `YYYY-MM-DD`, and someone pasting from
elsewhere may use a dot. All of those are accepted.

The rule the import follows applies here too: **empty means unknown, never zero.** Clearing
the stand area does not set it to 0 m2; it sets it back to "not confirmed", which is what
moves an opportunity from READY back to PROVISIONAL. A zero would silently claim a stand of
no size had been agreed.

Errors are collected rather than raised one at a time, so a form with three mistakes shows
all three at once instead of making the user resubmit three times.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


@dataclass
class FormErrors:
    messages: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, message: str) -> None:
        self.messages.setdefault(name, message)

    def __bool__(self) -> bool:
        return bool(self.messages)

    def get(self, name: str) -> str | None:
        return self.messages.get(name)


def parse_decimal(
    raw: str | None,
    name: str,
    errors: FormErrors,
    *,
    label: str,
    minimum: Decimal | None = Decimal("0"),
    maximum: Decimal | None = None,
    places: int = 2,
) -> Decimal | None:
    """Accept 12500,00 / 12.500,00 / 50,000.00 / 12500.00 / 4.5 / 12500. Empty is None.

    The separators are resolved like this:

      * both a comma and a dot  -> whichever comes LAST is the decimal point, so
        12.500,00 (Italian) and 50,000.00 (English) both read correctly;
      * one kind, several times -> it groups thousands: 1.250.000;
      * one kind, once, followed by exactly three digits -> REFUSED. "12.500" is twelve
        thousand five hundred to an Italian reader and twelve and a half to an English one,
        and silently picking one would be an error of a factor of a thousand on a budget.
        The message says how to write it unambiguously;
      * otherwise it is the decimal point: 12500,00, 4.5.
    """
    text = (raw or "").strip().replace(" ", "").replace("€", "").replace(" ", "")
    if not text:
        return None
    if not re.fullmatch(r"-?[\d.,]+", text) or not re.search(r"\d", text):
        errors.add(name, f"{label} must be a number, for example 12500,00.")
        return None

    has_comma, has_dot = "," in text, "." in text
    if has_comma and has_dot:
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        group_sep = "." if decimal_sep == "," else ","
        normalised = text.replace(group_sep, "").replace(decimal_sep, ".")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        head, *rest = text.split(sep)
        if len(rest) > 1:
            normalised = text.replace(sep, "")
        elif len(rest[0]) == 3:
            errors.add(
                name,
                f"{label}: “{text}” is ambiguous. Write {head}{rest[0]} for "
                f"{head} thousand {rest[0]}, or {head},{rest[0][:2]} for a decimal.",
            )
            return None
        else:
            normalised = text.replace(sep, ".")
    else:
        normalised = text

    try:
        value = Decimal(normalised)
    except InvalidOperation:
        errors.add(name, f"{label} must be a number, for example 12500,00.")
        return None

    if minimum is not None and value < minimum:
        errors.add(name, f"{label} cannot be negative.")
        return None
    if maximum is not None and value > maximum:
        errors.add(name, f"{label} looks too large (the maximum accepted is {maximum}).")
        return None
    return value.quantize(Decimal(1).scaleb(-places))


def parse_date(raw: str | None, name: str, errors: FormErrors, *, label: str) -> date | None:
    """Accept the browser date input (YYYY-MM-DD) and the archive format (DD/MM/YYYY)."""
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    errors.add(name, f"{label} must be a real date, for example 18/09/2026.")
    return None


def parse_datetime(raw: str | None, name: str, errors: FormErrors, *, label: str) -> datetime | None:
    """Accept datetime-local (YYYY-MM-DDTHH:MM) and DD/MM/YYYY HH:MM. Returned naive; the
    caller attaches Europe/Rome, the same zone the archive is written in."""
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    errors.add(name, f"{label} must be a date and time, for example 18/09/2026 10:30.")
    return None


def parse_choice(
    raw: str | None, name: str, errors: FormErrors, *, label: str, allowed: set[str],
    required: bool = True,
) -> str | None:
    text = (raw or "").strip()
    if not text:
        if required:
            errors.add(name, f"Choose a {label.lower()}.")
        return None
    if text not in allowed:
        errors.add(name, f"{text!r} is not a valid {label.lower()}.")
        return None
    return text


def parse_text(
    raw: str | None, name: str, errors: FormErrors, *, label: str, required: bool = False,
    max_length: int = 4000,
) -> str | None:
    text = (raw or "").strip()
    if not text:
        if required:
            errors.add(name, f"{label} cannot be empty.")
        return None
    if len(text) > max_length:
        errors.add(name, f"{label} is too long ({len(text)} characters; the limit is {max_length}).")
        return None
    return text


def parse_int(raw: str | None) -> int | None:
    text = (raw or "").strip()
    return int(text) if text.isdigit() else None
