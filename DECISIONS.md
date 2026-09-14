# Decision record

The brief says the evaluation is about *"how you approach the problem and the reasoning behind
your choices"*, so the choices are written down as choices: what was decided, what else was on
the table, and why the alternative lost. Entries are in the order the decisions were made.

Entries marked **measured** were settled by running something against the supplied archive or
the pinned images, not by judgement.

---

## 1. Work directly on `main`, no feature branch or worktree

The brief asks to keep development history and says a specific commit hash will be reviewed. A
single linear history on `main` is the most legible form of that. A branch or a git worktree
isolates work from something, and there is nothing else in this repository to be isolated from.

---

## 2. `git config core.fileMode false` before the first commit — **measured**

`git diff --summary` on the untouched starter reported:

```
mode change 100755 => 100644  dev.sh
mode change 100755 => 100644  reset.sh
mode change 100755 => 100644  verify.sh
```

Git Bash on Windows prints `-rwxr-xr-x` for these files, so `ls -l` hides the problem entirely.
The index holds `100755`, but a `git add -A` would have committed the downgrade, and the
reviewer's `./dev.sh` would fail with "permission denied" before anything else ran.

`core.fileMode false` makes Git ignore the working-tree permission bit and keep the mode already
in the index. Newly added `.sh` files still land as `644`, so
`git ls-files -s | grep '\.sh$'` is on the pre-commit checklist.

---

## 3. Python + FastAPI + server-rendered Jinja2 + htmx, not React

The role description asks for "React or similar". htmx is the "or similar" here, chosen
knowingly:

- **No Node toolchain.** One image, one language, one lockfile. A React frontend means a second
  build stage, a second dependency tree and a second class of build failure, inside a build that
  also has to import 75,000 rows and run an agent.
- **The screens are forms and lists.** Search, a record page, an inline edit, an append to a
  timeline, a queue. That is htmx's range exactly; none of it needs client-side state.
- **Shipping beats scaffolding.** A working vertical slice demonstrates more than a
  half-finished SPA.

Compensating for it rather than hiding it: the handoff assistant is exposed as a **JSON REST
API** alongside its HTML view (`/api/opportunities/{ref}/handoff/runs`), which is also what the
evaluation harness drives. The API design stays visible even though the UI is server-rendered.

**Cost, stated plainly:** no client-side routing, no optimistic UI, and a later React rewrite
would not reuse the templates.

---

## 4. Raw SQL behind repository functions, no ORM

- The brief makes query shape a graded concern: searches must stay practical at 100,000
  contacts. Indexes, keyset pagination and join plans are the substance of that answer, and an
  ORM hides exactly those.
- N+1 queries become hard to write by accident when every query is visible in one place.
- `psycopg.rows.class_row(Opportunity)` maps result rows onto frozen dataclasses directly, so
  the mapping layer an ORM would provide is about fifteen lines here.

**Rejected:** SQLAlchemy Core (buys composability the handful of queries here do not need) and
SQLAlchemy ORM plus Alembic (see the next entry).

---

## 5. Numbered `.sql` migrations applied at boot, not Alembic

Alembic's main value is autogenerating migrations by diffing ORM models against the database.
With no ORM there are no models, so adopting it would mean pulling in SQLAlchemy purely to get a
version table. Raw DDL is also directly readable by a reviewer without tracing Python.

`app/db/migrate.py` sorts `app/migrations/*.sql`, takes `pg_advisory_lock(8140250)`, applies
each unapplied file in its own transaction, and records `(version, sha256, applied_at)`. A
checksum mismatch on an already-applied file fails loudly at boot: silent schema divergence is
worse than a crash.

**Cost:** no down-migrations, no autogenerate, hand-written DDL. For a long-lived product across
several environments Alembic would win. There is one database here and `reset.sh` rebuilds it.

---

## 6. htmx 2.0.10, not 4.0.0 — **measured**

cdnjs reported `4.0.0` as latest. Checking the release list: 1.x has 36 stable releases, 2.x has
11, there is no 3.x at all, and 4.x has exactly one. A brand-new major line with no accumulated
patch releases is a risk with no matching benefit inside a time-boxed build, and every current
example targets 2.x. Vendored, with its SHA-256 recorded in `app/static/vendor/VENDORED.md`.

---

## 7. `POSTGRES_INITDB_ARGS` is deliberately left unset — **measured**

The obvious optimisation is `--locale=C`: faster text comparison, deterministic sort order, and
plain btree indexes able to serve `LIKE 'CO0001%'`. It is a trap here.

Run against `postgres:17.6-alpine3.22`:

| `POSTGRES_INITDB_ARGS` | `server_encoding` | `lower()` of an accented capital |
|---|---|---|
| `--locale=C` | **SQL_ASCII** | — |
| `--encoding=UTF8 --locale=C` | UTF8 | accent **not** folded |
| *(unset, the default)* | UTF8 | folded correctly |

**9,993 of the 20,000 contact rows contain an accented character**, in company names of the form
`Corara Cosmetics Societa Cooperativa` (with an accent on the final `a`). The first row silently
corrupts about half the company names in the archive. The second preserves them but breaks
case-insensitive matching on accented characters, which then has to be worked around in every
index expression.

The default costs exactly one thing: `LIKE 'CO0001%'` needs `text_pattern_ops` on the code
indexes. Four words of DDL, against a 50% data-corruption footgun.

---

## 8. `data/` is in `.dockerignore`

A `COPY . .` would bake a 12 MB snapshot of the archive into the image. The reviewer replaces
`data/` with their original copy before review, so any code path reading the baked copy would
silently import the wrong archive while appearing to work. The archive reaches the application
only through the read-only bind mount `./data:/srv/data:ro`, which also means the application
provably cannot modify it.

---

## 9. Migrations run synchronously at startup; the import runs on a background thread

`verify.sh` uses `curl --retry 10 --retry-delay 1 --retry-connrefused`, roughly a ten-second
budget from container start. The import takes longer than that.

| Approach | Port bound during import | Verdict |
|---|---|---|
| Entrypoint imports, then execs uvicorn | no, for the whole import | fails `verify.sh` |
| One-shot `init` service + `service_completed_successfully` | no, for the whole import | same delay, and two places migrations can run |
| **Lifespan: migrate (sync) then bind then import (thread)** | **yes, in about a second** | chosen |

Because the whole import is a single transaction, a request arriving mid-import sees an empty
database or a complete one, never a torn state.

If the import fails, the process keeps serving, marks `import_run.status='failed'`, and shows
the error on every page. A readable error page beats a crash loop the reviewer has to read
`docker logs` to understand.

---

## 10. The import ledger lives in the database, not in a file

`docker compose down && ./dev.sh` destroys the containers but keeps the `postgres-data` volume.
A marker file in the container filesystem would be gone; a marker file inside the volume would
be redundant with a table in the database sitting next to it. Only a row in the database can
answer "have we already imported?" across a container recreation, which is what the brief
requires: later starts must keep user changes and must not duplicate the import.

Belt and braces: `CREATE UNIQUE INDEX ... ON import_run (status) WHERE status = 'completed'`
makes a second completed import impossible at the schema level, not just in application code.

---

## 11. Synchronous psycopg3 with `def` endpoints, not `async def`

FastAPI runs `def` handlers in a threadpool. With one user and a ten-connection pool that is
ample, and it removes the entire class of bug where a synchronous call silently blocks the event
loop. It also makes the chunked `COPY` importer natural to write. `/healthz` stays `async def`
with no database access, so it never queues behind anything.

---

## 12. Docker: one base image, `uv` binary copied in, no `# syntax=` directive

- **No `# syntax=`** — it pulls `docker.io/docker/dockerfile`, which is outside the allowlist in
  `docs/image-policy.md`.
- **`python:3.13.15-slim-bookworm` for both build and runtime**, with the `uv` binary copied
  from `ghcr.io/astral-sh/uv:0.9.7`. Using a uv-flavoured builder image and a `python:` runtime
  would risk Python or distro skew between the two.
- **`psycopg[binary,pool]`** — the wheel bundles libpq, so the runtime image needs no `libpq5`,
  no compiler and no apt layer, and manylinux wheels exist for x86_64 and aarch64. psycopg
  upstream discourages `[binary]` for production because it ships its own libpq; that is a
  deliberate trade here for a reproducible, toolchain-free, multi-architecture build.
- **Healthcheck uses `python -c`, not `curl`** — slim images have no curl, and adding it means
  an apt layer for a job the standard library already does.
- **No `--platform`, no `TARGETARCH` branching, nothing built from source**, so one Dockerfile
  produces a working image on amd64 and arm64.

Resulting runtime image: 160 MB.

---

## 13. `name: exhibition-crm` in compose.yml

Without it, the Compose project name is derived from whatever directory the reviewer clones
into, and so is the volume name. Fixing it scopes `reset.sh`'s `down --volumes` to exactly this
project's resources on any machine, which is what the brief asks for.

---

# Data layer

## 14. Date parsers are plpgsql with an exception handler — **measured**

The plan assumed `to_date` is lenient and silently turns `31/02/2026` into 2026-03-03, and guarded
against it with a formatting round-trip. On PostgreSQL 17 it **raises** instead. That is better for
data quality and fatal for a single-transaction import: one bad row would abort the load. Catching
the error needs plpgsql.

A pure-SQL alternative that checked the day against the real month length, needing no exception
block, was benchmarked against it over 55,518 values: **818 ms against 83 ms**. The expectation was
that avoiding a subtransaction per call would make it faster; the arithmetic cost more than the
exception machinery saves.

The pure-SQL version was more correct in one respect — `to_date('00/01/2026')` returns 2026-01-01 —
so the shipped regex rejects day and month zero before `to_date` sees the value. Speed of one, the
correctness of the other.

## 15. `COPY` into all-text staging, then transform in SQL

Not mainly for speed. 2,654 rows in `activity_log.csv` carry a semicolon inside a quoted field, and
anything that splits on `;` shifts every later column on those rows without an error. `COPY … FORMAT
csv` is a real CSV reader. Staging every column as text means no cast can fail during the load;
the transforms apply parsers that return NULL and record an issue instead.

## 16. `ClientCursor` for the multi-statement transform files — **measured**

The first run failed with `cannot insert multiple commands into a prepared statement`: psycopg binds
parameters over the extended query protocol, which carries one statement per execute. Rejected:
splitting the files on semicolons (needs a parser that understands dollar-quoting), and substituting
the run id into the text (safe — it is an integer from the database — but it reads as injection).
`ClientCursor` binds client-side and sends a simple query, which allows many statements.

## 17. Surrogate keys, a unique legacy code, and composite foreign keys

User-created rows have no legacy code, and inventing `CO`/`AC` codes would pollute an identifier
space the data README calls stable. One route parameter accepts either form.

`FOREIGN KEY (opportunity_id, company_id) REFERENCES opportunity (id, company_id)` on activities and
follow-ups makes "a conversation can never be attached to another company's opportunity" a property
of the schema. The importer resolves mismatches itself before the constraint could reject a row and
abort the transaction.

## 18. One activity table; follow-ups in a table of their own

The five activity types share every source column, and both busy timelines are ordered across all
types, so one table with a type column keeps keyset paging index-ordered.

A follow-up is separate because `completion_marker = 'Y'` on a call means *the call happened*, while
a follow-up date on that row means *a promise is outstanding*. One column cannot hold both. The
`origin` column records which of two defensible readings produced each row — tasks only (1,192) or
any row with a date (5,718) — so the queue's default is a view choice, reversible in the UI.

---

# The policy

## 19. Four readiness states, not two

The director's rule and the coordinator's rule are both kept: a provisional handover satisfies the
first, a ready one the second, and 728 opportunities fall between them. A conflict (height over the
limit) is a different thing from missing information and is its own state, because the brief lists
"missing information and conflicting requests" separately. An unknown edition limit gives neither
ready nor blocked: the height was neither verified nor disproved.

The comparison is `<=`. 1,210 opportunities request exactly their edition's limit.

## 20. Readiness is a cached column with one writer, and no CHECK constraint

It cannot be a generated column — the rule needs the edition's height limit, from another table —
and a SQL view would put the rules in two languages. So `policy.assess()` runs in Python and the
result is stored, by one module only, inside the import transaction, on every edit, and at startup
when the stored policy version is stale. With no CHECK constraint, changing the policy needs no
migration. Restarting against an already-imported database logged "stored readiness predates
4state-v1; recomputing" and filled 15,000 rows in 536 ms.

## 21. Time is a separate question from readiness

7,823 opportunities have complete information for a fair that has already been held. Their
information is not incomplete, so folding dates into `assess()` would muddle a clear rule. A second
pure function, `is_actionable(ends_on, today)`, answers it, and the assistant's coordinator combines
the two. `today` is a parameter, not a clock read, so both stay testable.

---

# The screens

## 22. Trigram search with a three-character floor and a cap of 50

People type fragments — `rivamare`, `chris.conti` — and a full-text index cannot match inside a
word. Below three characters a trigram index cannot be used at all, so short terms get an indexed
prefix match rather than a silent scan. Similarity ordering has ties and no unique tiebreak, so
results cap at 50 instead of pretending to paginate.

## 23. Edition scoping in three places

The opportunity timeline query filters on the opportunity, never the company. The company page
groups by edition and collapses finished ones into a labelled history block. The assistant's
activity tool filters the same way. Other editions appear on an opportunity page only as links with
a status — seeing that last year exists is useful, seeing its figure beside this year's is the
complaint. 4,995 company-and-fair pairs in the archive span more than one edition.

## 24. Every write works with and without JavaScript

Each form is a real `method="post"` that htmx upgrades. With JavaScript the endpoint returns the
changed fragment — an edit returns the facts block plus the header badge swapped out-of-band; without
it, a 303 back to the page. Chosen by the `HX-Request` header in one handler, which also makes every
action testable with plain HTTP.

## 25. Ambiguous numbers are refused — **measured**

The first form parser read `12.500` as twelve and a half. To an Italian reader it is twelve thousand
five hundred, and the archive uses Italian conventions. Silently choosing is a factor-of-a-thousand
error on a budget, so a single separator followed by exactly three digits is rejected with a message
saying how to write it. `12.500,00` and `50,000.00` both still parse, because two different
separators are unambiguous.

## 26. Database sessions run in Europe/Rome

The database container runs in UTC, so `current_date` would be yesterday for the half hour after
midnight in Rome and a follow-up due today would read as due tomorrow. The team, the fairs and the
archive's timestamps are all in Italy.

## 27. A text filter on the follow-up queue — found by walking the brief's own example

Logging "customer will confirm the floor area on Friday" with a Friday follow-up worked, but the
follow-up landed at position 281 of 503 in "next 7 days". The archive dates from 1 September, so
thousands of its follow-ups fall due together, and "find it again later" meant nothing. The filter is
a separate form that does not carry the current tab, so a search from "Overdue" still finds a promise
due on Friday.

---

# The assistant

## 28. The three roles are the three voices in the dispute

The preparer drafts its first pass from the sales director's rule alone; the checker rules with the
policy for the technical side; the coordinator applies the reconciling policy. On any enquiry that is
not fully ready the checker therefore finds something real to object to, and the revision loop runs
rather than being dead code. The alternative — a preparer that always gets it right first time — would
leave "continue or stop" with nothing to decide.

The coordinator makes no model call. Choosing between revise and stop from a ruling that already
exists needs no language model.

## 29. Tools, not a pre-assembled bundle of facts

The preparer starts with only an opportunity code, asks which tools to call, and the results decide
the next round: an edition can be looked up only once named, a height checked only once the limit is
known. That dependency chain is what makes it an agent, and the trace shows skipped tools with a
reason. An unknown tool name is recorded as a failed call rather than crashing the run.

"Is this height allowed" has one definition, `policy.height_within_limit`, used by both the policy
and the `check_height_limit` tool.

## 30. The model behind a protocol, created once and passed down

`ModelClient` is a protocol; `create_model_client()` runs once at startup and the instance is passed
explicitly into each run — never imported as a global — so tests substitute doubles and a real
provider would be one new class. The stand-in is honest about what it is: output is a pure function
of input, it quotes sales notes rather than inventing a summary of them, and every run is stored as a
stub.

A single `for` over two iterations bounds the loop. A test with a model that never agrees proves a
decision still arrives.

## 31. Runs are append-only and render from their own snapshot

The run page reads only what the run stored — the facts, each step's input and output, each tool call.
Re-querying the opportunity would let a later edit rewrite what an old run shows it worked from.

## 32. Evaluation cases are chosen by query; baselines fingerprint behaviour, not text

The reviewer replaces `data/`, so an eval pinned to `OP000003` tests an assumption about the archive.
Each case describes a situation and takes the first matching row. The regression baseline records
decisions, pass counts and the order of steps and tool calls, but no names or prose, so it survives a
replaced archive while still catching a behavioural change. The gate was checked by corrupting a
baseline and confirming the run exited 1.

## 33. A budget-divergence rule was considered and not built — **measured**

Flagging a customer budget far below the recorded value sounds like a useful "conflicting request".
The archive has **0** such rows out of 14,635. Building a rule for an empty population would be
complexity with no evidence behind it.

## 34. The model chooses whether to look, never where — found with a misbehaving model — **measured**

Everything above was verified with the deterministic stand-in, which is well behaved by
construction. The question a real provider raises is what a *badly* behaved model can do to a run,
so a set of model doubles in `tests/unit/test_misbehaving_model.py` each misbehave in one way. Two of
them broke the assistant as it stood:

- A model that, having read the opportunity, also read the exhibitor's other enquiry "for context"
  **replaced the run's facts with last year's order**: the tools took whichever code the model
  passed, and results were stored by tool name. The brief was about the wrong edition — the sales
  coordinator's complaint, reproduced at the agent layer, with edition scoping in the SQL intact.
- A model that looked up last year's edition instead of the one the enquiry names produced a
  **false BLOCKED**, 4 m against a limit that did not apply — worse than a missing edition, because it
  sends sales to argue with the customer about the wrong rule.
- A tool call in the wrong shape (`{"name": …}` instead of `{"tool": …}`, or `tool_calls: null`)
  raised inside the preparer, so a run that hit one recorded nothing.

The fix is one rule: each tool declares which argument names its target, and the runtime fills it
from the run. Left blank, it is supplied; set to another value, the call is **refused and the
refusal is recorded** in the trace like any other call. A malformed call and calls beyond eight per
round are recorded the same way. The alternative — silently overriding the model's argument and
proceeding — would have hidden that the model wanted something else, and a model that wanders is
exactly what an evaluation should surface. Refusing fails closed: a model that only ever asks for
the wrong edition ends in *hold, edition not read*, which is true.

The change touches the tool executor and the preparer and nothing else. The eval regression gate
then ran against the same baselines and reported every case unchanged, which is the point of having
a behavioural fingerprint: the guard changed nothing for a model that behaves.
