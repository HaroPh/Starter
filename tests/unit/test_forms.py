"""Form input parsing.

The failure these tests guard against is silent: a budget typed as "12.500" read as 12.50
saves without complaint and is wrong by a factor of a thousand. Parsing is exactly the kind
of code that looks right, works on the cases you tried, and is wrong on the one a user types.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal as D

import pytest

from app.forms import FormErrors, parse_date, parse_datetime, parse_decimal


def dec(raw):
    errors = FormErrors()
    value = parse_decimal(raw, "x", errors, label="Budget")
    return value, errors


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12500,00", D("12500.00")),     # the archive's own format
        ("12.500,00", D("12500.00")),    # Italian with thousands grouping
        ("50,000.00", D("50000.00")),    # English with thousands grouping
        ("12500.00", D("12500.00")),
        ("12500", D("12500.00")),
        ("4,5", D("4.50")),
        ("4.5", D("4.50")),
        ("1.250.000", D("1250000.00")),  # repeated separator groups thousands
        ("  80,00  ", D("80.00")),
        ("80,00 €", D("80.00")),  # pasted from a rendered page
    ],
)
def test_accepts_the_formats_people_type(raw, expected):
    value, errors = dec(raw)
    assert not errors, errors.messages
    assert value == expected


@pytest.mark.parametrize("raw", ["12.500", "12,500"])
def test_refuses_to_guess_between_thousands_and_decimals(raw):
    """Twelve thousand five hundred, or twelve and a half? A factor of a thousand either way,
    so the parser asks rather than picks."""
    value, errors = dec(raw)
    assert value is None
    assert "ambiguous" in errors.get("x")


def test_empty_is_unknown_not_zero():
    """Clearing the stand area must move an opportunity back to PROVISIONAL. A zero would
    claim a stand of no size had been agreed."""
    value, errors = dec("")
    assert value is None
    assert not errors


@pytest.mark.parametrize("raw", ["abc", "12a", "--5", ",", "."])
def test_rejects_non_numbers(raw):
    value, errors = dec(raw)
    assert value is None
    assert errors.get("x")


def test_rejects_negative():
    value, errors = dec("-5")
    assert value is None
    assert "negative" in errors.get("x")


def test_collects_errors_rather_than_stopping_at_the_first():
    errors = FormErrors()
    parse_decimal("abc", "a", errors, label="Area")
    parse_decimal("12.500", "b", errors, label="Budget")
    parse_date("31/02/2026", "c", errors, label="Close date")
    assert set(errors.messages) == {"a", "b", "c"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2026-09-18", date(2026, 9, 18)), ("18/09/2026", date(2026, 9, 18)), ("", None)],
)
def test_dates(raw, expected):
    errors = FormErrors()
    assert parse_date(raw, "d", errors, label="Date") == expected
    assert not errors


def test_impossible_date_is_an_error_not_a_rollover():
    errors = FormErrors()
    assert parse_date("31/02/2026", "d", errors, label="Date") is None
    assert errors.get("d")


def test_datetime_accepts_browser_and_archive_formats():
    errors = FormErrors()
    expected = datetime(2026, 9, 18, 10, 30)
    assert parse_datetime("2026-09-18T10:30", "t", errors, label="When") == expected
    assert parse_datetime("18/09/2026 10:30", "t", errors, label="When") == expected
    assert not errors
