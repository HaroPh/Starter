-- Reference data: the sales team, the fairs, and their editions.
--
-- Executed with a named parameter %(run_id)s so every interpretation lands in import_issue
-- attached to the run that made it.


-- ---------------------------------------------------------------------------
-- The sales team
--
-- This table does not exist in the export. Companies carry a display name in `sales_rep`
-- ("Alex Morgan") and activities carry a username in `legacy_author` ("a.morgan"), with
-- nothing joining them. Linking the two is an INTERPRETATION and is recorded as one.
--
-- The rule -- first initial, a dot, last name, lowercased -- is applied algorithmically
-- rather than by hardcoding the six people who happen to be in this archive. A replaced
-- archive with different or additional staff therefore still works.
--
-- Where the rule is AMBIGUOUS the importer links nothing. If a future archive contained
-- both "Jamie Chen" and "John Chen", both would derive j.chen; silently attaching every
-- j.chen activity to one of them would be worse than leaving the author unlinked, because
-- the error would be invisible and would misattribute customer contact.

CREATE TEMP TABLE tmp_rep_names ON COMMIT DROP AS
SELECT DISTINCT
    btrim(sales_rep) AS display_name,
    lower(left(split_part(btrim(sales_rep), ' ', 1), 1)) || '.' ||
    lower((string_to_array(btrim(sales_rep), ' '))[
        cardinality(string_to_array(btrim(sales_rep), ' '))
    ]) AS derived_username
FROM stg_companies_contacts
WHERE crm_blank_to_null(sales_rep) IS NOT NULL;

-- Usernames produced by more than one display name, or display names that collide on one
-- username. Either way the link is not safe to make.
CREATE TEMP TABLE tmp_rep_ambiguous ON COMMIT DROP AS
SELECT derived_username
FROM tmp_rep_names
GROUP BY derived_username
HAVING count(*) > 1;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, message, details)
SELECT %(run_id)s, 'warning', 'ambiguous_author_mapping', 'companies_and_contacts.csv',
       'Two or more sales rep names derive the same legacy username, so no activity author '
       'link was made for them. Their activities keep legacy_author but have no rep.',
       jsonb_build_object('username', a.derived_username,
                          'display_names', (SELECT jsonb_agg(n.display_name)
                                            FROM tmp_rep_names n
                                            WHERE n.derived_username = a.derived_username))
FROM tmp_rep_ambiguous a;

-- Reps known by display name. The username is attached only when unambiguous.
INSERT INTO sales_rep (legacy_username, display_name, derived_from)
SELECT CASE WHEN a.derived_username IS NULL THEN n.derived_username END,
       n.display_name,
       CASE WHEN a.derived_username IS NULL THEN 'both' ELSE 'name_only' END
FROM tmp_rep_names n
LEFT JOIN tmp_rep_ambiguous a ON a.derived_username = n.derived_username
ON CONFLICT (display_name) DO NOTHING;

-- Authors seen in the activity log that no display name accounts for. They get a row so
-- their activities can still be attributed, with the username standing in as the name and
-- `derived_from` recording that this is all the archive knew.
INSERT INTO sales_rep (legacy_username, display_name, derived_from)
SELECT DISTINCT btrim(a.legacy_author), btrim(a.legacy_author), 'author_only'
FROM stg_activities a
WHERE crm_blank_to_null(a.legacy_author) IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM sales_rep r WHERE r.legacy_username = btrim(a.legacy_author))
  AND NOT EXISTS (SELECT 1 FROM sales_rep r WHERE r.display_name = btrim(a.legacy_author))
ON CONFLICT (display_name) DO NOTHING;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, message, details)
SELECT %(run_id)s, 'info', 'interpretation', 'companies_and_contacts.csv',
       'sales_rep and legacy_author were linked by the rule "first initial + dot + last '
       'name, lowercased". The export provides no explicit key between them.',
       jsonb_build_object(
           'linked',      (SELECT count(*) FROM sales_rep WHERE derived_from = 'both'),
           'name_only',   (SELECT count(*) FROM sales_rep WHERE derived_from = 'name_only'),
           'author_only', (SELECT count(*) FROM sales_rep WHERE derived_from = 'author_only'));


-- ---------------------------------------------------------------------------
-- Fairs and editions
--
-- A fair is derived from the edition-code PREFIX (BEAUTY-2027 gives BEAUTY), not from
-- fair_name. The code scheme is a real identifier; a display name could vary in spelling
-- between editions, and grouping on it would silently split one fair in two. Where a code
-- has no separator the whole code is used as the key.

CREATE TEMP TABLE tmp_editions ON COMMIT DROP AS
SELECT
    btrim(fair_edition_code) AS edition_code,
    CASE WHEN position('-' IN btrim(fair_edition_code)) > 0
         THEN split_part(btrim(fair_edition_code), '-', 1)
         ELSE btrim(fair_edition_code) END AS fair_code,
    CASE WHEN position('-' IN btrim(fair_edition_code)) > 0
         THEN substring(btrim(fair_edition_code) FROM position('-' IN btrim(fair_edition_code)) + 1)
         END AS edition_label,
    crm_blank_to_null(fair_name)  AS fair_name,
    crm_blank_to_null(city)       AS city,
    crm_blank_to_null(venue)      AS venue,
    crm_parse_date(starts_on)     AS starts_on,
    crm_parse_date(ends_on)       AS ends_on,
    crm_parse_dec(max_stand_height_m) AS max_stand_height_m,
    max_stand_height_m            AS max_stand_height_raw
FROM stg_fair_editions
WHERE crm_blank_to_null(fair_edition_code) IS NOT NULL;

-- One fair code carrying several display names is not fatal, but it is worth knowing about:
-- the most recent edition wins and the variants are recorded.
INSERT INTO import_issue (import_run_id, severity, kind, source_file, column_name, message, details)
SELECT %(run_id)s, 'warning', 'fair_name_varies', 'fair_editions.csv', 'fair_name',
       'One fair code appears with more than one display name. The latest edition name was '
       'used for the fair.',
       jsonb_build_object('fair_code', fair_code,
                          'names', jsonb_agg(DISTINCT fair_name))
FROM tmp_editions
GROUP BY fair_code
HAVING count(DISTINCT fair_name) > 1;

INSERT INTO fair (code, name)
SELECT DISTINCT ON (fair_code)
       fair_code,
       coalesce(fair_name, fair_code)
FROM tmp_editions
ORDER BY fair_code, starts_on DESC NULLS LAST, edition_code DESC
ON CONFLICT (code) DO NOTHING;

INSERT INTO fair_edition (legacy_code, fair_id, edition_label, city, venue,
                          starts_on, ends_on, max_stand_height_m)
SELECT e.edition_code, f.id, e.edition_label, e.city, e.venue,
       e.starts_on, e.ends_on, e.max_stand_height_m
FROM tmp_editions e
JOIN fair f ON f.code = e.fair_code
ON CONFLICT (legacy_code) DO NOTHING;

-- A height limit that would not parse matters more than most unparseable values, because
-- the handoff policy rules on it. Recorded explicitly rather than left as a silent NULL.
INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message)
SELECT %(run_id)s, 'warning', 'unparsed_number', 'fair_editions.csv', e.edition_code,
       'max_stand_height_m', e.max_stand_height_raw,
       'The edition height limit could not be parsed as a decimal-comma number. The handoff '
       'policy will treat the limit as unknown and will not rule on a height conflict.'
FROM tmp_editions e
WHERE crm_blank_to_null(e.max_stand_height_raw) IS NOT NULL
  AND e.max_stand_height_m IS NULL;
