-- Core CRM schema.
--
-- Key strategy: every entity carries a surrogate `id bigint GENERATED ALWAYS AS IDENTITY`
-- and a `legacy_code text UNIQUE`. Two reasons for not using the legacy codes as primary
-- keys directly:
--
--   * Rows the USER creates -- a logged call, a scheduled follow-up -- have no legacy code.
--     Minting fake CO/AC codes for them would pollute a namespace data/README.md calls a
--     "stable legacy identifier", and would make "did this come from the archive?"
--     unanswerable.
--   * Foreign keys become 8 bytes instead of a 14-byte varlena, and bigint comparison beats
--     collation-aware text comparison. At 200,000 activities with three FKs each, plus
--     their indexes, that is not academic.
--
-- Routing keeps the codes usable: one path parameter accepts either form, so
-- /opportunities/OP000003 works for a human and app-created rows still have a URL.


-- The six people in sales.
--
-- Deriving this table is an INTERPRETATION, not a fact in the export. Companies carry a
-- sales_rep display name ("Alex Morgan") and activities carry a legacy_author username
-- ("a.morgan"), with nothing linking the two. The importer applies the rule
--     first initial + '.' + last name, lowercased
-- and records how much evidence each row had. Where the rule is ambiguous it links NOTHING
-- and writes an import_issue rather than guessing.
CREATE TABLE sales_rep (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    legacy_username text UNIQUE,
    display_name    text NOT NULL UNIQUE,
    -- 'both' | 'name_only' | 'author_only'
    derived_from    text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE company (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    legacy_code   text UNIQUE,
    -- NOT unique, and deliberately so: CO000002 and CO000003 are both
    -- "Rivamare Packaging S.r.l." in different provinces with different reps. Every screen
    -- that lists a company therefore renders code, province and rep alongside the name.
    name          text NOT NULL,
    province_code text,
    region        text,
    sales_rep_id  bigint REFERENCES sales_rep(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE contact (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    legacy_code   text UNIQUE,
    legacy_row_id text UNIQUE,
    company_id    bigint NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    first_name    text NOT NULL,
    last_name     text NOT NULL,
    -- No UNIQUE on email. It is clean in the supplied archive (19,999 distinct values out
    -- of 19,999 non-empty), but every constraint is a way a REPLACED archive can abort the
    -- import. Constraints are kept only where the importer itself guarantees both sides.
    email         text,
    phone         text,
    -- Obsolete, and empty in 15,999 of 20,000 rows -- but it is still commercial contact
    -- data, and the brief asks to keep "the useful commercial information". Retained.
    fax           text,
    -- Obsolete PRESENTATION metadata from the old system, e.g. "FORM-2|ROW-1|ARCHIVE-A".
    -- Excluded from the working model but not discarded: dropping data silently is worse
    -- than parking it where a reviewer can inspect it. The exclusion is recorded once as an
    -- import_issue, not once per row.
    legacy_extra  jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Four fairs, sixteen editions, 2024 to 2027.
--
-- `fair` is derived from the edition-code PREFIX (BEAUTY-2027 gives BEAUTY) rather than
-- from fair_name. The code scheme is a real identifier; a display name could vary in
-- spelling between editions, and grouping on it would silently split one fair into two.
CREATE TABLE fair (
    id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code text NOT NULL UNIQUE,
    name text NOT NULL
);

CREATE TABLE fair_edition (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    legacy_code   text NOT NULL UNIQUE,
    fair_id       bigint NOT NULL REFERENCES fair(id),
    edition_label text,
    city          text,
    venue         text,
    starts_on     date,
    ends_on       date,
    -- Nullable on purpose. A replaced archive may omit it, and the handoff policy must then
    -- be able to say "the limit is unknown, so I cannot rule on this conflict" rather than
    -- assume the request is fine.
    max_stand_height_m numeric(5,2)
);

CREATE TABLE opportunity (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    legacy_code     text UNIQUE,
    company_id      bigint NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    contact_id      bigint REFERENCES contact(id) ON DELETE SET NULL,
    fair_edition_id bigint REFERENCES fair_edition(id),

    description text,
    brief_notes text,

    -- Two separate columns because data/README.md is emphatic that they are different
    -- things: amount_eur is the sales team recorded opportunity value, explicitly "not a
    -- calculated stand price or a confirmed customer budget", while client_budget_eur is
    -- the budget the customer stated. Collapsing them would destroy the distinction the
    -- handoff brief exists to present.
    amount_eur        numeric(14,2),
    client_budget_eur numeric(14,2),

    stand_area_sqm     numeric(10,2),
    -- A REQUEST, not an approval. The column name says so and the UI repeats it, because
    -- the entire technical dispute turns on that difference.
    requested_height_m numeric(5,2),

    status text REFERENCES opportunity_status(code),
    -- The source bytes, untouched, including stray whitespace such as ' OPEN '. The UI
    -- shows it on hover so a user can always see what was actually in the file.
    legacy_status_raw text,

    opened_on         date,
    expected_close_on date,
    historical_campaign_code text,

    -- Written ONLY by app/handoff/policy.py::assess(). It cannot be a generated column: a
    -- generation expression may reference only columns of the same row, and the height
    -- check needs fair_edition.max_stand_height_m.
    --
    -- Deliberately NO CHECK constraint. Changing the policy -- four states to two, or a
    -- different rule set entirely -- must stay a one-file change plus a version bump, with
    -- no migration to write and nothing to backfill by hand.
    readiness                text,
    readiness_findings       jsonb,
    readiness_policy_version text,
    readiness_computed_at    timestamptz,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    -- Redundant on its own, since id is already unique. It exists to be the target of the
    -- composite foreign keys below. See the note on activity.
    CONSTRAINT opportunity_id_company_uk UNIQUE (id, company_id)
);

-- ONE table with a type discriminator, not five tables.
--
--   * The five types share 100% of their source columns. There is no type-specific
--     attribute anywhere in activity_log.csv, so separate tables would be pure duplication.
--   * Both hot queries are cross-type: the opportunity timeline and the company timeline,
--     each ordered by occurred_at. Separate tables would force a UNION ALL and forfeit the
--     index-ordered scan that makes keyset pagination free.
--   * 200,000 rows at five times the current archive needs no partitioning.
CREATE TABLE activity (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    legacy_code    text UNIQUE,
    company_id     bigint NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    -- Nullable: 5,001 of the 40,000 entries are attached to the company rather than to any
    -- opportunity. Those appear in a separate company-notes tab and are never mixed into an
    -- opportunity timeline.
    opportunity_id bigint,
    activity_type  text NOT NULL REFERENCES activity_type(code),
    occurred_at    timestamptz NOT NULL,
    details        text NOT NULL DEFAULT '',
    author_rep_id  bigint REFERENCES sales_rep(id),
    -- Y / N / empty in the source. Kept verbatim for provenance; the working state lives on
    -- follow_up.status. In the supplied archive it is perfectly correlated with the type
    -- (every task is N, every note is empty, every call, email and meeting is Y), so it
    -- carries no information the type does not. That is an observation about this export,
    -- not a guarantee about the next one, so the column stays.
    legacy_completion_marker char(1),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),

    -- The edition-scoping invariant made declarative: an activity can never be attached to
    -- an opportunity belonging to a different company. Both sides are computed by the
    -- importer, so a hostile archive cannot abort the load on this -- the importer sets
    -- opportunity_id to NULL and records an issue instead.
    CONSTRAINT activity_opportunity_same_company
        FOREIGN KEY (opportunity_id, company_id)
        REFERENCES opportunity (id, company_id) ON DELETE SET NULL
);

-- A follow-up is a work item with a lifecycle, not a log line.
--
-- In the source, follow_up_on and completion_marker sit on the same row, but they describe
-- different things. completion_marker = 'Y' on a call means THE CALL HAPPENED; a
-- follow_up_on on that same row means SOMEONE STILL OWES THE CUSTOMER SOMETHING. Collapsing
-- the two makes "mark this follow-up done" impossible without lying about the call.
--
-- The brief describes the requirement in exactly those terms: "If a customer promises to
-- confirm the floor area on Friday, that needs to turn into something they can find and act
-- on." That is a task with a due date and a status, so it gets its own table.
CREATE TABLE follow_up (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_activity_id bigint REFERENCES activity(id) ON DELETE SET NULL,
    company_id         bigint NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    opportunity_id     bigint,
    due_on             date NOT NULL,
    status             text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'done', 'cancelled')),
    -- Which reading of the archive produced this row. Two definitions of "an open
    -- follow-up" are defensible:
    --   narrow -- only a task marked N is open work: 1,192 rows, 291 overdue at the archive
    --             reference date. This is what data/README.md literally says.
    --   broad  -- any entry carrying a follow_up_on is an outstanding promise: 5,718 rows.
    -- The queue defaults to broad, because that matches the sentence in the brief, and a
    -- facet narrows it to origin = 'task'. Recording the origin keeps the choice reversible
    -- in the UI instead of baking it into the import.
    origin             text NOT NULL
                       CHECK (origin IN ('task', 'interaction', 'note', 'manual')),
    assigned_rep_id    bigint REFERENCES sales_rep(id),
    note               text,
    completed_at       timestamptz,
    completed_activity_id bigint REFERENCES activity(id) ON DELETE SET NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT follow_up_opportunity_same_company
        FOREIGN KEY (opportunity_id, company_id)
        REFERENCES opportunity (id, company_id) ON DELETE SET NULL
);
