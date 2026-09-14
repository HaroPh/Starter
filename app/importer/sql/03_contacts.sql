-- Contacts.
--
-- One row per contact, so this is a straight load -- the only judgement calls are which
-- legacy columns survive into the working model and which are parked.
--
--   fax                 RETAINED. Empty in 15,999 of 20,000 rows and nobody sends faxes,
--                       but it is still commercial contact data and the brief asks to keep
--                       "the useful commercial information". Dropping a contact channel is
--                       a product decision, not a data-cleaning one.
--   legacy_print_layout EXCLUDED from the working model. Values look like
--                       "FORM-2|ROW-1|ARCHIVE-A" -- which paper form, which row on it, which
--                       physical archive box. That is presentation metadata for a system
--                       that no longer exists. It is parked in contact.legacy_extra rather
--                       than discarded, because silently dropping a column is worse than
--                       keeping it somewhere inspectable.

INSERT INTO contact (legacy_code, legacy_row_id, company_id, first_name, last_name,
                     email, phone, fax, legacy_extra)
SELECT DISTINCT ON (btrim(s.contact_code))
       btrim(s.contact_code),
       crm_blank_to_null(s.legacy_row_id),
       c.id,
       coalesce(crm_blank_to_null(s.contact_first_name), ''),
       coalesce(crm_blank_to_null(s.contact_last_name), ''),
       crm_blank_to_null(s.email),
       crm_blank_to_null(s.phone),
       crm_blank_to_null(s.fax),
       CASE WHEN crm_blank_to_null(s.legacy_print_layout) IS NOT NULL
            THEN jsonb_build_object('legacy_print_layout', btrim(s.legacy_print_layout))
            ELSE '{}'::jsonb END
FROM stg_companies_contacts s
JOIN company c ON c.legacy_code = btrim(s.company_code)
WHERE crm_blank_to_null(s.contact_code) IS NOT NULL
ORDER BY btrim(s.contact_code), s.legacy_row_id
ON CONFLICT (legacy_code) DO NOTHING;

-- A repeated contact code means the export disagrees with itself about who that contact is.
-- First occurrence wins, as above, and the duplicate is reported.
INSERT INTO import_issue (import_run_id, severity, kind, source_file, source_row_ref,
                          column_name, raw_value, message, details)
SELECT %(run_id)s, 'warning', 'duplicate_contact_code', 'companies_and_contacts.csv',
       min(s.legacy_row_id), 'contact_code', btrim(s.contact_code),
       'This contact code appears on more than one row. The row with the lowest '
       'legacy_row_id was used.',
       jsonb_build_object('occurrences', count(*))
FROM stg_companies_contacts s
WHERE crm_blank_to_null(s.contact_code) IS NOT NULL
GROUP BY btrim(s.contact_code)
HAVING count(*) > 1;

-- A contact with no usable channel is not an error, but it IS the thing an account manager
-- runs into when the brief says they "need to know who to call". Counted so the number is
-- visible on the import report rather than discovered one contact at a time.
INSERT INTO import_issue (import_run_id, severity, kind, source_file, message, details)
SELECT %(run_id)s, 'info', 'observation', 'companies_and_contacts.csv',
       'Some contacts are missing an email address, a phone number, or both. The contact '
       'panel shows which channel is missing rather than rendering an empty field.',
       jsonb_build_object(
           'no_email', count(*) FILTER (WHERE email IS NULL),
           'no_phone', count(*) FILTER (WHERE phone IS NULL),
           'neither',  count(*) FILTER (WHERE email IS NULL AND phone IS NULL),
           'total',    count(*))
FROM contact
HAVING count(*) FILTER (WHERE email IS NULL OR phone IS NULL) > 0;

-- The exclusion decision, recorded once for the whole import rather than 20,000 times.
INSERT INTO import_issue (import_run_id, severity, kind, source_file, column_name, message, details)
SELECT %(run_id)s, 'info', 'excluded_field', 'companies_and_contacts.csv',
       'legacy_print_layout',
       'Obsolete presentation metadata from the previous system (which paper form, which row '
       'on it, which archive box). Excluded from the working model; the raw value is kept in '
       'contact.legacy_extra so nothing is lost.',
       jsonb_build_object('rows_with_a_value', count(*) FILTER (WHERE legacy_extra ? 'legacy_print_layout'),
                          'example', min(legacy_extra ->> 'legacy_print_layout'))
FROM contact;

INSERT INTO import_issue (import_run_id, severity, kind, source_file, column_name, message)
VALUES (%(run_id)s, 'info', 'retained_field', 'companies_and_contacts.csv', 'fax',
        'Obsolete but retained: it is commercial contact data, and the brief asks to keep '
        'the useful commercial information. Shown on the contact panel only when present.');
