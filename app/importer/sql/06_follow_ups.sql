-- Follow-ups, derived from the activity log.
--
-- This is the most interesting modelling decision in the import, so the reasoning is worth
-- stating in full.
--
-- The brief says: "If a customer promises to confirm the floor area on Friday, that needs to
-- turn into something they can find and act on." That describes a work item with a due date
-- and a lifecycle -- not a log line. So a follow-up gets its own row and its own status,
-- and the activity it came from stays exactly as it was recorded.
--
-- The archive supports TWO defensible readings of what counts as an open follow-up, and the
-- importer refuses to pick one silently:
--
--   narrow  Only a `task` marked N is open work. data/README.md says so literally:
--           "N for pending tasks". That yields 1,192 items, 291 of them overdue at the
--           archive reference date.
--   broad   Any entry carrying a follow_up_on is an outstanding promise, whatever type it
--           is. A completed call that ends "customer will confirm the floor area on Friday"
--           is exactly the sentence the brief describes, and its marker is Y because THE
--           CALL happened -- not because the promise was kept. That yields 5,718 items.
--
-- Every row with a follow-up date is imported, and `origin` records which reading it
-- belongs to. The queue defaults to broad because that is what the brief describes; a facet
-- narrows it to origin = 'task'. Recording the origin keeps the choice reversible in the UI
-- instead of baked into the import, and lets the README quote both numbers honestly.

INSERT INTO follow_up (source_activity_id, company_id, opportunity_id, due_on,
                       status, origin, assigned_rep_id, note, completed_at)
SELECT
    a.id,
    a.company_id,
    a.opportunity_id,
    crm_parse_date(s.follow_up_on),
    -- A task already marked Y is a promise that was kept; it is imported as done so the
    -- history is complete, rather than dropped so the queue looks tidier.
    CASE WHEN a.activity_type = 'task' AND a.legacy_completion_marker = 'Y'
         THEN 'done' ELSE 'pending' END,
    CASE a.activity_type
         WHEN 'task' THEN 'task'
         WHEN 'note' THEN 'note'
         ELSE 'interaction'
    END,
    -- Whoever logged the entry owns the follow-up. The export has no separate assignee, and
    -- inventing a round-robin would be fabrication.
    a.author_rep_id,
    a.details,
    CASE WHEN a.activity_type = 'task' AND a.legacy_completion_marker = 'Y'
         THEN a.occurred_at END
FROM stg_activities s
JOIN activity a ON a.legacy_code = btrim(s.entry_id)
WHERE crm_parse_date(s.follow_up_on) IS NOT NULL;


INSERT INTO import_issue (import_run_id, severity, kind, source_file, message, details)
SELECT %(run_id)s, 'info', 'interpretation', 'activity_log.csv',
       'Follow-ups are derived from any activity carrying a follow_up_on, not only from '
       'pending tasks. Both readings are preserved: follow_up.origin records which one each '
       'row belongs to, the queue defaults to the broad reading because it matches the '
       'requirement in the brief, and a facet narrows it to tasks only.',
       jsonb_build_object(
           'broad_total',        count(*),
           'narrow_tasks_only',  count(*) FILTER (WHERE origin = 'task'),
           'by_origin',          (SELECT jsonb_object_agg(origin, n)
                                  FROM (SELECT origin, count(*) AS n FROM follow_up GROUP BY 1) x),
           'pending',            count(*) FILTER (WHERE status = 'pending'),
           'done',               count(*) FILTER (WHERE status = 'done'))
FROM follow_up;
