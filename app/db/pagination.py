"""Keyset pagination.

`OFFSET n` makes the database walk and discard n rows before returning anything, so page 50
costs fifty times page 1. It is also wrong under concurrent writes: insert a row while
someone is paging and they see a duplicate or miss an entry.

Keyset paging carries the sort key of the last row seen and asks for "the next ones after
this". Cost is constant, and because every list here is ordered by a column pair that already
has an index, the database walks straight to the position with no sort at all.

The cursor is opaque base64 so it does not invite hand-editing, NOT because it is secret --
it holds a timestamp and a row id, both of which the page already displays. A tampered or
stale cursor returns the first page rather than an error: a broken bookmark should show
something useful, not a 500.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def encode_cursor(values: tuple[Any, ...]) -> str:
    """Encode a sort-key tuple. Dates and datetimes go through ISO-8601."""
    payload = [v.isoformat() if isinstance(v, date | datetime) else v for v in values]
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> list[Any] | None:
    """Decode a cursor, or return None for anything that is not one.

    Every failure mode -- absent, truncated, not base64, not JSON, not a list -- lands here
    and yields None, which callers read as "start at the beginning".
    """
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:  # noqa: BLE001 -- a bad cursor is a page-1 request, not an error
        return None
    return payload if isinstance(payload, list) else None


def clamp_page_size(requested: int | None, default: int = DEFAULT_PAGE_SIZE) -> int:
    if not requested or requested < 1:
        return default
    return min(requested, MAX_PAGE_SIZE)


@dataclass(frozen=True)
class Page[T]:
    """One page of results plus the cursor that follows it."""

    items: list[T] = field(default_factory=list)
    next_cursor: str | None = None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


def build_page[T](rows: list[T], page_size: int, key: Any) -> Page[T]:
    """Trim an over-fetched result set into a page.

    Callers ask for `page_size + 1` rows. If the extra row came back there is another page,
    and the cursor is built from the last row that is actually shown. This is how "is there
    more?" is answered without a COUNT -- which at 200,000 activities would cost more than
    the page itself.
    """
    if len(rows) <= page_size:
        return Page(items=rows, next_cursor=None)
    visible = rows[:page_size]
    return Page(items=visible, next_cursor=encode_cursor(key(visible[-1])))
