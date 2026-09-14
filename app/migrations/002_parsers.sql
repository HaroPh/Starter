-- Parsing helpers for the legacy export, plus the lookup tables they resolve against.
--
-- The shape of every function here is the same and it is deliberate:
--
--     CASE WHEN <input matches the documented format> THEN <cast> ELSE NULL END
--
-- A bare cast raises on bad input. The import runs as ONE transaction, so a single raised
-- cast would abort the whole thing and leave the reviewer with an empty database. Testing
-- the format first and returning NULL instead lets a companion query record an
-- import_issue and carry on. That is what "the import must survive data it has not seen"
-- actually means in code.
--
-- The date and timestamp parsers are plpgsql rather than plain SQL because PostgreSQL 17
-- raises on an out-of-range date instead of silently accepting it, and that raise has to be
-- caught rather than allowed to abort the import transaction. See the note above each one.


-- Monetary values, areas and heights: decimal comma, exactly two decimal places.
-- Empty means unknown, never zero -- see data/README.md.
CREATE OR REPLACE FUNCTION crm_parse_dec(txt text) RETURNS numeric
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
    SELECT CASE
        WHEN btrim(coalesce(txt, '')) ~ '^-?[0-9]+,[0-9]{2}$'
        THEN replace(btrim(txt), ',', '.')::numeric
        ELSE NULL
    END
    $$;

-- DD/MM/YYYY.
--
-- Two things were measured before settling on this shape.
--
-- 1. PostgreSQL 17 CHANGED to_date's behaviour. Older versions were lenient and silently
--    turned '31/02/2026' into 2026-03-03; 17 raises "date/time field value out of range".
--    Better for data quality, fatal for a single-transaction import -- one bad row would
--    abort the whole thing. So the raise has to be caught, which means plpgsql.
--
-- 2. A pure-SQL alternative that validated the day against the month's real length
--    (make_date plus interval arithmetic, needing no exception block) was written and
--    benchmarked against this one over 55,518 values:
--
--        plpgsql + EXCEPTION      ~83 ms
--        pure SQL, no EXCEPTION  ~818 ms
--
--    Ten times slower, despite avoiding the subtransaction an EXCEPTION block sets up on
--    every call. The arithmetic simply costs more than the exception machinery saves. The
--    intuition was backwards and the measurement settled it.
--
-- The regex does the cheap half of the validation. It already rejects day 00, month 00,
-- day > 31 and month > 12 -- which matters because to_date ACCEPTS those:
-- to_date('00/01/2026', 'DD/MM/YYYY') silently returns 2026-01-01. to_date is then left
-- with only the calendar-invalid cases (31/04, 29/02 in a non-leap year), which it raises
-- on and the handler turns into NULL.
CREATE OR REPLACE FUNCTION crm_parse_date(txt text) RETURNS date
    LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
    AS $fn$
DECLARE
    v text := btrim(txt);
BEGIN
    IF v !~ '^(0[1-9]|[12][0-9]|3[01])/(0[1-9]|1[0-2])/[0-9]{4}$' THEN
        RETURN NULL;
    END IF;
    RETURN to_date(v, 'DD/MM/YYYY');
EXCEPTION WHEN others THEN
    RETURN NULL;
END $fn$;

-- DD/MM/YYYY HH:mm, to be read as Europe/Rome local time.
--
-- The cast order is load-bearing. to_timestamp returns timestamptz interpreted in the
-- SESSION time zone; casting to a naive ::timestamp converts it back to the same wall-clock
-- digits; AT TIME ZONE 'Europe/Rome' then reads those digits as Rome local time. The session
-- zone cancels out, so the result is identical whether the server runs in UTC or in Rome.
--
-- Writing it the obvious way instead -- to_timestamp(...) AT TIME ZONE 'Europe/Rome' --
-- would silently shift every timestamp by the server's own offset.
CREATE OR REPLACE FUNCTION crm_parse_ts_rome(txt text) RETURNS timestamptz
    LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE
    AS $fn$
DECLARE
    v text := btrim(txt);
BEGIN
    IF v !~ '^(0[1-9]|[12][0-9]|3[01])/(0[1-9]|1[0-2])/[0-9]{4} ([01][0-9]|2[0-3]):[0-5][0-9]$' THEN
        RETURN NULL;
    END IF;
    RETURN (to_timestamp(v, 'DD/MM/YYYY HH24:MI')::timestamp) AT TIME ZONE 'Europe/Rome';
EXCEPTION WHEN others THEN
    RETURN NULL;
END $fn$;

-- Empty means unknown. Applied to every imported text column except legacy_status_raw,
-- which keeps the source bytes untouched so the UI can show what was actually in the file.
CREATE OR REPLACE FUNCTION crm_blank_to_null(txt text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT nullif(btrim(coalesce(txt, '')), '') $$;


-- ---------------------------------------------------------------------------
-- Lookup tables
--
-- Text primary keys referencing a lookup table, not a native enum. Three reasons:
--   * the importer can admit an unseen value by INSERTing a row flagged is_known = false,
--     with no ALTER TYPE and no DDL inside the import transaction;
--   * the value reads directly in every query and template, so no join is needed to
--     display it;
--   * a lookup row carries a label and a sort order, which an enum cannot.

CREATE TABLE IF NOT EXISTS opportunity_status (
    code       text PRIMARY KEY,
    label      text    NOT NULL,
    sort_order int     NOT NULL,
    is_open    boolean NOT NULL,
    is_known   boolean NOT NULL DEFAULT true
);

INSERT INTO opportunity_status (code, label, sort_order, is_open) VALUES
    ('open',      'Open',      10, true),
    ('qualified', 'Qualified', 20, true),
    ('proposal',  'Proposal',  30, true),
    ('won',       'Won',       40, false),
    ('lost',      'Lost',      50, false)
ON CONFLICT (code) DO NOTHING;

-- The export spells the five statuses thirteen different ways, varying in case and in
-- surrounding whitespace ('WON', 'Won', ' lost ', ' OPEN ').
--
-- The aliases are keyed on lower(btrim(x)), so all thirteen collapse automatically and any
-- NEW case or whitespace variant in a replaced archive also resolves with no code change.
-- Hardcoding the thirteen observed spellings would have been brittle in exactly the place
-- the assignment replaces the data.
CREATE TABLE IF NOT EXISTS opportunity_status_alias (
    alias text PRIMARY KEY,
    code  text NOT NULL REFERENCES opportunity_status(code)
);

INSERT INTO opportunity_status_alias (alias, code)
SELECT code, code FROM opportunity_status
ON CONFLICT (alias) DO NOTHING;

CREATE OR REPLACE FUNCTION crm_canon_status(txt text) RETURNS text
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$
    SELECT a.code FROM opportunity_status_alias a
    WHERE a.alias = lower(btrim(coalesce(txt, '')))
    $$;

CREATE TABLE IF NOT EXISTS activity_type (
    code       text PRIMARY KEY,
    label      text    NOT NULL,
    sort_order int     NOT NULL,
    -- A completed call, email or meeting records customer contact; a note is internal;
    -- a task is work still to do. data/README.md is explicit about this.
    is_contact boolean NOT NULL,
    is_known   boolean NOT NULL DEFAULT true
);

INSERT INTO activity_type (code, label, sort_order, is_contact) VALUES
    ('call',    'Call',    10, true),
    ('email',   'Email',   20, true),
    ('meeting', 'Meeting', 30, true),
    ('note',    'Note',    40, false),
    ('task',    'Task',    50, false)
ON CONFLICT (code) DO NOTHING;
