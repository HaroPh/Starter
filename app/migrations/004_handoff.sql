-- Persistence for the handoff assistant.
--
-- The brief asks for each run to be saved with the opportunity, "including the information
-- it used, the role outputs and the reason for the decision", and to be revisitable and
-- re-runnable after editing the brief. Three design rules follow from that sentence:
--
--   1. A run stores a SNAPSHOT of the facts it used, not foreign keys to them. If the
--      opportunity or the fair edition is edited afterwards, the old run must still render
--      exactly what it saw. Re-deriving through FKs would let a later edit retroactively
--      rewrite history on screen.
--   2. Runs are never mutated. Re-running inserts a new row pointing at the previous one,
--      so the chain is the audit trail.
--   3. The role outputs and the tool calls are child rows, not a JSON array on the run.
--      They are rendered as an ordered list, queried by role, and asserted on in tests --
--      all of which are cheaper against rows.

CREATE TABLE handoff_run (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    opportunity_id bigint NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,
    run_no         int NOT NULL,
    previous_run_id bigint REFERENCES handoff_run(id) ON DELETE SET NULL,

    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL CHECK (status IN ('running', 'completed', 'failed')),

    -- The brief requires a deterministic local stand-in for model responses, labelled as
    -- such. Labelling it in the DATA rather than only in the UI means a run stays honest
    -- about its provenance even when read straight out of the database.
    model_kind    text NOT NULL,
    model_version text NOT NULL,
    model_is_stub boolean NOT NULL DEFAULT true,

    -- Which readiness policy produced the verdict. An old run keeps showing the version it
    -- was judged under, so swapping the policy does not silently reinterpret history.
    policy_version text NOT NULL,

    -- The complete fact snapshot, including max_stand_height_m as it stood at run time.
    -- This is what the run view renders; it never re-queries the opportunity.
    inputs jsonb NOT NULL,

    readiness       text NOT NULL,
    decision        text NOT NULL,
    decision_reason text NOT NULL,
    final_brief     text NOT NULL DEFAULT '',
    iterations      int  NOT NULL DEFAULT 1,

    -- 'opportunity' when the brief came from the record, 'edited' when the user supplied a
    -- revised one for this run. The edited text is kept here rather than written back to
    -- the opportunity by default -- "run it again after editing the brief" should not force
    -- a record mutation, though a checkbox offers to save it too.
    brief_source       text NOT NULL DEFAULT 'opportunity'
                       CHECK (brief_source IN ('opportunity', 'edited')),
    edited_brief_notes text,

    error text,

    CONSTRAINT handoff_run_no_uk UNIQUE (opportunity_id, run_no)
);

-- One row per role invocation, in order. `phase` distinguishes the preparer planning which
-- tools to call from the preparer drafting the brief afterwards.
CREATE TABLE handoff_run_step (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id    bigint NOT NULL REFERENCES handoff_run(id) ON DELETE CASCADE,
    seq       int NOT NULL,
    iteration int NOT NULL DEFAULT 0,
    role      text NOT NULL CHECK (role IN ('preparer', 'checker', 'coordinator')),
    phase     text,

    -- What the role was GIVEN, not only what it produced. Ten extra lines, and it turns the
    -- run view from a transcript into something a reviewer can actually audit.
    request     jsonb,
    output_text text,
    output_data jsonb,
    findings    jsonb,
    duration_ms int,
    created_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT handoff_run_step_seq_uk UNIQUE (run_id, seq)
);

-- Every tool the preparer called, with its arguments and its result.
--
-- This is what makes the run an agent trace rather than a templated pipeline: the model
-- proposes which tools to call, the runtime executes them, and the results feed the draft.
-- Because the stand-in is deterministic the same opportunity always produces the same plan,
-- which is what makes the whole run reproducible from its stored inputs.
CREATE TABLE handoff_tool_call (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      bigint NOT NULL REFERENCES handoff_run(id) ON DELETE CASCADE,
    step_id     bigint REFERENCES handoff_run_step(id) ON DELETE CASCADE,
    seq         int NOT NULL,
    tool_name   text NOT NULL,
    arguments   jsonb NOT NULL DEFAULT '{}'::jsonb,
    result      jsonb,
    ok          boolean NOT NULL DEFAULT true,
    error       text,
    duration_ms int,
    created_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT handoff_tool_call_seq_uk UNIQUE (run_id, seq)
);
