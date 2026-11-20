"""Runs the cases of a phase and writes the record; the negative run reuses the same driver."""
from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

from . import core, isolated, record
from .harness import Harness, VersionMismatch


@dataclass
class CaseContext:
    case_id: str
    events_dir: Path
    state: Path
    inert: bool
    binary: str | None = None

    def harness_for(self, binary: str | None) -> Harness:
        return Harness(self.case_id, self.events_dir, inert=self.inert, state=self.state, binary=binary)

    def harness(self) -> Harness:
        return self.harness_for(self.binary)


def result(status: str, evidence: dict, **extra) -> dict:
    return {"status": status, "evidence": evidence, **extra}


def run_case(info: core.CaseInfo, ctx: CaseContext) -> dict:
    try:
        outcome = core.load_case(info).run(ctx)
    except VersionMismatch:
        raise
    except isolated.ScratchNotInEffect as exc:
        outcome = result(record.UNOBSERVED, {"error": f"isolated environment not in effect: {exc}"})
    except Exception as exc:  # a case that cannot observe reports it, it does not abort the phase
        outcome = result(record.UNOBSERVED, {"error": f"{type(exc).__name__}: {exc}",
                                             "traceback": traceback.format_exc()[-1500:]})
    outcome["predicate"] = info.predicate
    return outcome


def run_phase(phase: core.Phase, *, case_ids: set[str] | None = None, inert: bool = False,
              state: Path | None = None, binary: str | None = None) -> tuple[Path, dict]:
    """Run the phase's cases (all or ``case_ids``) and write a record directory; a negative
    (``inert``) run writes under ``negative/`` which ``check-record`` never reads."""
    state = state or core.state_dir()
    kind = "negative" if inert else "runs"
    rec_dir = record.new_record_dir(state, kind)
    events_dir = rec_dir / "events"
    cases: dict[str, dict] = {}
    for info in core.phase_cases(phase):
        if case_ids and info.case_id not in case_ids:
            continue
        ctx = CaseContext(info.case_id, events_dir, state, inert, binary)
        events_dir.mkdir(parents=True, exist_ok=True)
        (events_dir / f"{info.case_id}.jsonl").touch()
        cases[info.case_id] = run_case(info, ctx)
    suite_hash = core.phase_hash(phase)
    version = core.read_pin()["version"]
    record.write_record(rec_dir, version, phase.name, suite_hash, cases,
                        {"inert_deny": True} if inert else None)
    return rec_dir, cases
