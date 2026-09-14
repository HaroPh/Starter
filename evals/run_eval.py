"""Evaluate the handoff assistant end to end, through its JSON API.

    docker compose --profile eval run --rm evals               # evaluate
    docker compose --profile eval run --rm evals --save-baseline

Exit codes, so this can gate a pipeline:
    0  every gate passed
    1  a gate failed -- the assistant behaved differently from what is expected
    2  infrastructure error -- the app or database could not be reached, or no row matched

Four gates:

  EXPECTATIONS  each case's decision, readiness, pass count, tools and wording.
  DETERMINISM   every case is run twice; the brief, decision, reason and every step's text
                must be byte-identical. The stand-in is supposed to be a pure function of its
                input, and this is where that claim is checked rather than asserted.
  COVERAGE      across all cases, every readiness state must actually occur. A suite that
                never exercises BLOCKED says nothing about BLOCKED.
  REGRESSION    each case's behavioural fingerprint -- decision, readiness, passes, the order
                of role steps and tool calls -- is compared with the committed baseline. The
                fingerprint deliberately excludes names, codes and brief text, so a baseline
                survives the archive being replaced while still catching a change in how the
                assistant behaves.

Cases are chosen by query (evals/cases.yaml), never by hardcoded opportunity code, for the
same reason: the reviewer swaps data/ before running anything.

Note that the harness calls the real API, so each run it makes is saved against the
opportunity it evaluated, exactly as a run started from the page would be.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg
import yaml
from psycopg.rows import dict_row

HERE = Path(__file__).resolve().parent
BASELINES = HERE / "baselines"
STATES = {"ready", "provisional", "incomplete", "blocked"}

BASE_URL = os.environ.get("CRM_BASE_URL", "http://localhost:3000").rstrip("/")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://crm:local-development-only@localhost:5432/crm")


class InfraError(Exception):
    pass


def wait_for_app(timeout_s: float = 120) -> None:
    """The import may still be running when this starts; wait for it to finish."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/readyz", timeout=5) as resp:
                body = json.loads(resp.read())
                if (body.get("import") or {}).get("status") == "completed":
                    return
                last = body
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(2)
    raise InfraError(f"app not ready at {BASE_URL} after {timeout_s:.0f}s: {last}")


def pick(conn, select: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.legacy_code FROM opportunity o "
            "JOIN fair_edition fe ON fe.id = o.fair_edition_id "
            f"WHERE {select} ORDER BY o.legacy_code LIMIT 1"
        )
        row = cur.fetchone()
    return row["legacy_code"] if row else None


def start_run(code: str) -> dict:
    req = urllib.request.Request(
        f"{BASE_URL}/api/opportunities/{code}/handoff/runs",
        data=b"{}", method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise InfraError(f"POST run for {code} failed: {exc}") from exc


def comparable(run: dict) -> dict:
    """What must be identical between two runs of the same input."""
    return {
        "final_brief": run["final_brief"],
        "decision": run["decision"],
        "decision_reason": run["decision_reason"],
        "steps": [(s["role"], s["phase"], s["output_text"]) for s in run["steps"]],
    }


def fingerprint(run: dict) -> dict:
    """Behaviour only: no names, codes or prose, so it survives a replaced archive."""
    return {
        "decision": run["decision"],
        "readiness": run["readiness"],
        "iterations": run["iterations"],
        "steps": [f"{s['role']}/{s['phase']}" for s in run["steps"]],
        "tools": [c["tool"] for s in run["steps"] for c in s["tool_calls"]],
        "model_is_stub": run["model"]["is_stub"],
    }


def check_expectations(run: dict, expect: dict) -> list[str]:
    problems = []
    for key in ("decision", "readiness", "iterations"):
        if key in expect and run[key] != expect[key]:
            problems.append(f"{key}: expected {expect[key]!r}, got {run[key]!r}")
    tools = {c["tool"] for s in run["steps"] for c in s["tool_calls"]}
    for t in expect.get("tools_called", []):
        if t not in tools:
            problems.append(f"tool {t} was not called")
    for t in expect.get("tools_skipped", []):
        if t in tools:
            problems.append(f"tool {t} was called but should have been skipped")
    for s in expect.get("brief_mentions", []):
        if s not in run["final_brief"]:
            problems.append(f"brief does not mention {s!r}")
    for s in expect.get("reason_mentions", []):
        if s not in run["decision_reason"]:
            problems.append(f"reason does not mention {s!r}")
    if not run["model"]["is_stub"]:
        problems.append("run is not labelled as coming from the stand-in")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--save-baseline", action="store_true", help="record fingerprints as the new baseline")
    args = parser.parse_args()

    cases = yaml.safe_load((HERE / "cases.yaml").read_text(encoding="utf-8"))["cases"]

    try:
        wait_for_app()
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)
    except Exception as exc:  # noqa: BLE001
        print(f"INFRA ERROR: {exc}")
        return 2

    failures: list[str] = []
    seen_states: set[str] = set()
    rows = []

    try:
        for case in cases:
            name = case["name"]
            code = pick(conn, case["select"])
            if code is None:
                print(f"INFRA ERROR: no opportunity matches case {name!r}; the archive does not contain that situation")
                return 2

            first, second = start_run(code), start_run(code)

            problems = check_expectations(first, case.get("expect", {}))
            deterministic = comparable(first) == comparable(second)
            if not deterministic:
                problems.append("two runs on the same input differed")

            fp = fingerprint(first)
            baseline_file = BASELINES / f"{name}.json"
            regression = "new"
            if args.save_baseline:
                baseline_file.write_text(json.dumps(fp, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                regression = "saved"
            elif baseline_file.exists():
                stored = json.loads(baseline_file.read_text(encoding="utf-8"))
                if stored == fp:
                    regression = "same"
                else:
                    regression = "CHANGED"
                    for key in sorted(set(stored) | set(fp)):
                        if stored.get(key) != fp.get(key):
                            problems.append(f"regression in {key}: baseline {stored.get(key)!r}, now {fp.get(key)!r}")

            seen_states.add(first["readiness"])
            status = "PASS" if not problems else "FAIL"
            rows.append((status, name, code, first["decision"], first["iterations"],
                         "yes" if deterministic else "NO", regression))
            failures += [f"{name}: {p}" for p in problems]
    except InfraError as exc:
        print(f"INFRA ERROR: {exc}")
        return 2
    finally:
        conn.close()

    missing_states = STATES - seen_states
    if missing_states:
        failures.append(f"coverage: no case exercised {sorted(missing_states)}")

    print()
    print(f"{'':4}  {'case':<20} {'opportunity':<12} {'decision':<21} {'passes':>6}  {'deterministic':<13} baseline")
    print("-" * 96)
    for status, name, code, decision, iterations, det, reg in rows:
        print(f"{status:4}  {name:<20} {code:<12} {decision:<21} {iterations:>6}  {det:<13} {reg}")
    print("-" * 96)
    print(f"coverage: {sorted(seen_states)}{'  (all four states)' if not missing_states else ''}")

    if failures:
        print("\nGATE FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nALL GATES PASSED" + ("  (baseline saved)" if args.save_baseline else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
