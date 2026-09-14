# Test fixture archive

**Hand-written test data. Not a sample of the supplied archive, and never read by the
application.** The app reads only `CRM_DATA_DIR` (`./data`, bind-mounted at runtime); this
directory exists so `tests/integration/test_import_fixture.py` can drive the importer through
every failure path in seconds. Codes start at `CO9`, `OP9`, `AC9`, `TEST-` so they cannot
collide with anything in the real export.

Same four files and layout as `data/`, with one deliberate problem per row:

| Row | Problem | Expected |
|---|---|---|
| `AC900001` | `;` inside a quoted `details` field | field intact, later columns not shifted |
| `CO900001` | accented company name (`Società`) | intact after import |
| `CO900002`, `CO900003` | reps *Jamie Chen* and *John Chen* both derive `j.chen` | neither is linked to a username |
| `OP900002` | status `" OPEN "` | canonical `open`, raw value kept |
| `OP900003` | company code that does not exist | row skipped, `error` issue, import continues |
| `OP900004` | `12.500,00` and `31/02/2027` | both unknown, one `warning` each |
| `OP900005` | contact of another company; unknown edition | contact and edition unlinked, two warnings, readiness *incomplete* |
| `OP900006` | status `quotation sent`; edition with no height limit | status unknown, readiness capped at *provisional* |
| `AC900003` | opportunity of another company; author `j.chen`, which two reps derive | kept, unlinked, `warning`; author unlinked, both names recorded |
| `AC900004` | company code that does not exist | skipped, `error` |
| `AC900006` | type `webinar`, impossible timestamp, `30/02/2027` follow-up | type admitted as unknown, dated to reference time, no follow-up |
| `AC900007` | author `z.nobody`, unknown to every company; task marked `Y` with a date | a stand-in rep is created so the entry keeps its author; follow-up imported as *done* |
| `AC900008` | opportunity code not in the export | kept, unlinked, `warning` |
| `manifest.json` | `entities` counts include the skipped rows | delta recorded as a `warning`, never fatal |
