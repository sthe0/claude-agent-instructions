"""Records of a probe run: where they live, what counts as acceptable, and how the pin follows them.

A record is ``<state>/runs/<UTC timestamp>/record.json`` plus ``events/<case>.jsonl`` beside it;
a ``runs/`` entry without ``record.json`` is no record. A negative run writes under
``<state>/negative/<timestamp>/`` which no reader of this module opens.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import core

SCHEMA = "opencode-probe-record/1"
ACCEPT_ABSENT_IN_K7 = False
PASS, ABSENT, FAIL, UNOBSERVED, SKIP = "pass", "absent", "fail", "unobserved", "skip"
STATUSES = (PASS, ABSENT, FAIL, UNOBSERVED, SKIP)
EXIT_OK, EXIT_FAIL, EXIT_INTERNAL, EXIT_UNOBSERVED = 0, 1, 2, 3


def accepted(status: str, k7: bool) -> bool:
    """``pass`` always; ``absent`` only for a case outside K7 (unless the constant says otherwise)."""
    if status == PASS:
        return True
    return status == ABSENT and (not k7 or ACCEPT_ABSENT_IN_K7)


def run_exit_code(statuses: dict[str, str], k7: dict[str, bool]) -> int:
    if any(s == FAIL for s in statuses.values()):
        return EXIT_FAIL
    if any(s == ABSENT and k7.get(cid, False) and not ACCEPT_ABSENT_IN_K7 for cid, s in statuses.items()):
        return EXIT_FAIL
    if any(s == UNOBSERVED for s in statuses.values()):
        return EXIT_UNOBSERVED
    return EXIT_OK


def worst_exit(codes: list[int]) -> int:
    for code in (EXIT_INTERNAL, EXIT_FAIL, EXIT_UNOBSERVED):
        if code in codes:
            return code
    return EXIT_OK


def new_record_dir(state: Path, kind: str = "runs") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = state / kind
    base.mkdir(parents=True, exist_ok=True)
    path = base / stamp
    suffix = 0
    while path.exists():
        suffix += 1
        path = base / f"{stamp}-{suffix}"
    path.mkdir()
    return path


def write_record(record_dir: Path, version: str, phase: str, suite_sha256: str,
                 cases: dict, negative: dict | None = None) -> Path:
    record = {"schema": SCHEMA, "version": version, "phase": phase, "suite_sha256": suite_sha256,
              "negative": negative, "cases": cases}
    path = record_dir / "record.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return path


def read_record(path: Path) -> dict | None:
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("schema") == SCHEMA else None


def latest_record(state: Path, phase: str, version: str, suite_sha256: str) -> tuple[Path, dict] | None:
    """The newest ``runs/*/record.json`` of the phase with this version and phase hash."""
    runs = state / "runs"
    if not runs.is_dir():
        return None
    for entry in sorted((p for p in runs.iterdir() if re.match(r"\d{8}T\d{6}Z", p.name)), reverse=True):
        record = read_record(entry / "record.json")
        if record and record.get("phase") == phase and record.get("version") == version \
                and record.get("suite_sha256") == suite_sha256:
            return entry / "record.json", record
    return None


def pin_path(probe_dir: Path = core.PROBE_DIR) -> Path:
    return probe_dir / core.PIN_NAME


def write_pin(probe_dir: Path, version: str, last_pass: dict[str, dict]) -> None:
    lines = [f'version = "{version}"', ""]
    for phase in sorted(last_pass):
        entry = last_pass[phase]
        lines += [f"[last_pass.{phase}]", f'version = "{entry["version"]}"',
                  f'suite_sha256 = "{entry["suite_sha256"]}"', f'record = "{entry["record"]}"', ""]
    pin_path(probe_dir).write_text("\n".join(lines), encoding="utf-8")


def record_problems(phase: core.Phase, path: Path, rec: dict, case_ids: set[str] | None) -> list[str]:
    """Why the cases of ``rec`` are not all accepted (a missing case counts)."""
    k7 = {c.case_id: c.k7 for c in core.phase_cases(phase)}
    problems = []
    for case_id, entry in sorted(rec["cases"].items()):
        if case_ids and case_id not in case_ids:
            continue
        if not accepted(entry["status"], k7.get(case_id, True)):
            problems.append(f"{phase.name}/{case_id}: {entry['status']} is not accepted")
    problems += [f"{phase.name}/{cid}: no result in {path}" for cid in sorted(set(k7) - set(rec["cases"]))
                 if not case_ids or cid in case_ids]
    return problems


def check_phase(state: Path, phase: core.Phase, *, require_pass: bool, case_ids: set[str] | None,
                probe_dir: Path = core.PROBE_DIR) -> list[str]:
    """Reasons the phase has no acceptable record; empty when it has one."""
    pin = core.read_pin(probe_dir)
    suite_hash = core.phase_hash(phase, probe_dir)
    found = latest_record(state, phase.name, pin["version"], suite_hash)
    if found is None:
        return [f"{phase.name}: no record for version {pin['version']} and phase hash {suite_hash[:12]}"]
    path, rec = found
    problems = []
    last_pass = pin.get("last_pass", {}).get(phase.name)
    if not last_pass:
        problems.append(f"{phase.name}: pin.toml has no last_pass entry (run `pin --record`)")
    elif last_pass.get("suite_sha256") != suite_hash or last_pass.get("version") != pin["version"]:
        problems.append(f"{phase.name}: pin.toml last_pass is stale for this tree")
    if require_pass:
        problems += record_problems(phase, path, rec, case_ids)
    return problems


def pin_record(state: Path, phase: core.Phase, probe_dir: Path = core.PROBE_DIR) -> list[str]:
    """Write ``last_pass.<phase>`` from the latest record of the current tree when that record
    is fully accepted; return the reasons it was not written."""
    pin = core.read_pin(probe_dir)
    suite_hash = core.phase_hash(phase, probe_dir)
    found = latest_record(state, phase.name, pin["version"], suite_hash)
    if found is None:
        return [f"{phase.name}: no record for version {pin['version']} and phase hash {suite_hash[:12]}"]
    path, rec = found
    problems = record_problems(phase, path, rec, None)
    if problems:
        return problems
    last_pass = {name: dict(entry) for name, entry in pin.get("last_pass", {}).items()}
    last_pass[phase.name] = {"version": pin["version"], "suite_sha256": suite_hash, "record": str(path)}
    write_pin(probe_dir, pin["version"], last_pass)
    return []
