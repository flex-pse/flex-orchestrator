"""``verify``: check a store and fingerprint what it holds (PLAN.md D14, M6).

Run after a restore, or any time. For every orchestrator study it reports, per
kind of item (results, datasets, model and surrogate specs, fits):

- how many runs there are in each status, and the tags that succeeded;
- **problems**: a tag stored twice (D9 allows one), a run still ``running``,
  and an item whose values can't be read back;
- for a sample of items (the first ``samples`` by tag), a hash of every value
  stored with it: read from the database, the tables and the files, so a
  restore that lost any of them shows.

:func:`compare` lines two reports up: a restore drill takes one before, one
after, and expects no differences.
"""

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from flex_orchestrator.backend.base import FileValue
from flex_orchestrator.data import DATA_KIND
from flex_orchestrator.orchestrator import LEDGERS

REPORT = "flexorch-verify/1"


def verify(orch: Any, studies: list[str] | None = None, samples: int = 5) -> dict:
    """Check every study (or ``studies``) and return the report.

    Args:
        orch: The orchestrator, on the store to check.
        studies: The studies to check; every orchestrator study by default.
        samples: How many items of each kind to read in full and hash.

    Returns:
        ``{"report", "checked_at", "samples", "studies": {study: {kind: {...}}},
        "problems": [...]}``; a kind holds ``runs`` (``{status: count}``),
        ``tags`` (sorted) and ``hashes`` (``{tag: {"run_id", "hash"}}``).
    """
    report: dict[str, Any] = {
        "report": REPORT,
        "checked_at": datetime.now(UTC).isoformat(),
        "samples": samples,
        "studies": {},
        "problems": [],
    }
    for study in studies if studies is not None else orch.studies():
        report["studies"][study] = _study(orch, study, samples, report["problems"])
    return report


def compare(before: dict, after: dict) -> list[str]:
    """Return how ``after`` differs from ``before``; ``[]`` if it doesn't.

    Compares the studies, each kind's run counts and tags, and the hashes of
    items sampled in both. ``checked_at`` and the problems lists aren't
    compared: :func:`verify` reports problems on its own.
    """
    differences = []
    old, new = before["studies"], after["studies"]
    for study in sorted(set(old) | set(new)):
        if study not in new:
            differences.append(f"study {study!r} is missing")
            continue
        if study not in old:
            differences.append(f"study {study!r} wasn't there before")
            continue
        for kind in sorted(set(old[study]) | set(new[study])):
            was, now = old[study].get(kind, _EMPTY), new[study].get(kind, _EMPTY)
            where = f"{study} {kind}"
            if was["runs"] != now["runs"]:
                differences.append(f"{where}: runs {was['runs']} -> {now['runs']}")
            lost = sorted(set(was["tags"]) - set(now["tags"]))
            gained = sorted(set(now["tags"]) - set(was["tags"]))
            if lost:
                differences.append(f"{where}: tags missing {lost}")
            if gained:
                differences.append(f"{where}: tags added {gained}")
            for tag in sorted(set(was["hashes"]) & set(now["hashes"])):
                if was["hashes"][tag] != now["hashes"][tag]:
                    differences.append(f"{where} {tag!r}: its values differ")
    return differences


_EMPTY: dict[str, Any] = {"runs": {}, "tags": [], "hashes": {}}


def _study(orch: Any, study: str, samples: int, problems: list[str]) -> dict:
    found: dict[str, Any] = {}
    for name in sorted(set(LEDGERS.values())):
        ledger = orch._ledger(study, name)
        if not orch.backend.has_study(ledger):
            continue
        by_kind: dict[str, list] = {}
        for run in orch.backend.runs(ledger):
            kind = run.metadata.get("flexorch_kind") or (
                DATA_KIND if name == "data" else name
            )
            by_kind.setdefault(kind, []).append(run)
        for kind, runs in sorted(by_kind.items()):
            found[kind] = _kind(orch, study, ledger, kind, runs, samples, problems)
    return found


def _kind(
    orch: Any,
    study: str,
    ledger: str,
    kind: str,
    runs: list,
    samples: int,
    problems: list[str],
) -> dict:
    where = f"{study} {kind}"
    succeeded = [r for r in runs if r.status == "succeeded"]
    tags = Counter(r.metadata.get("flexorch_tag") for r in succeeded)
    for tag, count in sorted(tags.items(), key=lambda item: str(item[0])):
        if count > 1:
            problems.append(f"{where} {tag!r} is stored {count} times")
    running = [r.metadata.get("flexorch_tag") for r in runs if r.status == "running"]
    if running:
        problems.append(f"{where}: runs still running {sorted(map(str, running))}")
    hashes = {}
    latest = {r.metadata.get("flexorch_tag"): r for r in succeeded}
    for tag in sorted(t for t in latest if t is not None)[:samples]:
        run = latest[tag]
        try:
            digest = _hash(orch.backend.records(ledger, run.run_id))
        except Exception as exc:  # any failure to read back is the finding
            problems.append(f"{where} {tag!r} can't be read: {exc}")
            continue
        hashes[tag] = {"run_id": run.run_id, "hash": digest}
    return {
        "runs": dict(sorted(Counter(r.status for r in runs).items())),
        "tags": sorted(str(t) for t in tags),
        "hashes": hashes,
    }


def _hash(records: list) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda r: r.label):
        digest.update(record.label.encode())
        digest.update(_bytes(record))
    return "sha256:" + digest.hexdigest()


def _bytes(record: Any) -> bytes:
    if record.kind == "timeseries":
        rows = [(when.isoformat(), value) for when, value in record.rows or []]
        return json.dumps(rows).encode()
    value = record.value
    if isinstance(value, FileValue):
        return value.name.encode() + b"\0" + value.data
    if record.kind == "table":
        return value.write_csv().encode()
    return json.dumps(value, sort_keys=True, default=str).encode()
