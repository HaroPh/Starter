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
