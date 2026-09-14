# Exhibition sales CRM

A sales CRM for a company that designs and builds exhibition stands, with a handoff assistant
that prepares briefs for the technical team. Read [the assignment](ASSIGNMENT.md) for the
brief; this file covers what was built and why. Every significant choice, with the
alternatives that lost, is in [DECISIONS.md](DECISIONS.md).

## Run it

```bash
./dev.sh                 # build, start, migrate, import on first use; stays in the foreground
docker compose down      # stop, keeping data
./reset.sh               # remove this project's data; the next start re-imports the archive
./verify.sh              # compose config + HTTP reachability
```

The app answers on **http://localhost:3000** as soon as the port binds. The archive finishes
loading about ten seconds later, when the logs say `import completed`; the home page shows a
banner until then. Port 3000 must be free — during development an unrelated container on
that port made `verify.sh` pass while this app had failed to start, because the check proves
something is listening, not that it is this app.

Tests and evaluation need no host Python:

```bash
docker compose --profile test run --rm tests    # ruff + 79 tests (unit, and the importer against a fixture)
docker compose --profile eval run --rm evals    # the assistant, end to end, through its API
```

## Try the assistant

Open an opportunity and press **Prepare a technical brief**. Each run is saved; the run page
shows the brief, the decision and its reason, and every step the three roles took.

| Enquiry | Open | What happens |
|---|---|---|
| **Complete** | [`/opportunities/OP000001`](http://localhost:3000/opportunities/OP000001) — Aster Cosmetics, Beauty Trade Forum 2027, budget €50.000, 80 m², 4 m against a 4,5 m limit | **Hand over.** One pass. The preparer calls five tools, including the height check. |
| **Incomplete** | [`/opportunities/OP000003`](http://localhost:3000/opportunities/OP000003) — Rivamare Packaging, Packaging Industry Week 2026, budget €30.000, no area, no height | **Hand over, provisional.** Two passes: the first draft proposes a straight handover, the checker objects that the dimensions are unknown, the second draft marks the brief provisional with the open questions. The height check is skipped, and the trace says why. |
| **Conflicting** | [`/opportunities/OP000005`](http://localhost:3000/opportunities/OP000005) — 6 m requested where the edition allows 5 m | **Blocked**, naming the conflict. |

Then, on OP000003, **Edit** the enquiry, enter an area of `48` and a height of `4`, save —
the badge moves to Ready — and press **Prepare the brief again**. The new run hands over in
one pass, and the earlier run still shows what it was working from before the edit.

To see edition scoping, open [`/companies/CO000001`](http://localhost:3000/companies/CO000001):
the 2027 enquiry is current, and the won 2026 order sits in a separate, collapsed *Previous
editions* block, labelled as agreed for that edition only.

## Stack and versions

| | |
|---|---|
| Language | Python 3.13.15 (`python:3.13.15-slim-bookworm`) |
| Web | FastAPI 0.141.1, Uvicorn 0.52.4, Jinja2 3.1.6 |
| Front end | Server-rendered templates + htmx 2.0.10, vendored with its SHA-256; hand-written CSS; no Node |
| Database | PostgreSQL 17.6 (`postgres:17.6-alpine3.22`), psycopg 3.3.5 with psycopg-pool 3.3.1, raw SQL, no ORM |
| Packaging | uv 0.9.7 (`ghcr.io/astral-sh/uv:0.9.7`), committed `uv.lock`, `uv sync --frozen` |
| Tests | pytest 8.4.2, ruff 0.16.7 |

Images are multi-architecture and nothing is pinned to amd64 or arm64. The runtime image is
160 MB and runs as a non-root user.

## Time spent

**5 hours**: data profiling and planning first, then the build in the order of the commit
history.

## How the team's competing requests were handled

### Sales director against technical coordinator — when does technical get involved?

The two positions look opposed but are about different things. The director wants technical
to **know early**; the coordinator wants technical not to **start work** on something that
cannot be delivered. They only collide when "hand over" and "start work" are one event.
Splitting them gives four states ([`app/handoff/policy.py`](app/handoff/policy.py)):

| State | Meaning | What happens |
|---|---|---|
| **Incomplete** | No fair edition, or no customer budget | Held. Not even the director's threshold is met. |
| **Provisional** | Fair and budget known; area or height still open | Handed over **now**, marked provisional, naming exactly what is missing, so technical can plan without starting build work. |
| **Ready** | Everything known and the height within the edition limit | Technical can start. |
| **Blocked** | The request conflicts with the fair's rules — today, a height above the edition maximum | Not handed over; the conflict is named. |

Both sides get what they actually asked for. The archive measures the gap: 14,635
opportunities meet the director's rule and 13,906 the coordinator's, so **728 sit exactly in
the space a two-state policy cannot express**. The distribution after import is 13,906 ready,
728 provisional, 365 incomplete and 1 blocked.

The policy is one pure function, tested as a truth table. The opportunity badge, the *needs
attention* list and the assistant's checker all use it, so they cannot disagree. Changing it —
say to two states — is an edit to that file and a version bump: the app recomputes every
stored verdict at startup when the version changes, and nothing needs migrating.

### Sales coordinator — "last year's agreement treated as if it still applied"

**The opportunity is the unit of edition scope.** An opportunity's timeline shows only that
opportunity's conversations; the query filters on the opportunity and never on the company,
with a comment saying why. The company page groups opportunities by fair edition and collapses
finished editions into a read-only history block whose figures are labelled as agreed for that
edition. Other editions of the same fair appear on an opportunity page as links, without their
amounts. The assistant's tools follow the same rule, so a 2027 brief never sees the 2026 order.

This is not an edge case: **4,995 company-and-fair pairs** in the archive span more than one
edition.

### Account managers — "who to call and what they're waiting for"

A follow-up is a work item with its own table, due date and status, not two columns on a log
entry. In the source, a completed call's marker says *the call happened*; a follow-up date on
the same row says *someone still owes the customer something*. Conflating them would make
"mark this done" overwrite the record of the call.

Recording a conversation can create the follow-up in the same submit — the brief's own example
is a promise made during a call. The queue orders overdue first, puts the contact's phone
number on each row, and can be filtered by exhibitor, opportunity code or what is being waited
for, which turned out to be necessary: a follow-up scheduled for Friday landed at position 281
of 503 in *next 7 days* until the filter existed.

## Import decisions

The importer streams each CSV into all-text staging tables with `COPY`, then transforms in SQL,
in one transaction. Every integrity problem becomes a row in `import_issue` rather than an
aborted load, because the archive is replaced before review. On the supplied archive: **0
errors, 0 warnings**, 11 recorded interpretations.

| Topic | Decision |
|---|---|
| **Quoted semicolons** | 2,654 activity rows contain `;` inside a quoted field. A split-on-semicolon parser shifts every later column on those rows with no error. `COPY … FORMAT csv` handles it; `AC0000001` is the check. |
| Encoding | 9,993 contact rows contain an accented character. `POSTGRES_INITDB_ARGS="--locale=C"` would have produced SQL_ASCII and corrupted them, so the image default is kept. Verified against the pinned image. |
| Status | 13 spellings collapse to 5 through an alias table keyed on `lower(btrim(value))`, so an unseen case or whitespace variant also resolves. The raw value is kept and shown on hover. |
| Dates and money | Decimal comma, `DD/MM/YYYY`, datetimes read as Europe/Rome. Unparseable values become unknown plus an issue, never an abort. PostgreSQL 17 raises on impossible dates where older versions silently rolled them over, so the parsers catch the error. |
| Empty values | Unknown, never zero — a missing area is "not confirmed", not 0 m². |
| `legacy_print_layout` | **Excluded** — presentation metadata for a system that no longer exists (`FORM-2\|ROW-1\|ARCHIVE-A`). Kept in `contact.legacy_extra`, not discarded. |
| `fax` | **Kept** — obsolete and mostly empty, but still commercial contact data. |
| Sales reps | The export has rep names on companies and usernames on activities, with no key between them. Linked by first initial plus surname (`Alex Morgan` → `a.morgan`); an ambiguous match links nothing rather than guessing. |
| Fairs | Derived from the edition-code prefix (`BEAUTY-2027` → `BEAUTY`), not the display name. |
| Value and budget | `amount_eur` and `client_budget_eur` stay separate; the data README is explicit that they mean different things. |
| Requested height | Stored as a request, never an approval, and compared with the edition limit rather than clamped. |
| Company names | Not unique — `CO000002` and `CO000003` are both Rivamare Packaging — so every list shows code, province and account manager. |
| Follow-ups | Created from any activity carrying a follow-up date (5,718). The data README's narrower reading — only pending tasks — gives 1,192. `follow_up.origin` keeps both; the queue defaults to the broad reading and a checkbox narrows it. |
| Checksums | Compared with `manifest.json` and recorded on mismatch, **never** used to refuse a load — the reviewer's archive is the one that must import. |
| Idempotency | A completed-import row in the database, behind an advisory lock and a partial unique index. `docker compose down` keeps the volume but not the container, so only the database can remember that the import happened. |

The supplied archive is clean, so running the importer on it proves the happy path and nothing
else. [`tests/integration/test_import_fixture.py`](tests/integration/test_import_fixture.py)
runs it against a **hand-written fixture archive** ([`tests/fixtures/data/`](tests/fixtures/data/)
— test data, not a sample of the export, never read by the app) in which every row breaks one
rule: a quoted `;`, an accented name, `" OPEN "`, `12.500,00`, 31 February, a company that
does not exist, a contact and an opportunity of the wrong company, an unknown edition, an
unknown activity type, an unknown author, an ambiguous one, and a manifest whose counts
include the skipped rows. Each has a pinned consequence, and the load never aborts. Writing
it found one real defect: an author name that two reps derive (*Jamie Chen*, *John Chen* →
`j.chen`) is deliberately linked to nobody, but the fallback that admits unknown usernames as
stand-in reps then created a third rep called `j.chen` and attributed the entries to it —
defeating the rule. The supplied archive has no ambiguous names, so no run on it could have
shown this.

## Scale

At 100,000 contacts this is still a small PostgreSQL database; the work is avoiding the four
things that actually hurt at that size.

- **Search** uses trigram GIN indexes over case- and accent-folded names, so a fragment like
  `societa` matches anywhere in a name. On the imported archive: company name 0.34 ms, contact
  name 0.47 ms, code prefix 0.08 ms, all index scans. Terms under three characters fall back to
  an indexed prefix match instead of scanning. Results cap at 50 rather than paginating.
- **Lists** use keyset pagination on indexed column pairs, never `OFFSET`, and no list page runs
  a `COUNT(*)`.
- **The follow-up queue** reads a partial index over pending rows only.
- **Statistics** are refreshed with `ANALYZE` right after the import, because a fresh reset is
  exactly the state a reviewer sees first.

Import: **9.3 s** for the full archive including readiness. Migrations: 74 ms. Readiness for
15,000 opportunities: 536 ms. These are measured on the supplied archive; the five-times case
was designed for but not measured.

## The handoff assistant

Three roles as plain functions ([`app/handoff/roles.py`](app/handoff/roles.py)), and they are
the three voices in the dispute above:

- **Preparer** — speaks for sales. It starts knowing only the opportunity code and asks the
  model which tools to call; the results decide what it can ask next (an edition can only be
  looked up once the opportunity names one, a height only checked once the limit is known).
  On its first pass it drafts from the sales director's rule alone.
- **Checker** — speaks for technical. It rules with the policy on the facts the preparer
  gathered, and objects when the proposal does not match.
- **Coordinator** — pure, no model call. It sends the draft back once or stops, with a
  decision and a reason. A finished fair edition stops the run whatever the readiness.

So the revision loop runs on every enquiry that is not fully ready, and a run's trace reads as
the dispute played out. The loop is a `for` over two passes, so it ends even if the model never
agrees; a test proves it.

**The model is a deterministic local stand-in** (`DeterministicStubModel`) behind a
`ModelClient` protocol that is created once at startup and passed down. It produces the shapes
a model would — a tool plan, a draft, a review — as a pure function of its input. No API keys,
no network calls, no downloaded models; every run is stored with `model_is_stub = true` and the
page says so. A real provider would be a second class implementing the same protocol.

Each run stores the facts it used, every step's input and output, every tool call with its
arguments and result, and the decision with its reason. The run page renders only from that
record, so editing the opportunity afterwards cannot change what an old run shows. Runs are
never modified; running again adds a new one, optionally with edited sales notes.

JSON API: `POST /api/opportunities/{code}/handoff/runs`, `GET /api/handoff/runs/{id}`.

### What a misbehaving model can do to a run

The stand-in behaves, so the question is what happens when a real model does not. A set of
model doubles ([`tests/unit/test_misbehaving_model.py`](tests/unit/test_misbehaving_model.py))
each misbehave in one way, and two of them broke the assistant:

| The model… | Before | Now |
|---|---|---|
| reads the exhibitor's *other* enquiry "for context" | the run's facts were **replaced by last year's order** and the brief was about the wrong edition | the call is refused and recorded; the brief stays about this enquiry |
| looks up last year's edition instead of this one | a **false Blocked**: 4 m against a limit that did not apply | refused; the run holds and says the edition was not read |
| returns a call in the wrong shape, or `tool_calls: null` | the run crashed and recorded nothing | each becomes a refused call in the trace; the run carries on |
| asks for 500 tool calls in one round | all 500 ran | eight run, one trace line says the rest did not |
| checks the height against a limit it made up | already safe: the checker rules from the edition, not from the preparer's arithmetic | unchanged, now pinned by a test |

The rule behind the fix: **the model chooses whether to look, never where.** Each tool declares
which argument names its target and the runtime fills it from the run; leave it blank and it is
supplied, name anything else and the call is refused. After the change the evaluation's regression
gate reported every case byte-identical — the guard changed nothing for a model that behaves.

## Evaluation

`docker compose --profile eval run --rm evals` runs six cases through the API and applies four
gates: **expectations** (decision, passes, tools, wording), **determinism** (every case twice,
byte-identical), **coverage** (all four readiness states occur) and **regression** against a
committed behavioural fingerprint. Exit codes 0 / 1 / 2 for pass, gate failure, infrastructure
error.

Cases are selected by query, never by code — "an open enquiry with a budget but no area and no
height" — so they survive the archive being replaced. Against the supplied archive the queries
found the hand-written demo rows on their own. The regression gate was checked by editing a
baseline and confirming the run failed.

## Where it breaks

Measured, not guessed.

1. **The stand-in cannot read prose.** Sales notes are quoted verbatim, marked as not
   interpreted. Only 12 distinct notes exist across 15,000 rows, so the real signal is in the
   structured fields — but a notes line saying *"client has not decided the height"* next to a
   recorded height would not be flagged. This is the first place a real model would add value.
2. **Readiness ignores time.** 7,823 opportunities are ready for a fair that has already been
   held. The assistant stops them through a separate time check; the badge and list still show
   them as ready, which is technically true and practically useless for past editions.
3. **The archive is a snapshot from 1 September.** Measured against today, 2,296 follow-ups are
   overdue, so the queue opens on a wall of red. Real use would not look like this; a toggle to
   view the queue as of the archive date would make the demo read better.
4. **Blocked rests on one row.** Only one opportunity in the archive requests a height above its
   limit, so that state is barely exercised by real data; the unit tests cover it synthetically.
5. **Rejected on evidence:** a rule flagging customer budgets far below the recorded value was
   considered. The archive has **0** such rows out of 14,635, so it was not built.

## Unfinished

- No screen for `import_issue`; the data is recorded and queryable, and summarised above.
- Contacts and fair editions cannot be edited in the UI; follow-ups are scheduled from an
  opportunity, not from a company page.
- No "as of archive date" view for the follow-up queue (see *Where it breaks*).
- Performance at five times the archive is designed for and not measured.
- No authentication, by design: the brief assumes a single user.

## Repository

```
app/
  handoff/      policy, tools, model client, roles, orchestrator, run storage
  importer/     COPY loader, manifest check, SQL transforms
  migrations/   numbered .sql files, applied at startup
  repositories/ SQL, one module per area
  routes/       HTML pages and the JSON API
  templates/    Jinja2 + htmx partials
evals/          cases, runner, committed baselines
tests/unit/     policy, forms, orchestration, misbehaving model
tests/integration/  the importer against a fixture archive with a problem in every row
DECISIONS.md    every significant choice and what it beat
```
