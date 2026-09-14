-- Staging tables: every column is text, every table is UNLOGGED.
--
-- Text columns are the whole point. The alternative -- typed columns, so the database
-- converts during COPY -- means a single malformed value aborts the load with a message
-- naming a byte offset rather than a row, and because the import is one transaction it
-- takes everything else with it. Loading as text always succeeds; the transforms then apply
-- the parsers from migration 002, which return NULL and record an issue instead of raising.
--
-- UNLOGGED skips WAL for these tables. They are rebuilt from the archive on every import and
-- dropped at the end, so durability buys nothing and the write volume is halved.
--
-- Column names match the CSV headers exactly. The importer builds its COPY column list from
-- the header line it actually reads, so a reordered or extended file still loads correctly
-- -- see app/importer/copy.py.

DROP TABLE IF EXISTS stg_companies_contacts;
DROP TABLE IF EXISTS stg_opportunities;
DROP TABLE IF EXISTS stg_activities;
DROP TABLE IF EXISTS stg_fair_editions;

CREATE UNLOGGED TABLE stg_companies_contacts (
    legacy_row_id       text,
    company_code        text,
    company_name        text,
    province_code       text,
    region              text,
    sales_rep           text,
    contact_code        text,
    contact_first_name  text,
    contact_last_name   text,
    email               text,
    phone               text,
    fax                 text,
    legacy_print_layout text
);

CREATE UNLOGGED TABLE stg_opportunities (
    opportunity_code         text,
    company_code             text,
    contact_code             text,
    description              text,
    amount_eur               text,
    legacy_status            text,
    opened_on                text,
    expected_close_on        text,
    historical_campaign_code text,
    fair_edition_code        text,
    stand_area_sqm           text,
    client_budget_eur        text,
    requested_height_m       text,
    brief_notes              text
);

CREATE UNLOGGED TABLE stg_activities (
    entry_id          text,
    company_code      text,
    opportunity_code  text,
    activity_type     text,
    occurred_at       text,
    details           text,
    follow_up_on      text,
    completion_marker text,
    legacy_author     text
);

CREATE UNLOGGED TABLE stg_fair_editions (
    fair_edition_code  text,
    fair_name          text,
    city               text,
    venue              text,
    starts_on          text,
    ends_on            text,
    max_stand_height_m text
);
