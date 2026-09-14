-- The activity log.
--
-- 40,000 rows, of which 5,001 are attached to a company but to no opportunity. Those are
-- not defects: they are company-level notes, and they get their own tab in the UI rather
-- than being forced into an opportunity timeline where they would break edition scoping.
--
-- This is also the file carrying the quoted-delimiter trap: 2,654 rows have a semicolon
-- inside the quoted `details` field. Nothing in this SQL has to care, because COPY already
-- parsed it correctly -- but that is only true because the loader uses a real CSV reader.
-- See app/importer/copy.py.

-- An activity type the lookup has never seen is admitted rather than rejected, flagged so
-- the UI can mark it and the import report can list it. This is why activity_type is a
-- lookup table and not a native enum: adding a value here needs no ALTER TYPE, which cannot
-- run inside the import transaction without risk.
INSERT INTO activity_type (code, label, sort_order, is_contact, is_known)
SELECT DISTINCT lower(btrim(s.activity_type)),
       initcap(btrim(s.activity_type)),
       900,
       false,
       false
FROM stg_activities s
WHERE crm_blank_to_null(s.activity_type) IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM activity_type t WHERE t.code = lower(btrim(s.activity_type)))
ON CONFLICT (code) DO NOTHING;

CREATE TEMP TABLE tmp_act ON COMMIT DROP AS
SELECT
    btrim(s.entry_id)                     AS entry_id,
    btrim(s.company_code)                 AS company_code,
    crm_blank_to_null(s.opportunity_code) AS opportunity_code,
    c.id                                  AS company_id,
    o.id                                  AS opportunity_id,
    o.company_id                          AS opportunity_company_id,
    lower(btrim(s.activity_type))         AS activity_type,
    crm_parse_ts_rome(s.occurred_at)      AS occurred_at,
    s.occurred_at                         AS occurred_raw,
    coalesce(s.details, '')               AS details,
    crm_parse_date(s.follow_up_on)        AS follow_up_on,
    s.follow_up_on                        AS follow_up_raw,
    upper(crm_blank_to_null(s.completion_marker)) AS completion_marker,
    r.id                                  AS author_rep_id
FROM stg_activities s
LEFT JOIN company     c ON c.legacy_code = btrim(s.company_code)
LEFT JOIN opportunity o ON o.legacy_code = crm_blank_to_null(s.opportunity_code)
LEFT JOIN sales_rep   r ON r.legacy_username = crm_blank_to_null(s.legacy_author)
WHERE crm_blank_to_null(s.entry_id) IS NOT NULL;

INSERT INTO activity (legacy_code, company_id, opportunity_id, activity_type, occurred_at,
                      details, author_rep_id, legacy_completion_marker)
SELECT DISTINCT ON (t.entry_id)
       t.entry_id,
       t.company_id,
       -- Dropped if the opportunity belongs to another company. The composite foreign key
       -- on activity would reject that row outright, and a rejected row would abort the
       -- single import transaction -- so the importer resolves it here and records an issue
       -- instead. The constraint stays as the guarantee; this is what keeps the load from
       -- ever having to test it.
       CASE WHEN t.opportunity_company_id = t.company_id THEN t.opportunity_id END,
       coalesce(t.activity_type, 'note'),
       -- A row with no usable timestamp still carries a customer conversation, so it is
       -- kept and dated to the archive reference time rather than discarded. The issue row
       -- below records that substitution.
       coalesce(t.occurred_at, timestamptz '2026-09-01 09:00+02'),
       t.details,
       t.author_rep_id,
       left(t.completion_marker, 1)
FROM tmp_act t
WHERE t.company_id IS NOT NULL
ORDER BY t.entry_id
ON CONFLICT (legacy_code) DO NOTHING;


-- ---------------------------------------------------------------------------
-- Integrity reports

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'error', 'orphan_company', 'activity_log.csv',
       t.entry_id, 'company_code', t.company_code,
       'No company with this code exists, so the log entry was skipped.'
FROM tmp_act t WHERE t.company_id IS NULL;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'warning', 'orphan_opportunity', 'activity_log.csv',
       t.entry_id, 'opportunity_code', t.opportunity_code,
       'No opportunity with this code exists. The entry was kept on the company timeline '
       'rather than discarded -- a customer conversation is worth more than its link.'
FROM tmp_act t
WHERE t.company_id IS NOT NULL AND t.opportunity_code IS NOT NULL AND t.opportunity_id IS NULL;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'warning', 'activity_wrong_company', 'activity_log.csv',
       t.entry_id, 'opportunity_code', t.opportunity_code,
       'The referenced opportunity belongs to a different company. The entry was kept on the '
       'company it names, with no opportunity link.'
FROM tmp_act t
WHERE t.company_id IS NOT NULL AND t.opportunity_id IS NOT NULL
  AND t.opportunity_company_id IS DISTINCT FROM t.company_id;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'warning', 'unparsed_timestamp', 'activity_log.csv',
       t.entry_id, 'occurred_at', t.occurred_raw,
       'Not a valid DD/MM/YYYY HH:mm value. The entry was kept and dated to the archive '
       'reference time, since the conversation itself is still evidence.'
FROM tmp_act t
WHERE t.company_id IS NOT NULL AND t.occurred_at IS NULL;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'warning', 'unparsed_date', 'activity_log.csv',
       t.entry_id, 'follow_up_on', t.follow_up_raw,
       'A follow-up date was present but unparseable, so no follow-up could be created from '
       'this entry.'
FROM tmp_act t
WHERE t.company_id IS NOT NULL
  AND crm_blank_to_null(t.follow_up_raw) IS NOT NULL
  AND t.follow_up_on IS NULL;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, column_name,
                          raw_value, message)
SELECT %(run_id)s, 'warning', 'unknown_activity_type', 'activity_log.csv', 'activity_type',
       t.code,
       'An activity type outside the five documented values. Admitted and flagged rather '
       'than dropped, so the entry survives and the UI can mark it.'
FROM activity_type t WHERE t.is_known = false;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, message)
SELECT %(run_id)s, 'warning', 'unmatched_author', 'activity_log.csv',
       min(t.entry_id), 'legacy_author',
       'This author could not be matched to a sales rep, so the entry shows no author.'
FROM tmp_act t
WHERE t.company_id IS NOT NULL AND t.author_rep_id IS NULL
GROUP BY t.entry_id
HAVING count(*) > 0;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, message, details)
SELECT %(run_id)s, 'info', 'observation', 'activity_log.csv',
       'Some log entries are attached to a company but to no opportunity. They are shown in '
       'a separate company-notes tab and are never mixed into an opportunity timeline, '
       'because an opportunity page must show one fair edition only.',
       jsonb_build_object('company_level_entries', count(*) FILTER (WHERE opportunity_id IS NULL),
                          'total', count(*))
FROM activity;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, column_name, message, details)
SELECT %(run_id)s, 'info', 'observation', 'activity_log.csv', 'completion_marker',
       'In this archive the completion marker is perfectly determined by the activity type: '
       'every task is N, every note is empty, and every call, email and meeting is Y. It is '
       'kept verbatim for provenance, but the working state of a follow-up lives on '
       'follow_up.status instead -- the marker describes whether the CONVERSATION happened, '
       'not whether the promise it created has been kept.',
       (SELECT jsonb_object_agg(activity_type, marks) FROM (
            SELECT activity_type,
                   jsonb_object_agg(coalesce(legacy_completion_marker, '(empty)'), n) AS marks
            FROM (SELECT activity_type, legacy_completion_marker, count(*) AS n
                  FROM activity GROUP BY 1, 2) x
            GROUP BY activity_type) y);
