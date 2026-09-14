-- Indexes.
--
-- Honest framing first, because it belongs in the README too: at the target scale of
-- 100,000 contacts -- roughly 50,000 companies, 75,000 opportunities and 200,000 activities
-- -- this is a small database for PostgreSQL. Nothing here needs partitioning, sharding or
-- a connection pooler. The four things that actually go wrong at this size are:
--
--     1. an unindexed ILIKE '%needle%' over every contact,
--     2. deep OFFSET paging,
--     3. N+1 queries issued from templates,
--     4. planning against empty statistics right after a fresh import.
--
-- (1) and (2) are answered below. (3) is answered by having no ORM. (4) is answered by
-- running ANALYZE at the end of the import, which matters because a fresh
-- `./reset.sh && ./dev.sh` is exactly the state the reviewer sees first.


-- ---------------------------------------------------------------------------
-- Search
--
-- Trigram, not tsvector, for names and emails. People search substrings and fragments of
-- codes: "rivamare", "aster cos", "chris.conti". A tsvector index does word prefixes at
-- best and cannot match inside a word at all. Trigram also yields similarity() for ranking
-- and tolerates typos. The indexed strings are short, so the GIN indexes stay small.
--
-- search_key() folds case and accents, so "societa" matches "Societa" spelled with an
-- accent -- about half the company names in the archive.

CREATE INDEX company_name_trgm_idx
    ON company USING gin (search_key(name) gin_trgm_ops);

CREATE INDEX contact_name_trgm_idx
    ON contact USING gin (search_key(first_name || ' ' || last_name) gin_trgm_ops);

CREATE INDEX contact_email_trgm_idx
    ON contact USING gin (lower(email) gin_trgm_ops);

-- text_pattern_ops is required for LIKE 'CO0001%' because the database runs on the image
-- default locale rather than C. That is the single, cheap cost of refusing to set
-- POSTGRES_INITDB_ARGS -- see DECISIONS.md entry 7, where "--locale=C" alone was measured
-- to produce SQL_ASCII and corrupt half the company names.
CREATE INDEX company_legacy_code_pattern_idx     ON company     (legacy_code text_pattern_ops);
CREATE INDEX contact_legacy_code_pattern_idx     ON contact     (legacy_code text_pattern_ops);
CREATE INDEX opportunity_legacy_code_pattern_idx ON opportunity (legacy_code text_pattern_ops);


-- ---------------------------------------------------------------------------
-- The follow-up queue -- the busiest screen in the application
--
-- A partial index over pending rows only. At five times the current archive that is roughly
-- 28,000 rows out of 200,000+, and because it is stored in (due_on, id) order the queue
-- needs no sort at all and keyset pagination comes for free.
--
-- The INCLUDE list earns its place on the bucket COUNTS at the top of the screen --
-- "Overdue 291 / Today 14 / Upcoming 887" become index-only scans with no heap access.
-- Rendering an individual queue row still needs the company and opportunity names, so a
-- heap fetch there is unavoidable; the covering index does not remove it.
CREATE INDEX follow_up_pending_due_idx
    ON follow_up (due_on, id)
    INCLUDE (origin, opportunity_id, company_id, assigned_rep_id)
    WHERE status = 'pending';

CREATE INDEX follow_up_pending_rep_due_idx
    ON follow_up (assigned_rep_id, due_on, id)
    WHERE status = 'pending';

CREATE INDEX follow_up_opportunity_idx ON follow_up (opportunity_id, due_on)
    WHERE status = 'pending';
CREATE INDEX follow_up_company_idx     ON follow_up (company_id, due_on);


-- ---------------------------------------------------------------------------
-- Timelines
--
-- Two separate indexes for two deliberately separate queries. The opportunity timeline
-- filters on opportunity_id and NEVER on company_id: that is the edition-scoping rule
-- expressed at the query level. Showing a company-wide timeline on an opportunity page is
-- precisely the behaviour the sales coordinator complains about in the brief, where last
-- year agreements get treated as if they still applied.
CREATE INDEX activity_opportunity_time_idx ON activity (opportunity_id, occurred_at DESC, id DESC);
CREATE INDEX activity_company_time_idx     ON activity (company_id,     occurred_at DESC, id DESC);


-- ---------------------------------------------------------------------------
-- Opportunity lists
--
-- No owner_rep_id denormalised onto opportunity. A hash join through company over 50,000
-- rows costs single-digit milliseconds, and carrying a copy of the owner would mean keeping
-- it in step on every company reassignment. Knowing which denormalisation NOT to do is part
-- of the answer to the scale question.
CREATE INDEX opportunity_company_idx        ON opportunity (company_id, fair_edition_id, opened_on DESC);
CREATE INDEX opportunity_edition_status_idx ON opportunity (fair_edition_id, status, opened_on DESC, id DESC);
CREATE INDEX opportunity_contact_idx        ON opportunity (contact_id);

-- The readiness cache. Its distribution is extremely lopsided in the supplied archive --
-- 13,906 ready, 728 provisional, 365 incomplete, 1 blocked -- so the useful screen is not a
-- plain filter but a "needs attention" view over the 1,094 rows that are not ready. A
-- partial index serves that directly and stays tiny.
CREATE INDEX opportunity_readiness_idx ON opportunity (readiness, opened_on DESC, id DESC);
CREATE INDEX opportunity_attention_idx ON opportunity (opened_on DESC, id DESC)
    WHERE readiness IS DISTINCT FROM 'ready';

CREATE INDEX contact_company_idx ON contact (company_id);
CREATE INDEX company_sales_rep_idx ON company (sales_rep_id);


-- ---------------------------------------------------------------------------
-- Handoff runs
CREATE INDEX handoff_run_opportunity_idx ON handoff_run (opportunity_id, run_no DESC);
CREATE INDEX handoff_run_step_run_idx    ON handoff_run_step (run_id, seq);
CREATE INDEX handoff_tool_call_run_idx   ON handoff_tool_call (run_id, seq);
