"""Reading data/manifest.json.

The manifest is used to VERIFY, never to GATE. Two reasons:

  * The archive is replaced with the reviewer's copy before review, and `reset.sh` wipes the
    volume. Logic keyed on a specific checksum would risk refusing to load exactly the data
    the reviewer supplied, which is the worst possible failure mode here.
  * The manifest is the archive describing itself. If it disagrees with the files next to
    it, the interesting thing to do is say so and carry on, not stop.

So a checksum mismatch, a row-count mismatch, or a missing manifest each produce an
import_issue and the import proceeds. Everything read here is optional.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("crm.import.manifest")


@dataclass
class Manifest:
    present: bool = False
    dataset_version: str | None = None
    reference_time: datetime | None = None
    # filename -> sha256
    checksums: dict[str, str] = field(default_factory=dict)
    # filename -> declared data_rows
    row_counts: dict[str, int] = field(default_factory=dict)
    # entity name -> declared count
    entities: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def load(data_dir: Path) -> Manifest:
    path = data_dir / "manifest.json"
    if not path.is_file():
        log.info("no manifest.json in %s; import proceeds without verification", data_dir)
        return Manifest(present=False)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- a broken manifest must not stop the import
        log.warning("manifest.json could not be parsed: %s", exc)
        return Manifest(present=True, error=str(exc))

    m = Manifest(present=True)
    m.dataset_version = raw.get("dataset_version")

    ref = raw.get("reference_time")
    if isinstance(ref, str):
        try:
            m.reference_time = datetime.fromisoformat(ref)
        except ValueError:
            log.warning("manifest reference_time is not ISO-8601: %r", ref)

    for name, info in (raw.get("files") or {}).items():
        if not isinstance(info, dict):
            continue
        if isinstance(info.get("sha256"), str):
            m.checksums[name] = info["sha256"]
        if isinstance(info.get("data_rows"), int):
            m.row_counts[name] = info["data_rows"]

    for name, n in (raw.get("entities") or {}).items():
        if isinstance(n, int):
            m.entities[name] = n

    log.info(
        "manifest.json: version=%s reference_time=%s files=%d",
        m.dataset_version, m.reference_time, len(m.checksums),
    )
    return m
