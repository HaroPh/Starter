"""Finding an exhibitor or a contact.

The brief puts this first: "find an exhibitor or contact and see the relevant opportunities
and fair editions". It also sets the bar that makes it interesting -- the archive is expected
to reach 100,000 contacts, and "everyday searches should remain practical at that size".

Three decisions carry that.

**Trigram, not full-text.** People search fragments: `rivamare`, `aster cos`, `CO0000`,
`chris.conti`. A tsvector index matches whole words and, at best, word prefixes; it cannot
match inside a word at all. `pg_trgm` with a GIN index does infix matching and gives
`similarity()` for ranking, which also absorbs typos. The indexed strings are short, so the
index stays small.

**A three-character floor.** Below three characters a trigram index cannot be used and the
query degrades to a sequential scan over every contact. Rather than let that happen quietly
at 100,000 rows, short terms fall back to an indexed prefix match with a hard cap.

**No pagination.** Results are capped at 50. Similarity ordering has ties and no unique
tiebreak, so it is not keyset-friendly, and searching is a "find it" action -- the answer to
too many results is a better query, not page seven. That is a deliberate choice, and it is
stated as one rather than left to look like an omission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from psycopg import Connection

MIN_TRIGRAM_LEN = 3
RESULT_CAP = 50

# Legacy identifiers in this archive: CO000001, OP000001, AC0000001, AN0000001. The pattern
# is deliberately loose -- two or more letters then a digit -- so a replaced archive using a
# different prefix still routes to an exact lookup instead of a fuzzy name search.
CODE_RE = re.compile(r"^[A-Za-z]{2,}[-_]?\d+$")


@dataclass(frozen=True)
class SearchOutcome:
    term: str
    kind: str                      # 'code' | 'email' | 'name' | 'too_short' | 'empty'
    companies: list[dict[str, Any]]
    contacts: list[dict[str, Any]]
    opportunities: list[dict[str, Any]]
    capped: bool = False
    note: str | None = None

    @property
    def total(self) -> int:
        return len(self.companies) + len(self.contacts) + len(self.opportunities)

    @property
    def single_hit(self) -> tuple[str, str] | None:
        """('companies', 'CO000002') when exactly one record matched, else None.

        Used to redirect straight to the record: typing a code and landing on a
        one-row result list is a wasted click.
        """
        if self.total != 1:
            return None
        if self.companies:
            return ("companies", self.companies[0]["legacy_code"])
        if self.contacts:
            return ("contacts", self.contacts[0]["legacy_code"])
        return ("opportunities", self.opportunities[0]["legacy_code"])


_COMPANY_COLUMNS = """
    c.id, c.legacy_code, c.name, c.province_code, c.region,
    r.display_name AS rep_name,
    (SELECT count(*) FROM opportunity o WHERE o.company_id = c.id) AS opportunity_count
"""


def find(conn: Connection, term: str) -> SearchOutcome:
    term = (term or "").strip()
    if not term:
        return SearchOutcome(term, "empty", [], [], [])

    if CODE_RE.match(term):
        return _by_code(conn, term)
    if "@" in term:
        return _by_email(conn, term)
    if len(term) < MIN_TRIGRAM_LEN:
        return _by_prefix(conn, term)
    return _by_name(conn, term)


def _by_code(conn: Connection, term: str) -> SearchOutcome:
    """Exact then prefix match on the legacy identifiers.

    Both use btree indexes declared with text_pattern_ops, which is what lets a LIKE prefix
    use an index on a database that is not in the C locale.
    """
    code = term.upper()
    pattern = code + "%"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COMPANY_COLUMNS} FROM company c "
            "LEFT JOIN sales_rep r ON r.id = c.sales_rep_id "
            "WHERE c.legacy_code LIKE %s ORDER BY c.legacy_code LIMIT %s",
            (pattern, RESULT_CAP),
        )
        companies = cur.fetchall()

        cur.execute(
            "SELECT ct.id, ct.legacy_code, ct.first_name, ct.last_name, ct.email, ct.phone,"
            " c.legacy_code AS company_code, c.name AS company_name "
            "FROM contact ct JOIN company c ON c.id = ct.company_id "
            "WHERE ct.legacy_code LIKE %s ORDER BY ct.legacy_code LIMIT %s",
            (pattern, RESULT_CAP),
        )
        contacts = cur.fetchall()

        cur.execute(
            "SELECT o.id, o.legacy_code, o.description, o.status, o.readiness,"
            " o.amount_eur, c.legacy_code AS company_code, c.name AS company_name,"
            " fe.legacy_code AS edition_code "
            "FROM opportunity o JOIN company c ON c.id = o.company_id "
            "LEFT JOIN fair_edition fe ON fe.id = o.fair_edition_id "
            "WHERE o.legacy_code LIKE %s ORDER BY o.legacy_code LIMIT %s",
            (pattern, RESULT_CAP),
        )
        opportunities = cur.fetchall()

    return SearchOutcome(term, "code", companies, contacts, opportunities)


def _by_email(conn: Connection, term: str) -> SearchOutcome:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ct.id, ct.legacy_code, ct.first_name, ct.last_name, ct.email, ct.phone,"
            " c.legacy_code AS company_code, c.name AS company_name "
            "FROM contact ct JOIN company c ON c.id = ct.company_id "
            "WHERE lower(ct.email) LIKE %s "
            "ORDER BY ct.email LIMIT %s",
            (f"%{term.lower()}%", RESULT_CAP + 1),
        )
        contacts = cur.fetchall()
    capped = len(contacts) > RESULT_CAP
    return SearchOutcome(term, "email", [], contacts[:RESULT_CAP], [], capped=capped)


def _by_prefix(conn: Connection, term: str) -> SearchOutcome:
    """One- and two-character terms.

    A trigram index needs three characters to produce a trigram, so an infix search on a
    shorter term cannot use the index and would scan every row. A prefix match on the same
    expression can still be answered from the index, so short terms get that instead, with
    an explicit note saying why the behaviour differs.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COMPANY_COLUMNS} FROM company c "
            "LEFT JOIN sales_rep r ON r.id = c.sales_rep_id "
            "WHERE search_key(c.name) LIKE search_key(%s) || '%%' "
            "ORDER BY c.name LIMIT %s",
            (term, RESULT_CAP + 1),
        )
        companies = cur.fetchall()
    capped = len(companies) > RESULT_CAP
    return SearchOutcome(
        term, "too_short", companies[:RESULT_CAP], [], [], capped=capped,
        note=(
            f"Showing names starting with {term!r}. Type at least {MIN_TRIGRAM_LEN} "
            "characters to search anywhere in a name."
        ),
    )


def _by_name(conn: Connection, term: str) -> SearchOutcome:
    """The normal path: infix trigram match on company and contact names.

    `search_key()` folds case and accents, so `societa` finds `Societa` spelled with an
    accent -- about half the company names in this archive. Ordering is by similarity first
    so the closest match is at the top, then by name for a stable order among equals.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COMPANY_COLUMNS}, similarity(search_key(c.name), search_key(%s)) AS score "
            "FROM company c LEFT JOIN sales_rep r ON r.id = c.sales_rep_id "
            "WHERE search_key(c.name) LIKE '%%' || search_key(%s) || '%%' "
            "ORDER BY score DESC, c.name, c.legacy_code LIMIT %s",
            (term, term, RESULT_CAP + 1),
        )
        companies = cur.fetchall()

        cur.execute(
            "SELECT ct.id, ct.legacy_code, ct.first_name, ct.last_name, ct.email, ct.phone,"
            " c.legacy_code AS company_code, c.name AS company_name,"
            " similarity(search_key(ct.first_name || ' ' || ct.last_name), search_key(%s)) AS score "
            "FROM contact ct JOIN company c ON c.id = ct.company_id "
            "WHERE search_key(ct.first_name || ' ' || ct.last_name) "
            "      LIKE '%%' || search_key(%s) || '%%' "
            "ORDER BY score DESC, ct.last_name, ct.legacy_code LIMIT %s",
            (term, term, RESULT_CAP + 1),
        )
        contacts = cur.fetchall()

    capped = len(companies) > RESULT_CAP or len(contacts) > RESULT_CAP
    return SearchOutcome(
        term, "name", companies[:RESULT_CAP], contacts[:RESULT_CAP], [], capped=capped,
        note=(
            f"More than {RESULT_CAP} matches; showing the closest. Add another word to narrow it."
            if capped else None
        ),
    )
