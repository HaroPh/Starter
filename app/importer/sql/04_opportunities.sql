-- Opportunities.
--
-- Every reference is resolved by an OUTER join and every integrity problem becomes an
-- import_issue rather than a failed constraint. The supplied archive has zero orphans of any
-- kind -- that was checked -- but the archive is replaced before review, so the checks are
-- the deliverable and the counts are incidental.
--
-- The one reference that cannot degrade is the owning company: an opportunity with no
-- company has nowhere to live and no way to be found, so those rows are skipped and
-- reported as errors. A missing contact or fair edition merely leaves a column NULL, and
-- the handoff policy is built to say "the fair edition is unknown" rather than assume.

CREATE TEMP TABLE tmp_opp ON COMMIT DROP AS
SELECT
    btrim(s.opportunity_code)               AS opportunity_code,
    btrim(s.company_code)                   AS company_code,
    crm_blank_to_null(s.contact_code)       AS contact_code,
    crm_blank_to_null(s.fair_edition_code)  AS fair_edition_code,
    c.id                                    AS company_id,
    ct.id                                   AS contact_id,
    ct.company_id                           AS contact_company_id,
    fe.id                                   AS fair_edition_id,
    crm_blank_to_null(s.description)        AS description,
    crm_blank_to_null(s.brief_notes)        AS brief_notes,
    crm_parse_dec(s.amount_eur)             AS amount_eur,
    crm_parse_dec(s.client_budget_eur)      AS client_budget_eur,
    crm_parse_dec(s.stand_area_sqm)         AS stand_area_sqm,
    crm_parse_dec(s.requested_height_m)     AS requested_height_m,
    crm_canon_status(s.legacy_status)       AS status,
    s.legacy_status                         AS legacy_status_raw,
    crm_parse_date(s.opened_on)             AS opened_on,
    crm_parse_date(s.expected_close_on)     AS expected_close_on,
    crm_blank_to_null(s.historical_campaign_code) AS historical_campaign_code,
    -- Raw values kept alongside so the issue rows can quote what was actually in the file.
    s.amount_eur         AS amount_raw,
    s.client_budget_eur  AS budget_raw,
    s.stand_area_sqm     AS area_raw,
    s.requested_height_m AS height_raw,
    s.opened_on          AS opened_raw,
    s.expected_close_on  AS close_raw
FROM stg_opportunities s
LEFT JOIN company      c  ON c.legacy_code  = btrim(s.company_code)
LEFT JOIN contact      ct ON ct.legacy_code = crm_blank_to_null(s.contact_code)
LEFT JOIN fair_edition fe ON fe.legacy_code = crm_blank_to_null(s.fair_edition_code)
WHERE crm_blank_to_null(s.opportunity_code) IS NOT NULL;

INSERT INTO opportunity (
    legacy_code, company_id, contact_id, fair_edition_id,
    description, brief_notes,
    amount_eur, client_budget_eur, stand_area_sqm, requested_height_m,
    status, legacy_status_raw, opened_on, expected_close_on, historical_campaign_code)
SELECT DISTINCT ON (t.opportunity_code)
       t.opportunity_code, t.company_id,
       -- A contact belonging to a DIFFERENT company is dropped rather than trusted. Showing
       -- one exhibitor a contact from another would be a confidentiality problem, not just
       -- a data one.
       CASE WHEN t.contact_company_id = t.company_id THEN t.contact_id END,
       t.fair_edition_id,
       t.description, t.brief_notes,
       t.amount_eur, t.client_budget_eur, t.stand_area_sqm, t.requested_height_m,
       t.status, t.legacy_status_raw, t.opened_on, t.expected_close_on,
       t.historical_campaign_code
FROM tmp_opp t
WHERE t.company_id IS NOT NULL
ORDER BY t.opportunity_code
ON CONFLICT (legacy_code) DO NOTHING;


-- ---------------------------------------------------------------------------
-- Integrity reports. Each is an anti-join, never a constraint that could abort the load.

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'error', 'orphan_company', 'opportunities.csv',
       t.opportunity_code, 'company_code', t.company_code,
       'No company with this code exists, so the opportunity was skipped: it would have no '
       'owner and no way to be found.'
FROM tmp_opp t WHERE t.company_id IS NULL;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'warning', 'orphan_contact', 'opportunities.csv',
       t.opportunity_code, 'contact_code', t.contact_code,
       'No contact with this code exists. The opportunity was imported with no primary '
       'contact.'
FROM tmp_opp t
WHERE t.company_id IS NOT NULL AND t.contact_code IS NOT NULL AND t.contact_id IS NULL;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'warning', 'contact_wrong_company', 'opportunities.csv',
       t.opportunity_code, 'contact_code', t.contact_code,
       'The primary contact belongs to a different company than the opportunity. The link '
       'was dropped rather than surfacing one exhibitor a contact belonging to another.'
FROM tmp_opp t
WHERE t.company_id IS NOT NULL AND t.contact_id IS NOT NULL
  AND t.contact_company_id IS DISTINCT FROM t.company_id;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'warning', 'orphan_fair_edition', 'opportunities.csv',
       t.opportunity_code, 'fair_edition_code', coalesce(t.fair_edition_code, '(empty)'),
       'No fair edition with this code exists. The opportunity was imported without one; '
       'the handoff policy treats a missing edition as incomplete rather than guessing.'
FROM tmp_opp t
WHERE t.company_id IS NOT NULL AND t.fair_edition_id IS NULL;

-- An unrecognised status is kept verbatim rather than forced into one of the five. The UI
-- renders it as "Unknown (imported as ...)" so a reviewer can see exactly what arrived.
INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'warning', 'unmapped_status', 'opportunities.csv',
       t.opportunity_code, 'legacy_status', t.legacy_status_raw,
       'This status spelling does not resolve to any of the five known statuses. The raw '
       'value was kept and the canonical status left unset.'
FROM tmp_opp t
WHERE t.company_id IS NOT NULL
  AND crm_blank_to_null(t.legacy_status_raw) IS NOT NULL
  AND t.status IS NULL;

-- Numbers and dates that carried a value but did not match the documented format. Empty is
-- unknown and is NOT reported; a non-empty value that failed to parse is a real loss and is.
INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'warning', 'unparsed_number', 'opportunities.csv', t.opportunity_code,
       v.col, v.raw,
       'Value present but not in the documented format (decimal comma, exactly two decimal '
       'places). Stored as unknown.'
FROM tmp_opp t
CROSS JOIN LATERAL (VALUES
    ('amount_eur',         t.amount_raw, t.amount_eur),
    ('client_budget_eur',  t.budget_raw, t.client_budget_eur),
    ('stand_area_sqm',     t.area_raw,   t.stand_area_sqm),
    ('requested_height_m', t.height_raw, t.requested_height_m)
) AS v(col, raw, parsed)
WHERE t.company_id IS NOT NULL
  AND crm_blank_to_null(v.raw) IS NOT NULL
  AND v.parsed IS NULL;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'warning', 'unparsed_date', 'opportunities.csv', t.opportunity_code,
       v.col, v.raw,
       'Value present but not a valid DD/MM/YYYY date. Stored as unknown.'
FROM tmp_opp t
CROSS JOIN LATERAL (VALUES
    ('opened_on',         t.opened_raw, t.opened_on),
    ('expected_close_on', t.close_raw,  t.expected_close_on)
) AS v(col, raw, parsed)
WHERE t.company_id IS NOT NULL
  AND crm_blank_to_null(v.raw) IS NOT NULL
  AND v.parsed IS NULL;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref, message)
SELECT %(run_id)s, 'warning', 'duplicate_opportunity_code', 'opportunities.csv',
       t.opportunity_code,
       'This opportunity code appears more than once. The first occurrence was used.'
FROM tmp_opp t
GROUP BY t.opportunity_code
HAVING count(*) > 1;


-- ---------------------------------------------------------------------------
-- Interpretations worth stating even though nothing went wrong.

INSERT INTO import_issue (import_run_id, severity, kind, source_file, message, details)
SELECT %(run_id)s, 'info', 'interpretation', 'opportunities.csv',
       'amount_eur and client_budget_eur are kept as separate columns. data/README.md is '
       'explicit that the first is the sales team recorded value and "not a calculated stand '
       'price or a confirmed customer budget", while the second is what the customer stated. '
       'The handoff brief presents both, because a technical reader needs to know which '
       'figure is the constraint.',
       jsonb_build_object(
           'both_present', count(*) FILTER (WHERE amount_eur IS NOT NULL AND client_budget_eur IS NOT NULL),
           'budget_unknown', count(*) FILTER (WHERE client_budget_eur IS NULL))
FROM opportunity;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, column_name, message)
VALUES (%(run_id)s, 'info', 'interpretation', 'opportunities.csv', 'requested_height_m',
        'Treated as a REQUEST throughout, never an approval. data/README.md says a requested '
        'height may exceed the edition limit and that both values must be kept, so the height '
        'is stored as given and compared against the edition limit by the handoff policy '
        'rather than being clamped or rejected at import time.');

INSERT INTO import_issue (import_run_id, severity, kind, source_file, column_name, message, details)
SELECT %(run_id)s, 'info', 'interpretation', 'opportunities.csv', 'legacy_status',
       'Status spellings vary in case and surrounding whitespace. They are canonicalised '
       'through an alias table keyed on lower(btrim(value)), so every case and whitespace '
       'variant resolves without code changes. The raw value is kept on the row.',
       jsonb_build_object('distinct_raw_spellings',
                          (SELECT count(DISTINCT legacy_status_raw) FROM opportunity),
                          'canonical_statuses',
                          (SELECT count(DISTINCT status) FROM opportunity WHERE status IS NOT NULL));
