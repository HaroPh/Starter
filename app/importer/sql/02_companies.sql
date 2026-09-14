-- Companies, derived from the 20,000 contact rows.
--
-- companies_and_contacts.csv is one row per CONTACT, with the company columns repeated for
-- every contact that company has. Ten thousand companies therefore arrive spread across
-- twenty thousand rows, and the importer has to collapse them.
--
-- The interesting part is what happens when the repeated columns DISAGREE -- when two rows
-- claiming the same company_code carry different names, provinces or reps. In the supplied
-- archive that never happens: 10,000 codes produce exactly 10,000 distinct attribute
-- tuples. But the archive is replaced before review, so the detector runs anyway and the
-- resolution is deterministic rather than accidental.


-- Which company codes carry conflicting attributes across their rows.
CREATE TEMP TABLE tmp_company_conflicts ON COMMIT DROP AS
SELECT company_code
FROM stg_companies_contacts
WHERE crm_blank_to_null(company_code) IS NOT NULL
GROUP BY company_code
HAVING count(DISTINCT (crm_blank_to_null(company_name),
                       crm_blank_to_null(province_code),
                       crm_blank_to_null(region),
                       crm_blank_to_null(sales_rep))) > 1;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref, message, details)
SELECT %(run_id)s, 'warning', 'company_attributes_conflict', 'companies_and_contacts.csv',
       c.company_code,
       'Rows sharing this company code disagree on the company attributes. The row with the '
       'lowest legacy_row_id was used, which is deterministic but arbitrary.',
       (SELECT jsonb_agg(DISTINCT jsonb_build_object(
                   'name', crm_blank_to_null(s.company_name),
                   'province', crm_blank_to_null(s.province_code),
                   'region', crm_blank_to_null(s.region),
                   'rep', crm_blank_to_null(s.sales_rep)))
        FROM stg_companies_contacts s WHERE s.company_code = c.company_code)
FROM tmp_company_conflicts c;

-- DISTINCT ON with an explicit ORDER BY makes "first row wins" a rule rather than whatever
-- order the scan happened to produce. legacy_row_id is the export order, so the earliest
-- occurrence is what lands.
INSERT INTO company (legacy_code, name, province_code, region, sales_rep_id)
SELECT DISTINCT ON (s.company_code)
       btrim(s.company_code),
       coalesce(crm_blank_to_null(s.company_name), btrim(s.company_code)),
       crm_blank_to_null(s.province_code),
       crm_blank_to_null(s.region),
       r.id
FROM stg_companies_contacts s
LEFT JOIN sales_rep r ON r.display_name = btrim(s.sales_rep)
WHERE crm_blank_to_null(s.company_code) IS NOT NULL
ORDER BY s.company_code, s.legacy_row_id
ON CONFLICT (legacy_code) DO NOTHING;

-- A contact row with no company code cannot be attached to anything, so it is dropped --
-- but loudly, with the row identifier, so the loss is visible rather than inferred from a
-- count that does not add up.
INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, message)
SELECT %(run_id)s, 'error', 'missing_company_code', 'companies_and_contacts.csv',
       coalesce(crm_blank_to_null(s.legacy_row_id), '(no legacy_row_id)'), 'company_code',
       'Row has no company code, so neither a company nor a contact could be created from it.'
FROM stg_companies_contacts s
WHERE crm_blank_to_null(s.company_code) IS NULL;

-- Company names are NOT unique, and that is a property of the data rather than a defect:
-- CO000002 and CO000003 are both "Rivamare Packaging S.r.l." in different provinces with
-- different reps. Recorded because it drives a UI decision -- every list that shows a
-- company also shows its code, province and rep, so the two can be told apart.
INSERT INTO import_issue (import_run_id, severity, kind, source_file, column_name, message, details)
SELECT %(run_id)s, 'info', 'observation', 'companies_and_contacts.csv', 'company_name',
       'Company names are not unique. Search results and every company list therefore show '
       'the code, province and owning rep alongside the name.',
       jsonb_build_object(
           'duplicated_names', count(*),
           'examples', (SELECT jsonb_agg(x) FROM (
               SELECT jsonb_build_object('name', name, 'codes', jsonb_agg(legacy_code ORDER BY legacy_code)) AS x
               FROM company GROUP BY name HAVING count(*) > 1 LIMIT 5) sub))
FROM (SELECT name FROM company GROUP BY name HAVING count(*) > 1) d
HAVING count(*) > 0;
